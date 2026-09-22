"""Bounded character bindings for the pinned Sovereign Gold runtime.

Only produces candidate bytes; Project owns all writes. Original graphics and
lookup entries remain intact. Format/source evidence lives in tiana-events-1.
"""
import base64
import binascii
import copy
import struct
from functools import lru_cache

from .formats import digest, require, span, file_span

BASELINE = 'b1ea4b20bbb1f1c22025ac159d390d60f76786ab530851b30ebad239cfa97d4d'
PACKAGE_SCHEMA = 'sovereign-character-package-v1'
MAX_CHARACTERS = 8
OVERWORLD_START = 7000
FRONT_START = 129
BACK_START = 17
OVERWORLD_ARCHIVE = 'a/0/8/1'
FRONT_ARCHIVE = 'a/0/5/8'
BACK_ARCHIVE = 'a/0/0/6'


def decoded(value):
    require(isinstance(value, str) and len(value) <= 350000, 'Invalid character payload', 'INVALID_CHARACTER')
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        from .formats import EditorError
        raise EditorError('INVALID_CHARACTER', 'Character payload is not base64') from exc


def validate_package(value):
    try:
        return _validate_package(value)
    except (KeyError, TypeError, ValueError, IndexError, struct.error) as exc:
        from .formats import EditorError
        raise EditorError('INVALID_CHARACTER', 'Malformed character package') from exc


def _validate_package(value):
    require(isinstance(value, dict) and value.get('schema') == PACKAGE_SCHEMA,
            'Choose a prepared Sovereign character package', 'INVALID_CHARACTER')
    require(isinstance(value.get('name'), str) and 1 <= len(value['name']) <= 12
            and value['name'].strip() == value['name'], 'Character name needs 1..12 characters', 'INVALID_CHARACTER')
    require(value.get('gender') in ('male', 'female'), 'Choose a character gender', 'INVALID_CHARACTER')
    from .dialogue_format import encode_message
    encode_message(value['name'])
    ow = decoded(value.get('overworld'))
    require(ow[:4] == b'BTX0' and len(ow) <= 65536, 'Invalid overworld texture container', 'INVALID_CHARACTER')
    from .nitro import texture_set
    textures, palettes = texture_set(ow)
    require(len(textures) == 16 and len(palettes) == 1,
            'This importer supports the qualified 16-frame human overworld layout', 'INVALID_CHARACTER')
    for kind in ('front', 'back'):
        parts = value.get(kind)
        require(isinstance(parts, list) and len(parts) == 5, 'Trainer graphics need five members', 'INVALID_CHARACTER')
        for part, magic in zip(parts, (b'RGCN', b'RLCN', b'RECN', b'RNAN', b'RGCN')):
            raw = decoded(part)
            require(raw[:4] == magic and 16 <= len(raw) <= 65536,
                    'Invalid trainer graphics member', 'INVALID_CHARACTER')
            require(struct.unpack_from('<I', raw, 8)[0] == len(raw),
                    'Trainer graphics length differs from its header', 'INVALID_CHARACTER')
        # Refuse palette overflow and incorrect cell count before any allocation.
        palette = decoded(parts[1])
        require(len(palette) == 552 and palette[16:20] == b'TTLP'
                and struct.unpack_from('<I', palette, 24)[0] == 3
                and struct.unpack_from('<I', palette, 32)[0] == 512
                and not any(palette[72:-2]) and palette[-2:] in (b'\0\0', b'IR'),
                'Trainer palette needs 16 colors with qualified stock padding', 'INVALID_CHARACTER')
        cell = decoded(parts[2])
        require(cell[16:20] == b'KBEC' and struct.unpack_from('<H', cell, 24)[0] == (1 if kind == 'front' else 5),
                'Unsupported trainer cell layout', 'INVALID_CHARACTER')
    _validate_graphics(value['overworld'], tuple(value['front']), tuple(value['back']))
    return copy.deepcopy(value)


@lru_cache(maxsize=16)
def _validate_graphics(overworld, front, back):
    from .character_preview import trainer_frames, overworld_frames
    trainer_frames(front); trainer_frames(back)
    overworld_frames({'overworld':overworld})


