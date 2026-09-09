from dataclasses import replace
from io import BytesIO

import polars as pl
import pytest

from ledger import service
from ledger.commission_registry import Registry
from ledger.workspace import Workspace
from test_pdd_historical_pricing import scenario


@pytest.mark.parametrize('pending', [False, True])
def test_single_store_save_and_preview_do_not_use_foreign_shared_file_slices(tmp_path, monkeypatch, pending):
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED', '0')
    model,result=scenario(**({'unit_cost':None,'cost_status':'missing_price'} if pending else {}))
    own=result.slices[('shop','2026-05')]
    result.slices[('other shop','2026-05')]=replace(own,store='other shop',pricing_gaps=pl.DataFrame(),
        nodes={key:replace(value,value=999.,available=True,missing_sources=[]) for key,value in own.nodes.items()})
    ws=Workspace(tmp_path)
    ws.keep('shared.csv',BytesIO(b'shared expense input'), 's')
    Registry(tmp_path)
    monkeypatch.setattr(service,'ingest',lambda *args,**kwargs:result.ingestion)
    monkeypatch.setattr(service,'run',lambda *args,**kwargs:result)
    output=service.recompute(ws,model,model.store('s'))
    assert output.failure is None
    assert len(output.periods)==1
    assert ws.conn.execute("SELECT count(*) FROM run WHERE store_id='s'").fetchone()[0]==1
    saved=ws.state('s','2026-05').result
    assert saved['pricing_pending_count']==int(pending)
    profit=next(row for row in saved['statement'] if row['id']=='profit')
    assert profit['value']==(None if pending else -10)
    preview=service.simulate(ws,model,model.store('s'))
    assert len(preview)==1
    assert preview[0]['after']['pricing_pending_count']==int(pending)


def test_only_foreign_slices_cannot_create_a_target_store_period(tmp_path,monkeypatch):
    monkeypatch.setenv('LEDGER_ORDER_FEED_ENABLED','0')
    model,result=scenario()
    result.slices={('other shop','2026-05'):replace(result.slices[('shop','2026-05')],store='other shop')}
    ws=Workspace(tmp_path);ws.keep('shared.csv',BytesIO(b'other store data'),'s')
    monkeypatch.setattr(service,'ingest',lambda *args,**kwargs:result.ingestion)
    monkeypatch.setattr(service,'run',lambda *args,**kwargs:result)
    output=service.recompute(ws,model,model.store('s'))
    assert output.failure is not None
    assert ws.conn.execute('SELECT count(*) FROM run').fetchone()[0]==0
    assert service.simulate(ws,model,model.store('s'))==[]
