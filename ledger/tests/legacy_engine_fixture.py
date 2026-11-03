"""Close two synthetic months with an extracted OLD engine package (subprocess only).

Usage: python legacy_engine_fixture.py ENGINE_LEDGER_DIR BUNDLE OUTPUT_WORKSPACE OVERHEAD

ENGINE_LEDGER_DIR is a `git archive` of an older revision's ledger/ directory. The
script imports that package exclusively, so the workspace, registry and order feed
schemas, run shas and close records are exactly what the old engine writes.
"""
import json
import os
from pathlib import Path
import sys

engine, bundle, output, overhead = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
sys.path.insert(0, str(engine))
os.environ.update(LEDGER_ORDER_FEED_ENABLED='1', LEDGER_ORDER_FEED_ROOT=str(bundle / 'feed'),
                  LEDGER_ORDER_FEED_URL='http://offline.invalid', LEDGER_COMMISSION_AUTH_MODE='open')

from ledger import commission_confirm, labor_api, service  # noqa: E402
from ledger.commission_registry import Registry  # noqa: E402
from ledger.model.loader import load_model  # noqa: E402
from ledger.model.schema import (Check, ColumnBinding, Metric, Model, Overhead, ParseOptions, Platform,  # noqa: E402
    SourceContract, StatementNode, Store, Template, ValueExpr)
from ledger.order_feed import OrderFeed  # noqa: E402
from ledger.workspace import Workspace  # noqa: E402

full = load_model(engine.parent / 'models' / 'cn-ecommerce')
goods = full.metric('goods_cost').for_platform('taobao').model_copy(update={'by_platform': ()})
template = Template(id='replay_orders', name='固定订单原表', source='order_detail',
    match_columns=('主订单号', '子订单号', '宝贝ID', '买家实付', '下单时间', '物流单号'),
    parse=ParseOptions(header_row=0), time_slots={'order_date': 'order_time'},
    bindings=tuple(ColumnBinding(role=role, columns=(column,), kind=kind)
        for role, column, kind in [('order_id', '主订单号', 'text'), ('sub_order_id', '子订单号', 'text'),
        ('product_id', '宝贝ID', 'text'), ('buyer_paid', '买家实付', 'number'),
        ('order_time', '下单时间', 'time'), ('tracking_no', '物流单号', 'text')]))
model = Model(id='cn-closed-history', name='历史月结模型',
    platforms=(Platform(id='taobao', name='淘宝', cost_pricing='required'),),
    stores=(Store(id='s1', name='甲店', platform='taobao'), Store(id='s2', name='乙店', platform='taobao')),
    sources=(SourceContract(id='order_detail', name='订单原表', is_spine=True, owner_role='shop_owner', cadence='monthly'),
        SourceContract(id='order_cost', name='订单成本', owner_role='warehouse', cadence='monthly'),
        SourceContract(id='after_sales', name='售后', owner_role='warehouse', cadence='monthly', required_for_close=False)),
    templates=(template,),
    metrics=(Metric(id='income', name='收入', source='order_detail', value=ValueExpr(op='sum', of=('buyer_paid',)),
                    link={'key': 'sub_order_id', 'to': 'order.sub_order_id', 'grain': 'order'}), goods),
    statement=(StatementNode(id='n_receipt', name='销售收入', level=2, headline='revenue', formula={'op': 'add', 'of': ['income']}),
        StatementNode(id='gross_profit', name='毛利', commission_base=True, is_total=True, formula={'op': 'add', 'of': ['income', 'goods_cost']}),
        StatementNode(id='net_profit', name='利润', commission_base=True, is_total=True, headline='profit', formula={'op': 'add', 'of': ['gross_profit']})),
    checks=(Check(id='goods_coverage', name='成本覆盖', kind='spine_coverage', metric='goods_cost', threshold=.95, blocking=True),),
    overheads=tuple(Overhead(period=p, amount=float(overhead), name='兼职人工费用') for p in ('2026-06', '2026-07')))

inputs = json.loads((bundle / 'inputs.json').read_text(encoding='utf-8'))
state = json.loads((bundle / 'feed-state.json').read_text(encoding='utf-8'))
ws = Workspace(output)
for slot in inputs['slots']:
    ws.keep(slot['name'], bundle / 'files' / slot['sha'][:2] / slot['sha'], slot['store_id'], by='upload')
OrderFeed(output, feed_root=bundle / 'feed')._install_snapshot(json.loads(state['manifest_json']), {'stores': inputs['mapping']},
    {'healthy': True}, source_revision=10, source_latest_seq=10, revision_etag='fixed')
registry = Registry(output)
for person in [{'id': 'p1', 'name': '甲'}, {'id': 'p2', 'name': '乙'}]:
    registry.person_save(person, 'seed', '人员')
for sid, product in [('s1', '123456789001'), ('s2', '123456789002')]:
    registry.save_store_member(sid, 'p1', 'produce', '', 'seed', '身份', valid_from='2026-06-01')
    registry.save_scheme(sid, product, {'segments': [{'valid_from': '2026-06-01', 'valid_to': '', 'mode': 'distribute',
        'total_rate': '.05', 'allocations': [{'person_id': 'p1', 'rate': '.05', 'role': '运营', 'duty': 'produce'}]}]},
        'seed', '关系', publish=True)
registry.save_policy({'base_node': 'net_profit', 'wages': 'skip_preview'}, '2026-06-01', 'seed', '口径')
for store in model.stores:
    result = service.recompute(ws, model, store)
    assert not result.failure, result.failure
for period in ('2026-06', '2026-07'):
    for sid in ('s1', 's2'):
        run = ws.latest_run(sid, period)
        share = labor_api.share_at_close(ws, model, period, sid)
        ws.close_period(sid, period, by='finance', note='月结', expected_run_id=run['id'], labor_cut=share)
for sid, period, amount in [('s1', '2026-06', '8.00'), ('s2', '2026-07', '7.50')]:
    context = commission_confirm.context(ws, registry, model, sid, period)
    commission_confirm.confirm(ws, registry, model, store_id=sid, period=period, run_id=context['run_id'],
        source_sha=context['source_sha'], expected_confirmation_id='', payouts=[{'person_id': 'p1', 'amount': amount}],
        no_payout=False, reason='财务核定实发', actor='finance')
ws.close()
print(json.dumps({'model_revision': ws.conn.execute('SELECT model_revision FROM run LIMIT 1').fetchone()[0]}))
