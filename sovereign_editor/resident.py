"""Resident overlay-129 layout v2 for chapter-size projects (PROD-CAP-001).

Overlay 129 is the hg-engine ARM9 extension: Main() loads it once into the
engine-reserved 0x023D8000..0x023E0000 region and nothing unloads it, so it is
the only memory that stays valid in the field, in battle (overlay 130 replaces
the field extension 131), after an ordinary loss and after a reload. The pinned
build already uses 29,903 of its 32,768 bytes.

Layout v1 (historical, <= 8 characters, <= 32 trainers, <= 32 created headers)
appends every addition to the overlay tail and stays byte-identical. Layout v2
is selected only beyond those bounds. It allocates every resident addition
through one first-fit allocator over:

* the stock trainer-class gender table (129 bytes) and prize-money table
  (516 bytes). Their only references (ARM9 0x020FFB90 and overlay 129
  0x023DC19C for gender; overlay 12 0x0223FC40/44 for money) are repointed to
  the extended copies, so both become free once character bindings run;
* the tail up to the reservation.

Boot data region (original content v1, PROD-04): read-only data may instead be placed in
0x023D6260..0x023D8000 (7,584 bytes), directly below overlay 129. No overlay loads there
(the battle extension 130 ends at 0x023D6248, the field extension 131 is capped below it
by FIELD_LIMIT, the battle-function overlays 133..148 end by 0x023C3128), the four HGSS
heaps (system.c sDefaultHeapSpec, 0x14D810 bytes from the arena above the last stock
overlay at 0x0226EC40) end near 0x023BD500, and ARM7 main memory starts at 0x023E0000.
hg-engine's boot routine (ARM9 0x02110334) loads overlay 129 and then runs two
``movs r0, #0``; they become a BL to a resident loader that reads the region's image
from an appended a/0/2/8 member with ReadWholeNarcMemberByIdPair (0x02007508: no heap,
an FSFile on the stack), then returns 0. Main() runs it at every boot and soft reset;
nothing else writes the region, so it stays valid in the field, in battle, after a loss
and after a reload.

``report()`` is the consolidated allocation report: address intervals,
alignment, kind, the ARM9 call site and Thumb BL reach of each hook, lifecycle,
maxima and remaining headroom. Pure candidate bytes; Project owns writes.
"""
import struct

from . import character_runtime as cr
from .formats import digest, immutable_digest, require, span

RESERVATION = 0x8000
STOCK_BYTES = 29903
RECLAIMED = (('stock trainer-class gender table', 0x6D1B, 0x6D9C),
             ('stock trainer-class prize-money table', 0x72A4, 0x74A8))
BL_REACH = 1 << 22
LIFECYCLE = 'resident: loaded once from Main() into 0x023D8000..0x023E0000; never unloaded (field, battle, loss, reload)'
BOOT_REGION = (0x023D6260, 0x023D8000)
BOOT_LIFECYCLE = ('boot data: read at every boot from a/0/2/8 into 0x023D6260..0x023D8000; no overlay or heap '
                  'uses that memory (field, battle, loss, reload)')
BOOT_ARCHIVE, BOOT_NARC = 'a/0/2/8', 28
LOADER_RESERVE = 24                # len(boot_loader(...)): always free at the overlay-129 tail
EXPANSION_BOOT = 0x0211033E        # hg-engine load_arm9_expansion, after BL HandleLoadOverlay129
EXPANSION_BEFORE = bytes.fromhex('00200020')   # movs r0, #0; movs r0, #0
READ_NARC_MEMBER = 0x02007508      # ReadWholeNarcMemberByIdPair(dest, narcId, fileId)
BATTLE_EXTENSION_END = 0x023D6248  # overlay 130 (stock size, loaded at 0x023C4000)
FIELD_LIMIT = cr.FIELD_LIMIT        # overlay 131 (0x023C8000) never reaches the boot region
V1_BOUNDS = {'characters': 8, 'trainers': 32, 'headers': 32}


def needed(characters, trainers, headers):
    """Layout v2 only beyond the historical v1 bounds (old projects stay byte-identical)."""
    return (characters > V1_BOUNDS['characters'] or trainers > V1_BOUNDS['trainers']
            or headers > V1_BOUNDS['headers'])


