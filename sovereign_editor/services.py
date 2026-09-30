"""NPC move tutor, move relearner and money grant steps for authored events.

Compiled into ordinary native script flows (the stock Blackthorn relearner and Draco
Meteor tutor pattern, pinned pret/pokeheartgold 9d8b759 scr_seq_0948_T30R0601):
party selection, eligibility, the native learn screen (free slot, replace or cancel)
and payment taken ONCE, only after the learn screen reports success (result 0).
Opcodes and handlers: docs/ASSETS_GAMEPLAY_COVERAGE.md; the relearner list is the
ROM's patched ARM9 eligibility function (level <= current, level-0 evolution moves
included, known and duplicate moves skipped). Pure bytes; Project owns writes.
"""
from .formats import require

OPS = {'tutor', 'relearner', 'give_money'}
RESULT, SLOT, SPECIES, FORM, COUNT = 0x800C, 0x8005, 0x8006, 0x8007, 0x8004
SETVAR = 41
# (opcode, description) — stock HGSS script commands, handlers checked in the ROM.
ADD_MONEY, SUB_MONEY, HAS_MONEY, SHOW_MONEY, HIDE_MONEY, UPDATE_MONEY = 110, 111, 112, 113, 114, 115
TAKE_ITEM, HAS_ITEM, HAS_MOVE, RESTORE, FADE, WAIT_FADE = 126, 128, 140, 150, 174, 175
PARTY_UI, PARTY_SELECTION, PARTY_SPECIES, PARTY_COUNT, RELEARNABLE = 349, 351, 354, 332, 466
RELEARNER_INIT, TUTOR_INIT, LEARN_RESULT, PARTY_FORM, SOUND = 467, 468, 469, 676, 73
CANCELLED = 255
TEXTS = {
    'tutor': {'offer': None, 'no_funds': "You don't have enough to\npay for the lesson.",
              'none': 'None of your Pokémon can\nlearn this move from me.',
              'which': 'Which Pokémon should learn\nthe move?',
              'ineligible': "Sorry, that Pokémon can't\nlearn this move.",
              'known': 'That Pokémon already knows\nthis move.',
              'cancelled': 'Changed your mind?\nNo charge, then.',
              'done': 'Lesson complete! Thank you\nfor your payment.'},
    'relearner': {'offer': None, 'no_funds': "You don't have enough to\npay for a reminder.",
                  'none': "None of your Pokémon have\nmoves to remember.",
                  'which': 'Which Pokémon should\nremember a move?',
                  'ineligible': "That Pokémon has nothing\nfor me to remind it of.",
                  'known': "That Pokémon has nothing\nfor me to remind it of.",
                  'cancelled': 'Changed your mind?\nNo charge, then.',
                  'done': 'It remembered the move!\nThank you for your payment.'}}
ORDER = ('offer', 'no_funds', 'none', 'which', 'ineligible', 'known', 'cancelled', 'done')


def integer(v, lo, hi): return type(v) is int and not isinstance(v, bool) and lo <= v <= hi


def validate(n):
    """Shape checks (project-independent); returns (fields, targets)."""
    op = n['op']
    if op == 'give_money':
        require(integer(n.get('amount'), 1, 99999), 'Money grant needs 1..99999', 'INVALID_EVENT')
        return {'id', 'op', 'amount'}, ['next']
    fields = {'id', 'op', 'cost', 'eligible', 'texts'} | ({'move'} if op == 'tutor' else set())
    if op == 'tutor':
        require(integer(n.get('move'), 1, 1023), 'Choose the move to teach', 'INVALID_EVENT')
    cost = n.get('cost')
    require(cost is None or isinstance(cost, dict) and (
        set(cost) == {'money'} and integer(cost['money'], 1, 99999)
        or set(cost) == {'item', 'count'} and integer(cost['item'], 1, 536) and integer(cost['count'], 1, 99)),
        'Cost is none, {money} or {item, count}', 'INVALID_EVENT')
    eligible = n.get('eligible', [])
    require(isinstance(eligible, list) and len(eligible) <= 12
            and all(isinstance(r, dict) and set(r) <= {'species', 'form'} and integer(r.get('species'), 1, 2047)
                    and integer(r.get('form', 0), 0, 31) for r in eligible),
            'Eligible Pokémon are up to 12 {species, form} references (empty = any)', 'INVALID_EVENT')
    texts = n.get('texts')
    require(isinstance(texts, dict) and isinstance(texts.get('offer'), list) and 1 <= len(texts['offer']) <= 3
            and set(texts) <= set(ORDER), 'Service texts need 1..3 offer pages (others optional)', 'INVALID_EVENT')
    from . import dialogue_format as fmt
    for key, value in texts.items():
        for page in (value if key == 'offer' else [value]):
            require(isinstance(page, str), 'Service texts are strings', 'INVALID_EVENT'); fmt.encode_message(page)
    return fields, ['yes', 'no']


def messages(n):
    """Every page this step allocates, in compile order."""
    if n['op'] == 'give_money':
        return []
    defaults = TEXTS[n['op']]
    out = list(n['texts']['offer'])
    for key in ORDER[1:]:
        out.append(n['texts'].get(key, defaults[key]))
    return out


def qualify(project, n):
    """Project checks: qualified move, eligible species/forms and cost item."""
    from . import gameplay, species_forms as sf
    if n['op'] == 'give_money':
        return
    if n['op'] == 'tutor':
        gameplay.valid_id(project, 'moves', n['move'])
    for ref in n.get('eligible', []):
        sf.require_supported(project, ref['species'], ref.get('form', 0), 'Eligible Pokémon')
    cost = n.get('cost') or {}
    if 'item' in cost:
        require(bool(gameplay.name(project, 222, cost['item'])), f"Unknown cost item {cost['item']}", 'UNSUPPORTED_ID')


