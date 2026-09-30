"""Warp-mat arrival facing and step (R82-WARP-01).

Qualified against the pinned ROM and pret/pokeheartgold 9d8b759 (asm/overlay_01_021E90C0.s,
src/field_warp_tasks.c; ledger work/original-content-v1/impl/LEDGER.md):

* A non-door warp arrival runs ov01_021E9FF8 (the ARM9 arrival task 0x02056530 starts it for
  every tile that is not a door, 0x69). It keeps the departure facing; when that facing is
  south it makes the player walk one step south (movement 0x0D) while the screen fades in,
  otherwise the player stays on the arrival tile.
* The stock world never arrives facing a warp mat's own opening: stock pairs join openings
  that face opposite ways (a house door north, its mat south). An authored cave hole on a
  south wall (mat 0x6F, opening south) reached by walking south through another south hole
  therefore arrived facing south and walked straight back toward the hole it came out of
  (user report: "arrival walks downward toward an exit and looks like the wrong hole").

The rule (two BLs in overlay 1, both resident routines in overlay 129): when the arrival
tile is a warp mat 0x6C/0x6D/0x6E/0x6F (opening east/west/north/south) and the player faces
that opening, the player turns around and takes the task's own one-step walk away from the
opening (the step movement follows the facing: walk group 12..15 via sub_0206234C). Every
other arrival, including every door and every stock pair, is byte-for-byte the stock path.
Pure candidate bytes; Project owns writes.
"""
import struct

from .formats import require, span

OVERLAY_FILE = 1                       # overlay 1 (uncompressed, file ID 1)
OVERLAY_BASE = 0x021E5900
FACING_CALL = 0x021EA030               # ov01_021E9FF8 state 0: bl PlayerAvatar_GetFacingDirection
STEP_CALL = 0x021EA0C6                 # state 1: bl MapObject_SetHeldMovement(obj, 0x0D)
GET_FACING, SET_FACING = 0x0205C654, 0x0205C660
GET_X, GET_Z = 0x0205C67C, 0x0205C688
BEHAVIOR_AT = 0x02054918               # GetMetatileBehavior(fieldSystem, x, z)
MOVEMENT_FOR, SET_HELD = 0x0206234C, 0x0206214C
WALK_SOUTH = 0x0D
MATS = {0x6C: 3, 0x6D: 2, 0x6E: 0, 0x6F: 1}   # behavior -> opening facing (east, west, north, south)
OPENINGS = sum(MATS[0x6C + i] << (8 * i) for i in range(4))


def facing_routine(address):
    """bl target at FACING_CALL: r0 = playerAvatar, r5 = fieldSystem (ov01_021E9FF8's)."""
    from .resident import thumb
    return thumb([
        0xB5D0,                  # push {r4, r6, r7, lr}
        0x1C04,                  # adds r4, r0, #0        playerAvatar
        ('bl', GET_FACING),
        0x1C06,                  # adds r6, r0, #0        facing
        0x1C20, ('bl', GET_X), 0x1C07,               # r7 = x
        0x1C20, ('bl', GET_Z), 0x1C02,               # r2 = z
        0x1C39,                  # adds r1, r7, #0
        0x1C28,                  # adds r0, r5, #0        fieldSystem
        ('bl', BEHAVIOR_AT),
        0x386C,                  # subs r0, #0x6C
        0x2803,                  # cmp r0, #3
        ('b', 8, 'stock'),       # bhi stock (also every behavior below 0x6C)
        0x00C0,                  # lsls r0, r0, #3
        ('ldr', 1, OPENINGS),
        0x40C1,                  # lsrs r1, r0            opening of this mat
        0x20FF,                  # movs r0, #0xFF
        0x4001,                  # ands r1, r0
        0x42B1,                  # cmp r1, r6
        ('b', 1, 'stock'),       # bne stock
        0x2101,                  # movs r1, #1
        0x4071,                  # eors r1, r6            the opposite facing
        0x1C20,                  # adds r0, r4, #0
        ('bl', SET_FACING),
        0x2001,                  # movs r0, #1            take the task's step path
        0xBDD0,                  # pop {r4, r6, r7, pc}
        'stock',
        0x1C30,                  # adds r0, r6, #0        the unchanged facing
        0xBDD0,                  # pop {r4, r6, r7, pc}
    ], address)


def step_routine(address):
    """bl target at STEP_CALL: r0 = player object, r1 = 0x0D, r5 = fieldSystem."""
    from .resident import thumb
    return thumb([
        0xB510,                  # push {r4, lr}
        0x1C04,                  # adds r4, r0, #0
        0x6C28,                  # ldr r0, [r5, #0x40]    playerAvatar
        ('bl', GET_FACING),
        0x210D,                  # movs r1, #0x0D         any member of the walk group
        ('bl', MOVEMENT_FOR),    # the walk in the facing direction (south stays 0x0D)
        0x1C01,                  # adds r1, r0, #0
        0x1C20,                  # adds r0, r4, #0
        ('bl', SET_HELD),
        0xBD10,                  # pop {r4, pc}
    ], address)


def bindings(blob, plan, layout):
    """Patch the two overlay-1 calls to resident routines (overlay 1 may already be edited)."""
    from . import character_runtime as cr
    _, raw = cr.file_by_id(blob, OVERLAY_FILE)
    data = bytearray(plan['files'].get(OVERLAY_FILE, raw))
    for site, target in ((FACING_CALL, GET_FACING), (STEP_CALL, SET_HELD)):
        at = site - OVERLAY_BASE
        require(bytes(data[at:at + 4]) == cr.thumb_bl(site, target) == bytes(raw[at:at + 4]),
                f'Overlay 1 arrival call at {site:#x} differs from the qualified build', 'BEFORE_VALUE_MISMATCH')
    at = STEP_CALL - 2 - OVERLAY_BASE
    require(bytes(raw[at:at + 2]) == struct.pack('<H', 0x2100 | WALK_SOUTH), 'Arrival step movement differs',
            'BEFORE_VALUE_MISMATCH')
    facing = layout.place('warp.arrival-facing', len(facing_routine(0)), 4, 'code', called_from=FACING_CALL,
                          note='BL from ov01_021E9FF8 state 0: turn away from a warp mat opening')
    layout.write(facing, facing_routine(facing))
    step = layout.place('warp.arrival-step', len(step_routine(0)), 4, 'code', called_from=STEP_CALL,
                        note='BL from ov01_021E9FF8 state 1: the arrival step follows the facing')
    layout.write(step, step_routine(step))
    for site, target in ((FACING_CALL, facing), (STEP_CALL, step)):
        at = site - OVERLAY_BASE
        data[at:at + 4] = cr.thumb_bl(site, target)
    result = {**plan, 'files': dict(plan['files']), 'patches': [dict(p) for p in plan['patches']]}
    result['files'][OVERLAY_FILE] = bytes(data)
    result['warp_arrival'] = {'facing': facing, 'step': step, 'calls': [FACING_CALL, STEP_CALL]}
    return result
