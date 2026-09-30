"""Able/Unable labels in the move tutor's party selection (R82-TUTOR-01).

Qualified against the pinned ROM and pret/pokeheartgold 9d8b759 (src/party_menu.c,
src/party_context_menu.c, src/launch_application.c; ledger work/original-content-v1/impl):

* The tutor step opens the party menu through ScrCmd_PartySelectUI (context 3, plain
  selection: panels with HP bars, no label). TM/HM use (context 6) draws each panel with
  PartyMenu_DrawPanels_UseTMHM (0x0207A4B4), whose per-slot drawer (0x0207D7A8) prints
  ABLE / NOT ABLE / LEARNED from PartyMenu_CheckCanLearnTMHMMove (0x020820DC: 0xFD when the
  Pokémon already knows args->moveId, 0xFF when the TM is incompatible).
* While a tutor selects, its script sets special variable 0x800B to 0xC000 | tutor index
  (and back to 0 after the selection). Two BLs are redirected to resident routines:
  PartyMenu_DrawPanelsAndPush's default-panel call (0x0207A288) draws the TM/HM panels
  instead when that marker is present in context 3, and the TM/HM drawer's compatibility
  call (0x0207D806) answers from the tutor's own rule, the same one the script enforces
  before teaching: LEARNED when a move slot holds the tutor move, NOT ABLE when the species
  and form are outside the tutor's list (an empty list admits every Pokémon), ABLE
  otherwise. Every other party menu (TM/HM, stones, trades, stock scripts) is unchanged:
  the marker is checked only in context 3, and the TM/HM check keeps its stock answer.
* The tutor table (move, list) lives in the resident boot data region.

Pure candidate bytes; Project owns writes.
"""
import struct

from .formats import require, span

MARK_VAR, MARK = 0x800B, 0xC000
ARGS_OFFSET = 0x654                 # PartyMenu.args
DEFAULT_CALL, DRAW_DEFAULT, DRAW_TMHM = 0x0207A288, 0x0207A2AC, 0x0207A4B4
CHECK_CALL, CHECK_TMHM = 0x0207D806, 0x020820DC
GET_VAR_POINTER, GET_MON_DATA = 0x02040374, 0x0206E540
MON_SPECIES, MON_FORM, MON_MOVE1 = 5, 0x70, 0x36
SELECT_CONTEXT = 3
KNOWN, INCOMPATIBLE = 0xFD, 0xFF
MAX_TUTORS = 64


def tutors(state):
    """(sequence key, node id, move, eligible) of every tutor step, in a stable order."""
    from .story_authoring import catalog
    rows = []
    for key, s in sorted(catalog(state, 'sequence').items()):
        for n in s.get('nodes', []):
            if n.get('op') == 'tutor':
                rows.append((key, n['id'], n['move'], [(r['species'], r.get('form', 0)) for r in n.get('eligible', [])]))
    return rows


def index(state):
    return {(k, i): pos for pos, (k, i, _, _) in enumerate(tutors(state))}


def table(rows, base):
    """[offsets u32 × n][entries: move u16, count u8, pad, (species u16, form u8, pad) × count]."""
    head = 4 * len(rows)
    offsets, body = [], bytearray()
    for _, _, move, refs in rows:
        offsets.append(base + head + len(body))
        body += struct.pack('<HBx', move, len(refs))
        for species, form in refs:
            body += struct.pack('<HBx', species, form)
    return struct.pack(f'<{len(rows)}I', *offsets) + bytes(body)


def _entry(count, offsets):
    """Shared prologue: r5 = PartyMenu. Leaves the entry address in r0 (0 when inactive)."""
    return [
        ('ldr', 0, ARGS_OFFSET), 0x5828,     # ldr r0, [r5, r0]      args
        0x2124, 0x5C41,                      # movs r1, #0x24; ldrb r1, [r0, r1]   context
        0x2903, ('b', 1, 'none'),            # cmp r1, #3; bne none
        0x69C0,                              # ldr r0, [r0, #0x1C]   fieldSystem
        ('ldr', 1, MARK_VAR),
        ('bl', GET_VAR_POINTER),
        0x8800,                              # ldrh r0, [r0]
        0x0A01, 0x29C0, ('b', 1, 'none'),    # lsrs r1, r0, #8; cmp r1, #0xC0; bne none
        0x0600, 0x0E00,                      # lsls r0, #24; lsrs r0, #24   index
        0x2800 | count, ('b', 2, 'none'),    # cmp r0, #count; bhs none
        0x0080,                              # lsls r0, r0, #2
        ('ldr', 1, offsets), 0x5808,         # ldr r0, [r1, r0]      entry
        ('b', None, 'found'),
        'none', 0x2000,                      # movs r0, #0
        'found',
    ]