class Layout:
    version = 2

    def __init__(self, blob):
        require(immutable_digest(blob) == cr.BASELINE, 'Resident layout v2 is qualified only for the pinned baseline',
                'UNSUPPORTED_RUNTIME')
        self.info = cr.overlay(blob, 129)
        require(len(self.info['data']) == STOCK_BYTES, 'Overlay 129 differs from the qualified build',
                'BEFORE_VALUE_MISMATCH')
        self.base = self.info['address']
        self.data = bytearray(self.info['data'])
        tail = (len(self.data) + 3) & ~3
        self.segments = [{'kind': 'reclaimed', 'name': name, 'start': lo, 'end': hi} for name, lo, hi in RECLAIMED]
        self.segments.append({'kind': 'tail', 'name': 'reservation tail', 'start': tail, 'end': RESERVATION})
        self.free = [[s['start'], s['end'], s['kind']] for s in self.segments]
        self.claimed = False
        self.items = []
        self.boot = bytearray()

    def claim_stock_tables(self):
        """Character bindings repointed every reference to the stock tables."""
        self.claimed = True

    def place_boot(self, name, size, align=4, note='', kind='data'):
        """Data (or code) in the boot data region; returns the absolute RAM address."""
        require(size > 0, 'Empty boot data item')
        at = len(self.boot) + (-(BOOT_REGION[0] + len(self.boot)) % align)
        free = BOOT_REGION[1] - BOOT_REGION[0] - len(self.boot)
        require(BOOT_REGION[0] + at + size <= BOOT_REGION[1],
                f'Boot data region is full: {name} needs {size} bytes, {free} remain', 'RESOURCE_CAPACITY')
        self.boot.extend(bytes(at + size - len(self.boot)))
        address = BOOT_REGION[0] + at
        self.items.append({'name': name, 'address': address, 'offset': address - self.base, 'size': size, 'align': align,
                           'kind': kind, 'segment': 'boot', 'lifecycle': BOOT_LIFECYCLE, 'note': note})
        return address

    def place(self, name, size, align=4, kind='data', called_from=None, modulo=None, note='', loader=False):
        """First fit, in segment order; returns the absolute RAM address.

        The overlay-129 tail always keeps LOADER_RESERVE bytes for the boot-data loader, which must
        itself live in overlay 129 (it fills the boot region); only ``loader=True`` may use them."""
        require(size > 0, 'Empty resident item')
        for seg in self.free:
            lo, hi, kind_ = seg
            if kind_ == 'reclaimed' and not self.claimed:
                continue
            if kind_ == 'tail' and not loader:
                hi -= LOADER_RESERVE
            at = lo + (-(self.base + lo) % align)
            if modulo:
                step, origin = modulo
                while (self.base + at - origin) % step:
                    at += align
            if at + size <= hi:
                seg[0] = at + size
                address = self.base + at
                item = {'name': name, 'address': address, 'offset': at, 'size': size, 'align': align,
                        'kind': kind, 'segment': kind_, 'lifecycle': LIFECYCLE, 'note': note}
                if called_from is not None:
                    delta = address - called_from - 4
                    item.update(called_from=called_from, reach_ok=-BL_REACH <= delta < BL_REACH)
                    require(item['reach_ok'], f'{name} is outside Thumb BL reach', 'RESOURCE_CAPACITY')
                self.items.append(item)
                return address
        # Overlay 129 is full: the item overflows into the resident boot data region (also never
        # unloaded). A position constraint (modulo) cannot move there.
        free = sum(max(0, hi - lo) for lo, hi, k in self.free if k == 'tail' or self.claimed)
        require(modulo is None and not loader,
                f'Resident runtime is full: {name} needs {size} bytes, {free} remain in overlay 129',
                'RESOURCE_CAPACITY')
        address = self.place_boot(name, size, align, note=note + ' (overflow from overlay 129)', kind=kind)
        if called_from is not None:
            delta = address - called_from - 4
            require(-BL_REACH <= delta < BL_REACH, f'{name} is outside Thumb BL reach', 'RESOURCE_CAPACITY')
            self.items[-1].update(called_from=called_from, reach_ok=True)
        return address

    def write(self, address, payload):
        if BOOT_REGION[0] <= address < BOOT_REGION[1]:
            at = address - BOOT_REGION[0]
            require(at + len(payload) <= len(self.boot), 'Boot data write outside its item')
            self.boot[at:at + len(payload)] = payload
            return
        at = address - self.base
        require(0 <= at and at + len(payload) <= RESERVATION, 'Resident write outside overlay 129')
        if at + len(payload) > len(self.data):
            self.data.extend(bytes(at + len(payload) - len(self.data)))
        self.data[at:at + len(payload)] = payload

    def patch(self, address, before, after):
        at = address - self.base
        require(self.data[at:at + len(before)] == before and len(before) == len(after),
                f'Overlay 129 before-value differs at {address:x}', 'BEFORE_VALUE_MISMATCH')
        self.data[at:at + len(after)] = after

    def free_bytes(self):
        return sum(max(0, hi - lo) for lo, hi, k in self.free if k == 'tail' or self.claimed)

    def report(self):
        segments = []
        for seg, (lo, hi, _) in zip(self.segments, self.free):
            usable = seg['kind'] == 'tail' or self.claimed
            segments.append({**seg, 'address': self.base + seg['start'], 'size': seg['end'] - seg['start'],
                             'free': max(0, hi - lo) if usable else 0, 'available': usable})
        return {'version': 2, 'overlay': 129, 'address': self.base, 'reservation': RESERVATION,
                'stock_bytes': STOCK_BYTES, 'bytes': len(self.data), 'free_bytes': self.free_bytes(),
                'boot_region': {'address': BOOT_REGION[0], 'size': BOOT_REGION[1] - BOOT_REGION[0],
                                'used': len(self.boot), 'free': BOOT_REGION[1] - BOOT_REGION[0] - len(self.boot),
                                'lifecycle': BOOT_LIFECYCLE, 'archive': BOOT_ARCHIVE if self.boot else None},
                'bss': 'none: the hg-engine linker places .bss inside the loaded image; every item is file data',
                'lifecycle': LIFECYCLE, 'segments': segments,
                'items': sorted(self.items, key=lambda i: i['address'])}


