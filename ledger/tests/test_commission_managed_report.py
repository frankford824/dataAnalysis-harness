import csv
import io
from decimal import Decimal

import pytest

from ledger.commission_managed_report import scope, evidence, _cache
from ledger.commission_registry import Registry
from ledger.commission_reports import business_export
from test_commission_profit import _persist


def sample(tmp_path):
    reg=Registry(tmp_path)
    a=reg.person_save({'name':'做货甲'},'test','test')['id']
    b=reg.person_save({'name':'抽点乙'},'test','test')['id']
    rows={'status':['distribute']*4,'person_id':[a,b,a,b],
          'product_id':['managed','managed','ordinary','ordinary'],'product_name':['托管品']*2+['普通品']*2,
          'spine_row':[1,1,2,2],'duty':['produce','cut']*2,'managed':[True,True,False,False],
          'managed_team_id':[b,b,'',''],'share':[.02,.03,.02,.03],'total_rate':['0.05']*4,
          'participation_sales':[100.0]*4,'participation_gross':[60.0]*4,'participation_profit':[50.0]*4,
          'amount':[1.0,1.5,1.0,1.5]}
    cid=_persist(reg,a,rows=rows)
    kwargs=dict(managed={b:100},outputs={a:{'gross':110},b:{'gross':0}},profits={a:80,b:0},
        payout_profits={a:32,b:48},rates={a:Decimal('.05'),b:Decimal('.05')},trials={a:1.6,b:2.4},
        keep=Decimal('.8'),roster={p['id']:p for p in reg.people()},store_id='s1',store='店铺',period='2026-06',run_id=11)
    return reg,a,b,{'calculation_id':cid},kwargs,rows


def test_person_managed_subset_preserves_costs_and_payouts(tmp_path):
    reg,a,b,c,kw,_=sample(tmp_path)
    root=scope(reg,c,**kw)[0]
    people={r['person_id']:r for r in root['children']}
    assert people[a]['sales']==100 and people[b]['sales']==0
    assert people[a]['gross']==55 and people[a]['profit_after_labor']==40
    assert people[b]['gross']==0 and people[b]['profit_after_labor']==0
    assert people[a]['trial_amount']==.8 and people[b]['trial_amount']==1.2
    assert root['sales']==100 and root['gross']==55 and root['profit_after_labor']==40 and root['trial_amount']==2
    assert all(r['amount'] is None and r['confirmed_amount'] is None for r in people.values())
    assert kw['profits']=={a:80,b:0} and kw['trials']=={a:1.6,b:2.4}
    filtered=scope(reg,c,**kw,selected_people=[a])[0]
    assert filtered['children']==[people[a]]
    columns,rows=business_export({'managed':[root]},'managed')
    out=list(rows)
    assert len(out)==5
    assert [r['层级'] for r in out]==[1,2,3,2,3]
    assert [r['行类型'] for r in out]==['团队合计','个人小计','商品明细','个人小计','商品明细']
    for field,key in [('托管销售额','sales'),('托管毛利额','gross'),('托管利润额（分摊后）','profit_after_labor'),('托管系统应发','trial_amount')]:
        for level in (1,2,3):
            assert sum(Decimal(str(r[field])) for r in out if r['层级']==level)==Decimal(str(root[key]))
    assert all('不可重复相加' in r['说明'] for r in out)
    assert all(not r['宝贝ID'] and not r['商品'] for r in out if r['层级']<3)
    assert '实发' not in ''.join(columns)


def test_cache_hit_never_reopens_archive(tmp_path,monkeypatch):
    from pathlib import Path
    reg,_,_,c,_,_=sample(tmp_path)
    first=evidence(reg,c);_cache.clear()
    original=Path.read_bytes
    def deny(path):
        if path.suffix=='.parquet':raise AssertionError('cache hit read archive')
        return original(path)
    monkeypatch.setattr(Path,'read_bytes',deny)
    assert evidence(reg,c)==first


@pytest.mark.parametrize('case',['missing','corrupt','inconsistent'])
def test_missing_or_inconsistent_evidence_never_invents_person_money(tmp_path,case,monkeypatch):
    reg,a,b,c,kw,_=sample(tmp_path)
    if case=='missing':c={}
    elif case=='inconsistent':kw['managed']={b:99}
    else:
        from pathlib import Path
        original=Path.read_bytes
        monkeypatch.setattr(Path,'read_bytes',lambda p:b'invalid archive' if p.suffix=='.parquet' else original(p))
    root=scope(reg,c,**kw)[0]
    assert root['children'][0]['person']=='待分配'
    assert root['sales']==kw['managed'][b]
    assert root['gross'] is None and root['profit_after_labor'] is None and root['trial_amount'] is None


