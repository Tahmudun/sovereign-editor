"""PROD-VIS-001 review tool: reachability follows one-way ledges and reports Surf access."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import area_review as ar  # noqa: E402

GROUND, SEA, EAST, WEST, SOUTH = 0x00, 0x15, 0x38, 0x39, 0x3B


def grid(rows):
    """'.' ground, '#' blocked ground, '~' sea, '>' '<' 'v' ledges (stock collision set)."""
    codes = {'.': (GROUND, False), '#': (GROUND, True), '~': (SEA, False),
             '>': (EAST, True), '<': (WEST, True), 'v': (SOUTH, True)}
    behaviour = np.array([[codes[c][0] for c in r] for r in rows])
    blocked = np.array([[codes[c][1] for c in r] for r in rows])
    return behaviour, blocked


def test_ledges_are_jumped_one_way_only():
    behaviour, blocked = grid(['..>..',
                               '#####'])
    east = ar.reachable(behaviour, blocked, (0, 0))
    assert east[0].tolist() == [True, True, False, True, True]
    west = ar.reachable(behaviour, blocked, (4, 0))
    assert west[0].tolist() == [False, False, False, True, True]


def test_south_ledge_lands_two_tiles_down():
    behaviour, blocked = grid(['..',
                               'v#',
                               '..'])
    assert ar.reachable(behaviour, blocked, (0, 0))[2].tolist() == [True, True]
    assert not ar.reachable(behaviour, blocked, (0, 2))[0].any()


def test_sea_is_not_walked_but_counts_with_surf():
    behaviour, blocked = grid(['..~~~',
                               '#####'])
    walk = ar.reachable(behaviour, blocked, (0, 0))
    assert walk[0].tolist() == [True, True, False, False, False]
    surf = ar.reachable(behaviour, blocked, (0, 0), surf=True)
    assert surf[0].all()


def test_oblique_view_shows_a_south_facing_wall_the_top_down_view_omits():
    from sovereign_editor.nitro import Primitive
    wall = Primitive(vertices=np.array([[0., 0, 32], [32, 0, 32], [32, 32, 32], [0, 32, 32]]),
                     colors=np.full((4, 3), 255.), uvs=np.zeros((4, 2)), triangles=np.array([[0, 1, 2], [0, 2, 3]]),
                     material={'alpha': 1, 'diffuse': (31, 31, 31), 'emission': (0, 0, 0), 'ambient': (31, 31, 31)})
    top = np.asarray(ar.oblique([wall], (0, 0, 4, 4), pitch=90))
    tilted = np.asarray(ar.oblique([wall], (0, 0, 4, 4), pitch=40))
    background = top[0, 0]
    assert (top == background).all()
    assert (tilted != background).any(axis=-1).sum() > 100
