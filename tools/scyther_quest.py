"""The injured Scyther quest, expressed as ordinary Project story operations.

No ROM, save or project writes occur here. Preview/apply use core.Project.
"""
STATE = 'scyther_quest'
MEDICINE = 'scyther_medicine'
POTION = 17


def operations(library):
    result = []

    def put(kind, key, value, header=72):
        cell = [value['x']//32, value['z']//32] if kind == 'sequence' else [0, 0]
        result.append({'kind':'story', 'context':{'header':header, 'cell':cell},
                       'request':{'kind':kind, 'key':key, 'value':value}})

    def say(key, text, next='end'):
        return dict(id=key, op='say', pages=[text] if isinstance(text,str) else text, next=next)

    def stage(key, value, next='sync'):
        return dict(id=key, op='set', state=STATE, value=value, next=next)

    def move(key, actor, path, next, speed='run'):
        return dict(id=key, op='move', actor=actor, path=path, speed=speed, next=next)

    def react(key, actor, next, reaction='exclamation'):
        return dict(id=key, op='reaction', actor=actor, reaction=reaction, next=next)

    def gather(destination, next):
        return dict(id='gather', op='gather', destination=destination, next=next)

    def end(): return dict(id='end', op='end')
    def sync(): return dict(id='sync', op='sync', next='end')

    def actor(key, header, xy, values, nodes, *, scyther=False, facing=1, donor=0):
        value=dict(kind='npc', x=xy[0], z=xy[1], donor_id=donor, facing=facing,
                   movement=0, range_x=0, range_z=0, character=None if scyther else 'tiana',
                   nodes=nodes, once_state=None, presence={'state':STATE,'values':values})
        if scyther:value['stock_sprite']=597   # actual Scyther binding (tag 552 shows a Seel here)
        put('sequence', key, value, header)

    def trigger(key, header, xy, size, value, nodes, donor=0):
        put('sequence',key,dict(kind='trigger',x=xy[0],z=xy[1],donor_id=donor,
            facing=1,movement=0,range_x=0,range_z=0,character=None,nodes=nodes,once_state=None,
            trigger={'state':STATE,'value':value,'width':size[0],'height':size[1]}),header)

    put('state',STATE,{'name':'Injured Scyther quest stage'})
    put('state',MEDICINE,{'name':'Scyther medicine received'})
    # Retain the trainer/character library and state identities, but remove every
    # playable practice event. The eastern house has a fresh stationary actor.
    for key in ('room_welcome','tiana_practice'):
        if key in library['sequences']:
            result.append({'kind':'story','context':{'header':72,'cell':[0,0]},
                           'request':{'kind':'sequence','key':key,'action':'delete'}})
    actor('tiana_house',71,(2,6),[0,8],[
        say('thanks',"I'm glad Scyther found\na friend in you!"),end()],facing=3)
    put('sequence','house_entry',dict(kind='entry',x=4,z=8,donor_id=0,
        facing=0,movement=0,range_x=0,range_z=0,character=None,once_state=None,
        trigger={'state':STATE,'value':0,'advance':1},nodes=[
        dict(id='gather',op='gather',destination=[5,8],next='notice',no='late'),
        react('notice','tiana_house','intro'),
        say('intro',["The residents told me about\nan injured Scyther.",
            "It comes here to gaze\nat the sea.","A Scyther?!\nWe have to help it!"],'leave'),
        move('leave','tiana_house',[[2,6],[2,7],[4,7]],'door'),
        dict(id='door',op='face',actor='tiana_house',direction=1,next='search'),
        say('late',"Tiana went to the sea\nto find an injured Scyther.",'search'),
        stage('search',1),sync(),end()]),71)
    hint="Didn't they say it was\n[hint]gazing at the sea[/hint]?"
    actor('tiana_search',67,(550,404),[1],[say('hint',hint),end()])
    actor('scyther_beach',67,(541,400),[1,2],[say('watch','Scyther gazes at the sea.\nIts wing looks hurt.'),end()],scyther=True,facing=2)
    actor('tiana_beach',67,(545,403),[2],[say('quiet',"Let's approach quietly.\nWe don't want to scare it."),end()])
    actor('scyther_town',67,(553,402),[3],[say('watch','Scyther looks ready to flee.'),end()],scyther=True)
    actor('tiana_town',67,(554,403),[3],[say('follow','There it is!\nPlease be careful.'),end()])
    trigger('beach_find',67,(541,397),(5,7),1,[gather([544,401],'approach'),
        move('approach','tiana_search',[[550,404],[545,404],[545,403]],'found'),
        say('found',"You found it!\nLet's approach it quietly...",'closer'),stage('closer',2),sync(),end()])
    trigger('beach_flee',67,(541,398),(3,4),2,[gather([543,399],'startle'),
        react('startle','scyther_beach','flee'),
        move('flee','scyther_beach',[[541,400],[541,401],[553,401],[553,402]],'follow'),
        move('follow','tiana_beach',[[545,403],[545,402],[552,402],[552,403],[554,403]],'town'),
        stage('town',3),sync(),end()])
    trigger('town_flee',67,(551,401),(5,4),3,[gather([551,402],'startle'),
        react('startle','scyther_town','flee'),
        move('flee','scyther_town',[[553,402],[558,402],[558,401],[560,401],[560,402],[561,402],[561,403],[564,403],[564,402],[565,402],[565,400],[573,400]],'follow'),
        move('follow','tiana_town',[[554,403],[555,403],[555,404],[564,404],[564,402],[565,402],[565,400],[572,400]],'road'),
        stage('road',4),sync(),end()])
    actor('scyther_road',33,(580,400),[4],[say('watch','Scyther is breathing\nheavily.'),end()],scyther=True,donor=2)
    actor('tiana_road',33,(579,402),[4],[say('follow','It went east!\nIts wing must really hurt.'),end()],donor=2)
    trigger('road_flee',33,(577,398),(5,5),4,[gather([578,400],'startle'),
        react('startle','scyther_road','flee'),
        move('flee','scyther_road',[[580,400],[587,400],[587,402],[592,402]],'follow'),
        move('follow','tiana_road',[[579,402],[587,402],[591,402]],'corner'),
        stage('corner',5),sync(),end()],donor=2)

    # Both branches advance the approach stage. A full bag never retriggers it.
    medicine=[dict(id='give',op='give_item',item=POTION,count=1,yes='given',no='full'),
        dict(id='given',op='set',state=MEDICINE,value=1,next='received'),
        dict(id='received',op='sound',sound='receive',next='explain'),
        say('explain','You received a Potion!\nGive it to Scyther.'),
        say('full','Your Bag has no room.\nMake space and talk to me.'),end()]
    actor('tiana_corner',33,(621,407),[5,6,7],[
        dict(id='healed',op='if',state=STATE,value=7,yes='trust',no='waiting'),
        say('trust','It trusts you now.\nWill you take it along?'),
        dict(id='waiting',op='if',state=STATE,value=5,yes='look',no='already'),
        say('look','Scyther looks exhausted...'),
        dict(id='already',op='if',state=MEDICINE,value=1,yes='replace',no='give'),
        say('replace','Used the Potion?\nThe Mart sells replacements.'),*medicine],donor=0)
    actor('scyther_corner',33,(619,406),[5,6,7],[
        dict(id='waiting',op='if',state=STATE,value=5,yes='cautious',no='healed'),
        say('cautious','Scyther is watching\nyou cautiously.'),
        dict(id='healed',op='if',state=STATE,value=7,yes='join',no='offer'),
        dict(id='offer',op='choice',pages=['Give Scyther a Potion?'],yes='has',no='end'),
        dict(id='has',op='has_item',item=POTION,count=1,yes='take',no='missing'),
        dict(id='take',op='take_item',item=POTION,count=1,yes='heal',no='missing'),
        say('missing','You need a Potion.\nAsk Tiana or visit the Mart.'),
        stage('heal',7,'sound'),dict(id='sound',op='sound',sound='heal',next='better'),
        say('better','Scyther is all better!','happy'),react('happy','scyther_corner','trust','happy'),
        say('trust',["It seems to trust you.","Scyther wants to join\nyour team!"],'join'),
        dict(id='join',op='choice',pages=['Invite Scyther along?'],yes='recruit',no='later'),
        say('later','Scyther will wait here\nuntil you are ready.'),
        dict(id='recruit',op='give_mon',species=123,level=8,yes='complete',no='full'),
        say('full','Your party is full.\nMake room and come back.'),stage('complete',8,'welcome'),
        say('welcome',['Scyther joined your team!',"Tiana: Let's head back\nto Cherrygrove!"],'sync'),sync(),end()],scyther=True,donor=0)
    trigger('corner_find',33,(618,406),(4,3),5,[gather([620,407],'look'),
        say('look',["Scyther is watching\nyou cautiously.","Tiana: It's badly hurt.\nIt can't go any farther.",
                    "A Potion should help."],'medicine'),stage('medicine',6,'give'),*medicine],donor=0)
    _stage_scenes(result)
    return result


def _stage_scenes(operations):
    """Reviewed beat sheet. All coordinates are composed-map X/Z tiles.

    The returned operations are the portable JSON input to Project/area-edit;
    these helpers only reduce repetition and never write project files.
    """
    import copy
    # Medicine branches occur in two scenes; each beat sheet owns its nodes.
    for o in operations:o['request']['value']=copy.deepcopy(o['request'].get('value'))
    scenes={o['request']['key']:o['request']['value'] for o in operations
            if o['request']['kind']=='sequence' and o['request'].get('action')!='delete'}
    def node(key,id):return next(n for n in scenes[key]['nodes'] if n['id']==id)
    def before(key,target,steps):
        nodes=scenes[key]['nodes'];first=steps[0]['id']
        for n in nodes:
            for field in ('next','yes','no','won','lost'):
                if n.get(field)==target:n[field]=first
        for a,b in zip(steps,steps[1:]):a['next']=b['id']
        steps[-1]['next']=target
        at=next(i for i,n in enumerate(nodes) if n['id']==target)
        nodes[at:at]=steps
    def look(id,actor,target,axis='x'):return dict(id=id,op='look',actor=actor,target=target,axis=axis)
    def face(id,actor,d):return dict(id=id,op='face',actor=actor,direction=d)
    def pose(id,**actors):return dict(id=id,op='pose',actors={k:{'tile':list(v[:2]),'facing':v[2]} for k,v in actors.items()})
    def pause(id,frames=12):return dict(id=id,op='wait',frames=frames)
    def move(id,actor,path,speed='walk'):return dict(id=id,op='move',actor=actor,path=path,speed=speed)

    # Stand immediately north of Tiana. The follower is east of the player;
    # Tiana's south/east doorway route stays clear of both.
    node('house_entry','gather')['destination']=[2,5]
    before('house_entry','notice',[look('player_greeting','player','tiana_house','z'),
        look('tiana_greeting','tiana_house','player','z'),
        pose('greeting_pose',player=(2,5,1),tiana_house=(2,6,0))])
    node('house_entry','leave')['speed']='walk'
    before('house_entry','search',[face('face_door','tiana_house',1),
        pose('door_pose',tiana_house=(4,7,1)),pause('door_pause',20)])
    # The loaded-room fallback must not run any spatial beat with unknown positions.
    node('house_entry','late')['next']='search'

    scenes['tiana_beach']['facing']=2
    scenes['tiana_town']['facing']=0
    scenes['scyther_town']['facing']=2
    before('beach_find','found',[look('player_listens','player','tiana_search','z'),
        look('tiana_speaks','tiana_search','player','z'),
        pose('conversation_pose',player=(544,401,1),tiana_search=(545,403,0))])
    before('beach_find','closer',[look('player_spots','player','scyther_beach'),
        look('tiana_spots','tiana_search','scyther_beach'),
        pose('watching_pose',player=(544,401,2),tiana_search=(545,403,2)),pause('watch_pause')])
    before('beach_find','end',[pose('handoff_pose',player=(544,401,2),tiana_beach=(545,403,2))])

    before('beach_flee','startle',[look('player_watches','player','scyther_beach'),
        look('tiana_watches','tiana_beach','scyther_beach'),
        look('scyther_notices','scyther_beach','player'),
        pose('startle_pose',player=(543,399,2),tiana_beach=(545,403,2),scyther_beach=(541,400,3)),pause('notice_pause')])
    node('beach_flee','flee')['path']=[[541,400],[541,401],[543,401]]
    before('beach_flee','follow',[look('player_tracks','player','scyther_beach','z'),
        look('tiana_tracks','tiana_beach','scyther_beach'),pause('passing_pause',6),
        move('passes','scyther_beach',[[543,401],[547,401]],'run'),
        look('player_turns','player','scyther_beach'),look('tiana_turns','tiana_beach','scyther_beach'),
        pose('passed_pose',player=(543,399,3),tiana_beach=(545,403,3)),
        move('runs_to_town','scyther_beach',[[547,401],[553,401],[553,402]],'run')])
    before('beach_flee','town',[face('scyther_waits','scyther_beach',2),face('tiana_waits','tiana_beach',0)])
    before('beach_flee','end',[pose('town_handoff',tiana_town=(554,403,0),scyther_town=(553,402,2))])

    before('town_flee','startle',[look('player_watches','player','scyther_town'),
        look('tiana_watches','tiana_town','scyther_town','z'),look('scyther_notices','scyther_town','player'),
        pose('startle_pose',player=(551,402,3),tiana_town=(554,403,0),scyther_town=(553,402,2)),pause('notice_pause')])
    before('town_flee','flee',[face('tiana_tracks','tiana_town',3)])

    # Map load occurs near x=576. Start >=589, outside its initial camera view.
    scenes['scyther_road'].update(x=590,z=402,facing=2)
    scenes['tiana_road'].update(x=589,z=403,facing=0)
    scenes['road_flee'].update(x=584,z=400)
    node('road_flee','gather')['destination']=[587,402]
    node('road_flee','flee')['path']=[[590,402],[604,402]]
    node('road_flee','follow')['path']=[[589,403],[593,403],[593,402],[603,402]]
    before('road_flee','startle',[look('player_watches','player','scyther_road'),
        look('tiana_watches','tiana_road','scyther_road','z'),look('scyther_notices','scyther_road','player'),
        pose('startle_pose',player=(587,402,3),tiana_road=(589,403,0),scyther_road=(590,402,2)),pause('notice_pause')])
    before('road_flee','flee',[face('tiana_tracks','tiana_road',3)])
    before('road_flee','corner',[pose('offscreen_exit',player=(587,402,3),tiana_road=(603,402,3),scyther_road=(604,402,3))])

    scenes['tiana_corner']['facing']=2
    before('corner_find','look',[look('player_observes','player','scyther_corner','z'),
        look('tiana_observes','tiana_corner','scyther_corner'),
        pose('observation_pose',player=(620,407,0),tiana_corner=(621,407,2)),pause('observation_pause')])
    before('corner_find','give',[look('player_receives','player','tiana_corner'),look('tiana_gives','tiana_corner','player'),
        pose('medicine_pose',player=(620,407,3),tiana_corner=(621,407,2))])
    before('corner_find','end',[look('player_returns_attention','player','scyther_corner','z'),look('tiana_returns_attention','tiana_corner','scyther_corner')])

    # Talk can start on any reachable adjacent tile, including retry after a full
    # party. Normalize first so the farewell route has a known free corridor.
    before('scyther_corner','waiting',[dict(id='gather',op='gather',destination=[620,407]),
        look('player_greets','player','scyther_corner','z'),look('scyther_greets','scyther_corner','player','z'),
        look('tiana_watches','tiana_corner','scyther_corner'),
        pose('recruitment_pose',player=(620,407,0),tiana_corner=(621,407,2),scyther_corner=(619,406,1))])
    node('scyther_corner','recruit')['yes']='collect_scyther'
    scenes['scyther_corner']['nodes'].extend([
        dict(id='collect_scyther',op='collect',actor='scyther_corner',yes='complete',no='collection_recovery'),
        dict(id='collection_recovery',op='say',pages=['Scyther is safe with you.'],next='complete')])
    node('scyther_corner','complete')['value']=9  # joined; Tiana still present while leaving
    scenes['tiana_corner']['presence']['values'].append(9)
    node('scyther_corner','welcome')['pages']=['Scyther joined your team!',"Tiana: I'll head back home.\nThank you for helping!"]
    # Collection finishes before the recruited object is removed.
    before('scyther_corner','welcome',[dict(id='remove_recruited',op='sync'),
        look('tiana_farewell','tiana_corner','player'),look('player_farewell','player','tiana_corner'),
        pose('farewell_pose',player=(620,407,3),tiana_corner=(621,407,2))])
    before('scyther_corner','sync',[move('walk_home','tiana_corner',[[621,407],[621,408],[608,408]]),
        pose('homeward_exit',tiana_corner=(608,408,2)),dict(id='home',op='set',state=STATE,value=8)])
