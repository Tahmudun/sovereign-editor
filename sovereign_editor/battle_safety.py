"""Guarded repairs for the pinned runtime's battle stat-stage faults.

BATTLE-STAT-002 (capped stat move repeats its limit message and never yields):
in engine overlay 142 the capped-stat early exits of
LoopCheckFunctionForSpreadMove_StatFailureSuccessCheck_StatChanges return
without BEFORE_MOVE_END's bookkeeping, so wb_seq_no stays non-zero and the
wrapper never unloads overlay 142. Command 31 then cannot load overlay 147 into
the same RAM and runs overlay 142's BeforeMove, which re-dispatches the move.
Each exit now jumps to the BEFORE_MOVE_END case instead (evidence:
evidence/scyther-quest-1/opus/BATTLE-STAT-002-DIAGNOSIS.md).

attack_down_effect is the historical r27 mitigation; exports no longer install
it because it replaced the correct limit message with a generic failure.
No baseline or game-source files are written.
"""
import struct

from .formats import require

EFFECT_ARCHIVE = 'a/0/3/0'
ATTACK_DOWN = 18
ORIGINAL = struct.pack('<5I', 50, 7, 2, 0x80000016, 224)

BEFORE_MOVE_ADDRESS = 0x021E5900          # overlay 142 load address
BEFORE_MOVE_SIZE = 39454
BEFORE_MOVE_END = 0x021E9B5A              # jump-table target of case BEFORE_MOVE_END (84)
# Each 32-byte block: LoadBattleSubSeqScript(MOVE_SEQ); server_seq_no = 24;
# ST_ServerTotteokiCountCalc; jump to the epilogue. Two per macro instance
# (stat-change and ability stat checks; non-spread and single-target spread).
BEFORE_MOVE_EXITS = {
    0x021E7B96: '9c4b39002000e258a14b02f040f818232100a36028009e4b02f039f8fdf7c1fe',
    0x021E7D74: '244b2000e2580021294b01f051ff18232100a3602800274b01f04afffdf7d2fd',
    0x021E8BF2: '9d4b39002000e258994b01f012f818232100a36028009d4b01f00bf8fcf793fe',
    0x021E8DD0: '254b2000e2580021214b00f023ff18232100a3602800264b00f01cfffcf7a4fd'}
BEFORE_MOVE_END_BYTES = '274b00222100280000f05ff8636e0028'   # IsValidParentalBondMove call, then wb_seq_no = 0


def before_move_overlay(raw):
    """Repair BATTLE-STAT-002: capped-stat early exits finish through BEFORE_MOVE_END."""
    from .character_runtime import thumb_bl
    require(len(raw) == BEFORE_MOVE_SIZE, 'Unexpected before-move overlay size', 'BEFORE_VALUE_MISMATCH')
    end = BEFORE_MOVE_END - BEFORE_MOVE_ADDRESS
    require(raw[end:end + 16] == bytes.fromhex(BEFORE_MOVE_END_BYTES),
            'Unexpected before-move end state', 'BEFORE_VALUE_MISMATCH')
    result = bytearray(raw)
    for site, before in BEFORE_MOVE_EXITS.items():
        at = site - BEFORE_MOVE_ADDRESS
        require(raw[at:at + 32] == bytes.fromhex(before), 'Unexpected before-move stat exit', 'BEFORE_VALUE_MISMATCH')
        # BL as a long branch, as the compiler already does here: LR is saved by
        # the function prologue, and r4 (context), r5 (system) and the frame match.
        result[at:at + 4] = thumb_bl(site, BEFORE_MOVE_END)
    return bytes(result)


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
