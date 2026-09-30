"""Extended ordinary trainers (BATTLE-02..05): per-Pokémon nature, IVs, EVs, ability slot and
ball, trainer AI flags, held-item bag and double battles, in the pinned engine's native layout.

Qualified against the pinned build (tools/trainer_party_qualification.py runs its own
MakeTrainerPokemonParty, overlay 131 = engine output_field.bin byte-for-byte):

* Trainer header (a/0/5/5, 20 bytes): data_type u8, class u8, 0, count u8, items 4xu16,
  aiFlags u32, doubleBattle u32. Stock doubles store 2 (BATTLE_TYPE_DOUBLES), which the
  battle setup ORs into the battle type (pret EnemyTrainerSet).
* Party entry (a/0/5/6): pow u8 (uniform IVs pow*31/255), abilityslot u8 (0 ability 1,
  1 ability 2 when the species has one, 2 hidden ability from code add-on a/0/2/8 member 7),
  level u16, species | form << 11, then per data_type bit: item 0x02, moves 0x01 (4 u16),
  ball 0x08, IV/EV 0x10 (6 IVs clamped to 31, 6 EVs), nature 0x20 (PID shifted to the
  nature), then ball seal u16. Stats are recalculated from the final values.

The explicit-ability bit (0x04) and shiny/status/stat overrides are not offered: an
ability ID the species cannot have is not a qualified behaviour. Pure data; Project owns writes.
"""
import struct

from .formats import require

POLICY = 'ordinary-v2'
MOVES, ITEMS, BALL, IV_EV, NATURE = 0x01, 0x02, 0x08, 0x10, 0x20
DOUBLES = 2
NATURES = ('Hardy', 'Lonely', 'Brave', 'Adamant', 'Naughty', 'Bold', 'Docile', 'Relaxed', 'Impish', 'Lax',
           'Timid', 'Hasty', 'Serious', 'Jolly', 'Naive', 'Modest', 'Mild', 'Quiet', 'Bashful', 'Rash',
           'Calm', 'Gentle', 'Sassy', 'Careful', 'Quirky')
ABILITY_SLOTS = {'first': 0, 'second': 1, 'hidden': 2}
# Engine AI flags (armips constants.s); offered where the pinned ROM's own trainers use them.
AI_FLAGS = {'prioritize_super_effective': 0, 'evaluate_attacks': 1, 'expert_attacks': 2,
            'prioritize_status_moves': 3, 'prioritize_damage': 5, 'use_weather': 9}
STOCK_AI_USE = {1, 3, 5, 7, 15, 33, 35, 519}   # distinct aiFlags values in stock a/0/5/5
MON_FIELDS = {'species', 'form', 'level', 'moves', 'held_item', 'nature', 'ivs', 'evs', 'ability', 'ball'}
STATS = ('HP', 'Attack', 'Defense', 'Speed', 'Sp. Atk', 'Sp. Def')


def integer(v, lo, hi): return type(v) is int and not isinstance(v, bool) and lo <= v <= hi


def abilities(project, species, form=0):
    """(first, second, hidden) ability IDs the engine resolves for this species/form."""
    from . import species as sp, species_forms as sf
    from .formats import resource
    adjusted = sf.personal_index(project, species, form)   # PokeOtherFormMonsNoGet mirror
    personal = sp.decode_personal(resource(project.blob, 'a/0/0/2', adjusted)[1])
    first, second = personal['abilities'][:2]
    table = resource(project.blob, 'a/0/2/8', 7)[1]
    hidden = struct.unpack_from('<H', table, 2 * adjusted)[0] if 2 * adjusted + 2 <= len(table) else 0
    return first, second, hidden


