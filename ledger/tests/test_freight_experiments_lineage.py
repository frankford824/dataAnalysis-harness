"""Lineage is actual projection data; ambiguous rows never receive fake residue."""
from decimal import Decimal
from types import SimpleNamespace
import polars as pl

from ledger import freight_experiments_lineage as lineage
from ledger import freight_experiments as experiments


def test_grouped_original_projection_does_not_claim_per_fee_target_amounts():
    store=SimpleNamespace(id='s',name='Synthetic',aliases=(),platform='taobao')
    source=pl.DataFrame({'store':['Synthetic']*2,'period':['2026-06']*2,'link_key':['o']*2,
        'file_sha':['f']*2,'sheet':['Sheet1']*2,'row_no':[2,3],'counted':[True,True],'contribution':[-4.,-6.]})
    projection=pl.DataFrame({'store':['Synthetic']*2,'period':['2026-06']*2,'link_key':['o']*2,
        'spine_row':[0,1],'amount':[-7.,-3.],'factor':[.7,.3]})
    spine=pl.DataFrame({'order_id':['o']*2,'sub_order_id':['c1','c2'],'product_id':['p1','p2'],'period':['2026-06']*2})
    groups=lineage.scope_groups(source,projection,spine,store);gid=next(iter(groups))
    charge={'charge_id':'one','file_sha':'f','sheet':'Sheet1','row_no':2,'amount':'4'}
    record={'source_facts':[{'counted':True,'contribution':'-4'}],'group_ids':[gid]}
    lineage.finish(charge,record,groups)
    assert record['source_matched'] and record['status']=='group_matched'
    assert record['attribution_status']=='charge_attribution_unavailable' and record['allocations']==[]
    assert sum(Decimal(t['amount']) for t in groups[gid]['targets'])==10


def test_original_six_decimal_residue_is_retained_without_wrapper_adjustment():
    store=SimpleNamespace(id='s',name='Synthetic',aliases=(),platform='taobao')
    source=pl.DataFrame({'store':['Synthetic'],'period':['2026-06'],'link_key':['o'],
        'file_sha':['f'],'sheet':['Sheet1'],'row_no':[2],'counted':[True],'contribution':[-10.]})
    costs=[1.]*7+[.999999]*3
    projection=pl.DataFrame({'store':['Synthetic']*10,'period':['2026-06']*10,'link_key':['o']*10,
        'spine_row':list(range(10)),'amount':[-c for c in costs],'factor':[.1]*10})
    spine=pl.DataFrame({'order_id':['o']*10,'sub_order_id':['c'+str(i) for i in range(10)],
        'product_id':['p'+str(i) for i in range(10)]})
    groups=lineage.scope_groups(source,projection,spine,store);gid=next(iter(groups))
    record={'source_facts':[{'counted':True,'contribution':'-10'}],'group_ids':[gid]}
    lineage.finish({'file_sha':'f','sheet':'Sheet1','row_no':2,'amount':'10'},record,groups)
    assert record['status']=='allocated'
    assert sum(Decimal(a['amount']) for a in record['allocations'])==Decimal('9.999997')
    assert record['original_rounding_residue']=='.000003' or Decimal(record['original_rounding_residue'])==Decimal('.000003')
    assert record['allocations'][0]['amount']=='1.0'


def test_connected_component_counts_shared_fees_and_original_projection_once():
    charges=[{'charge_id':'c'+str(i),'file_sha':'f','sheet':'S','row_no':i,'amount':str(v),'tracking_no':'W'+str(i)}
        for i,v in [(1,4),(2,6),(3,5)]]
    def source(i):return {'file_sha':'f','sheet':'S','row_no':i}
    def target(pid,order,product,value):
        return {'projection_record_id':pid,'store_id':'s','order_id':order,'product_id':product,'amount':str(value),'period':'2026-06'}
    groups={'g1':{'source_anchors':[source(1),source(2)],'targets':[target('r1','o1','p',7)]},
        'g2':{'source_anchors':[source(2),source(3)],'targets':[target('r2','o2','p',8)]}}
    baseline={c['charge_id']:{'source_matched':True} for c in charges}
    candidate={c['charge_id']:{'status':'allocated','packet_id':'p1' if c['charge_id']=='c1' else 'p2',
        'allocations':[{'store_id':'s','order_id':'o1' if c['charge_id']=='c1' else 'o2','product_id':'p',
            'child_id':'unrelated-namespace','amount':c['amount']}]} for c in charges}
    rows=lineage.components(charges,baseline,candidate,groups)
    assert len({r['component_id'] for r in rows})==1
    assert sum(Decimal(r['source_amount']) for r in rows if r['source_first'])==15
    assert sum(Decimal(r['a_amount']) for r in rows)==15
    assert sum(Decimal(r['b_amount']) for r in rows)==15
    assert {Decimal(r['delta']) for r in rows}=={Decimal(-3),Decimal(3)}
    records=[pid for r in rows for pid in r['original_projection_record_ids']]
    assert records==['r1','r2'] and len(records)==len(set(records))


def test_group_spanning_other_billing_sources_is_not_prorated_into_this_month():
    charge={'charge_id':'one','file_sha':'f','sheet':'S','row_no':1,'amount':'4','tracking_no':'W'}
    group={'g':{'source_anchors':[{'file_sha':'f','sheet':'S','row_no':1},{'file_sha':'old','sheet':'S','row_no':2}],
        'targets':[{'projection_record_id':'r','store_id':'s','order_id':'o','product_id':'p','amount':'10'}]}}
    candidate={'one':{'status':'allocated','packet_id':'p','allocations':[{'store_id':'s','order_id':'o','product_id':'p','amount':'4'}]}}
    rows=lineage.components([charge],{'one':{'source_matched':True}},candidate,group)
    assert all(r['a_amount'] is None and r['delta'] is None for r in rows)
    assert rows[0]['reason']=='original_group_spans_other_source_charges'


def test_missing_candidate_period_is_never_filled_with_the_fee_month():
    charge={'charge_id':'one','amount':'4','tracking_no':'W'}
    a={'one':{'status':'allocated','reason':'','attribution_status':'single_source_actual_projection',
        'allocations':[{'store_id':'s','order_id':'o','product_id':'p','period':'2026-05','amount':'4'}]}}
    b={'one':{'status':'allocated','reason':'','method':'direct','allocations':[{'store_id':'s','order_id':'o','product_id':'p','amount':'4'}]}}
    row=experiments.differences([charge],a,b,'2026-07')[0]
    assert row['posting_period']=='' and row['a_posting_periods']==['2026-05'] and row['b_posting_periods']==[]
    assert Decimal(row['delta'])==0