def file_by_id(blob, file_id):
    fat, size = struct.unpack_from('<II', blob, 0x48)
    require(type(file_id) is int and 0 <= file_id < size // 8, 'Invalid ROM file ID')
    start, end = struct.unpack_from('<II', blob, fat + 8 * file_id)
    return start, span(blob, start, end - start)


def overlay(blob, overlay_id):
    start, size = struct.unpack_from('<II', blob, 0x50)
    matches = []
    for offset in range(start, start + size, 32):
        entry = struct.unpack_from('<8I', blob, offset)
        if entry[0] == overlay_id:
            matches.append((offset, entry))
    require(len(matches) == 1, 'Required runtime overlay is absent', 'UNSUPPORTED_RUNTIME')
    offset, entry = matches[0]
    _, data = file_by_id(blob, entry[6])
    require(entry[3] == 0 and entry[7] >> 24 == 0 and len(data) == entry[2],
            'Character bindings require uncompressed overlays with no BSS', 'UNSUPPORTED_RUNTIME')
    return {'table_offset': offset, 'address': entry[1], 'file_id': entry[6], 'data': bytes(data)}


def thumb_bl(source, destination):
    delta = destination - source - 4
    require(delta % 2 == 0 and -(1 << 22) <= delta < (1 << 22), 'Character hook is outside Thumb BL range')
    return struct.pack('<HH', 0xf000 | ((delta >> 12) & 0x7ff), 0xf800 | ((delta >> 1) & 0x7ff))


def bindings(blob, packages, trainer_count=0):
    """Return guarded overlay/file patches and new archive members, without IO."""
    if not packages:
        return {'files': {}, 'patches': [], 'appends': {}, 'characters': []}
    require(digest(blob) == BASELINE, 'Character bindings are qualified only for the pinned baseline', 'UNSUPPORTED_RUNTIME')
    require(1 <= len(packages) <= MAX_CHARACTERS, 'Character library supports at most eight entries', 'RESOURCE_CAPACITY')
    packages = [validate_package(p) for p in packages]
    for archive, expected in ((OVERWORLD_ARCHIVE, 1553), (FRONT_ARCHIVE, 645), (BACK_ARCHIVE, 85)):
        raw = file_span(blob, archive)[1]
        require(struct.unpack_from('<H', raw, 24)[0] == expected, 'Baseline graphics allocation differs', 'BEFORE_VALUE_MISMATCH')
    overlays = {i: overlay(blob, i) for i in (12, 129, 131)}
    # Overlay 1 has an existing BSS; its data length stays unchanged.
    table, table_size = struct.unpack_from('<II', blob, 0x50)
    off = next(o for o in range(table, table + table_size, 32) if struct.unpack_from('<I', blob, o)[0] == 1)
    entry = struct.unpack_from('<8I', blob, off)
    require(entry[7] >> 24 == 0, 'Field overlay is compressed', 'UNSUPPORTED_RUNTIME')
    overlays[1] = {'table_offset': off, 'address': entry[1], 'file_id': entry[6], 'data': file_by_id(blob, entry[6])[1]}
    data = {i: bytearray(v['data']) for i, v in overlays.items()}
    patches = []
    arm_start = struct.unpack_from('<I', blob, 0x20)[0]

    def patch_arm(offset, before, after, label):
        require(span(blob, arm_start + offset, len(before)) == before and len(before) == len(after),
                'Character runtime before-value differs: ' + label, 'BEFORE_VALUE_MISMATCH')
        patches.append({'rom_offset': arm_start + offset, 'before': before.hex(), 'after': after.hex(), 'kind': label})

    def patch_overlay(i, address, before, after):
        at = address - overlays[i]['address']
        require(data[i][at:at + len(before)] == before and len(before) == len(after),
                f'Character overlay {i} before-value differs at {address:x}', 'BEFORE_VALUE_MISMATCH')
        data[i][at:at + len(after)] = after

    def append(i, payload):
        data[i].extend(b'\0' * (-len(data[i]) % 4))
        address = overlays[i]['address'] + len(data[i])
        data[i].extend(payload)
        return address

    count = len(packages)
    old_ow = bytes(data[131][0x2260:0x2260 + 9900])
    require(old_ow[-6:] == struct.pack('<3H', 65535, 0, 0), 'Overworld terminator differs')
    ow_table = old_ow[:-6] + b''.join(struct.pack('<3H', OVERWORLD_START + i, 1553 + i, 0)
                                     for i in range(count)) + old_ow[-6:]
    ow_address = append(131, ow_table)
    for i, addr in ((1, 0x21f92fc), (1, 0x21fa280), (131, 0x23c8f1c)):
        patch_overlay(i, addr, struct.pack('<I', 0x23ca260), struct.pack('<I', ow_address))
    gender_table = bytes(data[129][0x6d1b:0x6d1b + 129]) + bytes(p['gender'] == 'female' for p in packages)
    gender_address = append(129, gender_table)
    patch_overlay(129, 0x23dc19c, struct.pack('<I', 0x23ded1b), struct.pack('<I', gender_address))
    patch_arm(0xffb90, struct.pack('<I', 0x23ded1b), struct.pack('<I', gender_address), 'character.gender-table')
    money = bytes(data[129][0x72a4:0x72a4 + 516]) + b''.join(struct.pack('<2H', FRONT_START + i, 0) for i in range(count))
    money_address = append(129, money)
    for address, delta in ((0x223fc40, 0), (0x223fc44, 2)):
        patch_overlay(12, address, struct.pack('<I', 0x23df2a4 + delta), struct.pack('<I', money_address + delta))
    for address in (0x223fbd0, 0x223fbd4, 0x223fbdc):
        patch_overlay(12, address, b'\x81\x2c', bytes((FRONT_START + count, 0x2c)))
    # Sprite-resource selection calls this hook with (class, isLink). A new class
    # maps to its own back group; every original class tail-calls the original
    # function with arguments and return address intact. r3 is caller-saved.
    code = struct.pack('<8H', 0x2881, 0xd304, 0x2800 | (FRONT_START + count), 0xd202,
                       0x3881, 0x3011, 0x4770, 0x46c0)
    code += struct.pack('<2HI', 0x4b00, 0x4718, 0x0207280d)
    hook = append(129, code)
    patch_arm(0x70d66, thumb_bl(0x02070d66, 0x0207280c), thumb_bl(0x02070d66, hook), 'character.partner-back')
    require(type(trainer_count) is int and 0 <= trainer_count <= 32, 'Trainer capacity exceeded', 'RESOURCE_CAPACITY')
    if trainer_count:
        # SetupAndStartTrainerBattle: r7 is opponent1, r4 is the selected type.
        # Add stock BATTLE_TYPE_11 only for our dedicated practice trainer IDs,
        # including multi battles. Preserve every other register and setup path.
        # ldr r0,base; cmp r7,r0; blo done; ldr r0,end; cmp; bhs done;
        # mov r0,1; lsl r0,11; orr r4,r0; done: mov r0,11; mov r1,r4; bx lr.
        practice = struct.pack('<12H2I', 0x4805, 0x4287, 0xd305, 0x4805, 0x4287, 0xd202,
                               0x2001, 0x02c0, 0x4304, 0x200b, 0x1c21, 0x4770,
                               738, 738 + trainer_count)
        address = append(129, practice)
        patch_arm(0x513ac, bytes.fromhex('0b20211c'), thumb_bl(0x020513ac, address), 'trainer.practice-return')
    require(len(data[129]) <= 0x8000 and len(data[131]) <= 0x10000,
            'Character runtime expansion exceeds its reserved memory', 'RESOURCE_CAPACITY')
    files = {}
    for i, payload in data.items():
        info = overlays[i]
        if bytes(payload) == info['data']:
            continue
        files[info['file_id']] = bytes(payload)
        if len(payload) != len(info['data']):
            offset = info['table_offset'] + 8
            patches.append({'rom_offset': offset, 'before': span(blob, offset, 4).hex(),
                            'after': struct.pack('<I', len(payload)).hex(), 'kind': 'character.overlay-size'})
    appends = {OVERWORLD_ARCHIVE: [decoded(p['overworld']) for p in packages],
               FRONT_ARCHIVE: [decoded(v) for p in packages for v in p['front']],
               BACK_ARCHIVE: [decoded(v) for p in packages for v in p['back']]}
    rows = [{'name': p['name'], 'sprite': OVERWORLD_START + i, 'front_class': FRONT_START + i,
             'back_group': BACK_START + i} for i, p in enumerate(packages)]
    return {'files': files, 'patches': patches, 'appends': appends, 'characters': rows}
