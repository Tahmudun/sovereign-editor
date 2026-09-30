"""Airborne petals (EFFECT-01): operations, runtime bytes, CPU-emulated routines, export readback.

Software checks only; native layering, timing and transitions need the user's melonDS test."""
import struct
from pathlib import Path

import pytest

from sovereign_editor import character_runtime as cr, petal_effect as pe, resident, world, world_authoring
from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError, arm9_code, member_count, resource

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / 'projects/editor-completion-v1'
pytestmark = pytest.mark.skipif(not PARENT.is_dir(), reason='r82 parent absent')
CANOPY, CHERRYGROVE, DARK = 542, 67, 108
CONTEXT = {'header': 542, 'cell': [2, 0]}


def edit(p, *requests):
    return p.apply_area_edit(p.doc['revision'], operations=[{'kind': 'petals', 'context': CONTEXT, 'request': r}
                                                           for r in requests])


@pytest.fixture(scope='module')
def project(tmp_path_factory):
    p = Project(PARENT).clone(tmp_path_factory.mktemp('petals') / 'p')
    edit(p, {'action': 'set', 'header': CANOPY, 'density': 6, 'direction': -30},
         {'action': 'set', 'header': CHERRYGROVE})
    return p


def test_operations_refusals_view_and_undo(tmp_path):
    p = Project(PARENT).clone(tmp_path / 'p')
    before = p.path.read_bytes()
    for request, message in (({'action': 'set', 'header': CANOPY, 'density': 0}, 'density'),
                             ({'action': 'set', 'header': CANOPY, 'direction': 75}, 'direction'),
                             ({'action': 'set', 'header': CANOPY, 'speed': 2.5}, 'speed'),
                             ({'action': 'set', 'header': CANOPY, 'wind': 3}, 'fields'),
                             ({'action': 'set', 'header': DARK}, 'lighting'),
                             ({'action': 'set', 'header': 9999}, 'header'),
                             ({'action': 'remove', 'header': CANOPY}, 'no petals')):
        with pytest.raises(EditorError, match=message):
            edit(p, request)
    assert p.path.read_bytes() == before
    edit(p, {'action': 'set', 'header': CANOPY})
    with pytest.raises(EditorError, match='unchanged'):
        edit(p, {'action': 'set', 'header': CANOPY, 'density': pe.DEFAULT['density']})
    edit(p, {'action': 'set', 'header': CANOPY, 'speed': 7})
    view = p.petal_view()
    assert [(a['header'], a['density'], a['speed'], a['direction'], a['weather_replaced']) for a in view['areas']] == \
        [(CANOPY, 4, 7, -20, 1)]
    assert view['capacity']['areas'] == [1, pe.MAX_AREAS] and view['lighting_refused'] == [11, 12, 13]
    assert Project(p.root).petal_view() == view
    p.undo(p.doc['revision'])
    assert pe.enabled(p.composed()) == {CANOPY: {**pe.DEFAULT}}
    edit(p, {'action': 'remove', 'header': CANOPY})
    assert not pe.areas(p.composed())
    summaries = [s for s in p.diff() if s.get('operation') == 'effect.petals']
    assert [s['action'] for s in summaries] == ['set', 'remove']


def test_derived_runtime_numbers():
    d = pe.derived({'density': 4, 'speed': 4, 'direction': 0})
    assert (d['vx'], d['vy'], d['life'], d['spin_frames'], d['on_screen']) == (0, round(0.85 * 4096), 236, 12, 16)
    left = pe.derived({'density': 4, 'speed': 4, 'direction': -30})
    right = pe.derived({'density': 4, 'speed': 4, 'direction': 30})
    assert left['vx'] == -right['vx'] < 0 and left['vy'] == right['vy'] > 0
    dense = pe.derived({'density': 10, 'speed': 10, 'direction': -60})
    assert dense['on_screen'] == 40 < pe.POOL and dense['life'] < 65536 and dense['life_jitter'] <= 255
    slow = pe.derived({'density': 1, 'speed': 1, 'direction': 60})
    assert slow['rate'] >= 1 and slow['life'] == 1000


