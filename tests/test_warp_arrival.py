"""R82-WARP-01 arrival rule: resident routines and overlay-1 calls. Software checks only."""
import struct
from pathlib import Path

import pytest

from sovereign_editor import character_runtime as cr, warp_arrival as wa, world_authoring
from sovereign_editor.core import Project

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / 'projects/editor-completion-v1'
pytestmark = pytest.mark.skipif(not PARENT.is_dir(), reason='r82 parent absent')


def test_arrival_calls_reach_resident_routines_and_keep_stock_bytes():
    p = Project(PARENT)
    plan = world_authoring.runtime_plan(p, p.composed())
    info = plan['warp_arrival']
    ov1 = plan['files'][wa.OVERLAY_FILE]
    _, stock = cr.file_by_id(p.blob, wa.OVERLAY_FILE)
    for site, target in zip(info['calls'], (info['facing'], info['step'])):
        at = site - wa.OVERLAY_BASE
        assert ov1[at:at + 4] == cr.thumb_bl(site, target)
    changed = {i for i in range(len(stock)) if ov1[i] != stock[i]}
    sites = {s - wa.OVERLAY_BASE + k for s in info['calls'] for k in range(4)}
    assert changed - sites == {0x21f92fc - wa.OVERLAY_BASE, 0x21f92fd - wa.OVERLAY_BASE,
                               0x21fa280 - wa.OVERLAY_BASE, 0x21fa281 - wa.OVERLAY_BASE}  # earlier features
    from sovereign_editor import resident as res
    ov129 = plan['files'][cr.overlay(p.blob, 129)['file_id']]
    boot = plan['appends'][res.BOOT_ARCHIVE][0]
    for name, routine in (('facing', wa.facing_routine), ('step', wa.step_routine)):
        address = info[name]
        body = routine(address)
        # Resident code lives in overlay 129, or in the boot data region once overlay 129 is full.
        image, base = (boot, res.BOOT_REGION[0]) if res.BOOT_REGION[0] <= address < res.BOOT_REGION[1] \
            else (ov129, 0x023D8000)
        assert image[address - base:address - base + len(body)] == body
    # The opening table: 0x6C east, 0x6D west, 0x6E north, 0x6F south (facing 3, 2, 0, 1).
    assert [(wa.OPENINGS >> (8 * i)) & 0xFF for i in range(4)] == [3, 2, 0, 1]
