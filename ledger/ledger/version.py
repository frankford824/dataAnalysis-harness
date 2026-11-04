"""这份账是哪一版代码算出来的。

一个字符串，但没有它，「回滚」这个词是空的。

引擎会一直改——加平台、接新表、改口径，模型自己也会参与改。改错了不一定报错，
更常见的形态是某家店某个月的数字悄悄变了。发现的时候要问三个问题：

    这个数是哪一版算的        没有版本号就只能靠提交时间猜，而快照可以是几周前的
    那一版和现在差在哪         有版本号才 diff 得出来
    回到那一版                 回到一个说不清是什么的状态，不叫回滚

所以每次算账都盖一个印。印的内容是 git 提交号加上工作区脏不脏。

`-dirty` 那个后缀是这里最要紧的一位。工作区脏的时候算出来的结果不可复现——那份代码
不在版本库里，谁也拿不回来。这种结果照样能存、能看，但它不该被当成一个能回滚到的点，
更不该被录成基线。标出来，剩下的交给人判断。
"""

from __future__ import annotations

import os
import subprocess
import hashlib
import json
import re
import threading
from pathlib import Path

#: 仓库根。从这个文件往上两层：ledger/ledger/version.py → ledger/ → 仓库根。
_ROOT = Path(__file__).resolve().parents[2]

#: 只看这两个目录脏不脏。文档、笔记、别的项目改了不影响账怎么算。
_WATCHED = ("ledger", "models")

#: 部署时落下的版本印。
#:
#: 线上那台机器上没有 git，代码是打包送过去的，也就不是个 git 仓库——问 git 只会
#: 得到 `unknown`，于是生产环境里每一笔账都说不清是哪一版算的，上面那三个问题
#: 一个都答不了。恰恰是生产环境最需要答。
#:
#: 所以打包的时候把当时的版本号写进这个文件，跟着代码一起走。它不是猜的：
#: 内容就是打包机上 `engine_version()` 当场问出来的那一串，脏就带 `-dirty`。
_STAMP = _ROOT / "VERSION"

#: 问不出来时用这个。空字符串不行——落进数据库之后分不清是「没盖印」还是「盖了个空的」。
UNKNOWN = "unknown"


def engine_version() -> str:
    """引擎的版本标识：git 提交号 + 工作区是否有未提交改动。

    带发行批准收据的目录使用 `VERSION` 的完整部署身份，健康由独立源码护栏判断。
    开发目录先问 git，问不出来再读 `VERSION`；开发机的旧版本印不能覆盖当前 Git 状态。

    两个都问不出来返回 `unknown`。这种情况下不抛异常：算账不该因为版本号取不到就
    停下来，取不到本身也是一条要如实记下的信息。
    """
    # A packaged approval owns the full deployed label, even if stale Git
    # metadata was left beside it. Integrity is independently checked below.
    approval=_ROOT/'RELEASE-ACCEPTANCE.json'
    if approval.exists() or approval.is_symlink():return _stamped()
    try:
        sha = _git("rev-parse", "--short", "HEAD")
        dirty = _git("status", "--porcelain", "--", *_WATCHED)
    except (OSError, subprocess.SubprocessError):
        return _stamped()
    if not sha:
        return _stamped()
    return f"{sha}-dirty" if dirty else sha


def _stamped() -> str:
    try:
        value = _STAMP.read_text(encoding="utf-8").strip()
    except OSError:
        return UNKNOWN
    # 一行、无空白。文件被别的东西写花了就当没有——盖一个错的印比不盖更坏。
    return value if value and len(value.split()) == 1 else UNKNOWN


def reproducible(version: str) -> bool:
    """这个版本能不能原样再算一遍。

    能，才谈得上「回到这一版」。脏工作区和问不出版本号都不能。
    """
    return bool(version) and version != UNKNOWN and not version.endswith("-dirty")


