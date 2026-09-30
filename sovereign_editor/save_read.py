"""Read-only HGSS save inspection for access analysis (ACCESS-01, WORKSPACE-02).

Qualified on the pinned build's save (docs/ACTIVE_HANDOFF, WORLD-ACCESS-001): two 0x40000
blocks, each general block footer at +0xFF90 {save count u32, size u32 = 0xFFA0, magic u32 =
0x20060623}; the newer valid slot wins. SaveVarsFlags vars (0x170 u16) start at +4052 and the
flags follow them; the local-field Location {map, warp, x, z, facing} (s32) starts at +0x1424.
Never writes; callers pass the path of a copy or the original read-only.
"""
import struct
from pathlib import Path

from .formats import require

FOOTER = 0xFFA0 - 16
MAGIC, SIZE = 0x20060623, 0xFFA0
VARS0, NUM_VARS, NUM_FLAGS = 4052, 0x170, 0xB60
LOCATION = 0x1424
FACING = ('north', 'south', 'west', 'east')


def read(path):
    raw = Path(path).read_bytes()
    require(len(raw) >= 0x80000, 'Not an HGSS save (too short)', 'UNSUPPORTED_SAVE')
    slots = []
    for base in (0, 0x40000):
        count, size, magic = struct.unpack_from('<III', raw, base + FOOTER)
        if magic == MAGIC and size == SIZE:
            slots.append((count, base))
    require(slots, 'No valid save block found', 'UNSUPPORTED_SAVE')
    count, base = max(slots)
    flags_at = base + VARS0 + 2 * NUM_VARS
    flags = {f for f in range(1, NUM_FLAGS) if raw[flags_at + f // 8] >> (f % 8) & 1}
    header, warp, x, z, facing = struct.unpack_from('<5i', raw, base + LOCATION)
    return {'slot': 0 if base == 0 else 1, 'save_count': count, 'header': header, 'warp': warp, 'x': x, 'z': z,
            'facing': FACING[facing] if 0 <= facing < 4 else facing, 'flags': flags}
