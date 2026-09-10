from types import SimpleNamespace

from ledger import service
from ledger.model.schema import Model, Store
from ledger.workspace import Workspace


def test_low_disk_stops_recompute_before_ingestion_and_preserves_saved_period(tmp_path, monkeypatch):
    ws=Workspace(tmp_path)
    store=Store(id='s',name='店铺',platform='taobao')
    model=Model(id='test',name='test',stores=(store,))
    rid=ws.record('s','2026-06',{'can_close':False,'marker':'existing'},[])
    monkeypatch.setattr(service.shutil,'disk_usage',lambda root:SimpleNamespace(free=100*1024**2))
    monkeypatch.setattr(service,'ingest',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('must not read inputs')))
    result=service.recompute(ws,model,store)
    assert result.failure['kind']=='storage'
    assert result.periods==[]
    assert ws.state('s','2026-06').run_id==rid
    assert ws.state('s','2026-06').result['marker']=='existing'
