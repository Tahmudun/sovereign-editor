"""Exact-ROM battle-controller harness. Diagnostic only: NOT melonDS/native acceptance.

Executes the ROM's own ARM9, overlay 12 and engine code for BattleContext_Main
under Unicorn, one call per frame, from a single battle's action dispatch
(controller command 8) until the next turn begins (command 2 or 4).

Modeled, not native (report this with every result):
- pret poke_overlay.c leaves: GetOverlayLoadDestination (main RAM),
  GetOverlayRamBounds (ROM overlay table), LoadOverlay* (copy image, zero bss),
  FreeOverlayAllocation (mark inactive, fill RAM with 0xDE traps). The engine's
  HandleLoadOverlay/UnloadOverlayByID/IsOverlayLoaded and the stock
  CanOverlayBeLoaded/GetLoadedOverlaysInRegion run natively.
- ArchiveDataLoad (NARC member read), sys_AllocMemory (zeroed bump heap; frees
  are no-ops) and the server->client transport 0x02262240 (captured, never
  queued, so clients appear to finish instantly; no graphics or sound run).
- CopyBattleMonToPartyMon is a no-op. Parties are plaintext Gen 4 structures.
Battle initialization is reduced to stock BattleContext_Init and
BattleStructureCounterInit plus explicit fields (see setup_single). Unmapped or
trapped execution raises, so silent divergence is unlikely but not impossible.
Requires Unicorn (work/tiana-fixes-1/python-tools); callers skip without it.
"""
import json
import struct
import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'work/tiana-fixes-1/python-tools')]
from unicorn import (Uc, UcError, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_BLOCK, UC_HOOK_CODE,  # noqa: E402
                     UC_HOOK_MEM_INVALID)
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,  # noqa: E402
                               UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC)
import ndspy.code  # noqa: E402
import ndspy.narc  # noqa: E402
import ndspy.rom  # noqa: E402

# Offsets computed by clang (armv5te-none-eabi, -fshort-enums) from sovereign-gold
# include/battle.h; they agree with every annotated offset in that header.
OFF = json.loads(Path(__file__).with_name('battle_struct_offsets.json').read_text())
TRAP = 0xDE
STOP = 0x02001000                         # return sentinel inside ARM9 crt0
BSYS, CTX, ITEMS, OPPONENT, PARTIES = 0x02280000, 0x02290000, 0x022A0000, 0x022C0000, 0x022C8000
HEAP, HEAP_END, STACK_TOP = 0x02300000, 0x02380000, 0x023A0000
RESIDENT = (129, 12, 130)                 # ARM9 extension, battle, battle extension

FREE_ALLOCATION, LOAD_DESTINATION, REGION_TABLE_LITERAL, RAM_BOUNDS = 0x02006F58, 0x02006FAC, 0x0200713C, 0x02007148
LOAD_NORMAL, LOAD_NOINIT, LOAD_ASYNC = 0x02007180, 0x02007188, 0x020071C0
ARCHIVE_LOAD = 0x02007508
ALLOC, ALLOC_LO, FREE_EZ, FREE_EXPLICIT = 0x0201AA8C, 0x0201AACC, 0x0201AB0C, 0x0201AB80
TRANSPORT, COPY_TO_PARTY = 0x02262240, 0x02250C40
BATTLE_CONTEXT_MAIN, BATTLE_CONTEXT_INIT, COUNTER_INIT = 0x022486B0, 0x02250F44, 0x02251038
G_BATTLE_SYSTEM = 0x023D5B10              # engine battle_start.c sets it at battle start
OPTIONS = OPPONENT + 0x1800
MSG_STAT_WONT_GO_HIGHER, MSG_STAT_WONT_GO_LOWER = 142, 145

CYNDAQUIL = dict(species=155, level=5, hp=20, stats=[11, 10, 12, 11, 10], types=[10, 10], ability=66,
                 moves=[33, 43, 0, 0])    # Tackle, Leer
CHIKORITA = dict(species=152, level=5, hp=21, stats=[10, 12, 9, 10, 12], types=[12, 12], ability=65,
                 moves=[33, 45, 0, 0])    # Tackle, Growl


class Trap(Exception):
    pass


