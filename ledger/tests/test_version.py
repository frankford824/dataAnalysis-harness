"""版本印。

这里测的是一件很容易被认为不重要的事：线上那台机器上没有 git。

代码是打包送过去的，那个目录不是 git 仓库，`git rev-parse` 只会失败。原本的实现
在失败时返回 `unknown`，于是生产环境里每一笔账的运行记录上都写着「引擎 unknown」。
界面照样好看，账照样能结，但「这个数是哪一版算的」永久失去了答案——而这恰恰是
出了差错时唯一能救人的那条信息。开发机上一切正常，所以这个洞在本地永远看不见。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ledger import version


def test_actual_source_drift_changes_identity_even_when_version_stamp_does_not(tmp_path,monkeypatch):
    folder=tmp_path/'ledger/ledger';folder.mkdir(parents=True)
    source=folder/'example.py';source.write_text('value = 1\n')
    stamp=tmp_path/'VERSION';stamp.write_text('abcdef7')
    monkeypatch.setattr(version,'_STAMP',stamp)
    monkeypatch.setattr(version,'_git',lambda *args: (_ for _ in ()).throw(OSError('no repository')))
    label=version.engine_version();before=version.source_code_sha(tmp_path)
    source.write_text('value = 2\n')
    assert version.engine_version()==label=='abcdef7'
    assert version.source_code_sha(tmp_path)!=before
    after=version.source_code_sha(tmp_path)
    (folder/'unexpected_module.py').write_text('value = 3\n')
    assert version.source_code_sha(tmp_path)!=after


@pytest.fixture
def no_git(monkeypatch):
    """模拟机器上没装 git。"""
    def boom(*_a, **_k):
        raise OSError("git 不存在")
    monkeypatch.setattr(version, "_git", boom)


class TestStampTakesOverWhenGitIsGone:
    def test_reads_the_deploy_stamp(self, no_git, monkeypatch, tmp_path):
        stamp = tmp_path / "VERSION"
        stamp.write_text("c4b79a6\n", encoding="utf-8")
        monkeypatch.setattr(version, "_STAMP", stamp)
        assert version.engine_version() == "c4b79a6"

    def test_a_dirty_stamp_stays_dirty(self, no_git, monkeypatch, tmp_path):
        """打包时工作区是脏的，线上就该照实说脏——否则会被当成一个能回滚到的点。"""
        stamp = tmp_path / "VERSION"
        stamp.write_text("c4b79a6-dirty", encoding="utf-8")
        monkeypatch.setattr(version, "_STAMP", stamp)
        assert version.engine_version() == "c4b79a6-dirty"
        assert not version.reproducible(version.engine_version())

    def test_no_stamp_is_still_unknown(self, no_git, monkeypatch, tmp_path):
        monkeypatch.setattr(version, "_STAMP", tmp_path / "缺这个文件")
        assert version.engine_version() == version.UNKNOWN

    def test_a_garbled_stamp_is_refused(self, no_git, monkeypatch, tmp_path):
        """盖一个错的印比不盖更坏：它会让人以为查得到，然后查到一个假答案。"""
        stamp = tmp_path / "VERSION"
        stamp.write_text("c4b79a6 外加一句解释", encoding="utf-8")
        monkeypatch.setattr(version, "_STAMP", stamp)
        assert version.engine_version() == version.UNKNOWN

    def test_an_empty_stamp_is_refused(self, no_git, monkeypatch, tmp_path):
        stamp = tmp_path / "VERSION"
        stamp.write_text("   \n", encoding="utf-8")
        monkeypatch.setattr(version, "_STAMP", stamp)
        assert version.engine_version() == version.UNKNOWN


class TestGitWinsWhenItIsThere:
    def test_git_beats_a_stale_stamp(self, monkeypatch, tmp_path):
        """开发机上代码随时在动，git 说的才是当下这一刻；VERSION 是打包那一刻的。"""
        stamp = tmp_path / "VERSION"
        stamp.write_text("老版本", encoding="utf-8")
        monkeypatch.setattr(version, "_STAMP", stamp)
        monkeypatch.setattr(version, "_git",
                            lambda *a: "abc1234" if a[0] == "rev-parse" else "")
        assert version.engine_version() == "abc1234"

    def test_git_present_but_not_a_repo_falls_back(self, monkeypatch, tmp_path):
        """装了 git 但目录不是仓库——线上如果哪天装了 git，就是这个情况。"""
        stamp = tmp_path / "VERSION"
        stamp.write_text("c4b79a6", encoding="utf-8")
        monkeypatch.setattr(version, "_STAMP", stamp)

        def not_a_repo(*_a, **_k):
            raise subprocess.CalledProcessError(128, "git")
        monkeypatch.setattr(version, "_git", not_a_repo)
        assert version.engine_version() == "c4b79a6"


class TestTheStampLandsWhereTheCodeIs:
    def test_it_sits_at_the_repo_root(self):
        """VERSION 要和 ledger/、models/ 并排，打包脚本按这个路径写。"""
        assert version._STAMP.parent == version._ROOT
        assert version._STAMP.name == "VERSION"
        assert (version._ROOT / "ledger").is_dir()
        assert (version._ROOT / "models").is_dir()

    def test_real_version_is_a_single_token(self):
        """真跑一次。不管走的是 git 还是 VERSION，结果都得能塞进一列数据库字段。"""
        v = version.engine_version()
        assert v and len(v.split()) == 1


def test_api_version_exposes_actual_and_approved_source_status(monkeypatch):
    from ledger import api
    monkeypatch.setattr(api,'engine_version',lambda:'a'*40)
    monkeypatch.setattr(api,'_model_revision',lambda:'model-fixture')
    monkeypatch.setattr(api,'code_integrity',lambda:{'healthy':False,'source_code_sha':'b'*64,
        'approved_source_code_sha':'c'*64,'source_code_status':'source_drift'})
    result=api.version_info()
    assert result['version']=='a'*40 and result['source_code_sha']=='b'*64
    assert result['approved_source_code_sha']=='c'*64 and result['source_code_status']=='source_drift'


def test_packaged_approval_uses_full_version_stamp_over_leftover_git(monkeypatch,tmp_path):
    (tmp_path/'RELEASE-ACCEPTANCE.json').write_text('{}')
    stamp=tmp_path/'VERSION';stamp.write_text('a'*40+'\n')
    monkeypatch.setattr(version,'_ROOT',tmp_path);monkeypatch.setattr(version,'_STAMP',stamp)
    monkeypatch.setattr(version,'_git',lambda *args:'old1234-dirty')
    assert version.engine_version()=='a'*40
    # The label alone is not authority: malformed approval remains unhealthy.
    source=tmp_path/'ledger/ledger';source.mkdir(parents=True);(source/'module.py').write_text('VALUE=1\n')
    assert version.CodeIntegrityMonitor(tmp_path).state()['healthy'] is False
