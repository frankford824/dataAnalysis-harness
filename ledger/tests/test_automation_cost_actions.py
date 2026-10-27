"""Real service HTTP/auth and loopback upstream contracts, never production.

The ledger run is an explicitly declared synthetic summary with sealed original
coverage/pricing files. This is a posting/API contract, not a historical corpus
or end-to-end service replay certification.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal,localcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import threading
import urllib.parse

from fastapi import FastAPI
from fastapi.testclient import TestClient
import polars as pl
import pytest

from automation_fixtures import capture_synthetic_summary_fixture
from conftest import login_client
from ledger import automation_actions as actions, automation_checker as checker, automation_store as journal
from ledger import automation_cost_actions as service, cost_lines, write_auth
from ledger.automation_api import install
from ledger.commission_registry import Registry
from ledger.model.schema import Overhead, Store
from ledger.storage_integrity import seal
from ledger.workspace import Workspace
from test_commission import _model


PERIOD = '2026-06'
SERVICE_TOKEN = 'synthetic-service-token'


class Upstream:
    def __init__(self):
        self.calls = []
        self.status = 200
        self.history = [{'sku_id': 'sku-one', 'wms_co_id': '9', 'begin_date': '2026-06-01',
                         'end_date': '2026-06-04', 'cost_price': '3.3350'}]
        self.stores = {'stores': [{'ledger_store_id': 's1', 'order_store_id': '10',
                                  'mapping_status': 'confirmed', 'consumable': True}]}
        self.detail = {'revision': 12, 'order': {'order_id': '123', 'order_store_id': '10',
            'order_type': '普通订单', 'is_merge': False, 'is_split': False,
            'platform_order_identity_complete': True, 'platform_order_ids': ['platform-123']},
            'items': [{'sub_order_id': '456', 'sku_id': 'sku-one', 'sku_type': 'normal',
                'quantity': '2.0000', 'original_order_date': '2026-06-04', 'cost_company_id': '9',
                'platform_sub_order_id': 'outer-456', 'is_virtual': False, 'is_gift': False, 'is_suspect': False,
                'cost': {'unit_cost': '99999', 'cost_source': 'register', 'cost_as_of': '2026-10-27'}}],
            'relations': [], 'after_sales': []}

    def handle(self, path, headers):
        parsed = urllib.parse.urlsplit(path)
        self.calls.append((parsed.path, urllib.parse.parse_qs(parsed.query), dict(headers)))
        if parsed.path == '/api/integration/ledger/v1/stores': return 200, self.stores
        if parsed.path == '/api/integration/ledger/v1/orders/123': return 200, self.detail
        if parsed.path == '/api/cost/history': return self.status, {'data': self.history}
        return 404, {'detail': 'unknown synthetic source route'}


class Contract:
    def __init__(self, ws, model, upstream, client, human, run_id):
        self.ws, self.model, self.upstream, self.client, self.human, self.run_id = ws, model, upstream, client, human, run_id
        self.original = json.loads(ws.latest_run('s1', PERIOD)['result'])

    def grant(self):
        stamp = datetime.now(timezone.utc)
        preview = self.human.post('/api/automation/decisions/preview', json={
            'request_id': 'grant-preview', 'period': PERIOD, 'store_id': 's1', 'kind': 'rule_grant',
            'values': {'rule_id': service.RULE, 'rule_sha': service.RULE_SHA, 'enabled': True,
                'valid_from': (stamp-timedelta(days=1)).isoformat(), 'valid_until': (stamp+timedelta(days=1)).isoformat()},
            'reason': '本机合成来源合同：批准单店单月固定规则，财务终态仍需人工确认'})
        assert preview.status_code == 200, preview.text
        source = preview.json()
        response = self.human.post('/api/automation/decisions/apply', json={
            'request_id': 'grant-apply', 'proposal_id': source['proposal_id'], 'proposal_sha': source['proposal_sha'],
            'input_sha': source['input_sha']})
        assert response.status_code == 200, response.text
        return response.json()

    def request(self, **changes):
        actions.sync_month(self.ws, self.model, PERIOD)
        row = checker.check(self.ws, 's1', PERIOD, model=self.model)
        return {'request_id': 'history-once', 'rule_id': service.RULE, 'case_id': journal.digest(['s1', PERIOD])[:32],
            'store_id': 's1', 'period': PERIOD, 'run_id': self.run_id, 'coverage_key': 'key-one',
            'context_sha': 'a'*64, 'expected_line_revision': 0, 'input_sha': row['input_sha'], **changes}

    def post(self, body):
        return self.client.post('/api/automation/service-actions', json=body, headers={'Authorization': 'Bearer '+SERVICE_TOKEN})

    def financial_counts(self):
        return (self.ws.conn.execute('SELECT count(*) FROM cost_line_log').fetchone()[0],
                self.ws.conn.execute('SELECT count(*) FROM automation_decision WHERE kind=?', (service.RULE,)).fetchone()[0])


@pytest.fixture
def contract(tmp_path, monkeypatch):
    upstream = Upstream()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, payload = upstream.handle(self.path, self.headers)
            self.send_response(status); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(payload if isinstance(payload,bytes) else json.dumps(payload, ensure_ascii=False).encode())
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    origin = 'http://127.0.0.1:' + str(server.server_port)
    monkeypatch.setenv('LEDGER_ORDER_FEED_URL', origin+'/api/integration/ledger/v1')
    monkeypatch.setenv('LEDGER_ORDER_FEED_TOKEN', 'synthetic-order-token')
    monkeypatch.setenv('LEDGER_HISTORY_COST_API_BASE', origin)
    monkeypatch.setenv('LEDGER_HISTORY_COST_API_TOKEN', 'synthetic-history-token')
    monkeypatch.setenv('LEDGER_DETERMINISTIC_AUTOMATION_ENABLED', '1')
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED', '0')
    monkeypatch.setenv('LEDGER_RELEASE_MARKER', str(tmp_path/'no-active-release-marker'))
    identity = tmp_path/'identities.json'
    identity.write_text(json.dumps({'identities': [{'id': 'worker-http', 'enabled': True,
        'token_sha256': hashlib.sha256(SERVICE_TOKEN.encode()).hexdigest(),
        'expires_at': datetime.now(timezone.utc).timestamp()+3600, 'scopes': ['business'], 'capabilities': ['approved_action']}]}))
    monkeypatch.setenv('LEDGER_SERVICE_IDENTITIES', str(identity))
    base = _model(stores=(Store(id='s1', name='Shop One', platform='taobao'),), overheads=(Overhead(period=PERIOD, amount=0),))
    model = base.model_copy(update={
        'metrics': tuple(m.model_copy(update={'id':'goods_cost'}) if m.id=='goods' else m for m in base.metrics),
        'sources': tuple(s.model_copy(update={'provides':('goods_cost',)}) if s.id=='cost' else s for s in base.sources),
        'statement': tuple(n.model_copy(update={'headline':'revenue'}) if n.id=='n_receipt' else
            n.model_copy(update={'formula':n.formula.model_copy(update={'of':('goods_cost',)})}) if n.id=='n_goods' else n for n in base.statement)})
    ws = Workspace(tmp_path/'workspace'); checker.bind_model(ws, model)
    raw = {'store_id':'s1', 'store':'Shop One', 'period':PERIOD, 'can_close':False, 'missing_sources':[],
        'findings':[{'id':'goods_cost_gap','passed':False,'blocking':True}],
        'statement':[{'id':'n_receipt','value':100,'available':True}], 'commission':{'amount_complete':False},
        'calculation_inputs':{'metric_totals':{'receipt':100,'goods_cost':0}, 'unavailable_metrics':['goods_cost']}}
    run_id = ws.record('s1', PERIOD, raw, [], model_revision=journal.digest(model.model_dump(mode='json')))
    coverage = pl.DataFrame({'coverage_key':['key-one'], 'context_sha':['a'*64], 'order_id':['platform-123'],
        'sub_order_id':['outer-456'], 'order_date':['2026-06-04'], 'order_count':[1], 'product_count':[1],
        'product_ids':['product-one'], 'quantities':['2'], 'goods_scope_version':[1], 'partial_cost':[False]})
    coverage.write_parquet(ws.coverage_gaps_path(run_id)); seal(ws.coverage_gaps_path(run_id))
    pricing = pl.DataFrame({'metric_id':['goods_cost'], 'coverage_key':['key-one'], 'store':['Shop One'], 'period':[PERIOD],
        'order_id':['platform-123'], 'internal_order_id':['123'], 'internal_sub_order_id':['456'],
        'sku':['sku-one'], 'quantity':['2.0000'], 'order_date':['2026-06-04'], 'cost_company_id':['9'],
        'reason':['下单日历史成本依据未取到'], 'reference_unit_cost':[99999.]})
    pricing.write_parquet(ws.pricing_gaps_path(run_id)); seal(ws.pricing_gaps_path(run_id))
    capture_synthetic_summary_fixture(ws, model, run_id)
    app = FastAPI()
    install(app, lambda:ws, lambda:model, actor=lambda request:write_auth.strict_actor(request, ws))
    write_auth.install(app, lambda:ws)
    client, human = TestClient(app), TestClient(app)
    login_client(human, ws.root)
    try: yield Contract(ws, model, upstream, client, human, run_id)
    finally:
        client.close(); human.close(); ws.close()
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_actual_http_uses_server_quantity_times_dated_quote_and_atomic_audits(contract):
    c=contract; c.grant(); body=c.request()
    response=c.post(body)
    assert response.status_code==200, response.text
    result=response.json()
    assert result['status']=='applied' and result['receipt_id'] and result['recompute_required'] is True
    line=c.ws.conn.execute('SELECT * FROM cost_line_log').fetchone()
    assert Decimal(line['amount'])==Decimal('6.67')  # 2 × 3.3350 is already exact cents; no rounding.
    assert line['by']=='service:worker-http' and line['source_run_id']==c.run_id
    assert c.financial_counts()==(1,1)
    assert c.ws.state('s1',PERIOD).state=='open'
    assert json.loads(c.ws.latest_run('s1',PERIOD)['result'])==c.original
    for ref in result['source_refs']:
        path=Path(ref['path']); assert path.is_relative_to(c.ws.root)
        assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==ref['sha256']
    auth=c.ws.conn.execute("SELECT * FROM automation_authorization WHERE action=?",(service.RULE,)).fetchone()
    assert auth['identity_kind']=='delegated_service' and json.loads(auth['scope_json'])['approved_rule_grant']
    with Registry(c.ws.root).connect() as db:
        assert db.execute("SELECT * FROM pending WHERE store_id='s1'").fetchone()
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='queued'").fetchone()[0]==1
    history=next(call for call in c.upstream.calls if call[0]=='/api/cost/history')
    assert history[1]=={'sku_ids':['sku-one'],'as_of':['2026-06-04'],'wms_co_ids':['9']}
    assert history[2]['X-Erp-Bridge-Cost-Token']=='synthetic-history-token'
    assert next(call for call in c.upstream.calls if call[0].endswith('/stores'))[2]['X-Integration-Token']=='synthetic-order-token'


def test_same_successful_request_replays_without_quote_or_duplicate_posting(contract):
    c=contract; c.grant(); body=c.request(); first=c.post(body)
    assert first.status_code==200 and first.json()['status']=='applied', first.text
    count=len(c.upstream.calls)
    c.upstream.status=503
    second=c.post(body)
    assert second.status_code==200 and second.json()==first.json()
    assert len(c.upstream.calls)==count and c.financial_counts()==(1,1)


def _replace_source_quantities(c,quantity,count=1):
    """Declare a new frozen synthetic source scenario, never reseal an old run."""
    coverage=pl.read_parquet(c.ws.coverage_gaps_path(c.run_id)).row(0,named=True)
    pricing=pl.read_parquet(c.ws.pricing_gaps_path(c.run_id)).row(0,named=True)
    coverage_rows=[];pricing_rows=[];orders={}
    for index in range(count):
        internal,sub=str(123+index),str(456+index)
        key='key-'+str(index);context=hashlib.sha256(key.encode()).hexdigest()
        parent,child='platform-'+internal,'outer-'+sub
        coverage_rows.append({**coverage,'coverage_key':key,'context_sha':context,'order_id':parent,
            'sub_order_id':child,'quantities':quantity})
        pricing_rows.append({**pricing,'coverage_key':key,'order_id':parent,'internal_order_id':internal,
            'internal_sub_order_id':sub,'quantity':quantity})
        detail=deepcopy(c.upstream.detail)
        detail['order'].update(order_id=internal,platform_order_ids=[parent])
        detail['items'][0].update(sub_order_id=sub,quantity=quantity,platform_sub_order_id=child)
        orders[internal]=detail
    previous=c.upstream.handle
    def handle(path,headers):
        parsed=urllib.parse.urlsplit(path)
        prefix='/api/integration/ledger/v1/orders/'
        if parsed.path.startswith(prefix) and parsed.path[len(prefix):] in orders:
            c.upstream.calls.append((parsed.path,urllib.parse.parse_qs(parsed.query),dict(headers)))
            return 200,orders[parsed.path[len(prefix):]]
        return previous(path,headers)
    c.upstream.handle=handle
    c.run_id=c.ws.record('s1',PERIOD,c.original,[],model_revision=journal.digest(c.model.model_dump(mode='json')))
    pl.from_dicts(coverage_rows).write_parquet(c.ws.coverage_gaps_path(c.run_id));seal(c.ws.coverage_gaps_path(c.run_id))
    pl.from_dicts(pricing_rows).write_parquet(c.ws.pricing_gaps_path(c.run_id));seal(c.ws.pricing_gaps_path(c.run_id))
    capture_synthetic_summary_fixture(c.ws,c.model,c.run_id)
    return coverage_rows


@pytest.mark.parametrize('price',['3.3350','3.340000000000000000000000000000000000000000000000000001'])
def test_fractional_cent_source_is_pending_with_exact_amount_and_original_quote(contract,price):
    c=contract;c.grant();row=_replace_source_quantities(c,'1')[0]
    c.upstream.history[0]['cost_price']=price
    response=c.post(c.request(coverage_key=row['coverage_key'],context_sha=row['context_sha']))
    assert response.status_code==200,response.text
    result=response.json()
    assert result['status']=='pending' and result['reason_codes']==['historical_amount_not_exact_cents']
    assert c.financial_counts()==(0,0) and not result['recompute_required']
    evaluation=next(r for r in result['source_refs'] if r.get('source')=='derived:history-cost-exact-evaluation')
    retained=json.loads(Path(evaluation['path']).read_bytes())
    assert Decimal(retained['exact_amount'])==Decimal(price) and retained['quantity']=='1'
    assert retained['financial_applied'] is False
    assert retained['run_id']==c.run_id and retained['coverage_key']==row['coverage_key']
    assert retained['input_sha']==result['scope']['input_sha']
    quote_sources=[json.loads(Path(r['path']).read_bytes()) for r in result['source_refs']]
    assert any(isinstance(raw,dict) and raw.get('data',[{}])[0].get('cost_price')==price for raw in quote_sources)
    assert all(hashlib.sha256(Path(ref['path']).read_bytes()).hexdigest()==ref['sha256'] for ref in result['source_refs'])


def test_raw_json_numeric_quote_tail_cannot_disappear_before_exact_cent_gate(contract):
    c=contract;c.grant();row=_replace_source_quantities(c,'1')[0]
    price='3.340000000000000000000000000000000000000000000000000001'
    raw=(' {"data":[{"sku_id":"sku-one","wms_co_id":9,"begin_date":"2026-06-01",'
        '"end_date":null,"cost_price":'+price+'}]} ').encode()
    previous=c.upstream.handle
    def handle(path,headers):
        parsed=urllib.parse.urlsplit(path)
        if parsed.path=='/api/cost/history':
            c.upstream.calls.append((parsed.path,urllib.parse.parse_qs(parsed.query),dict(headers)))
            return 200,raw
        return previous(path,headers)
    c.upstream.handle=handle
    response=c.post(c.request(coverage_key=row['coverage_key'],context_sha=row['context_sha']))
    assert response.status_code==200,response.text
    result=response.json()
    assert result['status']=='pending' and result['reason_codes']==['historical_amount_not_exact_cents']
    evaluation=next(r for r in result['source_refs'] if r.get('source')=='derived:history-cost-exact-evaluation')
    retained=json.loads(Path(evaluation['path']).read_bytes())
    assert Decimal(retained['unit_cost'])==Decimal(price)==Decimal(retained['exact_amount'])
    assert any(Path(ref['path']).read_bytes()==raw for ref in result['source_refs'])
    assert c.financial_counts()==(0,0)


def test_one_hundred_distinct_fractional_cent_orders_cannot_accumulate_rounding_into_ledger(contract):
    c=contract;c.grant();rows=_replace_source_quantities(c,'1',100)
    exact=[]
    for index,row in enumerate(rows):
        response=c.post(c.request(request_id='fractional-order-'+str(index),coverage_key=row['coverage_key'],context_sha=row['context_sha']))
        assert response.status_code==200,response.text
        result=response.json()
        assert result['status']=='pending' and result['reason_codes']==['historical_amount_not_exact_cents']
        evaluation=next(r for r in result['source_refs'] if r.get('source')=='derived:history-cost-exact-evaluation')
        exact.append(Decimal(json.loads(Path(evaluation['path']).read_bytes())['exact_amount']))
    assert sum(exact)==Decimal('333.5000')
    assert Decimal('3.34')*100==Decimal('334.00')  # The rejected per-order rounding counterexample.
    assert c.financial_counts()==(0,0)
    assert c.ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state='pending_evidence'").fetchone()[0]==100
    assert not c.ws.conn.execute("SELECT 1 FROM automation_attempt WHERE state IN ('dispatch_pending','queued')").fetchone()


def test_decimal_underflow_is_pending_with_unmodified_operands_not_an_invented_zero(contract):
    c=contract;c.grant();row=_replace_source_quantities(c,'1')[0]
    c.upstream.history[0]['cost_price']='1E-1000050'
    response=c.post(c.request(coverage_key=row['coverage_key'],context_sha=row['context_sha']))
    assert response.status_code==200,response.text
    result=response.json()
    assert result['status']=='pending' and result['reason_codes']==['historical_amount_precision_unproved']
    evaluation=next(r for r in result['source_refs'] if r.get('source')=='derived:history-cost-exact-evaluation')
    retained=json.loads(Path(evaluation['path']).read_bytes())
    assert retained['quantity']=='1' and retained['unit_cost']=='1E-1000050'
    assert retained['arithmetic_error'] and 'exact_amount' not in retained
    assert retained['financial_applied'] is False and c.financial_counts()==(0,0)


def test_inherited_decimal_exponent_context_cannot_drop_a_positive_fractional_cent_tail(contract):
    c=contract;c.grant();row=_replace_source_quantities(c,'1')[0]
    price='0.030000000000000000000000000000000000000000000000000000000001'
    c.upstream.history[0]['cost_price']=price
    body=c.request(coverage_key=row['coverage_key'],context_sha=row['context_sha'])
    # An altered ambient context is fault injection, not a claim about the
    # production runtime. AnyIO carries this context into the sync endpoint.
    with localcontext() as context:
        context.Emin=0
        response=c.post(body)
    assert response.status_code==200,response.text
    result=response.json()
    assert result['status']=='pending' and result['reason_codes']==['historical_amount_precision_unproved']
    evaluation=next(r for r in result['source_refs'] if r.get('source')=='derived:history-cost-exact-evaluation')
    retained=json.loads(Path(evaluation['path']).read_bytes())
    assert retained['unit_cost']==price and retained['arithmetic_error'] and 'exact_amount' not in retained
    assert c.financial_counts()==(0,0)


def test_same_id_pending_history_recovers_then_replays_success_once(contract):
    c=contract; c.grant(); body=c.request(); c.upstream.history=[]
    pending=c.post(body)
    assert pending.status_code==200 and pending.json()['status']=='pending', pending.text
    assert 'history_price_not_covering_original_day' in pending.json()['reason_codes']
    assert pending.json()['source_refs'] and c.financial_counts()==(0,0)
    assert not c.ws.conn.execute("SELECT 1 FROM automation_receipt WHERE request_id='approved-action:history-once'").fetchone()
    assert checker.check(c.ws,'s1',PERIOD,model=c.model)['input_sha']==body['input_sha']
    c.upstream.history=[{'sku_id':'sku-one','wms_co_id':9,'begin_date':'2026-06-01','end_date':None,'cost_price':'3.3350'}]
    applied=c.post(body)
    assert applied.status_code==200 and applied.json()['status']=='applied', applied.text
    assert c.post(body).json()==applied.json() and c.financial_counts()==(1,1)


@pytest.mark.parametrize('field,value',[('amount','0.01'),('evidence_ref','ev_claim'),('url','http://untrusted.invalid')])
def test_service_dto_never_accepts_model_money_reference_or_origin(contract,field,value):
    c=contract; c.grant(); response=c.post({**c.request(),field:value})
    assert response.status_code==422 and c.financial_counts()==(0,0) and c.upstream.calls==[]


def test_no_grant_and_unidentified_caller_cannot_change_cost(contract):
    c=contract; body=c.request()
    assert c.post(body).status_code==403 and c.financial_counts()==(0,0)
    assert c.client.post('/api/automation/service-actions',json=body).status_code==401
    assert c.upstream.calls==[]


def test_old_input_and_wrong_case_are_rejected_before_reading_upstream(contract):
    c=contract; c.grant(); body=c.request()
    for change in ({'input_sha':'old-input'},{'case_id':'different-case'},{'store_id':'other-store'}):
        response=c.post({**body,**change})
        assert response.status_code in {403,409}, response.text
    assert c.financial_counts()==(0,0) and c.upstream.calls==[]


@pytest.mark.parametrize('change,reason',[
    ('quantity','live_quantity_day_or_company_conflict'),('date','live_quantity_day_or_company_conflict'),
    ('company','live_quantity_day_or_company_conflict'),('sku','single_live_component_unproved'),
    ('child','platform_child_identity_unproved'),('mapping','confirmed_store_or_order_identity_missing'),
    ('merge','reship_merge_or_split_not_supported'),('reship','reship_merge_or_split_not_supported'),
    ('gift','component_or_unit_conversion_unproved'),('sku_unknown','component_or_unit_conversion_unproved'),
    ('sku_zero','component_or_unit_conversion_unproved'),('order_zero','reship_merge_or_split_not_supported'),
])
def test_live_identity_quantity_day_company_and_unknown_types_remain_pending(contract,change,reason):
    c=contract; c.grant(); body=c.request(); item=c.upstream.detail['items'][0]; order=c.upstream.detail['order']
    if change=='quantity': item['quantity']='3'
    elif change=='date': item['original_order_date']='2026-06-03'
    elif change=='company': item['cost_company_id']='10'
    elif change=='sku': item['sku_id']='different-sku'
    elif change=='child': item['platform_sub_order_id']='different-child'
    elif change=='mapping': c.upstream.stores['stores'][0]['mapping_status']='suggested'
    elif change=='merge': order['is_merge']=True
    elif change=='reship': order['order_type']='补发订单'
    elif change=='gift': item['is_gift']=True
    elif change=='sku_unknown': item['sku_type']='unknown'
    elif change=='sku_zero': item['sku_type']='0'
    elif change=='order_zero': order['order_type']='0'
    response=c.post(body)
    assert response.status_code==200 and response.json()['status']=='pending', response.text
    assert response.json()['reason_codes']==[reason] and c.financial_counts()==(0,0)
    assert not any(call[0]=='/api/cost/history' for call in c.upstream.calls)


def test_partial_cost_keeps_explicit_scenario_pending_without_writing(contract):
    c=contract; c.grant()
    coverage=pl.read_parquet(c.ws.coverage_gaps_path(c.run_id))
    pricing=pl.read_parquet(c.ws.pricing_gaps_path(c.run_id))
    # A different source scenario is a new run. Never re-bless a changed archive
    # under its immutable original proof, even inside a synthetic fixture.
    c.run_id=c.ws.record('s1',PERIOD,c.original,[],model_revision=journal.digest(c.model.model_dump(mode='json')))
    path=c.ws.coverage_gaps_path(c.run_id)
    coverage.with_columns(pl.lit(True).alias('partial_cost')).write_parquet(path);seal(path)
    pricing.write_parquet(c.ws.pricing_gaps_path(c.run_id));seal(c.ws.pricing_gaps_path(c.run_id))
    capture_synthetic_summary_fixture(c.ws,c.model,c.run_id)
    body=c.request()
    response=c.post(body)
    assert response.status_code==200 and response.json()['reason_codes']==['partial_cost_requires_component_reconciliation']
    assert c.financial_counts()==(0,0) and c.upstream.calls==[]


@pytest.mark.parametrize('kind',['pricing','coverage'])
def test_resealed_changed_original_archive_cannot_become_a_trusted_gap(contract,kind):
    c=contract; c.grant(); body=c.request()
    if kind=='pricing':
        path=c.ws.pricing_gaps_path(c.run_id)
        pl.read_parquet(path).with_columns(pl.lit('3.0000').alias('quantity')).write_parquet(path)
        c.upstream.detail['items'][0]['quantity']='3.0000'
    else:
        path=c.ws.coverage_gaps_path(c.run_id)
        pl.read_parquet(path).with_columns(pl.lit('changed-product').alias('product_ids')).write_parquet(path)
    seal(path)  # Sidecar alone must not change the frozen original proof.
    response=c.post(body)
    assert response.status_code in {200,409}, response.text
    if response.status_code==200:
        assert response.json()['status']=='pending', response.text
    assert c.financial_counts()==(0,0)


def test_atomic_receipt_failure_rolls_back_financial_decision_and_handoff(contract):
    c=contract; c.grant(); body=c.request()
    c.ws.conn.execute("CREATE TRIGGER reject_success_receipt BEFORE INSERT ON automation_receipt WHEN NEW.request_id LIKE 'approved-action:%' BEGIN SELECT RAISE(ABORT,'receipt unavailable'); END")
    with pytest.raises(Exception,match='receipt unavailable'): c.post(body)
    assert c.financial_counts()==(0,0)
    assert not c.ws.conn.execute("SELECT 1 FROM automation_authorization WHERE request_id LIKE 'approved-action:%'").fetchone()
    assert not c.ws.conn.execute("SELECT 1 FROM automation_attempt WHERE state IN ('dispatch_pending','queued')").fetchone()
    with Registry(c.ws.root).connect() as db:
        assert not db.execute('SELECT 1 FROM pending').fetchone()
