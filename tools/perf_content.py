"""Mac timing of the original-content-v1 operations (PERF-01), companion to perf_contract.py.

Measures, on a scratch CLONE of the successor project (it applies and undoes edits), a representative
content batch built from public operations: an environment terrace placement (terrace + cobble
paving), a six-tree grove placement and four ground decals on the new raised top, in a created copy of
a Cherrygrove cell (set up first, not timed; anchors chosen by the planner). Targets are the
contract's: batch preview 10 s / apply 15 s, undo/redo 5 s; three samples, median at target and
max <= 2x.

Usage: perf_content.py CLONE OUT.json
"""
import json
import platform
import resource
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TARGETS = {'content_preview': 10, 'content_apply': 15, 'content_undo': 5, 'content_redo': 5}
TOWN = {'header': 67, 'cell': [17, 12]}


def summary(samples, target):
    out = {'samples': [round(s, 2) for s in samples], 'median': round(statistics.median(samples), 2),
           'max': round(max(samples), 2), 'target': target}
    out.update(median_ok=out['median'] <= target, max_ok=out['max'] <= 2 * target)
    return out


def timed(call):
    t = time.time()
    call()
    return time.time() - t


def setup(p):
    """A created copy of Cherrygrove's town cell with a door to an interior (terraces need reachability)."""
    from sovereign_editor import world_authoring as wa
    p.apply_area_edit(p.doc['revision'], label='perf setup', operations=[
        {'kind': 'world', 'context': TOWN, 'request': {
            'action': 'create', 'identity': 'perf_town', 'name': 'Perf Town', 'internal_name': 'PERF_TOWN',
            'template_header': 67, 'encounters': 'none', 'worldmap': [17, 12], 'cells': [{'cell': [0, 0], 'source': TOWN}]}},
        {'kind': 'world', 'context': {'header': 72, 'cell': [0, 0]}, 'request': {
            'action': 'create', 'identity': 'perf_room', 'name': 'Perf Room', 'internal_name': 'PERF_ROOM',
            'template_header': 72, 'cells': [{'cell': [0, 0], 'source': {'header': 72, 'cell': [0, 0]}}],
            'encounters': 'none', 'worldmap': [17, 12], 'close': True}}])
    h = {a['identity']: a['header'] for a in wa.areas(p.composed()).values()}
    p.apply_area_edit(p.doc['revision'], label='perf door', operations=[
        {'kind': 'world', 'context': {'header': h['perf_town'], 'cell': [0, 0]}, 'request': {
            'action': 'connect', 'x': 3, 'z': 15, 'destination': {'header': h['perf_room']}, 'arrival': {'x': 4, 'z': 8}}}])
    return {'header': h['perf_town'], 'cell': [0, 0]}


def fit(p, ctx, preset, candidates, **extra):
    """First anchor the planner accepts for a preset (untimed setup)."""
    from sovereign_editor.formats import EditorError
    for x, z in candidates:
        try:
            p.plan_area_edit([{'kind': 'environment', 'context': ctx, 'request': {
                'action': 'place', 'environment': 'cg_town', 'preset': preset, 'x': x, 'z': z, **extra}}])
            return x, z
        except EditorError:
            continue
    raise SystemExit(f'no place for {preset}')


def batch(ctx, i, spots):
    env = lambda **r: {'kind': 'environment', 'context': ctx, 'request': {'action': 'place', 'environment': 'cg_town', **r}}
    decal = lambda k, name, x, z: {'kind': 'decal', 'context': ctx, 'request': {
        'action': 'place', 'key': f'pf{i}_{k}', 'material': 'petals', 'decal': name, 'x': x, 'z': z}}
    (tx, tz), (gx, gz) = spots
    return [env(preset='small_terrace', x=tx, z=tz),
            env(preset='grove', x=gx, z=gz, key=f'pg{i}'),
            # decals on the new terrace's paved top (the preset's own paving is the border work)
            decal('a', 'pilem', tx + 1.0, tz + 1.0), decal('b', 'sc1', tx + 3.5, tz + 1.5),
            decal('c', 'sc2', tx + 3.0, tz + 1.0), decal('d', 'sc3', tx + 2.0, tz + 0.5)]


def main():
    from sovereign_editor.core import Project
    root, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    p = Project(root)
    ctx = setup(p)
    terrace = fit(p, ctx, 'small_terrace', [(x, z) for z in (16, 17, 18) for x in range(5, 12)])
    grove = fit(p, ctx, 'grove', [(x, z) for z in range(8, 21) for x in range(1, 24)],
                key='probe')
    spots = (terrace, grove)
    report = {'project': str(root), 'machine': platform.machine(), 'platform': platform.platform(),
              'python': platform.python_version(), 'operations': {}}
    ops = report['operations']
    report['requests_per_batch'] = len(batch(ctx, 0, spots))
    report['anchors'] = {'small_terrace': terrace, 'grove': grove}
    plans = []
    for i in range(3):
        b = batch(ctx, i, spots)
        t = time.time(); plan = p.plan_area_edit(b); plans.append(time.time() - t)
        report['transactions_per_batch'] = len(plan['transactions'])
    ops['content_preview'] = summary(plans, TARGETS['content_preview'])
    applies, undos, redos = [], [], []
    for i in range(3):
        b = batch(ctx, i, spots)
        applies.append(timed(lambda b=b: p.apply_area_edit(p.doc['revision'], operations=b, label='perf content')))
        undos.append(timed(lambda: p.undo(p.doc['revision'])))
        redos.append(timed(lambda: p.redo(p.doc['revision'])))
        undos.append(timed(lambda: p.undo(p.doc['revision'])))
    ops['content_apply'] = summary(applies, TARGETS['content_apply'])
    ops['content_undo'] = summary(undos, TARGETS['content_undo'])
    ops['content_redo'] = summary(redos, TARGETS['content_redo'])
    report['rss_mb_this_process'] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 1)
    report['revision'] = p.doc['revision']
    report['misses'] = [k for k, v in ops.items() if not (v['median_ok'] and v['max_ok'])]
    out.write_text(json.dumps(report, indent=1) + '\n')
    print(json.dumps({'misses': report['misses'], **{k: (v['median'], v['max'], v['target']) for k, v in ops.items()}}))


if __name__ == '__main__':
    main()
