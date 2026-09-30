"""PROD-03: measured capacity against the production targets (read-only).

Usage: capacity_measure.py OUT.json PROJECT [PROJECT...]

For each project: library/resident pools (capacity.report), consolidated runtime residency
(field overlay 131, resident overlay 129, boot data, battle overlay 130 lifetime) and per-map
buffers of every authored map (event file vs the 0x800 buffer, local messages vs 255, map
model bytes, placed objects vs 32). Targets are compared with the qualified limits; a target
above a limit is reported as unmet with its measured cause. Software evidence only.
"""
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TARGETS = {'authored_areas': 64, 'largest_area_cells': 16, 'trainer_teams': 256, 'characters': 64,
           'number_states': 128}
CAUSES = {
    'trainer_teams': 'authored trainer IDs 738..801: trainer defeat flags 0x550+ID end at 0x871, where the '
                     'on/off state region (0x872..0x95F) begins; more IDs need a qualified flag relocation',
    'characters': 'character classes 129..160 / back groups 17..48 were qualified for 32 library slots '
                  '(class-indexed tables and hooks); 64 needs the same audit for classes 161..192',
    'number_states': 'the stock save has 368 variables; exactly 60 have no script/code/save consumer. More '
                     'number states need a save-format extension (breaks existing saves) or packed small '
                     'ranges on flags',
}


def per_map(project, state):
    from sovereign_editor import dialogue_format as fmt, event_authoring as ev, scenery, world, world_authoring
    rows = []
    headers = set(project._world_headers)
    headers |= {c['header']['id'] for c in state.get('contexts', []) if isinstance(c, dict) and 'header' in c}
    for h in sorted(headers):
        head = project.header(h)
        events = ev.raw_member(project, head['event_file'], state)
        try:
            texts = len(fmt.text_entries(project.resource(fmt.TEXT_ARCHIVE, head['text_archive'])[1])[1])
        except Exception:
            texts = None
        rows.append({'header': h, 'name': head['name'], 'event_bytes': len(events), 'event_limit': world_authoring.EVENT_LIMIT,
                     'messages': texts, 'message_limit': 255})
    return rows


def measure(path):
    from sovereign_editor.core import Project
    from sovereign_editor import capacity, world_authoring
    p = Project(path)
    state = p.composed()
    report = capacity.report(p)
    runtime = capacity.runtime_report(p)
    areas = world_authoring.areas(state)
    lim = report['limits']
    measured = {'authored_areas': len(areas), 'largest_area_cells': max((len(a['cells']) for a in areas.values()), default=0),
                'trainer_teams': lim['trainers']['used'], 'characters': lim['characters']['used'],
                'number_states': lim['number_states']['used']}
    limits = {'authored_areas': lim['created_headers']['limit'], 'largest_area_cells': lim['area_cells']['limit'],
              'trainer_teams': lim['trainers']['limit'], 'characters': lim['characters']['limit'],
              'number_states': lim['number_states']['limit']}
    targets = {k: {'target': v, 'qualified_limit': limits[k], 'measured_in_project': measured[k],
                   'status': 'met' if limits[k] >= v else 'unmet', **({'cause': CAUSES[k]} if limits[k] < v else {})}
               for k, v in TARGETS.items()}
    maps = per_map(p, state)
    worst = {'event_bytes': max((m['event_bytes'] for m in maps), default=0),
             'messages': max((m['messages'] or 0 for m in maps), default=0)}
    return {'project': str(path), 'name': p.doc.get('name'), 'revision': p.doc['revision'], 'targets': targets,
            'pools': {k: {kk: vv for kk, vv in v.items() if kk != 'refusal'} for k, v in lim.items()},
            'residency': {'overlay_129': {k: runtime['overlay_129'].get(k) for k in ('layout', 'bytes', 'free_bytes', 'limit')},
                          'overlay_131': {k: runtime['overlay_131'][k] for k in ('bytes', 'free_bytes', 'limit')},
                          'lifecycle': {'overlay_129': 'resident from boot (field, battle, blackout, reload)',
                                        'boot_data': 'read at every boot into 0x023D6260..0x023D8000; untouched by '
                                                     'field, battle, blackout and reload',
                                        'overlay_131': 'field only; the battle overlay 130 replaces it and the field '
                                                       'reloads it on return (followers, shop lists, collection code)'}},
            'per_map_worst': worst, 'per_map': maps}


def main(out, *projects):
    result = {'schema': 'sovereign-capacity-measure-v1', 'targets': TARGETS,
              'projects': [measure(Path(p)) for p in projects],
              'scope': 'software/readback measurement; native timing and memory are separate'}
    Path(out).write_text(json.dumps(result, indent=1))
    for r in result['projects']:
        print(r['name'], {k: (v['measured_in_project'], v['qualified_limit'], v['status']) for k, v in r['targets'].items()},
              r['residency']['overlay_129'], r['per_map_worst'])


if __name__ == '__main__':
    main(*sys.argv[1:])
