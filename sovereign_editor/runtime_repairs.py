"""Qualified repairs of base-ROM defects found by native testing (R101-SHOP).

``mart_return_icon`` — every Poké Mart's Buy screen (the badge mart and special marts alike)
highlights Cancel/Exit through ov03_022573D4 case 8, reached by a touch on Exit, by A on
Cancel and by moving the cursor onto Cancel; B skips it. That path calls ov03_022585A4(data,
0xFFFF), which replaces the item icon with a/0/1/8 members GetItemIndex(0xFFFF, 1/2). In the
pinned base hack GetItemIndex is hg-engine's (ARM9 0x02077C18 tail-jumps to overlay 129
0x023D8FA4) and returns GFX_ITEM_RETURN_ID = (MAX_TOTAL_ITEM_NUM + 1) * 2 + 4 = 5398 and 5399,
but the base hack's generated icon archive stops at member 5395 (its data/graphics/itemgra.mk
lists graphics indices 2..2698 and omits the return entry). The loader is asked for members
past the archive's end. tools/shop_exit_qualification.py reproduces the request on the CPU.

The repair appends the missing members: 5396/5397 (graphics index 2699, never requested)
repeat the dummy icon, 5398/5399 are the base hack's own data/graphics/item/return.png encoded
exactly as its build encodes every icon (verified byte-exact against existing members).
No code, save or stock member changes; projects that do not apply it export unchanged.

A repair is a project transaction (area-edit kind ``repair``, actions apply/remove), so it is
versioned, replayed, undone and reported like any other edit. Pure planning and candidate
bytes; Project owns writes.
"""
import copy
import functools
import hashlib
import struct
from pathlib import Path

from .formats import EditorError, member_count, require, resource

SCHEMA = 'sovereign-runtime-repair-transaction-v1'
ACTIONS = ('apply', 'remove')
ICON_ARCHIVE = 'a/0/1/8'
ICON_MEMBERS = 5396
GET_ITEM_INDEX_HOOK, GET_ITEM_INDEX_HOOK_BYTES = 0x02077C18, bytes.fromhex('004a1047a58f3d02')
GET_ITEM_INDEX, GET_ITEM_INDEX_SIZE = 0x023D8FA4, 0x94
GET_ITEM_INDEX_SHA256 = '398078d3f9d015579535116bbb6c40c5718ee317afea5721beaf88e052a2d183'
RETURN_CHAR, RETURN_PLTT = 0x1516, 0x1517           # the routine's GFX_ITEM_RETURN_ID literals
RETURN_LITERALS = (0x023D902C, 0x023D9034)
DUMMY_CHAR, DUMMY_PLTT = 2, 3                       # the base hack's none.png (item 0) members
ASSET = Path(__file__).with_name('assets') / 'repairs' / 'item_return.png'
ASSET_SHA256 = 'b5fb95c8757fd258fab449ece81f1675b782f04443657d3887a0e7b5dcca62cd'
REPAIRS = {
    'mart_return_icon': {
        'name': 'Poké Mart Cancel/Exit icon',
        'issue': 'R101-SHOP',
        'effect': 'Buy-screen Exit by touch, A on Cancel and the cursor on Cancel no longer request '
                  'item-icon members past the end of a/0/1/8',
    },
}


def is_transaction(t):
    return isinstance(t, dict) and t.get('schema') == SCHEMA


def applied(state):
    return set((state.get('runtime_repairs') or {}).get('applied', []))


# ---- qualification ---------------------------------------------------------------------------

def qualify(project):
    """Before-values of the defect in this baseline; refuses any other build."""
    from . import character_runtime as cr
    blob = project.blob
    at = GET_ITEM_INDEX_HOOK - 0x02000000
    require(bytes(project._base_arm9[at:at + 8]) == GET_ITEM_INDEX_HOOK_BYTES,
            'GetItemIndex is not the qualified hg-engine hook in this baseline', 'UNSUPPORTED_RUNTIME')
    ov129 = cr.overlay(blob, 129)
    base = 0x023D8000
    code = bytes(ov129['data'][GET_ITEM_INDEX - base:GET_ITEM_INDEX - base + GET_ITEM_INDEX_SIZE])
    require(hashlib.sha256(code).hexdigest() == GET_ITEM_INDEX_SHA256,
            'GetItemIndex differs from the qualified hg-engine routine', 'UNSUPPORTED_RUNTIME')
    literals = tuple(struct.unpack_from('<I', ov129['data'], a - base)[0] for a in RETURN_LITERALS)
    require(literals == (RETURN_CHAR, RETURN_PLTT), 'Return-icon members differ from the qualified build',
            'UNSUPPORTED_RUNTIME')
    count = member_count(blob, ICON_ARCHIVE)
    require(count == ICON_MEMBERS, f'{ICON_ARCHIVE} has {count} members, not the qualified {ICON_MEMBERS}',
            'UNSUPPORTED_RUNTIME')
    return {'archive': ICON_ARCHIVE, 'members_before': count, 'requested': [RETURN_CHAR, RETURN_PLTT],
            'appended': list(range(count, RETURN_PLTT + 1))}


