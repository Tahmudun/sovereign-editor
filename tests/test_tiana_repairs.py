"""Reported native failures: finite Growl cap path and readable trigger pages."""
import struct

import pytest

from sovereign_editor import battle_safety as safety, event_sequences as seq
from sovereign_editor.formats import EditorError


def effect_result(raw, stage, contrary, mold_breaker=False):
    words = struct.unpack('<' + 'I' * (len(raw) // 4), raw)
    pc = 0; values = {2: 0, 10: 0}
    def jump(delta): return delta if delta < 2**31 else delta - 2**32
    for _ in range(20):
        op = words[pc]; pc += 1
        if op == 224: return values
        if op == 161:
            mode, battler, ability, delta = words[pc:pc+4]; pc += 4
            assert (mode, battler, ability) == (0, 2, 126)
            if contrary and not mold_breaker: pc += jump(delta)
        elif op == 33:
            mode, battler, field, value, delta = words[pc:pc+5]; pc += 5
            assert (mode, battler, field) == (0, 2, 19)
            if stage == value: pc += jump(delta)
        elif op == 50:
            mode, variable, value = words[pc:pc+3]; pc += 3
            if mode == 7: values[variable] = value
            else:
                assert mode == 10; values[variable] |= value
        elif op == 59:
            delta = words[pc]; pc += 1; pc += jump(delta)
        else: raise AssertionError(f'Unexpected effect opcode {op}')
        assert 0 <= pc < len(words)
    raise AssertionError('Effect failed to terminate')


@pytest.mark.parametrize('contrary,mold_breaker', [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize('stage', range(13))
def test_attack_limit_terminates_without_scheduling_stat_message(stage, contrary, mold_breaker):
    code = safety.attack_down_effect(safety.ORIGINAL)
    result = effect_result(code, stage, contrary, mold_breaker)
    capped = stage == (12 if contrary and not mold_breaker else 0)
    assert result == ({2: 0, 10: 0x40} if capped else {2: 0x80000016, 10: 0})


def test_effect_guard_refuses_different_before_bytes():
    with pytest.raises(EditorError, match='before-value'):
        safety.attack_down_effect(safety.ORIGINAL[:-1] + b'\x01')


@pytest.mark.parametrize('kind', ['npc', 'trigger'])
def test_pages_use_saved_text_speed_and_only_ab_to_advance(kind):
    spec = {'kind': kind, 'nodes': [
        {'id': 'talk', 'op': 'say', 'pages': ['First page.', 'Second page.'], 'next': 'end'},
        {'id': 'end', 'op': 'end'}]}
    code = seq.compile_sequence(spec, {}, {}, 5)
    start = 8 if kind == 'npc' else 2
    assert code[start:start+14] == struct.pack('<HB2HHB2H', 45, 5, 49, 53, 45, 6, 49, 53)
