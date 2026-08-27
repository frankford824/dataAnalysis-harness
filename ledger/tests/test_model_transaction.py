from concurrent.futures import ThreadPoolExecutor

import pytest

from ledger.model.config import add_store
from ledger.model.loader import ModelError, load_model
from ledger.model.repository import ModelRepository
from ledger.model.schema import Store
from ledger.model.transaction import assert_revision, model_revision


@pytest.fixture
def model_dir(tmp_path):
    root = tmp_path / "model"
    root.mkdir()
    (root / "model.yaml").write_text("id: t\nname: 测试模型\n", encoding="utf-8")
    (root / "stores.yaml").write_text(
        "- id: taobao_a\n  name: 淘宝甲店\n  platform: taobao\n",
        encoding="utf-8",
    )
    return root


def test_concurrent_model_writes_do_not_lose_each_other(model_dir):
    stores = (
        Store(id="pdd_c", name="拼多多丙店", platform="pdd"),
        Store(id="jd_d", name="京东丁店", platform="jd"),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda store: add_store(model_dir, store), stores))
    assert {store.id for store in load_model(model_dir).stores} >= {"pdd_c", "jd_d"}


def test_stale_revision_is_rejected(model_dir):
    stale = model_revision(model_dir)
    add_store(model_dir, Store(id="pdd_c", name="拼多多丙店", platform="pdd"))
    with pytest.raises(ModelError, match="试跑之后"):
        assert_revision(model_dir, stale)


def test_csv_inputs_change_revision_and_cached_snapshot(model_dir):
    csv = model_dir / "dictionary.csv"
    csv.write_text("platform,raw,minor,major,naturally_unlinked\n", encoding="utf-8")
    repo = ModelRepository(model_dir)
    first = repo.get()
    assert repo.get() is first

    csv.write_text(
        "platform,raw,minor,major,naturally_unlinked\n"
        "taobao,服务费,平台服务费,platform_fee,0\n",
        encoding="utf-8",
    )
    second = repo.get()
    assert second is not first
    assert second.revision != first.revision