def boot_image(blob):
    """The boot data image an exported ROM loads (None when its boot routine is stock)."""
    from .formats import resource
    arm_start = struct.unpack_from('<I', blob, 0x20)[0]
    call = span(blob, arm_start + EXPANSION_BOOT - 0x02000000, 4)
    if bytes(call) == EXPANSION_BEFORE:
        return None
    hi, lo = struct.unpack('<HH', call)
    delta = ((hi & 0x7FF) << 12) | ((lo & 0x7FF) << 1)
    target = EXPANSION_BOOT + 4 + (delta - (1 << 23) if delta & (1 << 22) else delta)
    info = cr.overlay(blob, 129)
    at = target - info['address']
    body = boot_loader(target, 0)
    require(info['data'][at:at + len(body) - 4] == body[:-4], 'Boot loader differs from the qualified code',
            'UNSUPPORTED_RUNTIME')
    dest, member = struct.unpack_from('<2I', info['data'], at + len(body) - 8)
    require(dest == BOOT_REGION[0], 'Boot loader destination differs', 'UNSUPPORTED_RUNTIME')
    return resource(blob, BOOT_ARCHIVE, member)[1]


def read_resident(blob, address, size):
    """Bytes of an exported ROM's resident memory: overlay 129 or the boot data image."""
    if BOOT_REGION[0] <= address < BOOT_REGION[1]:
        image = boot_image(blob)
        require(image is not None and address - BOOT_REGION[0] + size <= len(image),
                'Boot data is absent or too short', 'UNSUPPORTED_RUNTIME')
        return bytes(image[address - BOOT_REGION[0]:address - BOOT_REGION[0] + size])
    info = cr.overlay(blob, 129)
    at = address - info['address']
    require(0 <= at and at + size <= len(info['data']), 'Resident address is outside overlay 129', 'UNSUPPORTED_RUNTIME')
    return bytes(info['data'][at:at + size])


def boot_loader(address, member):
    """Resident routine run once at boot: read the boot data image, return 0."""
    return thumb([0xB508,                          # push {r3, lr}
                  ('ldr', 0, BOOT_REGION[0]),
                  0x2100 | BOOT_NARC,              # movs r1, #28 (a/0/2/8)
                  ('ldr', 2, member),
                  ('bl', READ_NARC_MEMBER),
                  0x2000,                          # movs r0, #0 (as the replaced instructions)
                  0xBD08], address)                # pop {r3, pc}


