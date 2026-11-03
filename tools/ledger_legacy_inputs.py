"""Recompute an old engine's run input identities (run.shas) inside a scratch root.

Run only as a subprocess of build-historical-golden.py::

    python ledger_legacy_inputs.py REQUEST.json

REQUEST names an extracted `git archive` of the run's own engine revision. Only
that package is importable here, so ingestion and the order-feed overlay use the
exact code that produced the frozen run. Every write goes to REQUEST['scratch'].
"""
import json
from pathlib import Path
import shutil
import sys

request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
sys.path.insert(0, request['engine'])

from ledger.engine.runtime import ingest  # noqa: E402
from ledger.model.schema import Model  # noqa: E402
from ledger.order_feed import OrderFeed  # noqa: E402
from ledger.workspace import Workspace  # noqa: E402

root = Path(request['scratch'])
model = Model.model_validate_json(Path(request['model']).read_text(encoding='utf-8'))
store = model.store(request['store_id'])
ws = Workspace(root)
for slot in request['slots']:
    ws.keep(slot['name'], Path(slot['path']), slot['store_id'], by='historical-golden:legacy-inputs')
shutil.copyfile(request['feed_db'], root / 'order-feed.db')
for folder in request['support']:
    shutil.copytree(folder, root / Path(folder).name)
ingestion = ingest(ws.active_files(store.id), model, [store.name, *store.aliases],
                   cache_root=request['cache'], default_store=store.name)
OrderFeed(root, feed_root=Path(request['feed_root'])).append_to(ingestion, store)
ws.close()
print(json.dumps({'shas': sorted({item.ref.sha256 for item in ingestion.items})}))
