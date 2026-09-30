"""Fly marker selection and flight lookups (R101-FLY). CPU evidence, not native acceptance.

Runs the ROM's own Thumb code under Unicorn (ARM9 with runtime patches, overlays 100/101/129
and the resident boot data), for every Fly row of the extended Pokégear table:

1. ov101_021EA81C / PokegearMap_GetFlyDestinationAtCoord (0x021EA874) at the row's marker
   cell with its flypoint flag set (Save_VarsFlags_FlypointFlagAction stubbed): the chosen
   warp map, or 0.
2. PokegearMap_GetLocationSpecByMapID (0x021EA758) for the row's name map: the spec the
   selection uses. NULL here is the r101 defect.
3. ov101_021EB784 (the A/touch selection): the map ID it passes to the landmark-name printer
   (0x021EB4C4, stubbed) after the step above. Address 0 is mapped with the ARM9 ITCM image
   (the DS mirrors ITCM there), so a NULL spec reads what the hardware would.
4. Field side: sub_0203BB50 (fly map -> spawn ID) and GetFlyWarpData (0x0203BA74).

Stock rows are expected to behave identically before and after the repair. With the zeroed app
stand-in PokegearMap_ShouldLocationBeHidden hides the Safari Zone row (map 174), so its spec is
NULL here as in any save that has not unlocked it; such stock rows are reported, not failed.
Usage: fly_qualification.py ROM.nds [OUT.json]
"""
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools'), str(ROOT / 'work/tiana-fixes-1/python-tools')]
from unicorn import UC_HOOK_CODE  # noqa: E402
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_PC, UC_ARM_REG_LR  # noqa: E402
from sovereign_editor import character_runtime as cr, world_runtime as wr, travel_points as tp  # noqa: E402
from sovereign_editor.formats import arm9_code, digest  # noqa: E402
from world_integration_qualification import Machine, APP, SPECS_FIELD, COUNT_FIELD  # noqa: E402

FLY_AT_COORD, NAME_BY_MAP, SELECT = 0x021EA874, 0x021EA758, 0x021EB784
FLAG_ACTION = 0x02066930
PRINT_NAME, SPAWN_MENU, TOUCH_XY = 0x021EB4C4, 0x021EB428, 0x021E9464
FLY_SPAWN, FLY_WARP = 0x0203BB50, 0x0203BA74
GEAR, ARGS, OUT = 0x02310000, 0x02311000, 0x02312000
SELECTED_SPEC = 0x118


def overlay_image(blob, ident):
    import ndspy.codeCompression
    start, size = struct.unpack_from('<II', blob, 0x50)
    for off in range(start, start + size, 32):
        e = struct.unpack_from('<8I', blob, off)
        if e[0] == ident:
            raw = bytes(cr.file_by_id(blob, e[6])[1])
            return e[1], ndspy.codeCompression.decompress(raw) if e[7] & 0x01000000 else raw
    raise SystemExit(f'overlay {ident} missing')


def itcm(arm):
    """First autoload block (ITCM, 0x01FF8000) of the decompressed ARM9."""
    at = arm.find(struct.pack('<II', 0xDEC00621, 0x2106C0DE))
    first, _, start = struct.unpack_from('<3I', arm, at - 0x1C)
    address, size, _ = struct.unpack_from('<3I', arm, first - 0x02000000)
    assert address == 0x01FF8000
    return arm[start - 0x02000000:start - 0x02000000 + size]


class FlyMachine(Machine):
    def __init__(self, blob, images=None):
        """``images`` (arm9, overlay 101, overlay 129, boot data) from a runtime plan; default: the ROM's."""
        from sovereign_editor import resident
        if images is None:
            images = (bytes(arm9_code(blob)), wr.town_overlay(blob)['data'], cr.overlay(blob, 129)['data'],
                      resident.boot_image(blob))
        arm = bytes(images[0])
        super().__init__(arm, *images[1:])
        base, image = overlay_image(blob, 100)
        self.u.mem_write(base, image)
        self.u.mem_map(0, 0x10000)
        self.u.mem_write(0, itcm(arm))
        self.flags, self.printed = set(), []
        self.u.hook_add(UC_HOOK_CODE, self.stubs)

    def stubs(self, u, address, size, _):
        if address == FLAG_ACTION:          # (flags, action 2 = check, index)
            u.reg_write(UC_ARM_REG_R0, int(u.reg_read(UC_ARM_REG_R2) in self.flags))
        elif address == PRINT_NAME:
            value = u.reg_read(UC_ARM_REG_R1)
            self.printed.append(value - (1 << 32) if value >= 1 << 31 else value)
        elif address in (SPAWN_MENU, TOUCH_XY):
            pass
        else:
            return
        u.reg_write(UC_ARM_REG_PC, u.reg_read(UC_ARM_REG_LR))

    def app(self):
        app = bytearray(0x400)
        struct.pack_into('<I', app, SPECS_FIELD, wr.TOWN_SPECS)
        app[COUNT_FIELD] = wr.TOWN_STOCK_COUNT
        struct.pack_into('<I', app, 0x10, GEAR)            # mapApp->pokegear
        struct.pack_into('<I', app, 0x84, GEAR + 0x100)    # mapApp->objManager (cursor sprite read)
        self.u.mem_write(APP, bytes(app))
        gear = bytearray(0x40)
        struct.pack_into('<I', gear, 0x20, ARGS)          # pokegear->args
        struct.pack_into('<I', gear, 0x2C, GEAR + 0x30)    # saveVarsFlags (only passed to the stub)
        self.u.mem_write(GEAR, bytes(gear))
        self.u.mem_write(GEAR + 0x100, struct.pack('<3I', 0, 0, GEAR + 0x200) + bytes(0x1F4))
        self.u.mem_write(ARGS, bytes(0x40))