def finish(blob, plan, layout):
    """Write the final overlay-129 image and its size into a runtime plan."""
    info = layout.info
    result = {**plan, 'files': dict(plan['files']), 'patches': [dict(p) for p in plan['patches']]}
    if layout.boot:
        from .formats import member_count
        require(BOOT_ARCHIVE not in result.get('appends', {}), f'Another runtime edit appends to {BOOT_ARCHIVE}',
                'RESOURCE_CONFLICT')
        member = member_count(blob, BOOT_ARCHIVE)
        arm_start = struct.unpack_from('<I', blob, 0x20)[0]
        offset = arm_start + EXPANSION_BOOT - 0x02000000
        require(span(blob, offset, 4) == EXPANSION_BEFORE, 'hg-engine boot routine differs from the qualified build',
                'BEFORE_VALUE_MISMATCH')
        code = layout.place('resident.boot-loader', len(boot_loader(0, 0)), 4, 'code', called_from=EXPANSION_BOOT,
                            note=f'BL from hg-engine boot 0x{EXPANSION_BOOT:08X}; reads {BOOT_ARCHIVE} member {member}',
                            loader=True)
        require(not BOOT_REGION[0] <= code < BOOT_REGION[1], 'The boot-data loader must live in overlay 129',
                'RESOURCE_CAPACITY')
        layout.write(code, boot_loader(code, member))
        result['patches'].append({'rom_offset': offset, 'before': EXPANSION_BEFORE.hex(),
                                  'after': cr.thumb_bl(EXPANSION_BOOT, code).hex(), 'kind': 'resident.boot-loader'})
        result['appends'] = {**result.get('appends', {}), BOOT_ARCHIVE: [bytes(layout.boot)]}
        result['boot_data'] = {'address': BOOT_REGION[0], 'bytes': len(layout.boot), 'archive': BOOT_ARCHIVE,
                               'member': member, 'loader': code, 'sha256': digest(bytes(layout.boot))}
    result['files'][info['file_id']] = bytes(layout.data)
    offset = info['table_offset'] + 8
    existing = next((p for p in result['patches'] if p['rom_offset'] == offset), None)
    after = struct.pack('<I', len(layout.data)).hex()
    if existing:
        existing['after'] = after
    else:
        result['patches'].append({'rom_offset': offset, 'before': span(blob, offset, 4).hex(), 'after': after,
                                  'kind': 'resident.extension-size'})
    result['resident'] = layout.report()
    return result


def thumb_blx(source, destination):
    """BLX from Thumb at ``source`` to the ARM routine at ``destination`` (word aligned)."""
    delta = destination - ((source + 4) & ~3)
    require(destination % 4 == 0 and -(1 << 22) <= delta < (1 << 22), 'ARM call is outside Thumb BLX range')
    return struct.pack('<HH', 0xf000 | ((delta >> 12) & 0x7ff), 0xe800 | ((delta >> 1) & 0x7ff))


def thumb(program, base):
    """Assemble a small Thumb routine: halfwords, labels, pc-relative literal loads
    ('ldr', rd, value), conditional/unconditional branches ('b', cond|None, label),
    calls ('bl', absolute address), ARM calls ('blx', absolute address) and a trailing
    word-aligned literal pool. ``base`` must be word aligned; a routine with calls is
    assembled at its final address (base 0 only measures its size)."""
    require(base % 4 == 0, 'Resident routines are word aligned')
    labels, size = {}, 0
    for op in program:
        if isinstance(op, str):
            labels[op] = size
        else:
            size += 4 if isinstance(op, tuple) and op[0] in ('bl', 'blx') else 2
    pool_at = size + (size % 4)
    # One pool word per load, in program order: code bytes never depend on values.
    literals = [op[2] for op in program if isinstance(op, tuple) and op[0] == 'ldr']
    out, at, loads = bytearray(), 0, 0
    for op in program:
        if isinstance(op, str):
            continue
        if isinstance(op, int):
            out += struct.pack('<H', op)
        elif op[0] == 'ldr':
            target = pool_at + 4 * loads
            loads += 1
            imm = (target - ((at + 4) & ~3)) // 4
            require(0 <= imm < 256, 'Literal out of reach')
            out += struct.pack('<H', 0x4800 | op[1] << 8 | imm)
        elif op[0] == 'bl':
            out += cr.thumb_bl(base + at, op[1]) if base else bytes(4)
            at += 4
            continue
        elif op[0] == 'blx':
            out += thumb_blx(base + at, op[1]) if base else bytes(4)
            at += 4
            continue
        elif op[0] == 'b':
            off = (labels[op[2]] - (at + 4)) // 2
            if op[1] is None:
                require(-1024 <= off < 1024, 'Branch out of reach')
                out += struct.pack('<H', 0xE000 | (off & 0x7FF))
            else:
                require(-128 <= off < 128, 'Branch out of reach')
                out += struct.pack('<H', 0xD000 | op[1] << 8 | (off & 0xFF))
        at += 2
    if len(out) % 4:
        out += struct.pack('<H', 0x46C0)
    for value in literals:
        out += struct.pack('<I', value & 0xFFFFFFFF)
    return bytes(out)