def test_runtime_plan_bytes(project):
    blob, state = project.blob, project.composed()
    plan = world_authoring.runtime_plan(project, state)
    info = plan['petals']
    image = plan['appends'][resident.BOOT_ARCHIVE][0]

    def boot(address, size):
        return image[address - resident.BOOT_REGION[0]:address - resident.BOOT_REGION[0] + size]

    raw = pe._overlay(blob)
    ov1 = plan['files'][pe.OVERLAY_FILE]
    for address, stock, value in pe.HALFWORDS:
        assert struct.unpack_from('<H', ov1, address - pe.OVERLAY_BASE)[0] == value != stock
    assert struct.unpack_from('<I', ov1, pe.TABLE_LITERAL - pe.OVERLAY_BASE)[0] == info['table']
    at = pe.LOADER_CALL - pe.OVERLAY_BASE
    assert ov1[at:at + 4] == cr.thumb_bl(pe.LOADER_CALL, info['loader'])
    # Only the petal sites differ from the same plan without petals (other features edit overlay 1 too).
    import copy
    bare = copy.deepcopy(state)
    bare['effects'] = {}
    other = world_authoring.runtime_plan(project, bare)['files'].get(pe.OVERLAY_FILE, raw)
    sites = {a - pe.OVERLAY_BASE + k for a, _, _ in pe.HALFWORDS for k in range(2)}
    sites |= {pe.TABLE_LITERAL - pe.OVERLAY_BASE + k for k in range(4)} | {at + k for k in range(4)}
    assert {i for i in range(len(raw)) if other[i] != ov1[i]} <= sites
    # Weather table: 14 stock rows then weather 14; code and records at their addresses.
    table = boot(info['table'], 15 * pe.ROW)
    assert table[:14 * pe.ROW] == pe._at(raw, pe.STOCK_TABLE, 14 * pe.ROW)
    assert table[14 * pe.ROW:] == pe.weather_row(info['task'])
    assert boot(info['task'], pe.SNOW_END - pe.SNOW_TASK) == pe.relocated_task(raw, info['task'], info['spawn'],
                                                                              info['motion'])
    assert boot(info['spawn'], len(pe.spawn_routine(0, 0))) == pe.spawn_routine(info['spawn'], info['lookup'])
    assert boot(info['motion'], len(pe.motion_routine(0))) == pe.motion_routine(info['motion'])
    assert boot(info['lookup'], len(pe.lookup_routine(0, 0))) == pe.lookup_routine(info['lookup'], info['records'])
    assert boot(info['loader'], len(pe.loader_routine(0, 0, 0))) == pe.loader_routine(info['loader'], info['table'],
                                                                                       info['row'])
    assert boot(info['row'], pe.ROW) == pe.weather_row(info['task'])
    records = boot(info['records'], 4 + 16 * 3)
    assert struct.unpack_from('<I', records)[0] == 2
    maps = [pe.PARAMS.unpack_from(records, 4 + 16 * i)[0] for i in range(3)]
    assert maps == [0xFFFF, CHERRYGROVE, CANOPY]
    canopy = pe.PARAMS.unpack_from(records, 4 + 32)
    d = pe.derived({'density': 6, 'speed': 4, 'direction': -30})
    assert canopy == (CANOPY, d['rate'], d['vx'], d['vy'], d['life'], d['life_jitter'], d['spin_frames'])
    # Header weather: the stock header through the ARM9 table, the created one through its record.
    start = struct.unpack_from('<I', blob, 0x20)[0]
    offset = start + world.HEADER_TABLE + CHERRYGROVE * world.HEADER_SIZE + 20
    hit = [x for x in plan['patches'] if x['rom_offset'] == offset]
    assert len(hit) == 1 and (struct.unpack('<I', bytes.fromhex(hit[0]['after']))[0] >> 1) & 0x7F == pe.WEATHER
    assert (struct.unpack('<I', bytes.fromhex(hit[0]['before']))[0] >> 1) & 0x7F == 0
    record = pe.header_raw(state, CANOPY, project._world_headers[CANOPY]['raw'])
    assert (struct.unpack_from('<I', record, 20)[0] >> 1) & 0x7F == pe.WEATHER
    assert record[:20] + record[24:] == project._world_headers[CANOPY]['raw'][:20] + project._world_headers[CANOPY]['raw'][24:]
    # a/0/6/3: petal tiles and palette appended; set 10 in all four resource lists.
    char, pltt = plan['appends'][pe.ARCHIVE]
    tiles, colors = pe.sprite()
    assert char[48:] == tiles and char[:48] == resource(blob, pe.ARCHIVE, 20)[1][:48]
    assert struct.unpack_from('<16H', pltt, 40)[1:5] == colors[1:5] and pltt[:40] == resource(blob, pe.ARCHIVE, 21)[1][:40]
    lists = plan['replacements'][pe.ARCHIVE]
    for kind, member in pe.LISTS.items():
        new, old = lists[member], resource(blob, pe.ARCHIVE, member)[1]
        assert new[:-2 * pe.ENTRY.size] == old[:-pe.ENTRY.size] and new[-pe.ENTRY.size:] == pe.TERMINATOR
        entry = pe.ENTRY.unpack_from(new, len(new) - 2 * pe.ENTRY.size)
        snow = pe.ENTRY.unpack_from(old, 4 + pe.ENTRY.size * pe.SNOW_SET)
        want = {'char': 59, 'pltt': 60, 'cell': 19, 'anim': 18}[kind]
        assert entry == (snow[0], want, snow[2], 1010, snow[4], snow[5])