class Harness:
    def __init__(self, rom_bytes, overlay_data=None):
        rom = self.rom = ndspy.rom.NintendoDSRom(rom_bytes)
        self.overlays = rom.loadArm9Overlays()
        for ovy, data in (overlay_data or {}).items():
            assert len(data) == len(self.overlays[ovy].data), ovy
            self.overlays[ovy].data = bytes(data)
        u = self.u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        u.mem_map(0x01FF8000, 0x8000)         # ITCM
        u.mem_map(0x02000000, 0x400000)       # main RAM
        u.mem_map(0x027E0000, 0x20000)        # DTCM + system area
        u.mem_map(0x04000000, 0x2000)         # I/O registers (inert)
        for section in ndspy.code.MainCodeFile(rom.arm9, rom.arm9RamAddress).sections:
            u.mem_write(section.ramAddress, bytes(section.data))
        for o in self.overlays.values():      # overlay RAM starts trapped
            if 0x02000000 <= o.ramAddress < 0x02400000:
                u.mem_write(o.ramAddress, bytes([TRAP]) * (o.ramSize + o.bssSize))
        self.table = struct.unpack('<I', u.mem_read(REGION_TABLE_LITERAL, 4))[0]
        for slot, ovy in enumerate(RESIDENT):
            self._copy(ovy)
            u.mem_write(self.table + 8 * slot, struct.pack('<2I', ovy, 1))
        self.log = []; self.messages = []; self.events = []; self.narcs = {}; self.heap = HEAP
        self.blocks = []; self.frame = 0
        self.stubs = {FREE_ALLOCATION: self._free, LOAD_DESTINATION: lambda: 0, RAM_BOUNDS: self._bounds,
                      LOAD_NORMAL: self._load, LOAD_NOINIT: self._load, LOAD_ASYNC: self._load,
                      ARCHIVE_LOAD: self._archive, ALLOC: self._alloc, ALLOC_LO: self._alloc,
                      FREE_EZ: lambda: 0, FREE_EXPLICIT: lambda: 0,
                      TRANSPORT: self._transport, COPY_TO_PARTY: lambda: 0}
        for addr in self.stubs:
            u.hook_add(UC_HOOK_CODE, self._stub, begin=addr, end=addr)
        u.hook_add(UC_HOOK_MEM_INVALID, self._invalid)

    # ----- memory helpers
    def w32(self, a, v): self.u.mem_write(a, struct.pack('<I', v & 0xffffffff))
    def w16(self, a, v): self.u.mem_write(a, struct.pack('<H', v & 0xffff))
    def w8(self, a, v): self.u.mem_write(a, bytes([v & 0xff]))
    def r32(self, a): return struct.unpack('<I', self.u.mem_read(a, 4))[0]
    def reg(self, r): return self.u.reg_read(r)
    def mon(self, battler): return CTX + OFF['off_battlemon'] + battler * 0xC0

    def debug_blocks(self, keep=60):
        self.blocks = deque(maxlen=keep)
        self.u.hook_add(UC_HOOK_BLOCK, lambda u, a, s, _: self.blocks.append(a))

    # ----- modeled leaves
    def _fill(self, start, data):
        # Host writes do not invalidate Unicorn's translated blocks; overlays share RAM.
        self.u.mem_write(start, data)
        self.u.ctl_remove_cache(start, start + len(data))

    def _copy(self, ovy):
        o = self.overlays[ovy]
        self._fill(o.ramAddress, bytes(o.data) + bytes(o.bssSize))

    def _bounds(self):
        o = self.overlays.get(self.reg(UC_ARM_REG_R0))
        if o is None:
            return 0
        self.w32(self.reg(UC_ARM_REG_R1), o.ramAddress)
        self.w32(self.reg(UC_ARM_REG_R2), o.ramAddress + o.ramSize + o.bssSize)
        return 1

    def _load(self):
        ovy = self.reg(UC_ARM_REG_R1)
        self._copy(ovy); self.events.append(('load', ovy)); return 1

    def _free(self):
        entry = self.reg(UC_ARM_REG_R0); ovy = self.r32(entry); o = self.overlays[ovy]
        self._fill(o.ramAddress, bytes([TRAP]) * (o.ramSize + o.bssSize))
        self.w32(entry + 4, 0); self.events.append(('free', ovy)); return 0

    def member(self, arc, index):
        if arc not in self.narcs:
            self.narcs[arc] = ndspy.narc.NARC(self.rom.getFileByName('a/{}/{}/{}'.format(*f'{arc:03d}'))).files
        return self.narcs[arc][index]

    def _archive(self):
        dest, arc, index = (self.reg(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2))
        self.u.mem_write(dest, bytes(self.member(arc, index))); self.events.append(('archive', arc, index)); return 0

    def _alloc(self):
        size = (self.reg(UC_ARM_REG_R1) + 7) & ~7
        at = self.heap; self.heap += size
        if self.heap > HEAP_END:
            raise Trap('harness heap exhausted')
        self.u.mem_write(at, bytes(size)); return at

    def _transport(self):
        battler = self.reg(UC_ARM_REG_R2); data = self.reg(UC_ARM_REG_R3)
        raw = bytes(self.u.mem_read(data, min(self.r32(self.reg(UC_ARM_REG_SP)), 64)))
        if raw[0] == 0x15:   # PrintMessage: u16 message id at +2, params follow
            self.messages.append({'frame': self.frame, 'battler': battler, 'id': struct.unpack_from('<H', raw, 2)[0],
                                  'raw': raw[:24].hex()})
        return 0

    def _stub(self, u, addr, size, _):
        u.reg_write(UC_ARM_REG_R0, self.stubs[addr]())
        u.reg_write(UC_ARM_REG_PC, u.reg_read(UC_ARM_REG_LR))

    def _invalid(self, u, access, addr, size, value, _):
        raise Trap(f'invalid memory access {access} at {addr:#x} pc={u.reg_read(UC_ARM_REG_PC):#x}')

    # ----- battle state
    def call(self, addr, *args, budget=200000):
        for i, a in enumerate(args):
            self.u.reg_write(UC_ARM_REG_R0 + i, a)
        self.u.reg_write(UC_ARM_REG_SP, STACK_TOP); self.u.reg_write(UC_ARM_REG_LR, STOP | 1)
        self.u.emu_start(addr | 1, STOP, count=budget)
        assert self.reg(UC_ARM_REG_PC) == STOP, hex(self.reg(UC_ARM_REG_PC))
        return self.reg(UC_ARM_REG_R0)

    def write_mon(self, at, spec):
        """Gen 4 Pokemon (0xEC), plaintext with partyDecrypted|boxDecrypted set.

        Personality 0 selects substruct order A,B,C,D, so the ROM's GetMonData reads
        the blocks directly; the checksum is not verified while those flags are set.
        """
        mon = bytearray(0xEC)
        struct.pack_into('<IH', mon, 0, 0, 0x3)
        a = 0x08; b = a + 32; c = b + 32
        struct.pack_into('<HHII', mon, a, spec['species'], spec.get('item', 0), 12345, 135)
        mon[a + 12] = 70; mon[a + 13] = spec['ability'] & 0xff
        for i, mv in enumerate(spec['moves']):
            struct.pack_into('<H', mon, b + 2 * i, mv); mon[b + 8 + i] = 20
        struct.pack_into('<11H', mon, c, *([0x12B] * 10 + [0xFFFF]))
        struct.pack_into('<IBBHH5H', mon, 0x88, 0, spec['level'], 0, spec['hp'], spec['hp'], *spec['stats'])
        self.u.mem_write(at, bytes(mon))

    def setup_single(self, player, enemy):
        """Trainer single battle; each spec: species, level, hp, stats(5), types(2), ability, moves, slot, states."""
        u = self.u
        u.mem_write(BSYS, bytes(OFF['size_BattleSystem'])); u.mem_write(CTX, bytes(OFF['size_BattleStruct']))
        self.w32(BSYS + OFF['bs_battleType'], 0x01)
        self.w32(BSYS + OFF['bs_sp'], CTX); self.w32(BSYS + OFF['bs_maxBattlers'], 2)
        self.w32(G_BATTLE_SYSTEM, BSYS)
        u.mem_write(OPTIONS, bytes(0x40)); self.w32(BSYS + 0x1B4, OPTIONS)     # zero: animations on
        self.call(BATTLE_CONTEXT_INIT, CTX); self.call(COUNTER_INIT, BSYS, CTX)
        for b in range(4):   # OpponentData +0x194 battler id, +0x195 battler type (bit0 = enemy side)
            od = OPPONENT + 0x200 * b
            u.mem_write(od, bytes(0x200)); self.w8(od + 0x194, b); self.w8(od + 0x195, b)
            self.w32(BSYS + 0x34 + 4 * b, od)
            profile = OPPONENT + 0x1000 + 0x100 * b                            # no badges; Lv5 obeys
            u.mem_write(profile, bytes(0x100)); self.w32(BSYS + OFF['bs_maxBattlers'] + 4 + 4 * b, profile)
        table = b''.join(m[:16].ljust(16, b'\0') for m in ndspy.narc.NARC(self.rom.getFileByName('a/0/1/1')).files)
        u.mem_write(CTX + OFF['off_moveTbl'], table[:16 * 1000])
        u.mem_write(CTX + OFF['off_aiWorkTable'] + OFF['ai_old_moveTbl'], table[:16 * 468])
        items = b''.join(ndspy.narc.NARC(self.rom.getFileByName('a/0/1/7')).files)   # LoadAllItemData
        u.mem_write(ITEMS, items); self.w32(CTX + 0x2120, ITEMS)
        for b, spec in enumerate((player, enemy)):
            party = PARTIES + 0x800 * b
            u.mem_write(party, struct.pack('<2I', 6, 1) + bytes(6 * 0xEC)); self.write_mon(party + 8, spec)
            self.w32(BSYS + OFF['bs_trainerParty'] + 4 * b, party)
            m = self.mon(b)
            self.w16(m + OFF['pm_species'], spec['species'])
            for i, s in enumerate(spec['stats']):
                self.w16(m + 2 + 2 * i, s)
            for i, mv in enumerate(spec['moves']):
                self.w16(m + OFF['pm_move'] + 2 * i, mv)
                self.w8(m + OFF['pm_pp'] + i, 20); self.w8(m + OFF['pm_pp'] + 4 + i, 20)
            u.mem_write(m + OFF['pm_states'], bytes(spec.get('states', [6] * 8)))
            self.w8(m + OFF['pm_type1'], spec['types'][0]); self.w8(m + OFF['pm_type1'] + 1, spec['types'][1])
            self.w8(m + OFF['pm_level'], spec['level'])
            self.w32(m + OFF['pm_hp'], spec['hp']); self.w32(m + OFF['pm_maxhp'], spec['hp'])
            self.w16(m + OFF['pm_ability'], spec['ability']); self.w32(m + OFF['pm_personal_rnd'], 0x1234 + b)
            pa = CTX + OFF['off_playerActions'] + 16 * b
            self.w32(pa, 13); self.w32(pa + 4, 1 - b); self.w32(pa + 12, 1)      # FIGHT, target, input
            self.w16(CTX + OFF['off_waza_no_pos'] + 2 * b, spec['slot'])
            self.w16(CTX + OFF['off_waza_no_select'] + 2 * b, spec['moves'][spec['slot']])
        u.mem_write(CTX + OFF['off_executionOrder'], bytes([0, 1, 2, 3]))
        u.mem_write(CTX + OFF['off_turnOrder'], bytes([0, 1, 2, 3]))
        u.mem_write(CTX + OFF['off_sel_mons_no'], bytes(4))
        self.w32(CTX + OFF['off_executionIndex'], 0); self.w32(CTX + OFF['off_server_seq_no'], 8)

    def run(self, frames=3000, stop_commands=(2, 4), budget=400000):
        last = None
        for self.frame in range(frames):
            cmd = self.r32(CTX + OFF['off_server_seq_no'])
            if cmd != last:
                self.log.append((self.frame, cmd)); last = cmd
            if cmd in stop_commands and self.frame:
                return {'result': 'reached', 'command': cmd, 'frames': self.frame}
            for r, v in ((UC_ARM_REG_SP, STACK_TOP), (UC_ARM_REG_LR, STOP | 1), (UC_ARM_REG_R0, BSYS), (UC_ARM_REG_R1, CTX)):
                self.u.reg_write(r, v)
            try:
                self.u.emu_start(BATTLE_CONTEXT_MAIN | 1, STOP, count=budget)
            except (UcError, Trap) as exc:
                return {'result': 'error', 'error': str(exc), 'pc': hex(self.reg(UC_ARM_REG_PC)),
                        'frames': self.frame, 'command': cmd, 'blocks': [hex(b) for b in self.blocks]}
            if self.reg(UC_ARM_REG_PC) != STOP:
                return {'result': 'budget', 'pc': hex(self.reg(UC_ARM_REG_PC)), 'frames': self.frame}
        return {'result': 'frame_limit', 'frames': frames}


def single(rom, player_slot, enemy_slot, player_states=None, enemy_states=None, overlay_data=None,
           frames=1500, player=CYNDAQUIL, enemy=CHIKORITA):
    """Run one turn: both battlers FIGHT with their chosen move slot."""
    h = Harness(rom, overlay_data)
    h.setup_single(dict(player, slot=player_slot, states=player_states or [6] * 8),
                   dict(enemy, slot=enemy_slot, states=enemy_states or [6] * 8))
    result = h.run(frames=frames)
    states = [list(bytes(h.u.mem_read(h.mon(b) + OFF['pm_states'], 8))) for b in (0, 1)]
    hp = [h.r32(h.mon(b) + OFF['pm_hp']) for b in (0, 1)]
    return dict(result=result, message_ids=[m['id'] for m in h.messages], final_states=states, hp=hp,
                overlay_events=[e for e in h.events if e[0] != 'archive'])
