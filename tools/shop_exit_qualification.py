"""Mart Buy-screen exit icon path (R101-SHOP). CPU + archive evidence, not native acceptance.

The mart's Cancel/Exit highlight (overlay 3 ov03_022573D4 case 8, reached by a touch on Exit,
by A on Cancel and by moving the cursor onto Cancel with the D-pad; B skips it) calls
ov03_022585A4(data, 0xFFFF), which reloads the item icon from a/0/1/8 with
GetItemIndex(0xFFFF, 1/2). In the base hack that is hg-engine's GetItemIndex (ARM9 0x02077C18
tail-jumps into overlay 129): it returns GFX_ITEM_RETURN_ID = (MAX_TOTAL_ITEM_NUM + 1) * 2 + 4.

This tool runs, under Unicorn, the ROM's own ov03_022585A4 with the real GetItemIndex and
stubbed sprite-resource leaves, records the archive members it asks for, and compares them
with the a/0/1/8 member count. A request past the end is the defect; after the baseline repair
(baseline_repairs.MART_RETURN_ICON) the requested members exist and hold the return icon.

Usage: shop_exit_qualification.py ROM.nds [OUT.json]
"""
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'work/tiana-fixes-1/python-tools')]
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE  # noqa: E402
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R3, UC_ARM_REG_SP,  # noqa: E402
                               UC_ARM_REG_LR, UC_ARM_REG_PC)
from sovereign_editor import character_runtime as cr  # noqa: E402
from sovereign_editor.formats import arm9_code, member_count, resource, digest  # noqa: E402

ICON_ENTRY = 0x022585A4          # ov03_022585A4(MartData *data, u16 itemID)
GET_ITEM_INDEX = 0x02077C18      # hg-engine hook site
FIND, REPLACE_CHAR, TRANSFER_CHAR = 0x0200A7BC, 0x0200A2E4, 0x0200AE8C
REPLACE_PLTT, TRANSFER_PLTT, DRAW_FLAG = 0x0200A350, 0x0200B084, 0x02024830
ICON_ARCHIVE, ICON_NARC_ID = 'a/0/1/8', 0x12
MART_TYPE = 0x283
DATA, STACK, STOP = 0x02300000, 0x02390000, 0x02001000


def overlay_image(blob, ident):
    import ndspy.codeCompression
    start, size = struct.unpack_from('<II', blob, 0x50)
    for off in range(start, start + size, 32):
        e = struct.unpack_from('<8I', blob, off)
        if e[0] == ident:
            raw = bytes(cr.file_by_id(blob, e[6])[1])
            data = ndspy.codeCompression.decompress(raw) if e[7] & 0x01000000 else raw
            return e[1], data
    raise SystemExit(f'overlay {ident} missing')


def requested_members(blob, item):
    """Members ov03_022585A4 asks ReplaceChar/ReplacePltt for (narc id, member) when showing ``item``."""
    u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
    u.mem_map(0x02000000, 0x400000)
    u.mem_write(0x02000000, bytes(arm9_code(blob)))
    for ident in (3, 129):
        base, image = overlay_image(blob, ident)
        u.mem_write(base, image)
    u.mem_write(DATA, bytes(0x400))
    u.mem_write(DATA + MART_TYPE, b'\x00')          # MART_TYPE_NORMAL shows item icons
    calls = []

    def leaf(uc, address, size, _):
        if address in (FIND, TRANSFER_CHAR, TRANSFER_PLTT, DRAW_FLAG, REPLACE_CHAR, REPLACE_PLTT):
            if address in (REPLACE_CHAR, REPLACE_PLTT):
                calls.append({'call': 'char' if address == REPLACE_CHAR else 'pltt',
                              'member': uc.reg_read(UC_ARM_REG_R3)})
            if address == FIND:
                uc.reg_write(UC_ARM_REG_R0, DATA + 0x300)
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
    u.hook_add(UC_HOOK_CODE, leaf)
    u.reg_write(UC_ARM_REG_R0, DATA)
    u.reg_write(UC_ARM_REG_R1, item)
    u.reg_write(UC_ARM_REG_SP, STACK)
    u.reg_write(UC_ARM_REG_LR, STOP | 1)
    u.emu_start(ICON_ENTRY | 1, STOP, count=20000)
    assert u.reg_read(UC_ARM_REG_PC) == STOP, 'icon routine did not return'
    return calls


def narc_id_operand(blob):
    """The archive id ov03_022585A4 passes (movs r2,#0x12 before each Replace call)."""
    base, image = overlay_image(blob, 3)
    at = ICON_ENTRY - base
    code = image[at:at + 0xA0]
    return [code[i] for i in range(0, len(code), 2) if code[i + 1] == 0x22]   # movs r2, #imm


def main():
    rom = Path(sys.argv[1])
    blob = rom.read_bytes()
    count = member_count(blob, ICON_ARCHIVE)
    report = {'rom': str(rom), 'sha256': digest(blob), 'icon_archive': ICON_ARCHIVE, 'members': count,
              'narc_operands': narc_id_operand(blob), 'items': {}}
    for item in (0x0001, 0x0011, 0xFFFF):
        calls = requested_members(blob, item)
        for c in calls:
            c['exists'] = c['member'] < count
            if c['exists']:
                c['bytes'] = len(resource(blob, ICON_ARCHIVE, c['member'])[1])
        report['items'][f'{item:#06x}'] = calls
    report['return_icon_in_range'] = all(c['exists'] for c in report['items']['0xffff'])
    report['stock_items_in_range'] = all(c['exists'] for k, v in report['items'].items() if k != '0xffff' for c in v)
    text = json.dumps(report, indent=1)
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(text)
    print(text)


if __name__ == '__main__':
    main()
