"""Disposable Route 30 fixture composed through Project, never raw ROM writes."""
from sovereign_editor import gameplay


def operations(project):
    data = project.gameplay_data()
    mikey = next(t for t in data['trainers'] if t['id'] == 47)
    edits = [{'method':'grass','field':'rate','value':20}]
    levels = [3,3,4,4,3,4,3,4,4,4,5,5]
    day = [16,161,16,161,19,19,161,16,19,161,16,19]
    night = [163,19,163,19,161,161,19,163,161,19,163,161]
    for i, level in enumerate(levels):
        edits.append({'method':'grass','field':'level','slot':i,'value':level})
    for time, species in [('morning',day),('day',day),('night',night)]:
        for i, value in enumerate(species):
            edits.append({'method':'grass','field':'species','time':time,'slot':i,'value':value})
    request = {'operations':[
        {'kind':'trainer','id':47,'before_sha256':mikey['before_sha256'],'party':[
            {'species':16,'level':4,'moves':[16,33,28,0],'held_item':0},
            {'species':19,'level':5,'moves':[98,33,39,0],'held_item':155}]},
        {'kind':'encounters','before_sha256':data['encounters']['before_sha256'],'edits':edits}]}
    context = {'header':34,'cell':[17,10]}
    return [
        {'kind':'event','context':context,'request':{'kind':'npc','event_id':5,'values':{'x':549,'z':348},
          'label':'Mikey: accessible Route 30 gameplay fixture'}},
        {'kind':'gameplay','context':context,'request':request}]
