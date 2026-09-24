import hashlib
import shutil

import polars as pl
import pytest

from conftest import MODELS,write_xlsx
from ledger import unclassified_evidence as evidence
from ledger.engine.types import ClassifyReport
from ledger.model.loader import load_model
from ledger.model.schema import FeeRule
from ledger.storage_integrity import seal,digest
from ledger.view import drill,UNCLASSIFIED_NODE
from ledger.workspace import Workspace,WorkspaceError


def facts(rows):
    base=dict(metric_id='trade_receipt',source_id='settlement',template_id='taobao_settlement_wechat_v1',
              file_sha='sha',file_name='账单.xlsx',sheet='sheet1',row_no=2,
              link_key='order',order_id=None,linked=True,subject=None,major=None,minor=None,
              amount=-6.02,counted=False,contribution=0.,classify_via='')
    return pl.DataFrame([{**base,**r} for r in rows])


def report():
    out=ClassifyReport()
    out.note_unmatched('（业务描述为空）biz_type=待核对 remark=保证金管控资金使用',('sha','sheet1','2'),-6.02)
    return out


def test_blank_subject_and_metric_duplicates_use_exact_audit_anchors():
    original=facts([{},dict(metric_id='software_fee'),dict(row_no=3,subject='已排除',classify_via='排除规则'),
                    dict(row_no=4,metric_id='goods_cost',amount=-10)])
    tagged=evidence.attach(original,report())
    assert tagged.select(original.columns).equals(original)
    result=drill(tagged,load_model(MODELS/'cn-ecommerce'),UNCLASSIFIED_NODE)
    assert result['rows']==1 and result['source_total']==-6.02 and result['value']==-6.02
    assert result['sample'][0]['row_no']==2
    assert result['sample'][0]['contribution']==0
    assert '保证金管控' in result['sample'][0]['subject']
    filtered=drill(tagged,load_model(MODELS/'cn-ecommerce'),UNCLASSIFIED_NODE,q='保证金管控')
    assert filtered['selection']['rows']==1


def test_empty_dictionary_does_not_turn_known_or_excluded_facts_into_pending():
    model=load_model(MODELS/'cn-ecommerce').model_copy(update={'dictionary':()})
    data=evidence.attach(facts([dict(subject='新费项',major='software_fee'),dict(row_no=3,subject='押金',classify_via='排除')]),ClassifyReport())
    assert drill(data,model,UNCLASSIFIED_NODE)['rows']==0


def test_null_sheet_anchor_preserves_original_fields():
    original=facts([dict(sheet=None)])
    r=ClassifyReport();r.note_unmatched('空描述',('sha','','2'),-6.02)
    tagged=evidence.attach(original,r)
    assert tagged.select(original.columns).equals(original)
    assert tagged[evidence.LABEL][0]=='空描述'


def test_zero_net_unclassified_rows_are_not_an_empty_result():
    original=facts([{},dict(row_no=3,amount=6.02)])
    r=report();r.note_unmatched(next(iter(r.unmatched_rows)),('sha','sheet1','3'),6.02)
    result=drill(evidence.attach(original,r),load_model(MODELS/'cn-ecommerce'),UNCLASSIFIED_NODE)
    assert result['rows']==2 and result['source_total']==0


def test_other_scope_pending_rows_do_not_enter_this_warning():
    original=facts([{},dict(row_no=3,amount=-99.)])
    tagged=evidence.attach(original,report())
    assert tagged.filter(pl.col(evidence.LABEL).is_not_null())['row_no'].to_list()==[2]


def test_direct_legacy_drill_cannot_guess_using_current_dictionary():
    with pytest.raises(WorkspaceError,match='归类行依据'):
        drill(facts([{}]),load_model(MODELS/'cn-ecommerce'),UNCLASSIFIED_NODE)


def legacy(tmp_path):
    from ledger.engine.runtime import ingest,_with_row_amount,ROW_AMOUNT
    from ledger.engine.classify import classify
    from ledger.commission_registry import Registry
    model=load_model(MODELS/'cn-ecommerce');sid='taobao_xibishun';t=model.template('taobao_settlement_wechat_v1')
    headers=[b.columns[0] for b in t.bindings]
    values={'txn_id':'9001','subject':None,'income':0,'outgo':6.02,'base_order_id':'3314020983139010783',
            'sub_order_id':'3314020983139010783','remark':'保证金管控资金使用','biz_type':'测试未归类保证金扣款',
            'store_name':model.store(sid).name,'settle_time':'2026-08-01 12:00:00'}
    path=write_xlsx(tmp_path/'对账微信.xlsx',[headers,[values.get(b.role) for b in t.bindings]],sheet='sheet1')
    sha=digest(path);ing=ingest([path],model,default_store=model.store(sid).name);item=ing.known[0]
    _,r=classify(_with_row_amount(item.frame,model.metric('trade_receipt').for_platform('taobao')),model,'taobao',ROW_AMOUNT,item.template)
    ws=Workspace(tmp_path/'ws');source=ws.root/'files'/sha[:2]/sha;source.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,source)
    payload=dict(platform='taobao',unclassified=[dict(label=k,count=c,amount=a) for k,(c,a) in r.unmatched.items()],
                 commission={'calculation_id':'old-model'},statement=[],can_close=False)
    rid=ws.record(sid,'2026-07',payload,[sha],model_revision=hashlib.sha256(model.model_dump_json().encode()).hexdigest())
    frame=facts([dict(file_sha=sha,file_name=path.name),dict(file_sha=sha,file_name=path.name,metric_id='software_fee')])
    frame.write_parquet(ws.facts_path(rid));seal(ws.facts_path(rid))
    registry=Registry(ws.root)
    with registry.transaction() as c:
        c.execute('INSERT INTO calculation VALUES(?,?,?,?,?,?,?,?,?,?,?)',('old-model',rid,sid,'2026-07','2026-09-01',0,'unused','unused',model.model_dump_json(),'{}','{}'))
    return ws,model,rid,source,payload,frame


