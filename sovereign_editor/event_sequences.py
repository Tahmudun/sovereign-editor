"""Bounded, acyclic HGSS event sequences. No file writes or arbitrary opcodes."""
import struct
from . import dialogue_format as fmt
from .formats import require
from . import scene_commands as scene
from . import services
from . import travel
from . import field_moves
from . import field_services

RESULT = 0x800C
PARTY_CHECK_DOUBLE = 222     # party_check_for_double: 1 when two Pokémon can battle
DEFAULT_INSUFFICIENT = ['You need two Pokémon\nready to battle us!']


def validate(nodes, variables, trainers):
    require(isinstance(nodes, list) and 1 <= len(nodes) <= 64, 'An event needs 1..64 steps', 'INVALID_EVENT')
    labels = [n.get('id') for n in nodes if isinstance(n, dict)]
    require(len(labels) == len(nodes) and all(isinstance(k, str) and k and len(k) <= 32 for k in labels)
            and len(set(labels)) == len(labels), 'Step names must be unique', 'INVALID_EVENT')
    edges = {}
    for n in nodes:
        op = n.get('op'); targets = []
        fields = {'id', 'op'}
        if op in ('say', 'choice'):
            fields |= {'pages'}
            require(isinstance(n.get('pages'), list) and 1 <= len(n['pages']) <= 12,
                    'Dialogue needs 1..12 pages', 'INVALID_EVENT')
            for page in n['pages']: fmt.encode_message(page)
        if op == 'say': targets = ['next']
        elif op == 'choice': targets = ['yes', 'no']
        elif op == 'if':
            fields |= {'state', 'value'}; targets = ['yes', 'no']
        elif op == 'set':
            fields |= {'state', 'value'}; targets = ['next']
        elif op == 'battle' and trainers.get(n.get('trainer'), {}).get('policy'):
            # Ordinary policy: a loss blacks out and ends this event, so there is
            # no local loss branch. Editors may send empty ally/loss fields.
            fields |= {'trainer', 'partner', 'opponent2', 'lost'}; targets = ['won']
            require(not n.get('partner') and not n.get('opponent2') and not n.get('lost'),
                    'Ordinary trainers fight single battles; a loss blacks out instead of branching', 'INVALID_EVENT')
        elif op == 'battle':
            fields |= {'trainer', 'partner', 'opponent2'}; targets = ['won', 'lost']
            require(n.get('trainer') in trainers, 'Choose an existing opponent trainer', 'INVALID_EVENT')
            require(bool(n.get('partner')) == bool(n.get('opponent2')), 'Allied battle needs a partner and two opponents', 'INVALID_EVENT')
            for field in ('partner', 'opponent2'):
                require(not n.get(field) or n[field] in trainers, 'Unknown battle trainer', 'INVALID_EVENT')
            if n.get('partner'):
                require(len({n['trainer'], n['partner'], n['opponent2']}) == 3,
                        'Allied battle needs three distinct trainer definitions', 'INVALID_EVENT')
                require(all(len(trainers[n[k]]['party']) <= 3 for k in ('trainer', 'partner', 'opponent2')),
                        'Multi battle parties support at most three Pokémon per trainer', 'INVALID_EVENT')
        elif op in scene.OPS:
            fields, targets = scene.validate(n)
        elif op in services.OPS:
            fields, targets = services.validate(n)
        elif op in travel.OPS:
            fields, targets = travel.validate(n)
        elif op in field_moves.OPS:
            fields, targets = field_moves.validate(n, variables, trainers)
        elif op in field_services.OPS:
            fields, targets = field_services.validate(n)
        elif op == 'end': fields |= {'complete'}
        else: require(False, 'Unknown event step', 'INVALID_EVENT')
        if op in ('if', 'set'):
            require(n.get('state') in variables, 'Choose a named persistent state', 'INVALID_EVENT')
            require(type(n.get('value')) is int and 0 <= n['value'] <= 65535, 'State value must be 0..65535', 'INVALID_EVENT')
            require(not variables[n['state']].get('switch') or n['value'] in (0, 1),
                    'An on/off state takes 0 (off) or 1 (on)', 'INVALID_EVENT')
        fields.update(targets)
        require(set(n) <= fields, 'Unknown event step fields', 'INVALID_EVENT')
        require(all(n.get(k) in labels for k in targets), 'Every branch needs an existing target step', 'INVALID_EVENT')
        require(op != 'end' or type(n.get('complete', True)) is bool, 'Invalid completion setting', 'INVALID_EVENT')
        edges[n['id']] = [n[k] for k in targets]
    visited, active = set(), set()
    def visit(label):
        require(label not in active, 'Event loops are unsupported; replay by interacting again', 'EVENT_CYCLE')
        if label in visited: return
        active.add(label)
        for child in edges[label]: visit(child)
        active.remove(label); visited.add(label)
    visit(labels[0])
    require(visited == set(labels), 'Remove unreachable event steps', 'INVALID_EVENT')
    field_services.check_event(nodes)