# ---- CPU emulation -----------------------------------------------------------------------------

class Machine:
    def __init__(self, arm, ov1, boot):
        from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE
        self.u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.u.mem_map(0x02000000, 0x400000)
        self.u.mem_write(0x02000000, bytes(arm))
        self.u.mem_write(pe.OVERLAY_BASE, bytes(ov1))
        self.u.mem_write(resident.BOOT_REGION[0], bytes(boot))
        self.stubs, self.calls = {}, []
        self.u.hook_add(UC_HOOK_CODE, self._code)

    def _code(self, u, address, size, _):
        from unicorn.arm_const import UC_ARM_REG_PC, UC_ARM_REG_LR
        if address in self.stubs:
            self.calls.append(address)
            self.stubs[address](self)
            u.reg_write(UC_ARM_REG_PC, u.reg_read(UC_ARM_REG_LR))

    def reg(self, name, value=None):
        from unicorn import arm_const
        r = getattr(arm_const, 'UC_ARM_REG_' + name.upper())
        if value is None:
            return self.u.reg_read(r)
        self.u.reg_write(r, value & 0xFFFFFFFF)

    def word(self, address, value=None):
        if value is None:
            return struct.unpack('<i', self.u.mem_read(address, 4))[0]
        self.u.mem_write(address, struct.pack('<i', value))

    def call(self, entry, *args):
        STOP = 0x02001000
        for i, a in enumerate(args):
            self.reg(f'r{i}', a)
        self.reg('sp', 0x023A0000)
        self.reg('lr', STOP | 1)
        self.u.emu_start(entry | 1, STOP, count=200000)
        return self.reg('r0')


@pytest.fixture(scope='module')
def cpu(project):
    import sys
    tools = str(ROOT / 'work/tiana-fixes-1/python-tools')      # the CPU harnesses' unicorn (tools/world_qualification.py)
    if tools not in sys.path:
        sys.path.append(tools)
    pytest.importorskip('unicorn')
    plan = world_authoring.runtime_plan(project, project.composed())
    return plan, lambda: Machine(arm9_code(project.blob), plan['files'][pe.OVERLAY_FILE],
                                 plan['appends'][resident.BOOT_ARCHIVE][0])


STATE, LOADER, FIELD, LOCATION, WORK, PARTICLE, PWORK, SPRITE = (0x02300000, 0x02301000, 0x02302000, 0x02303000,
                                                                0x02304000, 0x02305000, 0x02306000, 0x02307000)


