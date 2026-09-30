"""PROD-PERF-001: caches that keep whole-state validation fast must stay exact."""
from sovereign_editor import formats


def test_immutable_rom_digest_is_computed_once_per_object(monkeypatch):
    calls = []
    real = formats.digest
    monkeypatch.setattr(formats, 'digest', lambda blob: calls.append(len(blob)) or real(blob))
    rom = bytes(range(256)) * 64
    assert formats.immutable_digest(rom) == real(rom)
    assert formats.immutable_digest(rom) == real(rom)
    assert len(calls) == 1
    copy = rom[:-1] + b'\x00'                 # a different ROM object is hashed on its own
    assert formats.immutable_digest(copy) == real(copy) != real(rom)


def test_mutable_images_are_always_rehashed(monkeypatch):
    calls = []
    real = formats.digest
    monkeypatch.setattr(formats, 'digest', lambda blob: calls.append(1) or real(blob))
    image = bytearray(b'abc' * 100)
    first = formats.immutable_digest(image)
    image[0] = 0
    assert formats.immutable_digest(image) == real(bytes(image)) != first
    assert len(calls) == 2


def test_context_copies_never_share_nested_state(tmp_path):
    from pathlib import Path
    import pytest
    from sovereign_editor.core import Project
    baseline = Path(__file__).resolve().parents[1] / 'projects/scyther-orchestration-1/baseline.nds'
    if not baseline.exists():
        pytest.skip('Pinned baseline is local and untracked')
    p = Project.create(baseline, tmp_path / 'project')
    first = p.context(header=33, cell=[19, 12])
    first['origin'][0] += 1
    first['cell']['x'] = -1
    first['sections'].clear()
    second = p.context(header=33, cell=[19, 12])
    assert second['origin'][0] == 608 and second['cell']['x'] == 19 and second['sections']


def _reference_mapping(prims, x, z):
    """The original per-triangle sampler (kept here as the equivalence oracle)."""
    import numpy as np
    point = [((x + .5) * 16 - 256), ((z + .5) * 16 - 256), 1]
    candidates = []
    for p in prims:
        for tri in p.triangles:
            v = p.vertices[tri]
            a = np.column_stack([v[:, 0], v[:, 2], np.ones(3)])
            if abs(np.linalg.det(a)) < 1e-6:
                continue
            weights = np.linalg.solve(a.T, point)
            if np.min(weights) >= -1e-6:
                candidates.append((float(weights @ v[:, 1]), float(min(weights)), p, tri, a))
    if not candidates:
        return 'none'
    height = max(c[0] for c in candidates)
    top = [c for c in candidates if abs(c[0] - height) < .01]
    if len({c[2].material['name'] for c in top}) != 1:
        return 'ambiguous'
    _, _, p, tri, a = max(top, key=lambda c: c[1])
    if not (np.ptp(p.vertices[tri, 1]) < 1e-4 and all(p.material['repeat'])):
        return 'not flat'
    return {'material': p.material['name'], 'height': height,
            'affine': np.linalg.solve(a, p.uvs[tri]).round(12).reshape(-1).tolist()}


def test_vectorized_mapping_sampler_matches_the_per_triangle_reference(tmp_path):
    from pathlib import Path
    import pytest
    from sovereign_editor import mapscene, nitro, surface_native
    from sovereign_editor.core import Project
    from sovereign_editor.formats import EditorError
    baseline = Path(__file__).resolve().parents[1] / 'projects/scyther-orchestration-1/baseline.nds'
    if not baseline.exists():
        pytest.skip('Pinned baseline is local and untracked')
    p = Project.create(baseline, tmp_path / 'project')
    for header, cell in ((33, [19, 12]), (23, [40, 13])):
        ctx = p.context(header=header, cell=cell)
        from sovereign_editor import surface_authoring
        raw = surface_authoring.model(p, ctx, p.composed())
        tileset = mapscene.tilesets(p, ctx)[1]['map_tileset']
        _, prims = nitro.decode_model(raw, tileset=tileset)
        sample = surface_native.mapping_sampler(raw, tileset)
        for z in range(0, 32, 9):
            for x in range(32):
                try:
                    got = sample(x, z)
                except EditorError as exc:
                    got = {'No floor at this tile': 'none', 'Ambiguous overlapping surfaces': 'ambiguous',
                           'Sample a flat repeating floor': 'not flat'}[str(exc)]
                assert got == _reference_mapping(prims, x, z), (header, x, z)


def test_project_json_is_one_compact_line_per_top_level_key(tmp_path):
    import json
    from sovereign_editor.core import atomic_json
    value = {'schema': 's', 'revision': 3, 'history': [{'map_edits': [{'a': [1, 2]}]}], 'map_edits': [], 'name': 'x "q"'}
    path = tmp_path / 'project.json'
    atomic_json(path, value)
    text = path.read_text()
    assert json.loads(text) == value
    lines = text.splitlines()
    assert lines[0] == '{' and lines[-1] == '}' and len(lines) == len(value) + 2
    assert lines[3] == '  "history": [{"map_edits": [{"a": [1, 2]}]}],'
