"""Custom Pokémon packages and form identities (POKE-01..04). Software, CPU and ROM readback only;
battle/summary/PC/follower/save behaviour needs the user's melonDS test."""
import struct
from pathlib import Path

import pytest

from sovereign_editor import pokemon_packages as pp, resident, species_forms as sf, world_authoring
from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError, arm9_code, member_count, resource

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / 'projects/editor-completion-v1'
DOG = ROOT / 'artifacts/eevee-dog-v6'
pytestmark = pytest.mark.skipif(not (PARENT.is_dir() and DOG.is_dir()), reason='r82 parent or dog package absent')
CTX = {'header': 542, 'cell': [2, 0]}
IMPORT = {'key': 'jacobs_eevee', 'source': str(DOG), 'display': "Jacob's Eevee", 'template': 133, 'icon_palette': 2}


def edit(p, *requests):
    return p.apply_area_edit(p.doc['revision'], operations=[{'kind': 'pokemon', 'context': CTX, 'request': r}
                                                           for r in requests])


def stock_package(blob, folder, species):
    """A scratch package made from a stock species' own records (generality fixture)."""
    native = folder / 'native'
    native.mkdir(parents=True)
    rec = pp.template_records(blob, species)
    for role, name in (('front', 'front.NCGR'), ('back', 'back.NCGR'), ('normal', 'normal.NCLR'),
                       ('shiny', 'shiny.NCLR'), ('icon', 'icon.NCGR'), ('follower', 'follower.btx0')):
        (native / name).write_bytes(rec[role])
    (folder / 'notes.md').write_text('scratch generality fixture; not delivered\n')
    return folder


@pytest.fixture(scope='module')
def project(tmp_path_factory):
    p = Project(PARENT).clone(tmp_path_factory.mktemp('poke') / 'p')
    edit(p, IMPORT)
    edit(p, {'action': 'bind', 'key': 'jacobs_dog', 'package': 'jacobs_eevee'})
    return p


def test_stage_refuses_malformed_packages(tmp_path):
    p = Project(PARENT)
    bad = tmp_path / 'bad'
    (bad / 'native').mkdir(parents=True)
    for name in ('front.NCGR', 'back.NCGR', 'normal.NCLR', 'shiny.NCLR', 'icon.NCGR'):
        (bad / 'native' / name).write_bytes((DOG / 'native' / name).read_bytes())
    with pytest.raises(EditorError, match='follower.btx0 is missing'):
        p.stage_pokemon_source(bad, 'bad', 'Bad', 133, 2)
    (bad / 'native/follower.btx0').write_bytes((DOG / 'native/follower.btx0').read_bytes()[:-8])
    with pytest.raises(EditorError, match='follower'):
        p.stage_pokemon_source(bad, 'bad', 'Bad', 133, 2)
    (bad / 'native/follower.btx0').write_bytes((DOG / 'native/follower.btx0').read_bytes())
    (bad / 'native/icon.NCGR').write_bytes(b'RGCN' + bytes(10))
    with pytest.raises(EditorError, match='icon'):
        p.stage_pokemon_source(bad, 'bad', 'Bad', 133, 2)
    for args, message in (((DOG, 'Bad Key', 'Dog', 133, 2), 'Package id'), ((DOG, 'dog', 'Dog', 133, 3), 'Icon palette'),
                          ((DOG, 'dog', 'Dog', 500, 2), 'Template'), ((DOG, 'dog', '', 133, 2), 'Display')):
        with pytest.raises(EditorError, match=message):
            p.stage_pokemon_source(*args)
    staged = p.stage_pokemon_source(DOG, 'jacobs_eevee', "Jacob's Eevee", 133, 2)
    assert staged['operation']['action'] == 'import' and staged['report']['roles'] == \
        ['back', 'follower', 'front', 'icon', 'normal', 'shiny']


