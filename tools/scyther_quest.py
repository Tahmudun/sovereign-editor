"""The injured Scyther quest, expressed as ordinary Project story operations.

No ROM, save or project writes occur here. Preview/apply use core.Project.
"""
import copy

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
        if scyther:value['stock_sprite']=552
        put('sequence', key, value, header)

    def trigger(key, header, xy, size, value, nodes, donor=0):
        put('sequence',key,dict(kind='trigger',x=xy[0],z=xy[1],donor_id=donor,
            facing=1,movement=0,range_x=0,range_z=0,character=None,nodes=nodes,once_state=None,
            trigger={'state':STATE,'value':value,'width':size[0],'height':size[1]}),header)

    put('state',STATE,{'name':'Injured Scyther quest stage'})
    put('state',MEDICINE,{'name':'Scyther medicine received'})
    practice=copy.deepcopy(library['sequences']['tiana_practice'])
    for k in ('context','event_member','script_member','text_member','y','npc_id','hide_flag'):
        practice.pop(k,None)
    practice.update(movement=0,range_x=0,range_z=0,presence={'state':STATE,'values':[0,8]})
    put('sequence','tiana_practice',practice)
    trigger('room_welcome',72,(4,7),(1,1),0,[
        react('notice','tiana_practice','gather'), gather([8,6],'intro'),
        say('intro',["The residents told me about\nan injured Scyther.",
            "It comes here to gaze\nat the sea.","A Scyther?!\nWe have to help it!"],'leave'),
        move('leave','tiana_practice',[[8,5],[7,5],[7,7],[4,7]],'search'),
        stage('search',1),sync(),end()])
    hint="Didn't they say it was\n[hint]gazing at the sea[/hint]?"
    actor('tiana_search',67,(550,404),[1],[say('hint',hint),end()])
    actor('scyther_beach',67,(541,400),[1,2],[say('watch','Scyther gazes at the sea.\nIts wing looks hurt.'),end()],scyther=True,facing=2)
    actor('tiana_beach',67,(544,402),[2],[say('quiet',"Let's approach quietly.\nWe don't want to scare it."),end()])
    actor('scyther_town',67,(553,402),[3],[say('watch','Scyther looks ready to flee.'),end()],scyther=True)
    actor('tiana_town',67,(554,403),[3],[say('follow','There it is!\nPlease be careful.'),end()])
    trigger('beach_find',67,(541,397),(5,7),1,[gather([544,401],'approach'),
        move('approach','tiana_search',[[550,404],[544,404],[544,402]],'found'),
        say('found',"You found it!\nLet's approach it quietly...",'closer'),stage('closer',2),sync(),end()])
    trigger('beach_flee',67,(541,398),(3,4),2,[gather([543,400],'startle'),
        react('startle','scyther_beach','flee'),
        move('flee','scyther_beach',[[541,400],[541,401],[553,401],[553,402]],'follow'),
        move('follow','tiana_beach',[[544,402],[552,402],[552,403],[554,403]],'town'),
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
    return result
