from pathlib import Path

from tools.nas_layout import apply_layout, build_layout


MODEL = Path(__file__).resolve().parents[2] / "models" / "cn-ecommerce"


def test_layout_matches_current_model(tmp_path):
    manifest = build_layout(MODEL)
    assert len(manifest["platforms"]) == 9
    assert len(manifest["stores"]) == 52
    assert len(manifest["sources"]) == 11
    assert "00_上传区/00_全公司共享/聚水潭售后单" in manifest["directories"]
    assert "10_已接收/00_全公司共享/运费" in manifest["directories"]

    apply_layout(tmp_path, manifest)
    assert (tmp_path / "99_系统" / "manifests" / "layout.json").is_file()
    assert (tmp_path / "20_需修正").is_dir()


def test_layout_is_idempotent(tmp_path):
    manifest = build_layout(MODEL)
    apply_layout(tmp_path, manifest)
    marker = tmp_path / "00_上传区" / "keep-me.xlsx"
    marker.write_bytes(b"untouched")
    apply_layout(tmp_path, manifest)
    assert marker.read_bytes() == b"untouched"