def compile_node(c, n, page, labels, env=None):
    """Emit one service step. ``page(text_index)`` prints one allocated message and waits;
    ``labels`` maps 'yes'/'no'/'next' to step ids; ``env['tutors']`` maps this event's tutor
    step ids to their tutor_labels index (Able/Unable labels in the party selection)."""
    op, tag = n['op'], '$svc' + n['id']
    if op == 'give_money':
        c.emit('HI', ADD_MONEY, n['amount']); c.emit('2H', SOUND, 1801); c.jump(labels['next']); return
    cost = n.get('cost') or {}
    msg = iter(range(len(messages(n))))
    offer = [next(msg) for _ in n['texts']['offer']]
    no_funds, none, which, ineligible, known, cancelled, done = (next(msg) for _ in range(7))
    for i, m in enumerate(offer):
        page(m, choice=i == len(offer) - 1)
    c.emit('2H', 63, RESULT); c.emit('H', 53)                    # YesNo, CloseMsg
    c.compare(RESULT, 0); c.jump(tag + 'decline', 5)
    # Funds are checked before anything is selected; nothing is charged here.
    if 'money' in cost:
        c.emit('3H', SHOW_MONEY, 20, 2)
        c.emit('HHI', HAS_MONEY, RESULT, cost['money'])
        c.emit('H', HIDE_MONEY)
        c.compare(RESULT, 0); c.jump(tag + 'poor', 1)
    elif cost:
        c.emit('4H', HAS_ITEM, cost['item'], cost['count'], RESULT)
        c.compare(RESULT, 0); c.jump(tag + 'poor', 1)
    # Any eligible party member at all? (unrolled over the six party slots)
    c.emit('2H', PARTY_COUNT, COUNT)
    for slot in range(6):
        c.compare(COUNT, slot); c.jump(tag + 'none', 3)           # count <= slot: no more members
        _eligible(c, n, slot, tag + f'next{slot}')
        c.jump(tag + 'select')
        c.label(tag + f'next{slot}')
    c.label(tag + 'none'); page(none); c.jump(labels['no'])
    c.label(tag + 'poor'); page(no_funds); c.jump(labels['no'])
    c.label(tag + 'decline'); c.jump(labels['no'])
    # Party selection (stock fade / party UI / restore sequence); refusals return here.
    c.label(tag + 'select'); page(which)
    mark = ((env or {}).get('tutors') or {}).get(n['id']) if op == 'tutor' else None
    if mark is not None:
        from .tutor_labels import MARK, MARK_VAR
        c.emit('3H', SETVAR, MARK_VAR, MARK | mark)
    c.emit('5H', FADE, 6, 1, 0, 0); c.emit('H', WAIT_FADE); c.emit('H', PARTY_UI)
    c.emit('2H', PARTY_SELECTION, SLOT); c.emit('H', RESTORE)
    if mark is not None:
        c.emit('3H', SETVAR, MARK_VAR, 0)
    c.emit('5H', FADE, 6, 1, 1, 0); c.emit('H', WAIT_FADE)
    c.compare(SLOT, CANCELLED); c.jump(tag + 'cancel', 1)
    _eligible(c, n, SLOT, tag + 'refuse', known_label=tag + 'known')
    c.emit('5H', FADE, 6, 1, 0, 0); c.emit('H', WAIT_FADE)
    if op == 'tutor':
        c.emit('3H', TUTOR_INIT, SLOT, n['move'])
    else:
        c.emit('2H', RELEARNER_INIT, SLOT)
    c.emit('2H', LEARN_RESULT, RESULT); c.emit('H', RESTORE)
    c.emit('5H', FADE, 6, 1, 1, 0); c.emit('H', WAIT_FADE)
    c.compare(RESULT, CANCELLED); c.jump(tag + 'cancel', 1)
    # Learned: take the payment exactly once, then confirm.
    if 'money' in cost:
        c.emit('3H', SHOW_MONEY, 20, 2); c.emit('HI', SUB_MONEY, cost['money']); c.emit('H', UPDATE_MONEY)
        c.emit('2H', SOUND, 1801); page(done); c.emit('H', HIDE_MONEY)
    else:
        if cost:
            c.emit('4H', TAKE_ITEM, cost['item'], cost['count'], RESULT)
        c.emit('2H', SOUND, 1801); page(done)
    c.jump(labels['yes'])
    c.label(tag + 'refuse'); page(ineligible); c.jump(tag + 'select')
    c.label(tag + 'known'); page(known); c.jump(tag + 'select')
    c.label(tag + 'cancel'); page(cancelled); c.jump(labels['no'])


def _eligible(c, n, slot, fail, known_label=None):
    """Fall through when party ``slot`` (literal or variable) may use this service."""
    c.emit('3H', PARTY_SPECIES, slot, SPECIES)
    c.compare(SPECIES, 0); c.jump(fail, 1)                         # empty slot or egg
    refs = n.get('eligible', [])
    if refs:
        ok = fail + 'ok' + str(slot)
        c.emit('3H', PARTY_FORM, slot, FORM)
        for i, r in enumerate(refs):
            skip = f'{fail}r{slot}_{i}'
            c.compare(SPECIES, r['species']); c.jump(skip, 5)
            c.compare(FORM, r.get('form', 0)); c.jump(ok, 1)
            c.label(skip)
        c.jump(fail); c.label(ok)
    if n['op'] == 'tutor':
        c.emit('4H', HAS_MOVE, RESULT, n['move'], slot)
        c.compare(RESULT, 1); c.jump(known_label or fail, 1)
    else:
        c.emit('3H', RELEARNABLE, RESULT, slot)
        c.compare(RESULT, 0); c.jump(known_label or fail, 1)
