"""Bounded, acyclic HGSS event sequences. No file writes or arbitrary opcodes."""
import struct
from . import dialogue_format as fmt
from .formats import require
from . import scene_commands as scene

RESULT = 0x800C


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
        elif op == 'end': fields |= {'complete'}
        else: require(False, 'Unknown event step', 'INVALID_EVENT')
        if op in ('if', 'set'):
            require(n.get('state') in variables, 'Choose a named persistent state', 'INVALID_EVENT')
            require(type(n.get('value')) is int and 0 <= n['value'] <= 65535, 'State value must be 0..65535', 'INVALID_EVENT')
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


def messages(nodes, trainers):
    result = []
    for n in nodes:
        result.extend(n.get('pages', []))
        if n['op'] == 'battle':
            t = trainers[n['trainer']]
            result.extend(t['before']); result.extend(t['after'])
    return result


def compile_sequence(spec, variables, trainers, message_start, environment=None):
    """Compile acyclic native steps; reacquire movement locks after battle return."""
    nodes = spec['nodes']; validate(nodes, variables, trainers)
    c=scene.Code();message=message_start;env=environment or {'actors':{},'variables':variables}
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
    once=spec.get('once_state')
    if once:
        c.compare(variables[once]['variable'],0);c.jump('$exit',5)
    for n in nodes:
        c.label(n['id']);op=n['op']
        if op=='say':pages(n['pages']);c.jump(n['next'])
        elif op=='choice':
            pages(n['pages'],True);c.emit('2H',63,RESULT);c.emit('H',53)
            c.compare(RESULT,0);c.jump(n['yes'],1);c.jump(n['no'])
        elif op=='if':
            c.compare(variables[n['state']]['variable'],n['value']);c.jump(n['yes'],1);c.jump(n['no'])
        elif op=='set':c.emit('3H',41,variables[n['state']]['variable'],n['value']);c.jump(n['next'])
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
        else:
            if once and n.get('complete',True):c.emit('3H',41,variables[once]['variable'],1)
            c.jump('$exit')
    c.label('$exit');c.emit('H',97)
    c.label('$quiet');c.emit('H',2)
    return c.finish()