def messages(nodes, trainers):
    result = []
    for n in nodes:
        result.extend(n.get('pages', []))
        if n['op'] in services.OPS:
            result.extend(services.messages(n))
        if n['op'] in field_moves.OPS:
            result.extend(field_moves.messages(n))
        if n['op'] == 'battle':
            t = trainers[n['trainer']]
            result.extend(t['before']); result.extend(t['after']); result.extend(t.get('revisit', []))
            if t.get('battle') == 'double':
                result.extend(t.get('insufficient') or DEFAULT_INSUFFICIENT)
    return result


def compile_sequence(spec, variables, trainers, message_start, environment=None):
    """Compile acyclic native steps; reacquire movement locks after battle return."""
    nodes = spec['nodes']; validate(nodes, variables, trainers)
    c=scene.Code();message=message_start;env=environment or {'actors':{},'variables':variables};blackout=False
    def pages(items, choice=False):
        nonlocal message
        for i, _ in enumerate(items):
            require(message <= 255, 'Area message IDs exhausted', 'RESOURCE_CAPACITY')
            c.emit('HB',45,message);message+=1
            if not (choice and i==len(items)-1):c.emit('2H',49,53)
    guard=spec.get('trigger')
    if guard:
        c.compare(variables[guard['state']]['variable'],guard['value']);c.jump('$quiet',5)
    c.emit('H',96)
    if spec['kind']=='npc':c.emit('3H',73,1500,104)
    if (spec.get('field') or {}).get('family')=='strength':
        # Stock boulder order (member 146 entry 2): an already-active Strength is
        # acknowledged before any move/permission question (R82-STRENGTH-01).
        act=next(n['id'] for n in nodes if n['op']=='field_move')
        c.emit('HBH',field_moves.STRENGTH,2,RESULT);c.compare(RESULT,1);c.jump('$field'+act+'active',1)
    once=spec.get('once_state')
    if once:
        scene.state_jump_unless(c,variables[once],0,'$exit')
    for n in nodes:
        c.label(n['id']);op=n['op']
        if op=='say':pages(n['pages']);c.jump(n['next'])
        elif op=='choice':
            pages(n['pages'],True);c.emit('2H',63,RESULT);c.emit('H',53)
            c.compare(RESULT,0);c.jump(n['yes'],1);c.jump(n['no'])
        elif op=='if':
            scene.state_jump(c,variables[n['state']],n['value'],n['yes']);c.jump(n['no'])
        elif op=='set':scene.state_set(c,variables[n['state']],n['value']);c.jump(n['next'])
        elif op=='battle' and trainers[n['trainer']].get('policy'):
            # Ordinary single battle, as common script 953: no healing, result
            # checked before any field access, blackout ends the event. Defeat
            # persists once, after a confirmed win; a revisit never rebattles.
            # v2 (trainer_format): no defeat state = repeatable; a double battle first checks
            # for two able Pokémon natively and otherwise explains and ends the event.
            trainer=trainers[n['trainer']];defeat=variables[trainer['defeat_state']] if trainer.get('defeat_state') else None
            double=trainer.get('battle')=='double'
            if defeat:scene.state_jump_unless(c,defeat,0,'$revisit'+n['id'])
            if double:c.emit('2H',PARTY_CHECK_DOUBLE,RESULT);c.compare(RESULT,0);c.jump('$pair'+n['id'],1)
            pages(trainer['before']);c.emit('3H2B',213,trainer['trainer_id'],0,0,0)
            c.emit('2H',220,RESULT);c.compare(RESULT,0);c.jump('$blackout',1)
            c.emit('H',96)
            if defeat:scene.state_set(c,defeat,1)
            pages(trainer['after']);c.jump(n['won'])
            c.label('$revisit'+n['id']);pages(trainer['revisit']);c.jump(n['won']);blackout=True
            if double:c.label('$pair'+n['id']);pages(trainer.get('insufficient') or DEFAULT_INSUFFICIENT);c.jump('$exit')
        elif op=='battle':
            trainer=trainers[n['trainer']];pages(trainer['before']);c.emit('H',282)
            if n.get('partner'):
                c.emit('4HB',562,trainers[n['partner']]['trainer_id'],trainer['trainer_id'],trainers[n['opponent2']]['trainer_id'],0)
            else:c.emit('3H2B',213,trainer['trainer_id'],0,1,0)
            # Native encounter restoration unpauses ALL objects before returning
            # to this script. Lock again before the first post-battle page.
            c.emit('4H',96,220,RESULT,282)
            pages(trainer['after']);c.compare(RESULT,1);c.jump(n['won'],1);c.jump(n['lost'])
        elif op in scene.OPS:scene.emit_node(c,n,env)
        elif op in services.OPS:
            start=message;count=len(services.messages(n))
            require(start+count-1<=255,'Area message IDs exhausted','RESOURCE_CAPACITY')
            def page(i,choice=False,start=start):
                c.emit('HB',45,start+i)
                if not choice:c.emit('2H',49,53)
            services.compile_node(c,n,page,{k:n[k] for k in ('yes','no','next') if k in n},env);message+=count
        elif op in travel.OPS:travel.compile_node(c,n)
        elif op in field_services.OPS:field_services.compile_node(c,n,env)
        elif op in field_moves.OPS:
            start=message;count=len(field_moves.messages(n))
            require(start+count-1<=255,'Area message IDs exhausted','RESOURCE_CAPACITY')
            def page(i,start=start):
                c.emit('HB',45,start+i);c.emit('2H',49,53)
            field_moves.compile_node(c,n,page,{k:n[k] for k in ('yes','no','next') if k in n},spec,variables,env,trainers);message+=count
        else:
            if once and n.get('complete',True):scene.state_set(c,variables[once],1)
            c.jump('$exit')
    # Runtime guards (an actor or the player elsewhere) abort here. An entry scene
    # reruns every frame while its stage matches, so its abort still advances the
    # stage and only hides actors: the player's tile is unknown.
    c.label('$abort')
    if spec['kind']=='entry':
        c.emit('3H',41,variables[guard['state']]['variable'],guard['advance'])
        scene.emit_visibility(c,env.get('actors',{}),variables,live=True,prefix='$abortsync',show=False)
    c.label('$exit');c.emit('H',97)
    c.label('$quiet');c.emit('H',2)
    if blackout:
        # Stock loss tail: OverworldWhiteOut heals and warps to the saved spawn,
        # then the original event only releases and ends (no dialogue resumes).
        c.label('$blackout');c.emit('3H',219,97,2)
    return c.finish()


def alignment(spec):
    """Start alignment for a compiled sequence: movement data needs a 4-aligned script."""
    return 4 if any(n['op'] in scene.MOVEMENT_OPS for n in spec['nodes']) else 1
