"""Build the approved encounter as ordinary, reviewable Project operations."""
import json
from pathlib import Path

CONTEXT = {'header':72, 'cell':[0,0]}


def operations(package):
    result = []
    def put(kind, key, value):
        result.append({'kind':'story', 'context':CONTEXT, 'request':{'kind':kind, 'key':key, 'value':value}})
    put('character', 'tiana', package)
    for key, name in [('room_welcome','Room welcome seen'), ('tiana_progress','Tiana practice progress')]:
        put('state', key, {'name':name})
    def trainer(key, name, character, stock, party, before, after):
        put('trainer',key,{'name':name,'character':character,'stock_class':stock,
            'party':[{'species':s,'level':l} for s,l in party], 'before':before,'after':after})
    trainer('tiana','Tiana','tiana',None,[(152,5)],['Ready for a friendly spar?\nI will heal your team.'],['Good practice!\nYour team is healed.'])
    trainer('practice_ari','Ari',None,2,[(19,3)],['Let us practice together!'],['Thanks for the team battle!'])
    trainer('practice_bea','Bea',None,3,[(16,3)],['Let us do our best!'],['That was fun!'])
    base = dict(donor_id=0,facing=1,movement=0,range_x=0,range_z=0)
    welcome = [{'id':'hello','op':'say','pages':['Welcome to our\npractice room.','Tiana is on the right.\nTalk to her when ready.'],'next':'end'}, {'id':'end','op':'end'}]
    put('sequence','room_welcome',{**base,'kind':'trigger','x':4,'z':7,'character':None,'nodes':welcome,'once_state':'room_welcome'})
    nodes = [
        {'id':'progress','op':'if','state':'tiana_progress','value':0,'yes':'meet','no':'return'},
        {'id':'meet','op':'say','pages':['Hi! I am Tiana.\nWelcome to our room.','We can spar, then team up\nfor a practice battle.'],'next':'spar_choice'},
        {'id':'spar_choice','op':'choice','pages':['Try a friendly spar?'],'yes':'spar','no':'end'},
        {'id':'spar','op':'battle','trainer':'tiana','partner':None,'opponent2':None,'won':'spar_done','lost':'spar_done'},
        {'id':'spar_done','op':'set','state':'tiana_progress','value':1,'next':'team_choice'},
        {'id':'team_choice','op':'choice','pages':['Team up with me now?'],'yes':'team','no':'end'},
        {'id':'team','op':'battle','trainer':'practice_ari','partner':'tiana','opponent2':'practice_bea','won':'team_done','lost':'team_done'},
        {'id':'team_done','op':'set','state':'tiana_progress','value':2,'next':'complete'},
        {'id':'complete','op':'say','pages':['We make a great team!','Talk to me to replay either\nbattle whenever you like.'],'next':'end'},
        {'id':'return','op':'say','pages':['Welcome back!\nI remember our practice.'],'next':'replay_team'},
        {'id':'replay_team','op':'choice','pages':['Replay the team battle?'],'yes':'team','no':'spar_choice'},
        {'id':'end','op':'end'}]
    put('sequence','tiana_practice',{**base,'kind':'npc','x':8,'z':5,'movement':5,'range_x':1,'character':'tiana','nodes':nodes,'once_state':None})
    return result


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--package',type=Path,required=True);parser.add_argument('--request',type=Path,required=True)
    args=parser.parse_args()
    args.request.write_text(json.dumps({'operations':operations(json.loads(args.package.read_text())),'label':'Tiana practice room'},indent=2)+'\n')