def test_import_bind_allocation_refusals_reopen_and_undo(tmp_path):
    p = Project(PARENT).clone(tmp_path / 'p')
    edit(p, IMPORT)
    with pytest.raises(EditorError, match='NO_CHANGE|unchanged'):
        edit(p, {**IMPORT, 'action': 'revise'})
    edit(p, {'action': 'bind', 'key': 'jacobs_dog', 'package': 'jacobs_eevee'})
    dog = pp.identities(p.composed())['jacobs_dog']
    # Eevee's form 1 (partner) and 2 (battle-only) are occupied: the dog is form 3, personal 1490.
    assert (dog['species'], dog['form'], dog['personal'], dog['tag'], dog['evolution']) == (133, 3, 1490, 0x2400, 'none')
    assert pp.form_entry(p.blob, 133, 1) == 1302 and pp.form_entry(p.blob, 133, 2) & 0x8000
    for request, message in (({'action': 'bind', 'key': 'jacobs_dog', 'package': 'jacobs_eevee'}, 'permanent'),
                             ({'action': 'bind', 'key': 'other', 'package': 'nope'}, 'Import the package'),
                             ({'action': 'bind', 'key': 'other', 'package': 'jacobs_eevee', 'species': 25}, 'template'),
                             ({'action': 'bind', 'key': 'other', 'package': 'jacobs_eevee', 'evolution': 'x'},
                              'Evolution'),
                             ({'action': 'remove', 'key': 'jacobs_eevee'}, 'Identities use')):
        with pytest.raises(EditorError, match=message):
            edit(p, request)
    # The identity is accepted everywhere a species/form reference is qualified.
    row = sf.lookup(p, 133, 3)
    assert row['supported'] and row['kind'] == 'custom form' and row['personal_index'] == 1490
    assert not sf.lookup(p, 133, 4)['supported']
    assert any(e['key'] == '133:3' for e in sf.catalog(p, search="jacob")['entries'])
    view = p.gameplay_species(133, 3)
    assert view['inherited_from'] == 133 and view['personal'] == p.gameplay_species(133)['personal']
    # Reopen verifies the stored package; tampering refuses to open.
    reopened = Project(p.root)
    assert pp.identities(reopened.composed()) == pp.identities(p.composed())
    body = pp.verify_package(reopened, 'jacobs_eevee', pp.packages(reopened.composed())['jacobs_eevee']['package'])
    stored = pp.package_dir(p, 'jacobs_eevee', pp.packages(p.composed())['jacobs_eevee']['package'])
    assert (stored / 'source/battle-front.aseprite').is_file() and (stored / 'source/native/follower.btx0').is_file()
    assert body['icon_palette'] == 2 and body['template'] == 133
    front = stored / body['roles']['front']
    raw = front.read_bytes()
    front.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    with pytest.raises(EditorError, match='differs'):
        Project(p.root).composed()
    front.write_bytes(raw)
    p = Project(p.root)
    p.undo(p.doc['revision'])
    assert not pp.identities(p.composed())


def test_second_package_revision_and_generality(tmp_path):
    p = Project(PARENT).clone(tmp_path / 'p')
    pup = stock_package(p.blob, tmp_path / 'pup', 58)
    edit(p, {'key': 'scratch_pup', 'source': str(pup), 'display': 'Scratch Pup', 'template': 58, 'icon_palette': 0})
    edit(p, IMPORT)
    edit(p, {'action': 'bind', 'key': 'pup', 'package': 'scratch_pup', 'evolution': 'inherit'},
         {'action': 'bind', 'key': 'jacobs_dog', 'package': 'jacobs_eevee'})
    ids = pp.identities(p.composed())
    assert (ids['pup']['species'], ids['pup']['form'], ids['pup']['personal']) == (58, 2, 1490)  # Growlithe 1 = Hisuian
    assert (ids['jacobs_dog']['form'], ids['jacobs_dog']['personal']) == (3, 1491)
    # Revision: a changed palette replaces the art of every identity that uses the package.
    raw = bytearray((pup / 'native/normal.NCLR').read_bytes())
    raw[-2] ^= 0x1F
    (pup / 'native/normal.NCLR').write_bytes(bytes(raw))
    current = pp.packages(p.composed())['scratch_pup']
    edit(p, {'key': 'scratch_pup', 'source': str(pup), 'display': 'Scratch Pup', 'template': 58, 'icon_palette': 0,
             'action': 'revise'})
    after = pp.packages(p.composed())['scratch_pup']
    assert after['revision'] == current['revision'] + 1 and after['package'] != current['package']
    plan = world_authoring.runtime_plan(p, p.composed())
    assert plan['appends'][pp.PICTURES][4] == bytes(raw)
    assert plan['appends'][pp.EVOLUTION][-2] == resource(p.blob, pp.EVOLUTION, 58)[1]      # inherit
    assert plan['appends'][pp.EVOLUTION][-1] == bytes(pp.EVOLUTION_SIZE)                   # none