def test_negative_refunds_keep_signed_values(tmp_path):
    reg,a,b,c,kw,rows=sample(tmp_path)
    for key in ('participation_sales','participation_gross','participation_profit','amount'):
        rows[key]=[-v for v in rows[key]]
    c={'calculation_id':_persist(reg,a,rows=rows,run_id=12)}
    kw.update(managed={b:-100},outputs={a:{'gross':-120},b:{'gross':0}},profits={a:-100,b:0},
              payout_profits={a:-40,b:-60},trials={a:-2,b:-3},keep=Decimal(1))
    root=scope(reg,c,**kw)[0]
    assert root['sales']==-100 and root['gross']==-60 and root['profit_after_labor']==-50 and root['trial_amount']==-2.5


def test_unknown_duties_keep_sales_in_unassigned_bucket(tmp_path):
    reg,a,b,c,kw,rows=sample(tmp_path)
    rows['duty']=['','', 'produce','cut']
    c={'calculation_id':_persist(reg,a,rows=rows,run_id=12)}
    root=scope(reg,c,**kw)[0]
    unassigned=next(r for r in root['children'] if not r['person_id'])
    assert unassigned['sales']==100
    assert all(r['sales'] is None for r in root['children'] if r['person_id'])


def test_half_cent_rounding_never_creates_fake_unassigned_people(tmp_path):
    reg,a,b,c,kw,rows=sample(tmp_path)
    rows['participation_sales']=[21.475,21.475,56.965,56.965]
    rows['managed']=[True]*4
    rows['managed_team_id']=[b]*4
    c={'calculation_id':_persist(reg,a,rows=rows,run_id=12)}
    kw['managed']={b:78.44}
    root=scope(reg,c,**kw)[0]
    assert all(p['person_id'] for p in root['children'])
    assert sum(Decimal(str(p['sales'])) for p in root['children'])==Decimal('78.44')
    assert root['profit_after_labor'] is not None


def test_report_query_and_export_use_same_managed_tree(tmp_path):
    from test_commission_reports import fixture
    ws,reg,people,client=fixture(tmp_path)
    pid=people[0]['id']
    cid=_persist(reg,pid,rows={'status':['distribute'],'person_id':[pid],'product_id':['p'],
        'spine_row':[1],'duty':['produce'],'managed':[True],'managed_team_id':[pid],
        'share':[.05],'total_rate':[.05],'participation_sales':[100.0],
        'participation_gross':[60.0],'participation_profit':[50.0],'amount':[2.5]},run_id=1)
    ws.record('s1','2026-06',{'commission':{'engine':'commission-v2','calculation_id':cid,'managed_sales':{pid:100},
        'people':[{'person_id':pid,'person':'甲','amount':2.5}],'total':2.5,'amount_complete':True},
        'statement':[{'id':'n_receipt','value':100,'available':True},{'id':'gross','value':60,'available':True}]},[])
    selected={'start':'2026-06','end':'2026-06','view':'managed'}
    before=client.post('/api/commission-v2/reports/query',json={**selected,'view':'people'}).json()
    response=client.post('/api/commission-v2/reports/query',json=selected)
    assert response.status_code==200,response.text
    body=response.json()
    assert body['count']==1 and body['items'][0]['children'][0]['sales']==100
    assert body['items'][0]['trial_amount']==2.5
    exported=client.post('/api/commission-v2/export/reports/managed',json={**selected,'run_ids':body['run_ids'],'fingerprint':body['fingerprint'],'presentation':True})
    assert exported.status_code==200,exported.text
    lines=list(csv.DictReader(io.StringIO(exported.text.lstrip('\ufeff'))))
    assert len(lines)==3 and all(Decimal(r['托管销售额'])==Decimal('100') for r in lines)
    assert [r['层级'] for r in lines]==['1','2','3']
    team_filtered=client.post('/api/commission-v2/reports/query',json={**selected,'managed_team_ids':[pid]}).json()
    assert team_filtered['count']==1 and team_filtered['selection']['managed_team_ids']==[pid]
    absent=client.post('/api/commission-v2/reports/query',json={**selected,'managed_team_ids':['other-team']}).json()
    assert absent['count']==0
    after=client.post('/api/commission-v2/reports/query',json={**selected,'view':'people'}).json()
    assert after==before


def test_filtered_export_labels_subtotal_and_matches_selected_person(tmp_path):
    reg,a,b,c,kw,_=sample(tmp_path)
    tree=scope(reg,c,**kw,selected_people=[a])
    _,rows=business_export({'managed':tree,'selection':{'person_ids':[a]}},'managed')
    exported=list(rows)
    assert [r['行类型'] for r in exported]==['团队小计（筛选人员）','个人小计','商品明细']
    assert all(r['托管系统应发']==.8 for r in exported)
    assert all('不代表完整团队' in r['说明'] for r in exported)


def test_unknown_values_and_unassigned_subtotal_remain_explicit(tmp_path):
    reg,_,_,_,kw,_=sample(tmp_path)
    tree=scope(reg,{},**kw)
    _,rows=business_export({'managed':tree},'managed')
    exported=list(rows)
    assert [r['行类型'] for r in exported]==['团队合计','待分配小计','商品明细']
    assert all(r['托管毛利额'] is None and r['托管利润额（分摊后）'] is None and r['托管系统应发'] is None for r in exported)
    assert all(r['托管销售额']==100 for r in exported)
