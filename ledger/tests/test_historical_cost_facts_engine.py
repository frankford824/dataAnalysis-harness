"""Native Manager -> source facts -> original adapter/engine, no model calls."""
from datetime import datetime,timedelta,timezone
from decimal import Decimal,ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import threading

import polars as pl
import pytest

from conftest import MODELS,write_xlsx
from test_order_feed import _fixture,_write
from test_replay_service import bundle as template_bundle,seed
from ledger import automation_actions as actions,automation_checker as checker,automation_store as journal,service
from ledger import historical_cost_facts as facts
from conftest import scoped_manager as Manager
from ledger.commission_registry import Registry
from ledger.model.schema import Model,Overhead
from ledger.model.loader import load_model
from ledger.order_feed import OrderFeed
from ledger.storage_integrity import digest
from ledger.workspace import Workspace


@pytest.fixture
def native_factory(tmp_path,monkeypatch):
    template_root=tmp_path/'template';template_root.mkdir()
    original,_missing=template_bundle.__wrapped__(template_root)
    base=Model.model_validate_json((original/'model.json').read_text())
    active=[]
    def make(count,price,*,partial=False,reship=False,origin_problem=None,unexpanded=False,source_month=6,history_missing=False,quantity_proof=None,live_aliases=False,company_mode=None):
        root=tmp_path/('case-'+str(len(active)));root.mkdir();feed_root=root/'source-feed'
        source_period=f'2026-{source_month:02d}';source_day=source_period+'-02'
        model=base.model_copy(update={'stores':(base.store('s1'),),'overheads':tuple(Overhead(period=p,amount=0) for p in sorted({source_period,'2026-06'}))})
        if reship:
            replacement=load_model(MODELS/'cn-ecommerce').metric('reshipment_cost').for_platform('taobao').model_copy(update={'by_platform':()})
            model=model.model_copy(update={'metrics':(*model.metrics,replacement),
                'overheads':(Overhead(period='2026-05',amount=0),*model.overheads),
                'statement':tuple(n.model_copy(update={'formula':n.formula.model_copy(update={'of':('gross_profit','reshipment_cost')})}) if n.id=='net_profit' else n for n in model.statement)})
        manifest=_fixture(feed_root);headers=[];items=[];costs=[];raw=[];details={};calls=[]
        for index in range(count):
            oid=str(index+1);member=str(index+1001);parent=str(3310000000000000000+index);child=str(3310000001000000000+index)
            header={'order_id':oid,'online_order_no':parent,'order_store_id':'10','order_time':source_day+' 10:00:00',
                'pay_time':source_day+' 10:01:00','ship_time':source_day+' 11:00:00','order_status_raw':'Sent',
                'paid_amount':'10','refund_amount':'0','tracking_no':'W'+oid,'order_type':'普通订单',
                'is_merge':False,'is_split':False,'platform_order_ids':[parent],'platform_order_identity_complete':True}
            item={'order_id':oid,'sub_order_id':member,'online_order_no':parent,'order_store_id':'10',
                'sku_id':'SKU-ONE','sku_type':'combine' if unexpanded else 'normal','original_order_date':source_day,'cost_company_id':'9',
                'merchant_sku':'123456789001','shop_item_id':'123456789001','outer_sku':child,'product_name':'已展开套装普通组件' if partial else 'synthetic item',
                'quantity':'1','paid_amount':'10','line_amount':'10','refund_amount':'0','tracking_no':'W'+oid,
                'is_gift':False,'is_suspect':False,'is_virtual':False}
            cost={'order_id':oid,'sub_order_id':member,'sku_id':'SKU-ONE','quantity':'1','unit_cost':None,
                'cost_amount':None,'cost_source':None,'cost_status':'missing_price','cost_as_of':source_day,'pricing_evidence':'{}'}
            if company_mode:
                item['cost_company_id']=None if company_mode=='evidence_only' else '8'
                cost['pricing_evidence']=json.dumps({'cost_company_id':'9','order_date':source_day})
            if quantity_proof is not None:
                header['content_hash']='a'*64;item.update(is_suspect=True,unit_price='99999')
                proof={'version':'reship-cost-input.v1','source':'jst.order_detail','order_id':oid,'sub_order_id':member,
                    'sku_id':'SKU-ONE','quantity':'1.0000','source_order_hash':('a' if quantity_proof else 'b')*64,
                    'captured_at':'2026-10-12T12:00:00Z','identity_status':'verified','quantity_status':'verified',
                    'sale_price_status':'placeholder','failure_reason':None,
                    'components':[{'sub_order_id':member,'sku_id':'SKU-ONE','quantity':'1.0000'}]}
                cost['pricing_evidence']=json.dumps({'order_date':source_day,'cost_input':proof})
            headers.append(header);items.append(item);costs.append(cost)
            raw.append([parent,child,'123456789001',10,datetime(2026,source_month,2,10),'W'+oid])
            details[oid]={'revision':10,'order':header,'items':[{**item,'platform_sub_order_id':child,'cost':cost}], 'relations':[],'after_sales':[]}
            if live_aliases:
                live_header={**header,'status_code':header['order_status_raw'],'order_date':source_day}
                live_header.pop('order_status_raw');live_header.pop('order_time')
                details[oid]['order']=live_header
                if quantity_proof is not None:
                    details[oid]['items'][0]['cost']={**cost,'pricing_evidence':json.loads(cost['pricing_evidence'])}
            if partial:
                known_item={**item,'sub_order_id':str(index+5001),'sku_id':'SKU-KNOWN'}
                known_cost={**cost,'sub_order_id':known_item['sub_order_id'],'sku_id':'SKU-KNOWN',
                    'unit_cost':'2','cost_amount':'2','cost_source':'history','cost_status':'priced',
                    'pricing_evidence':json.dumps({'order_date':'2026-06-02'})}
                items.append(known_item);costs.append(known_cost)
                details[oid]['items'].append({**known_item,'platform_sub_order_id':child,'cost':known_cost})
        if reship:
            assert count==1 and not partial
            current=headers[0];current.update(order_type='补发订单',paid_amount='0')
            items[0].update(original_order_date='2026-05-02',paid_amount='0',line_amount='0')
            original_header={**current,'order_id':'9000','order_type':'普通订单','paid_amount':'10',
                'order_time':'2026-05-02 10:00:00','pay_time':'2026-05-02 10:01:00','ship_time':'2026-05-02 11:00:00'}
            original_item={**items[0],'order_id':'9000','sub_order_id':'9001','paid_amount':'10','line_amount':'10'}
            original_cost={**costs[0],'order_id':'9000','sub_order_id':'9001','unit_cost':'2','cost_amount':'2',
                'cost_source':'history','cost_status':'priced','cost_as_of':'2026-05-02','pricing_evidence':json.dumps({'order_date':'2026-05-02'})}
            headers.append(original_header);items.append(original_item);costs.append(original_cost)
            details['1']['items'][0].update(original_order_date='2026-05-02',paid_amount='0',line_amount='0')
            relation={'relation_type':'reship','source_order_id':'9000','target_order_id':'1'}
            details['1']['relations']=[relation]
            details['9000']={'revision':10,'order':original_header,'items':[{**original_item,'platform_sub_order_id':child,'cost':original_cost}],'relations':[],'after_sales':[]}
            if origin_problem=='ambiguous':
                details['9000']['items'].append({**details['9000']['items'][0],'sub_order_id':'9002'})
            if origin_problem=='company':details['9000']['items'][0]['cost_company_id']='8'
            manifest['objects']['order_relations.parquet']=_write(feed_root,'order_relations.parquet',pl.from_dicts([relation]))
            raw[0][4]=datetime(2026,5,2,10)
        for name,rows in [('orders.parquet',headers),('order_items.parquet',items),('order_costs.parquet',costs)]:
            manifest['objects'][name]=_write(feed_root,name,pl.from_dicts(rows,infer_schema_length=None))
        for name in ('after_sales.parquet','after_sale_items.parquet'):
            empty=pl.read_parquet(feed_root/manifest['objects'][name]['path']).clear();manifest['objects'][name]=_write(feed_root,name,empty)
        feed=OrderFeed(root,feed_root=feed_root)
        feed._install_snapshot(manifest,{'stores':[{'order_store_id':'10','ledger_store_id':'s1','mapping_status':'confirmed'}]},
            {'healthy':True},source_revision=10,source_latest_seq=10,revision_etag='synthetic-fixed')
        ws=Workspace(root)
        from conftest import fixture_publication_scope
        fixture_publication_scope(ws,model,[source_period,'2026-05','2026-06'])
        source=write_xlsx(root/'甲店订单原表.xlsx',[['主订单号','子订单号','宝贝ID','买家实付','下单时间','物流单号'],*raw])
        ws.keep(source.name,source,'s1')
        registry=Registry(root);fixed=seed()
        for person in fixed['people']:registry.person_save(person,'fixture','fixed synthetic identity')
        for row in fixed['members']:
            if row['store_id']=='s1':registry.save_store_member('s1',row['person_id'],row['duty'],'','fixture','fixed synthetic membership')
        for row in fixed['schemes']:
            if row['store_id']=='s1':
                if reship or source_month==5:row['body']['segments'][0]['valid_from']='2026-05-01'
                registry.save_scheme('s1',row['product_id'],row['body'],'fixture','fixed synthetic scheme',publish=True)
        registry.operator_add('fixture-human','synthetic-password');actor={**registry.actor(registry.login('fixture-human','synthetic-password')),'kind':'human','authenticated':True}
        stamp=datetime.now(timezone.utc)
        preview=actions.decision_preview(ws,model,request_id='grant-p',period='2026-05' if reship else source_period,store_id='s1',kind='rule_grant',
            values={'rule_id':facts.RULE,'rule_sha':facts.RULE_SHA,'enabled':True,'valid_from':(stamp-timedelta(hours=1)).isoformat(),
                'valid_until':(stamp+timedelta(hours=1)).isoformat(),**({'period_from':'2026-05','period_until':'2026-06'} if reship else {})},reason='explicit synthetic v2 grant',actor=actor)
        actions.decision_apply(ws,model,request_id='grant-a',proposal_id=preview['proposal_id'],proposal_sha=preview['proposal_sha'],input_sha=preview['input_sha'],actor=actor)
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path=self.path.split('?',1)[0];calls.append(path)
                if path.endswith('/stores'):body={'stores':[{'ledger_store_id':'s1','order_store_id':'10','mapping_status':'confirmed','consumable':True}]}
                elif '/orders/' in path:body=details[path.rsplit('/',1)[1]]
                elif path=='/api/cost/history':body={'data':[] if history_missing else [{'sku_id':'SKU-ONE','wms_co_id':'9','begin_date':'2026-05-01','end_date':'2026-06-30','cost_price':price}]}
                else:self.send_response(404);self.end_headers();return
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(body).encode())
            def log_message(self,*_):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        origin='http://127.0.0.1:'+str(server.server_port)
        monkeypatch.setenv('LEDGER_DETERMINISTIC_AUTOMATION_ENABLED','1');monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED','1')
        monkeypatch.setenv('LEDGER_ORDER_FEED_ROOT',str(feed_root));monkeypatch.setenv('LEDGER_ORDER_FEED_URL',origin+'/api/integration/ledger/v1')
        monkeypatch.setenv('LEDGER_ORDER_FEED_TOKEN','synthetic-order-token');monkeypatch.setenv('LEDGER_HISTORY_COST_API_BASE',origin)
        monkeypatch.setenv('LEDGER_HISTORY_COST_API_TOKEN','synthetic-history-token')
        hashes={str(p.relative_to(feed_root)):digest(p) for p in feed_root.rglob('*') if p.is_file()}
        active.append((server,ws))
        return ws,model,registry,calls,hashes,feed_root
    yield make
    for server,ws in active:server.shutdown();server.server_close();ws.close()


