"""Small, explicit set of stock autonomous NPC behaviors and human appearances."""
from .formats import require

# DSPRE PokeDatabase movementTypesArray and HGSS MapObject_InitFromObjectEvent.
# Script movement opcodes are a different namespace and are never used here.
BEHAVIORS={0:'Stand still',2:'Look around',3:'Wander',4:'Walk north / south',5:'Walk west / east'}
APPEARANCES={
    315:'Young boy',316:'Young girl',317:'Boy 2',318:'Boy 3',319:'Girl 1',
    320:'Girl 2',321:'Girl 3',322:'Man 1',323:'Man 2',324:'Man 3',
    325:'Woman 1',326:'Woman 2',327:'Woman 3',328:'Middle-aged man',
    329:'Middle-aged woman',330:'Older man',331:'Older woman',332:'Large man',
    333:'Hiker',334:'Shopkeeper',338:'Scientist',341:'Gentleman',345:'Camper',346:'Picnicker',348:'Sailor'}


def palette(project):
    """Only expose named appearances actually present in this immutable ROM."""
    if not hasattr(project,'_npc_palette'):
        import ndspy.narc
        from . import world,event_authoring
        from .formats import file_span,digest,EditorError
        archive=ndspy.narc.NARC(file_span(project.blob,world.EVENT_ARCHIVE)[1])
        found={}
        for member,raw in enumerate(archive.files):
            # Some unused/stock members have duplicate local IDs. They cannot be
            # appearance donors under our existing event qualification rules.
            try:rows=event_authoring.records(raw)
            except EditorError:continue
            for r in rows:
                if r['kind']=='npc' and r['sprite'] in APPEARANCES and r['sprite'] not in found:
                    found[r['sprite']]={'sprite':r['sprite'],'name':APPEARANCES[r['sprite']],
                        'event_member':member,'npc_id':r['id'],'sha256':digest(r['raw'])}
        project._npc_palette=[found[k] for k in sorted(found)]
    return project._npc_palette


def validate_fields(s):
    movement=s.get('movement',0);rx=s.get('range_x',0);rz=s.get('range_z',0)
    require(type(movement) is int and movement in BEHAVIORS,'Choose a supported NPC behavior','INVALID_INPUT')
    require(all(type(v) is int and 0<=v<=8 for v in (rx,rz)),'Movement ranges must be 0..8 tiles','INVALID_INPUT')
    if movement in (0,2):require(rx==rz==0,'Standing or turning NPCs have zero movement range','INVALID_INPUT')
    elif movement==4:require(rx==0 and rz>0,'North/south walking needs a positive Z range and zero X range','INVALID_INPUT')
    elif movement==5:require(rz==0 and rx>0,'West/east walking needs a positive X range and zero Z range','INVALID_INPUT')
    else:require(rx>0 and rz>0,'Wandering needs a positive range on both axes','INVALID_INPUT')


def validate_area(project,context,s,state,rows,*,height_at=None):
    from . import world,scenery
    validate_fields(s)
    height_at=height_at or scenery.floor_height
    height=height_at(project,context,s)
    raw=project.member_raw(context['map_member'])
    for x in range(s['x']-s.get('range_x',0),s['x']+s.get('range_x',0)+1):
        for z in range(s['z']-s.get('range_z',0),s['z']+s.get('range_z',0)+1):
            offset=world.cell_offset(context,x,z)
            pair=state['permissions'].get((context['map_member'],offset),raw[offset:offset+2])
            require(not world.is_blocked(pair) and pair[0] in (0,2),
                    f'NPC movement area reaches blocked or special terrain at {x},{z}','BLOCKED_TILE')
            require(height_at(project,context,{'x':x,'z':z})==height,
                    f'NPC movement area changes height at {x},{z}','UNSUPPORTED_HEIGHT')
            for r in rows:
                if r['kind']=='npc':
                    if r['id']==s['npc_id']:continue
                    conflict=abs(r['x']-x)<=max(0,r['range_x']) and abs(r['z']-z)<=max(0,r['range_z'])
                elif r['kind']=='warp':conflict=r['x']==x and r['z']<=z<=r['z']+1
                elif r['kind']=='trigger':conflict=r['x']<=x<r['x']+r['width'] and r['z']<=z<r['z']+r['height']
                elif r['kind']=='background':conflict=(r['x'],r['z'])==(x,z)
                else:continue
                require(not conflict,f'NPC movement area overlaps {r["kind"]} {r["id"]} at {x},{z}','EVENT_CONFLICT')
