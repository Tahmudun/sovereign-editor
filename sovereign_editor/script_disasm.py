"""Read-only HGSS field-script disassembly for qualification and project analysis.

Argument widths come from the pinned engine's armips scriptmacros.s (assets/
script-opcodes.json, with its source hash); the five commands whose width depends on
their first argument are decoded here. Control flow follows goto/goto_if/call/call_if
and the object/bg/direction jumps from every entry; decoding stops at end, return,
endstd or an unconditional goto. Nothing is written.
"""
import json
import struct
from pathlib import Path

from .formats import EditorError, require

_TABLE = json.loads((Path(__file__).parent / 'assets/script-opcodes.json').read_text())
OPCODES = {int(k): (v[0], v[1]) for k, v in _TABLE['opcodes'].items()}
END, RETURN, ENDSTD, GOTO, CALL = 2, 27, 21, 22, 26
BRANCHES = {23, 24, 25, 28, 29}                    # (byte selector, s32 offset)
STOPS = {END, RETURN, ENDSTD, GOTO}
FIELD_ACTIONS = {400, 401, 402}                    # action byte; action 2 adds a var


def _widths(op, raw, at):
    if op in FIELD_ACTIONS:
        return [1, 2] if raw[at] == 2 else [1]
    if op == 465:
        first = struct.unpack_from('<H', raw, at)[0]
        return [2, 2, 2] if first <= 3 else [2] if first == 6 else [2, 2]
    if op == 489:
        first = struct.unpack_from('<H', raw, at)[0]
        return [2, 2] if 1 <= first <= 3 else [2, 2, 2] if first in (5, 6) else [2]
    return OPCODES[op][1]


def decode_at(raw, at):
    """(opcode, name, args, next offset, jump target or None) of one command."""
    require(at + 2 <= len(raw), 'Script command runs past its member', 'INVALID_SCRIPT')
    op = struct.unpack_from('<H', raw, at)[0]
    require(op in OPCODES, f'Unknown script opcode {op} at {at}', 'INVALID_SCRIPT')
    cursor, args = at + 2, []
    for width in _widths(op, raw, cursor):
        require(cursor + width <= len(raw), 'Script command runs past its member', 'INVALID_SCRIPT')
        args.append(struct.unpack_from({1: '<B', 2: '<H', 4: '<i'}[width], raw, cursor)[0])
        cursor += width
    target = None
    if op in (GOTO, CALL):
        target = cursor + args[0]
    elif op in BRANCHES:
        target = cursor + args[1]
    return op, OPCODES[op][0], args, cursor, target


def entries(raw):
    from .dialogue_format import script_entries
    return script_entries(raw)[1]


def disassemble(raw, starts=None):
    """{offset: (opcode, name, args, target)} reachable from ``starts`` (default: all entries)."""
    todo = list(entries(raw) if starts is None else starts)
    seen = {}
    while todo:
        at = todo.pop()
        while at not in seen:
            try:
                op, name, args, nxt, target = decode_at(raw, at)
            except EditorError:
                seen[at] = (None, 'undecodable', [], None)
                break
            seen[at] = (op, name, args, target)
            if target is not None and 0 <= target < len(raw):
                todo.append(target)
            if op in STOPS:
                break
            at = nxt
    return dict(sorted(seen.items()))


def listing(raw, start):
    """Readable lines for one entry and everything it reaches (qualification evidence)."""
    lines = []
    for at, (op, name, args, target) in disassemble(raw, [start]).items():
        suffix = f' -> {target}' if target is not None else ''
        lines.append(f'{at:5d}: {name}({", ".join(str(a) for a in args)}){suffix}')
    return lines