def fly_rows(blob, images=None):
    """Extended (or stock) Pokégear fly rows as the ROM's overlay 101 addresses them."""
    from sovereign_editor import resident
    ov101 = images[1] if images else wr.town_overlay(blob)['data']
    base = wr.TOWN_ADDRESS
    table = struct.unpack_from('<I', ov101, tp.FLY_LITERALS[0][0] - base)[0]
    count = ov101[tp.FLY_BOUNDS[0][0] - base]
    if table == tp.FLY_TABLE:
        raw = ov101[table - base:table - base + tp.FLY_SIZE * count]
    else:
        boot = images[3] if images else resident.boot_image(blob)
        raw = boot[table - resident.BOOT_REGION[0]:table - resident.BOOT_REGION[0] + tp.FLY_SIZE * count]
    return [struct.unpack_from('<HH9Bx', raw, tp.FLY_SIZE * i) for i in range(count)]


def qualify(blob, images=None):
    """Per-row selection/flight results (report dict)."""
    m = FlyMachine(blob, images)
    m.app()
    rows, failures = [], 0
    for index, row in enumerate(fly_rows(blob, images)):
        name_map, warp_map, flag, unk5, x, y = row[:6]
        width, height = row[8] & 15, row[8] >> 4
        m.flags = {flag}
        # Region check: the curRegion byte is set to the row's own region (stock rule).
        region = m.call(0x021E5C50, x, y + 2)
        m.u.mem_write(APP + 0xE, bytes([region]))
        dest = m.call(FLY_AT_COORD, APP, x, y)
        spec = m.call(NAME_BY_MAP, APP, name_map)
        spec_map = struct.unpack('<H', m.u.mem_read(spec, 2))[0] if spec else None
        m.u.mem_write(APP + SELECTED_SPEC, struct.pack('<I', spec))
        m.printed = []
        ret = m.call(SELECT, APP, dest) if dest else None
        printed = m.printed[0] if m.printed else None
        spawn = m.call(FLY_SPAWN, warp_map)
        warp = None
        if spawn:
            m.call(FLY_WARP, spawn, OUT)
            warp = list(struct.unpack('<5i', m.u.mem_read(OUT, 20)))
        authored = index >= tp.STOCK_FLYPOINTS
        ok = bool(dest == warp_map and spec and spec_map == name_map and printed == name_map
                  and spawn and warp[0] == warp_map) if authored else (not dest or not spec or printed == name_map)
        failures += not ok
        rows.append({'row': index, 'authored': authored, 'name_map': name_map, 'warp_map': warp_map,
                     'flag_index': flag, 'marker': [x, y], 'size': [width, height], 'destination': dest,
                     'name_spec': hex(spec), 'name_spec_map': spec_map, 'select_return': ret,
                     'printed_map': printed, 'spawn': spawn, 'fly_warp': warp, 'pass': ok})
    return {'rows': len(rows), 'authored': [r for r in rows if r['authored']],
            'stock': rows[:tp.STOCK_FLYPOINTS], 'failed': failures, 'asserts': m.asserts}


def main():
    rom = Path(sys.argv[1])
    blob = rom.read_bytes()
    result = qualify(blob)
    report = {'rom': str(rom), 'sha256': digest(blob), 'rows': result['rows'], 'authored': result['authored'],
              'stock_sample': result['stock'][:3], 'failed': result['failed'], 'asserts': result['asserts']}
    text = json.dumps(report, indent=1)
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(text)
    print(text)


if __name__ == '__main__':
    main()
