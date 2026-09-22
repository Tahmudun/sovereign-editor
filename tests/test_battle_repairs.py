"""BATTLE-STAT-002/003: guarded overlay repairs and exact-ROM controller regressions.

Controller cases execute the ROM's battle code under Unicorn (tools/battle_harness.py).
They are software evidence, not melonDS/native acceptance.
"""
from pathlib import Path
import sys

import pytest

from sovereign_editor import battle_safety as safety, character_runtime as cr
from sovereign_editor.formats import EditorError

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / 'projects/tiana-fixes-1/baseline.nds'
pytestmark = pytest.mark.skipif(not BASELINE.exists(), reason='pinned baseline ROM absent')


@pytest.fixture(scope='module')
def blob():
    return BASELINE.read_bytes()


def test_before_move_repair_redirects_only_the_four_capped_stat_exits(blob):
    raw = cr.overlay(blob, 142)['data']
    fixed = safety.before_move_overlay(raw)
    changed = [i for i, (a, b) in enumerate(zip(raw, fixed)) if a != b]
    assert len(fixed) == len(raw)
    sites = [s - safety.BEFORE_MOVE_ADDRESS for s in safety.BEFORE_MOVE_EXITS]
    assert set(changed) <= {s + i for s in sites for i in range(4)}
    for site in safety.BEFORE_MOVE_EXITS:
        at = site - safety.BEFORE_MOVE_ADDRESS
        assert fixed[at:at + 4] == cr.thumb_bl(site, safety.BEFORE_MOVE_END)
    with pytest.raises(EditorError, match='before-move'):
        safety.before_move_overlay(fixed)           # never patch twice or a changed build
    with pytest.raises(EditorError):
        safety.before_move_overlay(raw[:-2])


def test_stat_clamp_repair_changes_one_instruction(blob):
    raw = cr.overlay(blob, 137)['data']
    fixed = safety.stat_stage_overlay(raw)
    assert [i for i, (a, b) in enumerate(zip(raw, fixed)) if a != b] == [0x1f6, 0x1f7]
    with pytest.raises(EditorError):
        safety.stat_stage_overlay(fixed)


sys.path.insert(0, str(ROOT / 'tools'))
try:
    import battle_harness as harness
except ImportError:          # Unicorn lives in untracked work/tiana-fixes-1/python-tools
    harness = None


def repaired(blob):
    return {137: safety.stat_stage_overlay(cr.overlay(blob, 137)['data']),
            142: safety.before_move_overlay(cr.overlay(blob, 142)['data'])}


@pytest.mark.skipif(harness is None, reason='Unicorn harness unavailable')
@pytest.mark.parametrize('user,move_slot,stat', [(0, 1, 2), (1, 1, 1)])   # Leer vs Def -6; Growl vs Atk -6
def test_capped_stat_move_loops_on_baseline_and_finishes_when_repaired(blob, user, move_slot, stat):
    target = 1 - user
    states = [[6] * 8, [6] * 8]; states[target][stat] = 0
    slots = (move_slot, 0) if user == 0 else (0, move_slot)
    base = harness.single(blob, *slots, *states, frames=400)
    assert base['result']['result'] == 'frame_limit'
    assert base['message_ids'].count(harness.MSG_STAT_WONT_GO_LOWER) >= 4
    fixed = harness.single(blob, *slots, *states, overlay_data=repaired(blob))
    assert fixed['result'] == {'result': 'reached', 'command': 2, 'frames': fixed['result']['frames']}
    assert fixed['message_ids'].count(harness.MSG_STAT_WONT_GO_LOWER) == 1
    assert fixed['final_states'][target][stat] == 0
    assert fixed['hp'] != [20, 21]                                     # the other battler's Tackle ran
    assert ('free', 142) in fixed['overlay_events'] and ('load', 147) in fixed['overlay_events']


@pytest.mark.skipif(harness is None, reason='Unicorn harness unavailable')
def test_two_stage_drop_from_minus_five_reaches_minus_six_only_with_clamp_repair(blob):
    player = dict(harness.CYNDAQUIL, moves=[33, 103, 0, 0])            # Screech
    states = [6, 6, 1, 6, 6, 6, 6, 6]
    base = harness.single(blob, 1, 0, enemy_states=states, player=player)
    assert base['final_states'][1][2] == 2                             # pinned defect: -5 becomes -4
    fixed = harness.single(blob, 1, 0, enemy_states=states, player=player, overlay_data=repaired(blob))
    assert fixed['result']['result'] == 'reached' and fixed['final_states'][1][2] == 0


@pytest.mark.skipif(harness is None, reason='Unicorn harness unavailable')
def test_capped_boost_prints_one_limit_message_when_repaired(blob):
    player = dict(harness.CYNDAQUIL, moves=[33, 14, 0, 0])             # Swords Dance at +6
    fixed = harness.single(blob, 1, 0, player_states=[6, 12, 6, 6, 6, 6, 6, 6], player=player,
                           overlay_data=repaired(blob))
    assert fixed['result']['result'] == 'reached'
    assert fixed['message_ids'].count(harness.MSG_STAT_WONT_GO_HIGHER) == 1
    assert fixed['final_states'][0][1] == 12