def source_code_manifest(root: Path | None = None) -> list[dict]:
    """Actual deployed Python bytes, independently of the mutable VERSION label."""
    root=Path(root or _ROOT).resolve()
    folder=root/'ledger/ledger'
    if not folder.is_dir():raise ValueError('Ledger source tree is missing')
    if folder.is_symlink() or getattr(folder,'is_junction',lambda:False)():
        raise ValueError('Ledger source root is symbolic')
    result=[]
    for path in sorted(folder.rglob('*'),key=lambda path:path.relative_to(root).as_posix()):
        if path.is_symlink() or getattr(path,'is_junction',lambda:False)():
            raise ValueError('Ledger source contains a symbolic path')
        if path.suffix=='.py' and path.is_file():
            result.append({'path':path.relative_to(root).as_posix(),
                           'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    if not result:raise ValueError('Ledger source identity is empty')
    return result


def source_code_sha(root: Path | None = None) -> str:
    return hashlib.sha256(json.dumps(source_code_manifest(root),sort_keys=True,
        ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()


CODE_MAINTENANCE = '系统源码或批准身份不可验证，已暂停业务写入与核算发布，请交技术维护恢复批准代码并重启服务'


class CodeIntegrityError(RuntimeError):
    def __init__(self, integrity=None):
        super().__init__(CODE_MAINTENANCE)
        self.integrity=dict(integrity or {})


class _IntegrityFault(ValueError):
    pass


def _canonical_sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,
        separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _development_checkout(root):
    if not (root/'.git').exists():return False
    try:
        found=subprocess.run(['git','-C',str(root),'rev-parse','--show-toplevel'],capture_output=True,
            text=True,check=True,timeout=5).stdout.strip()
        return bool(found) and Path(found).resolve()==root
    except (OSError,subprocess.SubprocessError,ValueError):return False


class CodeIntegrityMonitor:
    """A process-start identity, never an approval reset from a later request.

    An approved deployment has VERSION and an immutable receipt anchor. A real
    source checkout without either release marker may use its startup bytes for
    development. Any observed mismatch remains maintenance until a new process.
    """
    def __init__(self, root):
        self.root=Path(root).resolve();self.receipt=self.root/'RELEASE-ACCEPTANCE.json';self.stamp=self.root/'VERSION'
        self._lock=threading.Lock();self._fault=None;self._approved=None;self._receipt_bytes=None
        self._had_receipt=self.receipt.exists() or self.receipt.is_symlink()
        self._had_stamp=self.stamp.exists() or self.stamp.is_symlink()
        self._initial_stamp=None;self._mode='approved'
        try:self._initial_source=source_code_sha(self.root)
        except (OSError,ValueError):self._initial_source=None;self._fault='source_unavailable'
        try:self._initial_stamp=self._stamp_value()
        except (OSError,ValueError):self._fault=self._fault or 'version_mismatch'
        if self._had_receipt:
            try:
                self._receipt_bytes=self._receipt_value()
                self._approved=self._approval(self._receipt_bytes,self._initial_stamp)
            except _IntegrityFault as error:self._fault=self._fault or str(error)
            except (OSError,ValueError,TypeError,KeyError,AttributeError,ImportError):self._fault=self._fault or 'receipt_invalid'
            if self._approved is not None and self._initial_source!=self._approved:
                self._fault=self._fault or 'source_drift'
        elif not self._had_stamp and _development_checkout(self.root):
            self._mode='development';self._approved=self._initial_source
        else:self._fault=self._fault or 'receipt_missing'

    def _stamp_value(self):
        if self.stamp.is_symlink():raise ValueError('Version stamp is symbolic')
        if not self.stamp.exists():return None
        return self.stamp.read_text(encoding='utf-8-sig').strip()

    def _receipt_value(self):
        if self.receipt.is_symlink():raise ValueError('Release receipt is symbolic')
        with self.receipt.open('rb') as stream:raw=stream.read(4*1024*1024+1)
        if len(raw)>4*1024*1024:raise ValueError('Release receipt is not bounded')
        return raw

    @staticmethod
    def _approval(raw,stamp):
        value=json.loads(raw.decode('utf-8-sig'))
        if not isinstance(value,dict):raise _IntegrityFault('receipt_invalid')
        code=value.get('code_sha')
        if not isinstance(code,str) or not re.fullmatch('[0-9a-f]{40}',code) or stamp!=code:
            raise _IntegrityFault('version_mismatch')
        from .release_gate import validate
        validate(value,version=code)
        approved=value.get('source_code_sha');manifest=value.get('source_code_manifest')
        if not isinstance(approved,str) or not re.fullmatch('[0-9a-f]{64}',approved) or not isinstance(manifest,list) or not manifest:
            raise _IntegrityFault('receipt_invalid')
        paths=[]
        for entry in manifest:
            if (not isinstance(entry,dict) or set(entry)!={'path','sha256'} or not isinstance(entry.get('path'),str)
                    or not entry['path'].startswith('ledger/ledger/') or not entry['path'].endswith('.py')
                    or '..' in Path(entry['path']).parts or not isinstance(entry.get('sha256'),str)
                    or not re.fullmatch('[0-9a-f]{64}',entry['sha256'])):
                raise _IntegrityFault('receipt_invalid')
            paths.append(entry['path'])
        if paths!=sorted(set(paths)) or _canonical_sha(manifest)!=approved:
            raise _IntegrityFault('receipt_invalid')
        declared=value.get('receipt_sha')
        if not isinstance(declared,str) or not re.fullmatch('[0-9a-f]{64}',declared) or declared!=_canonical_sha({key:item for key,item in value.items() if key!='receipt_sha'}):
            raise _IntegrityFault('receipt_invalid')
        return approved

    def state(self):
        try:actual=source_code_sha(self.root)
        except (OSError,ValueError):actual=None
        if os.environ.get('LEDGER_CODE_INTEGRITY')=='off' and actual is not None:
            return {'healthy':True,'source_code_sha':actual,'approved_source_code_sha':actual,
                'source_code_status':'unchecked','restart_required':False}
        with self._lock:
            fault=self._fault
            if fault is None:
                if actual is None:fault='source_unavailable'
                elif actual!=self._approved:fault='source_drift'
                elif self._had_receipt:
                    if not self.receipt.exists():fault='receipt_missing'
                    else:
                        try:
                            if self._receipt_value()!=self._receipt_bytes:fault='receipt_changed'
                        except (OSError,ValueError):fault='receipt_invalid'
                    try:
                        if self._stamp_value()!=self._initial_stamp:fault=fault or 'version_mismatch'
                    except (OSError,ValueError):fault=fault or 'version_mismatch'
                elif self.receipt.exists() or self.receipt.is_symlink():fault='receipt_changed'
                elif self.stamp.exists() or self.stamp.is_symlink():fault='version_mismatch'
            self._fault=fault
            return {'healthy':fault is None,'source_code_sha':actual,'approved_source_code_sha':self._approved,
                'source_code_status':fault or self._mode,'restart_required':fault is not None}


# Eager import-time capture: a drift before the first HTTP request is still a
# drift. This object is never re-baselined by code_integrity() or a business API.
_CODE_MONITOR=CodeIntegrityMonitor(_ROOT)


def code_integrity():
    return _CODE_MONITOR.state()


def assert_code_integrity():
    status=code_integrity()
    if status.get('healthy') is not True:raise CodeIntegrityError(status)
    return status


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(_ROOT), *args],
        capture_output=True, text=True, timeout=5, check=True,
    ).stdout.strip()


__all__ = ["UNKNOWN", "engine_version", "reproducible", "source_code_manifest", "source_code_sha",
    "code_integrity", "assert_code_integrity", "CodeIntegrityError", "CodeIntegrityMonitor"]