def draw_routine(address, count, offsets):
    """BL target at DEFAULT_CALL: r0 = PartyMenu, r1 = panel layout."""
    from .resident import thumb
    return thumb([
        0xB5F0,                              # push {r4-r7, lr}
        0x1C05, 0x1C0C,                      # adds r5, r0, #0; adds r4, r1, #0
        *_entry(count, offsets),
        0x2800, ('b', 0, 'plain'),           # cmp r0, #0; beq plain
        0x1C28, 0x1C21, ('bl', DRAW_TMHM),   # tutor: the TM/HM panels (labels, no HP bars)
        0xBDF0,                              # pop {r4-r7, pc}
        'plain',
        0x1C28, 0x1C21, ('bl', DRAW_DEFAULT),
        0xBDF0,
    ], address)


def check_routine(address, count, offsets):
    """BL target at CHECK_CALL: r0 = PartyMenu, r1 = Pokémon."""
    from .resident import thumb
    return thumb([
        0xB5F0,                              # push {r4-r7, lr}
        0x1C05, 0x1C0C,                      # r5 = PartyMenu, r4 = mon
        *_entry(count, offsets),
        0x2800, ('b', 0, 'stock'),           # not a tutor: the stock TM/HM answer
        0x1C06,                              # adds r6, r0, #0       entry
        0x2700,                              # movs r7, #0
        'moves',
        0x1C39, 0x3136,                      # adds r1, r7, #0; adds r1, #0x36   MON_DATA_MOVE1 + i
        0x1C20, 0x2200, ('bl', GET_MON_DATA),
        0x8831, 0x4288, ('b', 0, 'known'),   # ldrh r1, [r6]; cmp r0, r1; beq known
        0x3701, 0x2F04, ('b', 3, 'moves'),   # adds r7, #1; cmp r7, #4; blo moves
        0x78B7,                              # ldrb r7, [r6, #2]     count
        0x2F00, ('b', 0, 'able'),            # an empty list admits every Pokémon
        0x1C20, 0x2105, 0x2200, ('bl', GET_MON_DATA), 0x1C05,     # r5 = species
        0x1C20, 0x2170, 0x2200, ('bl', GET_MON_DATA), 0x1C04,     # r4 = form
        'refs',
        0x3604,                              # adds r6, #4
        0x8830, 0x42A8, ('b', 1, 'next'),    # ldrh r0, [r6]; cmp r0, r5; bne next
        0x78B0, 0x42A0, ('b', 0, 'able'),    # ldrb r0, [r6, #2]; cmp r0, r4; beq able
        'next',
        0x3F01, ('b', 1, 'refs'),            # subs r7, #1; bne refs
        0x20FF, 0xBDF0,                      # NOT ABLE
        'known', 0x20FD, 0xBDF0,             # LEARNED
        'able', 0x2000, 0xBDF0,              # ABLE
        'stock',
        0x1C28, 0x1C21, ('bl', CHECK_TMHM), 0xBDF0,
    ], address)


def bindings(blob, plan, state, layout):
    rows = tutors(state)
    if not rows:
        return plan
    require(len(rows) <= MAX_TUTORS, f'At most {MAX_TUTORS} tutor steps show Able/Unable labels', 'RESOURCE_CAPACITY')
    from . import character_runtime as cr
    arm_start = struct.unpack_from('<I', blob, 0x20)[0]
    result = {**plan, 'files': dict(plan['files']), 'patches': [dict(p) for p in plan['patches']]}
    size = len(table(rows, 0))
    offsets = layout.place_boot('tutor.table', size, 4, note=f'{len(rows)} tutor rules (move, species/forms)')
    layout.write(offsets, table(rows, offsets))
    draw = layout.place('tutor.draw', len(draw_routine(0, len(rows), 0)), 4, 'code', called_from=DEFAULT_CALL,
                        note='BL from PartyMenu_DrawPanelsAndPush: TM/HM panels while a tutor selects')
    layout.write(draw, draw_routine(draw, len(rows), offsets))
    check = layout.place('tutor.check', len(check_routine(0, len(rows), 0)), 4, 'code', called_from=CHECK_CALL,
                         note='BL from the TM/HM panel drawer: the tutor rule while a tutor selects')
    layout.write(check, check_routine(check, len(rows), offsets))
    for site, stock, target, kind in ((DEFAULT_CALL, DRAW_DEFAULT, draw, 'tutor.draw-hook'),
                                      (CHECK_CALL, CHECK_TMHM, check, 'tutor.check-hook')):
        offset = arm_start + site - 0x02000000
        before = cr.thumb_bl(site, stock)
        require(bytes(span(blob, offset, 4)) == before, f'Party menu call at {site:#x} differs from the qualified build',
                'BEFORE_VALUE_MISMATCH')
        result['patches'].append({'rom_offset': offset, 'before': before.hex(), 'after': cr.thumb_bl(site, target).hex(),
                                  'kind': kind})
    result['tutor_labels'] = {'tutors': len(rows), 'table': offsets, 'draw': draw, 'check': check}
    return result
