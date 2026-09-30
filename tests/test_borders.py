"""PROD-VIS-001: stock-compatible path rims and tall-grass border pieces.

The rules are checked against the pinned stock maps themselves: Route 29's own
tall-grass patches and its L-shaped road are rebuilt from their tile sets.
"""
from pathlib import Path

import numpy as np
import pytest

from sovereign_editor import borders, nitro, mapscene
from sovereign_editor.formats import EditorError, map_data

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / 'projects/scyther-orchestration-1/baseline.nds'


# ---- pure rules ------------------------------------------------------------------

def kinds(pieces):
    return {(p['material'], p['half'] if 'half' in p else p['tile']) for p in pieces}


def test_single_grass_tile_gets_a_full_ring():
    interior, ring = borders.grass_pieces({(10, 10)})
    assert [p['tile'] for p in interior] == [(10, 10)]
    by = {p['half']: p['material'] for p in ring}
    assert len(by) == 12                                    # 4 corners + 8 edge halves
    assert by[(19, 19)] == by[(22, 22)] == 'egrass_ro'
    assert by[(20, 19)] == by[(21, 22)] == 'egrass_u' and by[(19, 20)] == by[(22, 21)] == 'egrass_v'


def test_concave_grass_corner_uses_inner_piece():
    _, ring = borders.grass_pieces({(0, 0), (1, 0), (0, 1)})
    by = {p['half']: p['material'] for p in ring}
    assert by[(2, 2)] == 'egrass_ri'


def test_grass_uv_quadrants_follow_stock_parity():
    _, ring = borders.grass_pieces({(0, 0)})
    by = {p['half']: p for p in ring}
    ro = by[(-1, -1)]                                       # odd/odd half-cell: (0..0.5, 0..0.5)
    assert np.allclose(ro['uv'], [[0, 0], [0.5, 0], [0.5, 0.5], [0, 0.5]])
    top = by[(0, -1)]                                       # strip above grass uses v 0..0.5
    assert np.allclose([v for _, v in top['uv']], [0, 0, 0.5, 0.5])


def test_path_ring_kinds_for_thin_and_isolated_paths():
    ring = borders.rim_pieces({(5, 5)})
    kinds_ = {p['tile']: (p['kind'], p['orientation']) for p in ring}
    assert kinds_[(5, 4)] == ('straight', 'S') and kinds_[(6, 5)] == ('straight', 'W')
    assert kinds_[(4, 4)] == ('outer', 'SE') and kinds_[(6, 6)] == ('outer', 'NW')
    thin = borders.rim_pieces({(x, 3) for x in range(10)})
    assert all(p['kind'] == 'straight' for p in thin if 0 <= p['tile'][0] < 10)


def test_path_rim_refuses_one_tile_gaps():
    with pytest.raises(EditorError, match='between two path'):
        borders.rim_pieces({(0, 0), (0, 2)})
    with pytest.raises(EditorError, match='between two path'):
        borders.rim_pieces({(0, 0), (1, 0), (2, 0), (0, 1), (2, 1)})   # notch tile (1,1)


def test_rim_uv_frames_match_stock_examples():
    # Stock Route 29 map 2: inner corner at (25,27) with road W and S; outer at (23,29).
    inner = borders.rim_uv('inner', 'SW', 0, 0)
    assert np.allclose(inner([1, 1]), [0.51, 0.49], atol=1e-6) and np.allclose(inner([0, 0]), [0.99, 0.01], atol=1e-6)
    outer = borders.rim_uv('outer', 'NE', 0, 0)
    assert np.allclose(outer([0, 0]), [0.51, 0.99], atol=1e-6) and np.allclose(outer([1, 0]), [0.51, 0.51], atol=1e-6)
    straight = borders.rim_uv('straight', 'N', 31, 29)
    u, v = straight([1, 1])
    assert abs(u - 0.01) < 1e-6 and abs(v % 1.0) < 1e-6


# ---- the rules reproduce stock geometry ---------------------------------------------

@pytest.fixture(scope='module')
def route29():
    if not BASELINE.exists():
        pytest.skip('Pinned baseline is local and untracked')
    from sovereign_editor.core import Project
    p = Project.create(BASELINE, Path(__import__('tempfile').mkdtemp()) / 'route29')
    out = {}
    for cell in ([18, 12], [19, 12], [20, 12]):
        ctx = p.context(header=33, cell=cell)
        raw = map_data(p.member_raw(ctx['map_member']))[2]
        out[tuple(cell)] = nitro.decode_model(raw, tileset=mapscene.tilesets(p, ctx)[1]['map_tileset'])[1]
    return out


def stock_family(prims, names):
    return {p.material['texture_name']: p for p in prims if p.material['texture_name'] in names}


def test_grass_rule_reproduces_route29_pieces(route29):
    matched = total = 0
    for prims in route29.values():
        fam = stock_family(prims, borders.GRASS_FAMILY)
        grass, stock = borders.stock_grass(fam)
        _, ring = borders.grass_pieces(grass)
        mine = {p['half']: (p['material'], tuple(np.round(p['uv'][0], 2) % 1.0)) for p in ring
                if 1 <= p['half'][0] < 63 and 1 <= p['half'][1] < 63}
        for half, value in stock.items():
            if not (1 <= half[0] < 63 and 1 <= half[1] < 63):
                continue
            total += 1
            matched += mine.get(half, (None,))[0] == value[0]
    assert total > 150 and matched / total >= 0.97


def test_path_rim_rule_reproduces_route29_road(route29):
    fam = stock_family(route29[(19, 12)], ('road01', 'road01_r'))
    region, stock = borders.stock_rims(fam['road01'], fam['road01_r'])
    ring = {p['tile']: p for p in borders.rim_pieces(region, allow_unsupported=True) if p['kind'] != 'unsupported'}
    def same(t, kind, uv):
        # Straight pieces run v continuously along the edge with a free offset.
        mine = np.array(borders.rim_uv(kind, ring[t]['orientation'], *t)([0.5, 0.5])) % 1.0
        keep = slice(0, 1) if kind == 'straight' else slice(0, 2)
        return np.allclose(mine[keep], (np.array(uv) % 1.0)[keep], atol=0.02)
    agree = [t for t, (kind, uv) in stock.items() if t in ring and ring[t]['kind'] == kind and same(t, kind, uv)]
    assert len(stock) >= 10 and len(agree) == len(stock)
