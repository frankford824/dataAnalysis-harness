import hashlib
from pathlib import Path

import pytest

from ledger.freight_trial_inputs import _materialize,InputError


def source(tmp_path,data=b'original-data'):
    root=tmp_path/'space';sha=hashlib.sha256(data).hexdigest();path=root/'files'/sha[:2]/sha
    path.parent.mkdir(parents=True);path.write_bytes(data)
    return root,path,{'sha':sha,'name':'订单明细.xlsx'}


def test_archived_symlink_is_copied_without_cloning_or_changing_the_link(tmp_path):
    root,path,record=source(tmp_path)
    archive=tmp_path/'archive.data';archive.write_bytes(path.read_bytes());path.unlink();path.symlink_to(archive)
    target=_materialize(root,root/'freight-shadow',record)
    assert path.is_symlink() and not target.is_symlink() and target.read_bytes()==archive.read_bytes()
    assert target!=path and target.stat().st_ino!=archive.stat().st_ino
    assert _materialize(root,root/'freight-shadow',record)==target


def test_existing_bad_input_copy_is_not_silently_used_or_overwritten(tmp_path):
    root,path,record=source(tmp_path)
    target=_materialize(root,root/'freight-shadow',record);target.write_bytes(b'wrong')
    with pytest.raises(InputError,match='校验'):_materialize(root,root/'freight-shadow',record)
    assert path.read_bytes()==b'original-data' and target.read_bytes()==b'wrong'


def test_original_hash_mismatch_blocks_proposal_input(tmp_path):
    root,path,record=source(tmp_path);path.write_bytes(b'changed')
    with pytest.raises(InputError,match='校验'):_materialize(root,root/'freight-shadow',record)


def test_untrusted_copy_symlink_cannot_redirect_a_write(tmp_path):
    root,path,record=source(tmp_path)
    cache=root/'freight-shadow';target=cache/'input-copies-v1'/record['sha']/record['name']
    target.parent.mkdir(parents=True);outside=tmp_path/'outside';outside.write_bytes(b'keep');target.symlink_to(outside)
    with pytest.raises(InputError,match='路径'):_materialize(root,cache,record)
    assert outside.read_bytes()==b'keep'