def test_runtime_records_and_table_extensions(project):
    blob, state = project.blob, project.composed()
    plan = world_authoring.runtime_plan(project, state)
    info = plan['pokemon']
    assert info['identities'][0]['personal'] == 1490 and info['identities'][0]['follower_gfx'] == \
        1553 + len(plan['appends'][pp.OVERWORLD]) - 1
    a, r = plan['appends'], plan['replacements']
    dog = DOG
    assert a[pp.PERSONAL] == [resource(blob, pp.PERSONAL, 133)[1]]
    pics = a[pp.PICTURES]
    front, back = (dog / 'native/front.NCGR').read_bytes(), (dog / 'native/back.NCGR').read_bytes()
    assert pics == [back, back, front, front, (dog / 'native/normal.NCLR').read_bytes(),
                    (dog / 'native/shiny.NCLR').read_bytes()]
    assert a[pp.HEIGHTS] == [resource(blob, pp.HEIGHTS, 4 * 133 + k)[1] for k in range(4)]
    assert a[pp.ICONS] == [(dog / 'native/icon.NCGR').read_bytes()]
    assert len(a[pp.EVOLUTION]) == 17 and a[pp.EVOLUTION][-1] == bytes(56)
    assert a[pp.OVERWORLD][-1] == (dog / 'native/follower.btx0').read_bytes()
    addons = r[pp.ADDONS]
    assert struct.unpack_from('<H', addons[pp.FORM_TABLE], 2 * (32 * 133 + 2))[0] == 1490
    assert addons[pp.FORM_TABLE][:2 * (32 * 133 + 2)] == resource(blob, pp.ADDONS, 11)[1][:2 * (32 * 133 + 2)]
    assert struct.unpack_from('<H', addons[pp.FORM_SPECIES], 2 * (1490 - 1076))[0] == 133
    for member, follower in ((pp.HIDDEN_ABILITY, 8), (pp.BASE_EXP, 9)):
        old, new = resource(blob, pp.ADDONS, member)[1], addons[member]
        assert new[:len(old)] == old and new[len(old):len(old) + 28] == resource(blob, pp.ADDONS, follower)[1][:28]
        assert new[2 * 1490:] == old[2 * 133:2 * 133 + 2]
    assert addons[pp.MACHINES][1490 * 44:] == resource(blob, pp.ADDONS, 14)[1][133 * 44:134 * 44]
    assert addons[pp.TUTORS][1490 * 8:] == resource(blob, pp.ADDONS, 15)[1][133 * 8:134 * 8]
    assert r[pp.LEVELUP][0][1490 * 136:] == resource(blob, pp.LEVELUP, 0)[1][133 * 136:134 * 136]
    assert r[pp.OFFSETS][0][1490 * 0x59:] == resource(blob, pp.OFFSETS, 0)[1][133 * 0x59:134 * 0x59]
    # Boot data: icon selectors (stock 1490 + the dog's 2) and the (species, form, tag) row.
    image = a[resident.BOOT_ARCHIVE][0]

    def boot(address, size):
        return image[address - resident.BOOT_REGION[0]:address - resident.BOOT_REGION[0] + size]
    from sovereign_editor import character_runtime as cr
    ov129 = cr.overlay(blob, 129)
    assert boot(info['icon_table'], 1491) == ov129['data'][0x10:0x10 + 1490] + b'\x02'
    assert boot(info['tag_table'], 8) == struct.pack('<4H', 133, 3, 0x2400, 0)
    start = struct.unpack_from('<I', blob, 0x20)[0]
    words = {p['rom_offset'] - start + 0x02000000: bytes.fromhex(p['after']) for p in plan['patches']}
    assert words[0x02074408] == struct.pack('<I', info['icon_table'])
    assert words[0x02069D74] == struct.pack('<I', info['wrapper'] | 1)
    # Overlay 131: the follower table copy ends with the dog row; three references point to it.
    info131 = cr.overlay(blob, 131)
    data131 = plan['files'][info131['file_id']]
    rows = pp.tag_rows(blob, info['follower_table'], data131)
    assert rows[-1] == (0x2400, info['identities'][0]['follower_gfx'], pp.OW_SMALL)
    import copy
    bare = copy.deepcopy(state)
    bare['pokemon'] = {}
    other = world_authoring.runtime_plan(project, bare)['files'][info131['file_id']]
    assert rows[:-1] == pp.tag_rows(blob, struct.unpack_from('<I', other, 0x023C8F1C - 0x023C8000)[0], other)
    assert struct.unpack_from('<I', data131, 0x023C8F1C - 0x023C8000)[0] == info['follower_table']
    assert struct.unpack_from('<I', data131, 0x023C8F64 - 0x023C8000)[0] == info['wrapper'] | 1