def world_with_map(m, map_id):
    m.word(STATE, LOADER)
    m.word(LOADER + pe.FIELD_SYSTEM, FIELD)
    m.word(FIELD + 0x20, LOCATION)
    m.word(LOCATION, map_id)
    m.word(STATE + pe.WORK_OFFSET, WORK)


def test_lookup_finds_the_current_map_or_the_default(cpu):
    plan, machine = cpu
    info = plan['petals']
    for map_id, index in ((CANOPY, 2), (CHERRYGROVE, 1), (29, 0)):
        m = machine()
        world_with_map(m, map_id)
        assert pe._bl_target(info['spawn'] + 8, bytes(m.u.mem_read(info['spawn'] + 8, 4))) == info['lookup']
        assert m.call(info['lookup'], STATE) == info['records'] + 4 + 16 * index


def test_spawn_and_motion_follow_the_area_record(cpu):
    plan, machine = cpu
    info = plan['petals']
    m = machine()
    world_with_map(m, CANOPY)
    rands = iter(range(1000, 2000, 7))
    positions, frames, allocs = [], [], []

    def alloc(m):
        allocs.append(1)
        m.word(PARTICLE + 4, SPRITE)
        m.word(PARTICLE + 8, PWORK)
        m.reg('r0', PARTICLE if len(allocs) <= 3 else 0)

    def rand(m):
        m.reg('r0', next(rands))

    def udiv(m):
        n, d = m.reg('r0'), m.reg('r1')
        m.reg('r0', n // d)
        m.reg('r1', n % d)

    def set_pos(m):
        positions.append(tuple(struct.unpack('<3i', m.u.mem_read(m.reg('r1'), 12))))

    def set_frame(m):
        frames.append(m.reg('r1'))
    m.stubs = {pe.ALLOC: alloc, pe.MTRANDOM: rand, pe.UDIV: udiv, pe.SET_POS: set_pos, pe.SET_FRAME: set_frame}
    record = pe.PARAMS.unpack_from(bytes(m.u.mem_read(info['records'] + 4 + 32, 16)))
    _, rate, vx, vy, life, jitter, spin = record
    # One spawner tick of 6 count units: rate * 6 / 1024 petals, remainder kept.
    m.word(WORK + pe.ACC, 1000)
    m.call(info['spawn'], STATE, 6)
    total = 1000 + rate * 6
    assert len(allocs) == min(3, total >> 10) + (1 if (total >> 10) > 3 else 0)
    assert m.word(WORK + pe.ACC) == total & 0x3FF
    # The last particle initialised: speed factor, lifetime, spin, frame and start position.
    work = [m.word(PWORK + 4 * i) for i in range(8)]
    age, life_, pvy, state, pvx, countdown, frame, period = work
    assert age == 0 and state == 0 and life <= life_ <= life + jitter
    assert 12 * vy >> 4 <= pvy <= 20 * vy >> 4 and (pvx < 0) == (vx < 0)
    assert spin <= period <= spin + 7 and countdown == period and 0 <= frame <= 3 and frames[-1] == frame
    x, y, z = positions[-1]
    assert -64 << 12 <= x < 256 << 12 and y == -8 << 12 and z == 0
    # Motion: one frame moves by the particle's own drift; the spin advances on its countdown.
    m2 = machine()
    moved, spun, removed = [], [], []
    m2.stubs = {pe.GET_POS: lambda m: m.u.mem_write(m.reg('r0'), struct.pack('<3i', 100 << 12, 50 << 12, 0)),
                pe.SET_POS: lambda m: moved.append(struct.unpack('<3i', m.u.mem_read(m.reg('r1'), 12))),
                pe.SET_FRAME: lambda m: spun.append(m.reg('r1')),
                pe.REMOVE: lambda m: removed.append(m.reg('r0'))}
    m2.word(PARTICLE + 4, SPRITE)
    m2.word(PARTICLE + 8, PWORK)
    for i, v in enumerate((10, 20, 3000, 0, -1200, 1, 3, 9)):
        m2.word(PWORK + 4 * i, v)
    m2.call(info['motion'], PARTICLE)
    assert moved == [((100 << 12) - 1200, (50 << 12) + 3000, 0)] and spun == [0]
    assert [m2.word(PWORK + 4 * i) for i in (0, 3, 5, 6)] == [11, 0, 9, 0]
    m2.word(PWORK, 20)                                    # past its life: marked, removed next frame
    m2.word(PWORK + 20, 5)
    m2.call(info['motion'], PARTICLE)
    assert m2.word(PWORK + 12) == 1 and not removed
    m2.call(info['motion'], PARTICLE)
    assert removed == [PARTICLE]


def test_loader_refreshes_the_table_from_the_stock_rows(cpu, project):
    plan, machine = cpu
    info = plan['petals']
    m = machine()
    m.u.mem_write(info['table'], b'\xAA' * 15 * pe.ROW)     # stale runtime values from an earlier field
    m.stubs = {pe.LOADER: lambda m: m.reg('r0', 0x12345678)}
    assert m.call(info['loader'], FIELD) == 0x12345678
    table = bytes(m.u.mem_read(info['table'], 15 * pe.ROW))
    assert table[:14 * pe.ROW] == pe._at(pe._overlay(project.blob), pe.STOCK_TABLE, 14 * pe.ROW)
    assert table[14 * pe.ROW:] == pe.weather_row(info['task'])


# ---- export ------------------------------------------------------------------------------------

def test_export_reads_back_and_undo_restores(project, tmp_path):
    from sovereign_editor import world_readback
    out = tmp_path / 'e'
    project.export(out, project.doc['revision'])
    rom = (out / 'game.nds').read_bytes()
    try:
        assert member_count(rom, pe.ARCHIVE) == 61
        tiles, _ = pe.sprite()
        assert resource(rom, pe.ARCHIVE, 59)[1][48:] == tiles
        for member in pe.LISTS.values():
            assert len(resource(rom, pe.ARCHIVE, member)[1]) == 4 + pe.ENTRY.size * 12
        head = world.read_header(rom, CHERRYGROVE, arm9_code(rom))
        assert head['weather'] == pe.WEATHER
        report = world_readback.check(rom, project)
        assert report['failed'] == 0, [c for c in report['checks'] if not c['pass']][:3]
        _, ov1 = cr.file_by_id(rom, pe.OVERLAY_FILE)
        assert struct.unpack_from('<H', ov1, 0x021EB270 - pe.OVERLAY_BASE)[0] == 0x2C0F
        image = resident.boot_image(rom)
        assert image is not None and len(image) >= 15 * pe.ROW
    finally:
        (out / 'game.nds').unlink()


# ---- editor UI ---------------------------------------------------------------------------------

def test_world_editor_effects_tab_previews_and_applies(tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.map_inspector import MapInspectorWindow
    from sovereign_editor.world_ui import WorldEditor
    p = Project(PARENT).clone(tmp_path / 'p')
    app = QApplication.instance() or QApplication([])
    w = MapInspectorWindow(p, context=(542, [0, 0])); w.show(); app.processEvents()
    d = WorldEditor(w); d.tabs.setCurrentIndex(d.tabs.indexOf(d.effect_list.parentWidget())); d.show()
    app.processEvents()
    assert d.effect_header.value() == CANOPY and 'areas 0/64' in d.effect_list.toPlainText()
    before = p.path.read_bytes()
    d.effect_header.setValue(DARK)
    d.stage_effect(); app.processEvents()
    assert 'UNSUPPORTED_WEATHER' in d.status.text() and not d.apply_button.isEnabled()
    d.effect_header.setValue(CANOPY)
    d.effect_fields['density'].setValue(7); d.effect_fields['direction'].setValue(-35)
    d.stage_effect(); app.processEvents()
    assert d.apply_button.isEnabled() and 'petals on screen' in d.summary.toPlainText()
    assert p.path.read_bytes() == before
    d.apply(); app.processEvents()
    assert pe.enabled(p.composed()) == {CANOPY: {'density': 7, 'speed': 4, 'direction': -35}}
    assert 'density 7 speed 4 direction -35' in d.effect_list.toPlainText()
    d.grab().save(str(ROOT / 'work/original-content-v1/impl/effects-tab.png'))
    d.close(); w.close(); app.processEvents()
