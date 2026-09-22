"""Guard the pinned runtime's reported Growl stat-limit message lockup.

Keep the original Attack-down effect when it can change the stat. At the cap,
use MOVE_STATUS_FAILED and End instead of entering subscript12's buffered-message
path. This is a bounded mitigation; the full native lockup is not reproduced by
the isolated stat-command harness. No baseline or game-source files are written.
"""
import struct

from .formats import require

EFFECT_ARCHIVE = 'a/0/3/0'
ATTACK_DOWN = 18
ORIGINAL = struct.pack('<5I', 50, 7, 2, 0x80000016, 224)


def stat_stage_overlay(raw):
    """Repair the independently reproduced negative-clamp sign error (003).

    In pinned overlay137, a reduction past -6 incorrectly copies the positive
    remaining stage into r7. Negating that value clamps to zero instead. Keep
    the instruction size, branch destinations and every unrelated byte exact.
    This does not claim to repair the separate reported cap-message loop (002).
    """
    offset=0x1ec
    before=bytes.fromhex('1b781b061b16fb4200d51f0022009032')
    require(raw[offset:offset+len(before)]==before,
            'Stat lower-clamp instructions differ','BEFORE_VALUE_MISMATCH')
    result=bytearray(raw);result[0x1f6:0x1f8]=bytes.fromhex('5f42') # rsbs r7,r3,#0
    return bytes(result)


def attack_down_effect(raw):
    require(raw == ORIGINAL, 'Attack-down effect before-value differs', 'BEFORE_VALUE_MISMATCH')
    # Script parameters and signed branch offsets count 32-bit words.
    words = []; labels = {}; jumps = []
    def emit(*values): words.extend(values)
    def branch(label): jumps.append((len(words), label)); emit(0)
    # CheckIgnorableAbility(HAVE, DEFENDER, CONTRARY): the direction reverses
    # unless the attacking ability suppresses Contrary (e.g. Mold Breaker).
    emit(161, 0, 2, 126); branch('contrary')
    emit(33, 0, 2, 19, 0); branch('failed')  # Attack stage == -6
    labels['normal'] = len(words)
    words.extend(struct.unpack('<5I', raw))
    labels['contrary'] = len(words)
    emit(33, 0, 2, 19, 12); branch('failed')  # Reversed change at +6
    emit(59); branch('normal')
    labels['failed'] = len(words)
    emit(50, 10, 10, 0x40, 224)  # MOVE_STATUS_FAILED; End
    for at, label in jumps: words[at] = (labels[label] - at - 1) & 0xffffffff
    return struct.pack('<' + 'I' * len(words), *words)