def validate(project, trainer):
    """Shape and qualification checks of the v2 fields (the v1 ordinary checks run first)."""
    from . import gameplay
    battle = trainer.get('battle', 'single')
    require(battle in ('single', 'double'), 'Battle is single or double', 'INVALID_INPUT')
    require(battle == 'single' or len(trainer['party']) >= 2, 'A double battle needs at least two Pokémon',
            'INVALID_INPUT')
    ai = trainer.get('ai', ['prioritize_super_effective'])
    require(isinstance(ai, list) and set(ai) <= set(AI_FLAGS) and len(set(ai)) == len(ai),
            f"AI settings are {', '.join(AI_FLAGS)}", 'INVALID_INPUT')
    items = trainer.get('items', [])
    require(isinstance(items, list) and len(items) <= 4, 'A trainer carries up to four items', 'INVALID_INPUT')
    for item in items:
        require(integer(item, 1, 1023) and bool(gameplay.name(project, 222, item)), f'Unknown item {item}',
                'UNSUPPORTED_ID')
    for mon in trainer['party']:
        require(set(mon) <= MON_FIELDS, f"Party fields are {', '.join(sorted(MON_FIELDS))}", 'INVALID_INPUT')
        if 'nature' in mon:
            require(integer(mon['nature'], 0, 24), 'Nature is 0..24', 'INVALID_INPUT')
        if 'ivs' in mon:
            require(isinstance(mon['ivs'], list) and len(mon['ivs']) == 6 and all(integer(v, 0, 31) for v in mon['ivs']),
                    'IVs are six values 0..31', 'INVALID_INPUT')
        if 'evs' in mon:
            require(isinstance(mon['evs'], list) and len(mon['evs']) == 6 and all(integer(v, 0, 252) for v in mon['evs'])
                    and sum(mon['evs']) <= 510, 'EVs are six values 0..252 totalling at most 510', 'INVALID_INPUT')
        require(('ivs' in mon) == ('evs' in mon), 'Set IVs and EVs together (the engine stores both)', 'INVALID_INPUT')
        if 'ability' in mon:
            require(mon['ability'] in ABILITY_SLOTS, 'Ability is first, second or hidden', 'INVALID_INPUT')
            first, second, hidden = abilities(project, mon['species'], mon.get('form', 0))
            if mon['ability'] == 'second':
                require(second != 0, 'This species/form has no second ability', 'UNSUPPORTED_ABILITY')
            if mon['ability'] == 'hidden':
                require(hidden != 0, 'This species/form has no hidden ability in the pinned table', 'UNSUPPORTED_ABILITY')
        if 'ball' in mon:
            require(integer(mon['ball'], 1, 16), 'Ball is a stock Poké Ball item 1..16', 'INVALID_INPUT')
    ball_modes = {('ball' in m) for m in trainer['party']}
    stat_modes = {('ivs' in m) for m in trainer['party']}
    nature_modes = {('nature' in m) for m in trainer['party']}
    require(len(ball_modes) == len(stat_modes) == len(nature_modes) == 1,
            'Ball, IV/EV and nature settings apply to the whole team (the engine stores one layout per trainer)',
            'INVALID_INPUT')


def encode(trainer, trainer_class):
    """(header, party) bytes in the pinned native layout."""
    party = trainer['party']
    moves = party[0]['moves'] is not None
    items = any(m['held_item'] for m in party)
    ball, stats, nature = 'ball' in party[0], 'ivs' in party[0], 'nature' in party[0]
    kind = (MOVES if moves else 0) | (ITEMS if items else 0) | (BALL if ball else 0) | (IV_EV if stats else 0) \
        | (NATURE if nature else 0)
    ai = sum(1 << AI_FLAGS[a] for a in trainer.get('ai', ['prioritize_super_effective']))
    bag = list(trainer.get('items', [])) + [0] * (4 - len(trainer.get('items', [])))
    header = struct.pack('<BHB4HII', kind, trainer_class, len(party), *bag, ai,
                         DOUBLES if trainer.get('battle') == 'double' else 0)
    rows = b''
    for m in party:
        rows += struct.pack('<BBHH', 0, ABILITY_SLOTS[m.get('ability', 'first')], m['level'],
                            m['species'] | m.get('form', 0) << 11)
        rows += struct.pack('<H', m['held_item']) if items else b''
        rows += struct.pack('<4H', *m['moves']) if moves else b''
        rows += struct.pack('<H', m['ball']) if ball else b''
        rows += bytes(m['ivs']) + bytes(m['evs']) if stats else b''
        rows += bytes([m['nature']]) if nature else b''
        rows += struct.pack('<H', 0)
    return header, rows


def decode(header, party):
    """Readback of an encoded trainer: the fields the party generator will read."""
    kind, trainer_class, count = header[0], struct.unpack_from('<H', header, 1)[0], header[3]
    bag = [v for v in struct.unpack_from('<4H', header, 4) if v]
    ai, double = struct.unpack_from('<II', header, 12)
    at, mons = 0, []
    for _ in range(count):
        pow_, slot, level, word = struct.unpack_from('<BBHH', party, at); at += 6
        mon = {'pow': pow_, 'ability_slot': slot, 'level': level, 'species': word & 0x7FF, 'form': word >> 11}
        if kind & ITEMS:
            mon['held_item'] = struct.unpack_from('<H', party, at)[0]; at += 2
        if kind & MOVES:
            mon['moves'] = list(struct.unpack_from('<4H', party, at)); at += 8
        if kind & BALL:
            mon['ball'] = struct.unpack_from('<H', party, at)[0]; at += 2
        if kind & IV_EV:
            mon['ivs'], mon['evs'] = list(party[at:at + 6]), list(party[at + 6:at + 12]); at += 12
        if kind & NATURE:
            mon['nature'] = party[at]; at += 1
        at += 2
        mons.append(mon)
    require(at == len(party), 'Encoded party length differs from its layout', 'BEFORE_VALUE_MISMATCH')
    return {'data_type': kind, 'class': trainer_class, 'items': bag, 'ai': ai, 'double': double, 'party': mons}


def stat_summary(mon):
    parts = []
    if 'nature' in mon:
        parts.append(NATURES[mon['nature']])
    if 'ability' in mon:
        parts.append(f"{mon['ability']} ability")
    if 'ivs' in mon:
        parts.append('IV ' + '/'.join(map(str, mon['ivs'])) + ' EV ' + '/'.join(map(str, mon['evs'])))
    return ', '.join(parts)