@functools.lru_cache(maxsize=4)
def _icon(template_char, template_pltt):
    """(NCGR, NCLR) of the return icon in the base hack's own icon encoding."""
    from PIL import Image
    require(hashlib.sha256(ASSET.read_bytes()).hexdigest() == ASSET_SHA256, 'Return icon asset changed', 'INVALID_ASSET')
    with Image.open(ASSET) as im:
        require(im.mode == 'P' and im.size == (32, 32), 'Return icon must be a 32x32 indexed image', 'INVALID_ASSET')
        px, pal = list(im.getdata()), im.getpalette()
    require(max(px) < 16, 'Return icon uses more than 16 colours', 'INVALID_ASSET')
    tiles = bytearray()
    for ty in range(4):
        for tx in range(4):
            for y in range(8):
                row = (ty * 8 + y) * 32 + tx * 8
                tiles += bytes(px[row + x] | px[row + x + 1] << 4 for x in range(0, 8, 2))
    char = bytearray(template_char)
    require(len(char) == 560 and char[:4] == b'RGCN', 'Unexpected icon character template', 'UNSUPPORTED_RUNTIME')
    char[48:560] = tiles
    pltt = bytearray(template_pltt)
    require(len(pltt) == 552 and pltt[:4] == b'RLCN', 'Unexpected icon palette template', 'UNSUPPORTED_RUNTIME')
    colors = [(pal[3 * i] >> 3) | (pal[3 * i + 1] >> 3) << 5 | (pal[3 * i + 2] >> 3) << 10
              if 3 * i + 2 < len(pal) else 0 for i in range(16)]
    struct.pack_into('<16H', pltt, 40, *colors)
    return bytes(char), bytes(pltt)


def members(blob):
    """The four appended a/0/1/8 members (5396..5399)."""
    char = resource(blob, ICON_ARCHIVE, DUMMY_CHAR)[1]
    pltt = resource(blob, ICON_ARCHIVE, DUMMY_PLTT)[1]
    ret_char, ret_pltt = _icon(bytes(char), bytes(pltt))
    return [bytes(char), bytes(pltt), ret_char, ret_pltt]


# ---- transactions ----------------------------------------------------------------------------

def plan(project, state, index, action, repair=None, label=None):
    require(action in ACTIONS, f"Repair action is one of {', '.join(ACTIONS)}", 'INVALID_INPUT')
    require(repair in REPAIRS, f"Repair is one of {', '.join(REPAIRS)}", 'INVALID_INPUT')
    require(label is None or isinstance(label, str) and 0 < len(label) <= 160, 'Invalid label', 'INVALID_INPUT')
    report = qualify(project)
    on = repair in applied(state)
    if action == 'apply':
        require(not on, f'{repair} is already applied', 'NO_CHANGE')
    else:
        require(on, f'{repair} is not applied', 'NO_CHANGE')
    info = REPAIRS[repair]
    return {'schema': SCHEMA, 'index': index, 'action': action, 'repair': repair,
            'label': label or f"{'Apply' if action == 'apply' else 'Remove'} repair: {info['name']}",
            'request': {'action': action, 'repair': repair, 'label': label},
            'before': on, 'after': action == 'apply', 'report': {**report, 'issue': info['issue']}}


def replay(project, state, t, index):
    try:
        expected = plan(project, state, index, **t['request'])
    except (KeyError, TypeError) as exc:
        raise EditorError('INVALID_INPUT', 'Malformed runtime repair transaction') from exc
    require(expected == t, 'Runtime repair before-value or qualification differs', 'BEFORE_VALUE_MISMATCH')
    table = state.setdefault('runtime_repairs', {'applied': []})
    rows = set(table['applied'])
    rows.add(t['repair']) if t['after'] else rows.discard(t['repair'])
    table['applied'] = sorted(rows)


def summary(t):
    return {'operation': 'runtime.repair', 'index': t['index'], 'action': t['action'], 'repair': t['repair'],
            'label': t['label'], 'report': copy.deepcopy(t['report'])}


def view(project, state):
    rows = []
    for key, info in REPAIRS.items():
        try:
            report, available = qualify(project), True
        except EditorError as exc:
            report, available = {'refusal': exc.code, 'message': str(exc)}, False
        rows.append({'repair': key, **info, 'applied': key in applied(state), 'available': available, 'report': report})
    return {'repairs': rows}


def bindings(project, state, plan):
    """Appends of the applied repairs (runtime plan)."""
    if 'mart_return_icon' not in applied(state):
        return plan
    qualify(project)
    appends = dict(plan.get('appends', {}))
    require(ICON_ARCHIVE not in appends, f'Another runtime edit appends to {ICON_ARCHIVE}', 'RESOURCE_CONFLICT')
    appends[ICON_ARCHIVE] = members(project.blob)
    return {**plan, 'appends': appends,
            'runtime_repairs': {'mart_return_icon': {'archive': ICON_ARCHIVE, 'first': ICON_MEMBERS, 'count': 4}}}
