"""Financial input references survive the ordinary old-run hot-window policy."""
import sqlite3
from ledger.storage_maintenance import protected_runs


def test_approved_period_cost_keeps_its_original_source_run(tmp_path):
    root=tmp_path/'workspace';root.mkdir()
    with sqlite3.connect(root/'workspace.db') as db:
        db.executescript("""CREATE TABLE run(id INTEGER PRIMARY KEY,store_id TEXT,period TEXT,evidence_ready INTEGER);
        CREATE TABLE period(store_id TEXT,period TEXT,run_id INTEGER);
        CREATE TABLE period_cost_input(id TEXT,source_run_id INTEGER);""")
        db.executemany('INSERT INTO run VALUES(?,?,?,1)',[(i,'s','2026-09') for i in range(1,6)])
        db.execute("INSERT INTO period VALUES('s','2026-09',5)")
        db.execute("INSERT INTO period_cost_input VALUES('original-approval',1)")
    keep,known=protected_runs(root,2)
    assert known=={1,2,3,4,5}
    assert keep=={1,4,5}
    # Its raw capture lives outside runs/ and is not a managed run-cache object.
    source=root/'period-cost-input-evidence'/'capture.json';source.parent.mkdir();source.write_text('fixed original capture')
    protected_runs(root,1)
    assert source.read_text()=='fixed original capture'


def test_pre_feature_workspace_retention_still_reads_without_schema_write(tmp_path):
    with sqlite3.connect(tmp_path/'workspace.db') as db:
        db.executescript("""CREATE TABLE run(id INTEGER PRIMARY KEY,store_id TEXT,period TEXT,evidence_ready INTEGER);
        CREATE TABLE period(store_id TEXT,period TEXT,run_id INTEGER);
        INSERT INTO run VALUES(1,'s','2026-09',1);""")
    assert protected_runs(tmp_path,1)==({1},{1})
    with sqlite3.connect(tmp_path/'workspace.db') as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='period_cost_input'").fetchone()
