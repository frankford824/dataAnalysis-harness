"""Root close wiring over declared providers; real task/raw tests live separately."""
import pytest
from ledger import automation_actions as actions,automation_source_cost_v3 as v3,source_recovery as source
from test_automation import sample,HUMAN,PERIOD


def technical_providers(monkeypatch):
    state={'source':'processing','quote':'processing','events':[],'source_valid':True,'quote_valid':True}
    def capture(ws,model,**kw):
        state['events'].append(('source',kw['request_id']))
        return {'required':True,'status':state['source'],'refresh_id':'source-one','generation_sha':'generation-one'}
    def quotes(ws,model,period,stores,request_id,actor):
        state['events'].append(('quote',request_id))
        return {'required':True,'status':state['quote'],'refresh_id':'quote-one','quote_sha':'quote-one-sha'}
    def check_source(*args,**kw):
        state['events'].append(('source_verify',kw['refresh_id']))
        if not state['source_valid']:raise actions.Conflict('source generation changed')
    def check_quote(*args,**kw):
        state['events'].append(('quote_verify',args[4]))
        if not state['quote_valid']:raise actions.Conflict('quote generation changed')
    monkeypatch.setattr(source,'ensure_close_refresh',capture)
    monkeypatch.setattr(source,'verify_close_refresh',check_source)
    monkeypatch.setattr(v3,'ensure_quote_refresh',quotes)
    monkeypatch.setattr(v3,'verify_quote_refresh',check_quote)
    return state


def test_close_capture_stages_are_serial_and_cannot_authorize_early(sample,monkeypatch):
    ws,model=sample;state=technical_providers(monkeypatch)
    def preview():return actions.close_preview(ws,model,request_id='same-close-attempt',period=PERIOD,store_ids=['s1'],actor=HUMAN)
    first=preview();assert first['status']=='processing' and first['proposal_id'] is None
    assert state['events']==[('source','same-close-attempt')]
    assert not ws.conn.execute('SELECT 1 FROM automation_proposal').fetchone()
    assert not ws.conn.execute("SELECT 1 FROM period WHERE state='closed'").fetchone()
    state['source']='verified';second=preview()
    assert second['status']=='processing' and second['source_quote_refresh']['refresh_id']=='quote-one'
    assert not ws.conn.execute('SELECT 1 FROM automation_proposal').fetchone()
    state['quote']='verified';third=preview()
    assert third['proposal_id'] and len(third['eligible'])==1
    assert ws.conn.execute('SELECT count(*) FROM automation_proposal').fetchone()[0]==1
    proposal=ws.conn.execute('SELECT payload_json FROM automation_proposal WHERE id=?',(third['proposal_id'],)).fetchone()[0]
    assert 'source-one' in proposal and 'quote-one-sha' in proposal
    confirmed=actions.close_confirm(ws,model,request_id='confirm-serial-close',proposal_id=third['proposal_id'],
        proposal_sha=third['proposal_sha'],input_sha=third['input_sha'],actor=HUMAN)
    assert len(confirmed['completed'])==1 and not confirmed['unfinished']
    assert ws.state('s1',PERIOD).closed and not ws.state('s2',PERIOD).closed


@pytest.mark.parametrize('changed',['source_valid','quote_valid'])
def test_confirm_rechecks_proposal_capture_before_any_close(sample,monkeypatch,changed):
    ws,model=sample;state=technical_providers(monkeypatch);state.update(source='verified',quote='verified')
    proposal=actions.close_preview(ws,model,request_id='ready-source-close',period=PERIOD,store_ids=['s1','s2'],actor=HUMAN)
    state[changed]=False
    with pytest.raises(actions.Conflict,match='generation changed'):
        actions.close_confirm(ws,model,request_id='cannot-close-old-capture',proposal_id=proposal['proposal_id'],
            proposal_sha=proposal['proposal_sha'],input_sha=proposal['input_sha'],actor=HUMAN)
    assert not ws.conn.execute("SELECT 1 FROM period WHERE state='closed'").fetchone()
    assert not ws.conn.execute("SELECT 1 FROM automation_receipt WHERE action='close.confirm'").fetchone()


def test_real_source_conflict_is_pending_without_quote_or_proposal(sample,monkeypatch):
    ws,model=sample;state=technical_providers(monkeypatch)
    def conflict(*args,**kwargs):raise source.SourcePending('physical_source_semantics_changed')
    monkeypatch.setattr(source,'ensure_close_refresh',conflict)
    result=actions.close_preview(ws,model,request_id='source-conflict',period=PERIOD,store_ids=['s1'],actor=HUMAN)
    assert result['status']=='pending' and result['reason_codes']==['physical_source_semantics_changed']
    assert result['financial_applied'] is False and result['proposal_id'] is None
    assert state['events']==[] and not ws.conn.execute('SELECT 1 FROM automation_proposal').fetchone()
