"""Immutable-image cache never trusts stale bytes or mutable test buffers."""
from sovereign_editor.core import Project
from sovereign_editor import core,formats


def test_reuses_only_identical_immutable_blob_and_rehashes_disk_rereads(monkeypatch,tmp_path):
    p=object.__new__(Project);path=tmp_path/'baseline.nds';path.write_bytes(b'first baseline')
    calls=[];original=formats.digest
    def measured(data):calls.append(data);return original(data)
    # Immutable images hash through formats.immutable_digest; mutable buffers through core.digest.
    monkeypatch.setattr(core,'digest',measured);monkeypatch.setattr(formats,'digest',measured)
    p.blob=path.read_bytes()
    assert formats.baseline_digest(p)==original(p.blob)
    assert formats.baseline_digest(p)==original(p.blob) and len(calls)==1
    # A fresh read rehashes even equal data; in-place file changes are never hidden.
    p.blob=path.read_bytes();assert formats.baseline_digest(p)==original(p.blob) and len(calls)==2
    path.write_bytes(b'changed baseline');p.blob=path.read_bytes()
    assert formats.baseline_digest(p)==original(b'changed baseline') and len(calls)==3
    p.blob=bytearray(b'mutable');first=formats.baseline_digest(p);p.blob[0]=77
    assert formats.baseline_digest(p)!=first and len(calls)==5