def test_follower_tag_wrapper_on_the_cpu(project):
    import sys
    tools = str(ROOT / 'work/tiana-fixes-1/python-tools')
    if tools not in sys.path:
        sys.path.append(tools)
    pytest.importorskip('unicorn')
    from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE
    from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_SP, UC_ARM_REG_LR, \
        UC_ARM_REG_PC
    plan = world_authoring.runtime_plan(project, project.composed())
    info = plan['pokemon']
    image = plan['appends'][resident.BOOT_ARCHIVE][0]
    from sovereign_editor import character_runtime as cr
    ov129 = plan['files'][cr.overlay(project.blob, 129)['file_id']]
    calls = []

    def run(species, form, female):
        u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        u.mem_map(0x02000000, 0x400000)
        u.mem_write(0x023D8000, bytes(ov129))
        u.mem_write(resident.BOOT_REGION[0], bytes(image))

        def hook(uc, address, size, _):
            if address == pp.OW_TAG_FN:
                calls.append((uc.reg_read(UC_ARM_REG_R0), uc.reg_read(UC_ARM_REG_R1), uc.reg_read(UC_ARM_REG_R2),
                              uc.reg_read(UC_ARM_REG_LR)))
                uc.reg_write(UC_ARM_REG_R0, 610)
                uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
        u.hook_add(UC_HOOK_CODE, hook)
        for reg, value in ((UC_ARM_REG_R0, species), (UC_ARM_REG_R1, form), (UC_ARM_REG_R2, female),
                           (UC_ARM_REG_SP, 0x023A0000), (UC_ARM_REG_LR, 0x02001001)):
            u.reg_write(reg, value)
        u.emu_start(info['wrapper'] | 1, 0x02001000, count=1000)
        return u.reg_read(UC_ARM_REG_R0)
    assert run(133, 3, 0) == 0x2400 and run(133, 3, 1) == 0x2400 and not calls
    assert run(133, 0, 1) == 610 and calls[-1] == (133, 0, 1, 0x02001001)          # ordinary Eevee: stock path
    assert run(133, 1, 0) == 610 and calls[-1][:3] == (133, 1, 0)                   # partner: stock path
    assert run(58, 3, 0) == 610 and calls[-1][:3] == (58, 3, 0)