@pytest.mark.parametrize('count,price',[(1,'3.335'),(100,'3.335'),(100,'0.004')])
def test_one_native_batch_and_original_engine_preserve_fractional_costs(native_factory,monkeypatch,count,price):
    ws,model,registry,calls,before,feed_root=native_factory(count,price)
    original=service.recompute;computations=[]
    def recorded(*args,**kwargs):computations.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(service,'recompute',recorded)
    registry.enqueue_files({'s1'},'nas:'+ws.conn.execute('SELECT sha FROM slot WHERE store_id=?',('s1',)).fetchone()[0])
    manager=Manager(lambda:ws,lambda:model)
    manager.once()
    rows=ws.conn.execute('SELECT values_json FROM automation_decision WHERE kind=? AND action=?',(facts.RULE,'apply')).fetchall()
    assert len(rows)==1
    adopted=json.loads(rows[0]['values_json'])['facts']
    assert len(adopted)==count and {r['unit_cost'] for r in adopted}=={price}
    assert ws.conn.execute('SELECT count(*) FROM cost_line_log').fetchone()[0]==0
    assert ws.conn.execute("SELECT count(*) FROM automation_attempt WHERE state IN ('queued','dispatch_pending')").fetchone()[0]==1
    assert calls.count('/api/cost/history')==1
    assert sum(path.endswith('/stores') for path in calls)==1
    manager.once()
    state=ws.state('s1','2026-06');nodes={r['id']:r for r in state.result['statement']}
    expected=float(Decimal(10)*count-(Decimal(price)*count).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
    # Statement metric nodes keep the original two-decimal aggregate policy;
    # raw cost contributions retain the original fractional-cent source amount.
    assert nodes['gross_profit']['value']==pytest.approx(expected,abs=1e-6)
    assert nodes['net_profit']['value']==pytest.approx(expected,abs=1e-6)
    raw_cost=pl.read_parquet(ws.facts_path(state.run_id)).filter(pl.col('metric_id')=='goods_cost')
    assert raw_cost['contribution'].sum()==pytest.approx(-float(Decimal(price)*count),abs=1e-9)
    assert len(computations)==2
    assert checker.check(ws,'s1','2026-06',model=model,verify_artifacts=True)['allowed']
    proof=json.loads(ws.conn.execute('SELECT manifest_json FROM automation_run_proof WHERE run_id=?',(state.run_id,)).fetchone()[0])
    assert len(proof['approved_cost_sources'])==count
    assert {str(p.relative_to(feed_root)):digest(p) for p in feed_root.rglob('*') if p.is_file()}==before


def test_exact_missing_normal_component_is_filled_without_replacing_priced_sibling(native_factory):
    ws,model,registry,calls,before,feed_root=native_factory(1,'3.335',partial=True)
    registry.enqueue_files({'s1'},'nas:'+ws.conn.execute('SELECT sha FROM slot WHERE store_id=?',('s1',)).fetchone()[0])
    manager=Manager(lambda:ws,lambda:model);manager.once()
    rows=facts.active(ws.conn,'s1','2026-06')
    assert len(rows)==1 and rows[0]['coverage']['partial_cost'] is True
    assert rows[0]['sku']=='SKU-ONE'
    manager.once();state=ws.state('s1','2026-06')
    frame=pl.read_parquet(ws.facts_path(state.run_id)).filter(pl.col('metric_id')=='goods_cost')
    assert frame.filter(pl.col('sku')=='SKU-KNOWN')['contribution'].sum()==-2
    assert frame.filter(pl.col('sku')=='SKU-ONE')['contribution'].sum()==pytest.approx(-3.335,abs=1e-9)
    assert frame['contribution'].sum()==pytest.approx(-5.335,abs=1e-9)
    assert ws.conn.execute('SELECT count(*) FROM cost_line_log').fetchone()[0]==0
    assert checker.check(ws,'s1','2026-06',model=model)['allowed']


def test_unique_original_reship_member_uses_original_engine_month(native_factory):
    ws,model,registry,calls,before,feed_root=native_factory(1,'3.335',reship=True)
    registry.enqueue_files({'s1'},'nas:'+ws.conn.execute('SELECT sha FROM slot WHERE store_id=?',('s1',)).fetchone()[0])
    manager=Manager(lambda:ws,lambda:model);manager.once()
    adopted=facts.active(ws.conn,'s1','2026-05')
    assert len(adopted)==1 and adopted[0]['origin']['internal_sub_order_id']=='9001'
    assert adopted[0]['pricing_day']=='2026-05-02'
    manager.once();state=ws.state('s1','2026-05')
    frame=pl.read_parquet(ws.facts_path(state.run_id)).filter(pl.col('metric_id')=='reshipment_cost')
    assert frame['contribution'].sum()==pytest.approx(-3.335,abs=1e-9)
    assert frame['period'].unique().to_list()==['2026-05']
    assert calls.count('/api/cost/history')==1 and any(p.endswith('/orders/9000') for p in calls)
    assert ws.conn.execute('SELECT count(*) FROM cost_line_log').fetchone()[0]==0
    assert {str(p.relative_to(feed_root)):digest(p) for p in feed_root.rglob('*') if p.is_file()}==before


@pytest.mark.parametrize('problem,reason',[('ambiguous','reship_original_member_not_unique'),('company','reship_original_day_or_company_conflict')])
def test_reship_header_never_broadcasts_ambiguous_or_conflicting_original_member(native_factory,problem,reason):
    ws,model,registry,calls,_before,_feed=native_factory(1,'3.335',reship=True,origin_problem=problem)
    registry.enqueue_files({'s1'},'nas:'+ws.conn.execute('SELECT sha FROM slot WHERE store_id=?',('s1',)).fetchone()[0])
    Manager(lambda:ws,lambda:model).once()
    assert facts.active(ws.conn,'s1','2026-05')==[]
    result=json.loads(ws.conn.execute("SELECT result_json FROM automation_attempt WHERE state='source_facts_evaluated' ORDER BY rowid DESC LIMIT 1").fetchone()[0])
    assert reason in {code for outcome in result['outcomes'] for code in outcome.get('reason_codes',[])}
    assert calls.count('/api/cost/history')==0
    assert not checker.check(ws,'s1','2026-05',model=model)['allowed']


def test_unexpanded_combine_has_no_invented_unit_conversion(native_factory):
    ws,model,registry,calls,_before,_feed=native_factory(1,'3.335',unexpanded=True)
    registry.enqueue_files({'s1'},'nas:'+ws.conn.execute('SELECT sha FROM slot WHERE store_id=?',('s1',)).fetchone()[0])
    Manager(lambda:ws,lambda:model).once()
    assert facts.active(ws.conn,'s1','2026-06')==[]
    outcome=json.loads(ws.conn.execute("SELECT result_json FROM automation_attempt WHERE state='source_facts_evaluated' ORDER BY rowid DESC LIMIT 1").fetchone()[0])
    assert 'component_or_unit_conversion_unproved' in outcome['outcomes'][0]['reason_codes']
    assert calls.count('/api/cost/history')==0


def test_old_month_fact_never_authorizes_new_posting_month(native_factory):
    from datetime import datetime
    from openpyxl import load_workbook
    ws,model,registry,_calls,_before,_feed=native_factory(1,'3.335',source_month=5)
    registry.enqueue_files({'s1'},'nas:'+ws.conn.execute('SELECT sha FROM slot WHERE store_id=?',('s1',)).fetchone()[0])
    manager=Manager(lambda:ws,lambda:model);manager.once();manager.once()
    assert len(facts.active(ws.conn,'s1','2026-05'))==1
    source=ws.root/'甲店订单原表.xlsx';book=load_workbook(source);book.active.cell(2,5,datetime(2026,6,2,10));book.save(source);book.close()
    kept=ws.keep(source.name,source,'s1');registry.enqueue_files({'s1'},'nas:'+kept.sha)
    manager.once()
    state=ws.state('s1','2026-06')
    assert state is not None
    cost=pl.read_parquet(ws.facts_path(state.run_id)).filter(pl.col('metric_id')=='goods_cost')
    assert cost['contribution'].sum()==0
    assert facts.active(ws.conn,'s1','2026-06')==[]
    assert not checker.check(ws,'s1','2026-06',model=model)['allowed']


def test_one_missing_quote_is_reused_for_all_identical_batch_queries(native_factory):
    ws,model,registry,calls,_before,_feed=native_factory(100,'3.335',history_missing=True)
    registry.enqueue_files({'s1'},'nas:'+ws.conn.execute('SELECT sha FROM slot WHERE store_id=?',('s1',)).fetchone()[0])
    Manager(lambda:ws,lambda:model).once()
    assert facts.active(ws.conn,'s1','2026-06')==[] and calls.count('/api/cost/history')==1
    outcomes=json.loads(ws.conn.execute("SELECT result_json FROM automation_attempt WHERE state='source_facts_evaluated' ORDER BY rowid DESC LIMIT 1").fetchone()[0])['outcomes']
    assert len(outcomes)==100 and all(o['reason_codes']==['history_price_not_covering_original_day'] for o in outcomes)
    assert all(len(o['source_refs'])==4 for o in outcomes)
    assert not checker.check(ws,'s1','2026-06',model=model)['allowed']


def test_related_return_month_retains_original_quote_without_unrelated_month_refs(native_factory):
    from types import SimpleNamespace
    from ledger.automation_period_proof import _cost_sources
    ws,model,registry,_calls,_before,_feed=native_factory(1,'3.335')
    registry.enqueue_files({'s1'},'nas:'+ws.conn.execute('SELECT sha FROM slot WHERE store_id=?',('s1',)).fetchone()[0])
    manager=Manager(lambda:ws,lambda:model);manager.once();manager.once()
    original=facts.active(ws.conn,'s1','2026-06')[0]
    unrelated={**original,'internal_order_id':'9999','sku':'OTHER','period':'2026-08'}
    ing=SimpleNamespace(historical_cost_evidence=[original,unrelated])
    sl=SimpleNamespace(facts=pl.DataFrame({'source_id':['cost_return'],'internal_order_id':[original['internal_order_id']],'sku':[original['sku']]}))
    references=_cost_sources(ws,'s1','2026-07',{},ing,sl)
    assert len(references)==1 and references[0]['quote_sha']==original['quote_sha']
    assert references[0]['origin_period']=='2026-06' and references[0]['dependent_period']=='2026-07'
    assert references[0]['artifacts']


@pytest.mark.parametrize('valid',[True,False])
def test_v2_uses_exact_existing_placeholder_quantity_admission(native_factory,valid):
    ws,model,registry,calls,_before,_feed=native_factory(1,'3.335',quantity_proof=valid,live_aliases=True)
    registry.enqueue_files({'s1'},'nas:'+ws.conn.execute('SELECT sha FROM slot WHERE store_id=?',('s1',)).fetchone()[0])
    manager=Manager(lambda:ws,lambda:model);manager.once()
    adopted=facts.active(ws.conn,'s1','2026-06')
    if valid:
        assert len(adopted)==1 and adopted[0]['quantity']=='1.0'
        manager.once();state=ws.state('s1','2026-06')
        raw=pl.read_parquet(ws.facts_path(state.run_id)).filter(pl.col('metric_id')=='goods_cost')
        assert raw['contribution'].sum()==pytest.approx(-3.335,abs=1e-9)
        assert checker.check(ws,'s1','2026-06',model=model)['allowed']
    else:
        assert adopted==[] and calls.count('/api/cost/history')==0
        outcome=json.loads(ws.conn.execute("SELECT result_json FROM automation_attempt WHERE state='source_facts_evaluated' ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        assert outcome['outcomes'][0]['reason_codes']==['legacy_quantity_evidence_unverified']
        assert not checker.check(ws,'s1','2026-06',model=model)['allowed']


@pytest.mark.parametrize('mode,adopted',[('evidence_only',True),('conflict',False)])
def test_company_evidence_uses_identical_admission_and_overlay_contract(native_factory,mode,adopted):
    ws,model,registry,calls,_before,_feed=native_factory(1,'3.335',company_mode=mode)
    registry.enqueue_files({'s1'},'nas:'+ws.conn.execute('SELECT sha FROM slot WHERE store_id=?',('s1',)).fetchone()[0])
    manager=Manager(lambda:ws,lambda:model);manager.once()
    selected=facts.active(ws.conn,'s1','2026-06')
    assert bool(selected) is adopted
    if adopted:
        assert selected[0]['company']=='9';manager.once()
        state=ws.state('s1','2026-06')
        cost=pl.read_parquet(ws.facts_path(state.run_id)).filter(pl.col('metric_id')=='goods_cost')
        assert cost['contribution'].sum()==pytest.approx(-3.335,abs=1e-9)
        assert checker.check(ws,'s1','2026-06',model=model)['allowed']
    else:
        assert calls.count('/api/cost/history')==0
        assert not checker.check(ws,'s1','2026-06',model=model)['allowed']


@pytest.mark.parametrize('remove',[False,True])
def test_only_effective_manual_save_blocks_source_fact(native_factory,monkeypatch,remove):
    from ledger import cost_lines
    ws,model,registry,calls,_before,_feed=native_factory(1,'3.335')
    monkeypatch.setenv('LEDGER_DETERMINISTIC_AUTOMATION_ENABLED','0')
    registry.enqueue_files({'s1'},'nas:'+ws.conn.execute('SELECT sha FROM slot WHERE store_id=?',('s1',)).fetchone()[0])
    manager=Manager(lambda:ws,lambda:model);manager.once();run=ws.state('s1','2026-06').run_id
    row=cost_lines.current(ws,run,'s1','2026-06')['items'][0]
    saved=cost_lines.save(ws,'s1','2026-06',run,coverage_key=row['coverage_key'],context_sha=row['context_sha'],
        amount='4.00',reason='explicit synthetic manual cost',by='fixture-human')
    revision=saved['line_revision']
    if remove:
        removed=cost_lines.save(ws,'s1','2026-06',run,coverage_key=row['coverage_key'],context_sha=row['context_sha'],
            amount=None,action='remove',expected_line_revision=revision,reason='withdraw manual override',by='fixture-human')
        revision=removed['line_revision']
    monkeypatch.setenv('LEDGER_DETERMINISTIC_AUTOMATION_ENABLED','1')
    registry.enqueue_cost_lines('s1','2026-06',revision)
    manager.once()
    if remove:
        assert len(facts.active(ws.conn,'s1','2026-06'))==1
        manager.once()
    else:
        assert facts.active(ws.conn,'s1','2026-06')==[] and calls.count('/api/cost/history')==0
    final=ws.state('s1','2026-06')
    raw=pl.read_parquet(ws.facts_path(final.run_id)).filter(pl.col('metric_id')=='goods_cost')
    assert raw['contribution'].sum()==pytest.approx(-3.335 if remove else -4,abs=1e-9)
    assert checker.check(ws,'s1','2026-06',model=model)['allowed']