def test_legacy_recovery_uses_frozen_rules_and_does_not_modify_any_evidence(tmp_path):
    ws,old,rid,source,payload,original=legacy(tmp_path)
    changed=old.model_copy(update={'fee_rules':(*old.fee_rules,FeeRule(platform='taobao',field='biz_type',how='contains',value='测试未归类',major='software_fee',stage='before'))})
    before=(digest(source),digest(ws.facts_path(rid)),ws.conn.execute('select result from run where id=?',(rid,)).fetchone()[0])
    recovered=evidence.for_run(ws,rid,changed)
    result=drill(recovered,changed,UNCLASSIFIED_NODE)
    assert result['rows']==1 and result['source_total']==-6.02
    assert result['sample'][0]['row_no']==2 and '保证金管控' in result['sample'][0]['subject']
    assert before==(digest(source),digest(ws.facts_path(rid)),ws.conn.execute('select result from run where id=?',(rid,)).fetchone()[0])


def test_missing_legacy_source_never_looks_like_zero_rows(tmp_path):
    ws,model,rid,source,_,_=legacy(tmp_path);source.unlink()
    with pytest.raises(WorkspaceError,match='原文件缺失'):evidence.for_run(ws,rid,model)


def test_repeated_reads_reuse_verified_pending_rows_but_still_check_source(tmp_path,monkeypatch):
    import ledger.engine.runtime as runtime
    ws,model,rid,source,_,_=legacy(tmp_path)
    first=evidence.for_run(ws,rid,model)
    monkeypatch.setattr(runtime,'_ingest_file_cached',lambda *a,**k:pytest.fail('same frozen evidence should not reparse'))
    assert evidence.for_run(ws,rid,model).equals(first)
    source.unlink()
    with pytest.raises(WorkspaceError,match='原文件缺失'):evidence.for_run(ws,rid,model)


def test_disagreement_with_frozen_warning_is_explicit(tmp_path):
    ws,model,rid,source,payload,frame=legacy(tmp_path)
    import json
    payload['unclassified'][0]['count']=2
    with ws.conn:ws.conn.execute('update run set result=? where id=?',(json.dumps(payload),rid))
    with pytest.raises(WorkspaceError,match='笔数或金额不一致'):evidence.for_run(ws,rid,model)


def test_failed_warning_with_empty_archive_is_not_reported_as_zero(tmp_path):
    ws,model,rid,source,payload,frame=legacy(tmp_path)
    import json
    payload.update(unclassified=[],findings=[{'id':'chk_no_unclassified','passed':False}])
    with ws.conn:ws.conn.execute('update run set result=? where id=?',(json.dumps(payload),rid))
    with pytest.raises(WorkspaceError,match='归档清单为空'):evidence.for_run(ws,rid,model)


def test_new_evidence_does_not_need_source_reparse(tmp_path,monkeypatch):
    ws,model,rid,source,payload,frame=legacy(tmp_path)
    r=ClassifyReport();r.note_unmatched(payload['unclassified'][0]['label'],(frame['file_sha'][0],'sheet1','2'),-6.02)
    evidence.attach(frame,r).write_parquet(ws.facts_path(rid));seal(ws.facts_path(rid))
    monkeypatch.setattr(evidence,'_archived_model',lambda *a:pytest.fail('new evidence should not load model'))
    assert evidence.for_run(ws,rid,model).height==1


def test_api_restores_legacy_row_and_exposes_missing_evidence_instead_of_zero(tmp_path,monkeypatch):
    import ledger.api as api
    from fastapi.testclient import TestClient
    ws,model,rid,source,_,_=legacy(tmp_path)
    monkeypatch.setattr(api,'workspace',lambda:ws)
    monkeypatch.setattr(api,'_model',lambda:model)
    client=TestClient(api.app)
    response=client.get(f'/api/runs/{rid}/drill/__unclassified__',params={'q':'保证金'})
    assert response.status_code==200,response.text
    assert response.json()['rows']==1 and response.json()['source_total']==-6.02
    assert response.json()['selection']['rows']==1
    source.unlink()
    response=client.get(f'/api/runs/{rid}/drill/__unclassified__')
    assert response.status_code==409
    assert '原文件缺失' in response.json()['detail']


def test_engine_warning_and_persisted_pending_rows_are_the_same_set(tmp_path):
    from ledger.engine.runtime import ingest,run
    from test_reship_period_and_cost_drill import item
    from datetime import datetime
    ws,model,rid,source,payload,_=legacy(tmp_path)
    ing=ingest([source],model,default_store=model.store('taobao_xibishun').name)
    orders=[dict(order_id='3314020983139010783',product_id='p',store_name=model.store('taobao_xibishun').name,
                 order_time=datetime(2026,7,1),buyer_paid=20.,quantity=1.)]
    ing.items.append(item('order_detail',orders,orders[0].keys()))
    result=run(ing,'taobao')
    assert result.slices
    for sl in result.slices.values():
        pending=sl.facts.filter(pl.col(evidence.LABEL).is_not_null()).unique(subset=evidence.KEYS)
        assert pending.height==sum(count for count,amount in sl.classify_report.unmatched.values())
        assert pending[evidence.AMOUNT].sum()==pytest.approx(sum(amount for count,amount in sl.classify_report.unmatched.values()))