def test_export_reads_back_every_record(project, tmp_path):
    from sovereign_editor import world_readback, world
    out = tmp_path / 'e'
    project.export(out, project.doc['revision'])
    rom = (out / 'game.nds').read_bytes()
    try:
        blob = project.blob
        assert member_count(rom, pp.PERSONAL) == 1491 and resource(rom, pp.PERSONAL, 1490)[1] == \
            resource(blob, pp.PERSONAL, 133)[1]
        assert member_count(rom, pp.PICTURES) == 6 * 1491
        assert resource(rom, pp.PICTURES, 6 * 1490 + 3)[1] == (DOG / 'native/front.NCGR').read_bytes()
        assert member_count(rom, pp.ICONS) == 7 + 1491 and member_count(rom, pp.HEIGHTS) == 4 * 1491
        assert member_count(rom, pp.EVOLUTION) == 1491
        assert resource(rom, pp.ADDONS, 11)[1][2 * (32 * 133 + 2):2 * (32 * 133 + 3)] == struct.pack('<H', 1490)
        # Ordinary Eevee, partner Eevee and the battle form keep every stock record.
        for m in (133, 1302, 1403):
            assert resource(rom, pp.PERSONAL, m)[1] == resource(blob, pp.PERSONAL, m)[1]
            assert [resource(rom, pp.PICTURES, 6 * m + k)[1] for k in range(6)] == \
                [resource(blob, pp.PICTURES, 6 * m + k)[1] for k in range(6)]
        assert resource(rom, pp.OVERWORLD, 431)[1] == resource(blob, pp.OVERWORLD, 431)[1]
        gfx = world_authoring.runtime_plan(project, project.composed())['pokemon']['identities'][0]['follower_gfx']
        assert resource(rom, pp.OVERWORLD, gfx)[1] == (DOG / 'native/follower.btx0').read_bytes()
        arm = arm9_code(rom)
        table = struct.unpack_from('<I', arm, 0x74408)[0]
        assert resident.read_resident(rom, table + 1490, 1) == b'\x02'
        assert world_readback.check(rom, project)['failed'] == 0
    finally:
        (out / 'game.nds').unlink()


def test_gameplay_editor_pokemon_tab_imports_and_binds(tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.gameplay_ui import GameplayEditor
    p = Project(PARENT).clone(tmp_path / 'p')
    app = QApplication.instance() or QApplication([])
    w = GameplayEditor(p); w.tabs.setCurrentWidget(w.pokemon); w.show(); app.processEvents()
    panel = w.pokemon
    before = p.path.read_bytes()
    panel.source.setText(str(DOG)); panel.key.setText('jacobs_eevee'); panel.display.setText("Jacob's Eevee")
    panel.template.setValue(249)                          # Lugia: large follower layout, refused
    panel.guard(panel.stage_import); panel.guard(panel.preview_ops); app.processEvents()
    assert 'bytes, the template record has' in panel.status.text() or 'differs' in panel.status.text()
    panel.discard(); panel.template.setValue(133); panel.palette.setCurrentIndex(2)
    panel.guard(panel.stage_import); panel.guard(panel.preview_ops); app.processEvents()
    assert 'import jacobs_eevee: roles back, follower, front, icon, normal, shiny' in panel.preview.toPlainText()
    assert p.path.read_bytes() == before
    panel.guard(panel.apply); app.processEvents()
    assert 'package jacobs_eevee' in panel.list.toPlainText() and panel.package.count() == 1
    panel.identity.setText('jacobs_dog'); panel.guard(panel.stage_bind); panel.guard(panel.preview_ops)
    assert 'form 3 · personal 1490' in panel.preview.toPlainText()
    panel.guard(panel.apply); app.processEvents()
    assert 'identity jacobs_dog · species 133 form 3 · personal 1490' in panel.list.toPlainText()
    w.grab().save(str(ROOT / 'work/original-content-v1/impl/pokemon-tab.png'))
    w.close(); app.processEvents()
