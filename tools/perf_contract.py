"""Mac timing contract at the editor v1 chapter load (RELEASE-02). Wall-clock evidence on this machine.

Targets (PRODUCTION_AUTHORING_V2_IMPLEMENTATION.md): open/first view 10 s; warm inspection 2 s; small
preview 2 s / apply 5 s; mixed 20-operation preview 10 s / apply 15 s; undo 5 s / redo 5 s; export with
readback 45 s; each median at target and max ≤ 2× target; ≥3 samples. Run on a scratch CLONE of the chapter
project (it applies, undoes and redoes edits), with no other heavy job running.

Usage: perf_contract.py CLONE OUT.json [--skip-export]
"""
import json
import os
import platform
import resource
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PY = str(ROOT / '.venv/bin/python')
TARGETS = {'open_first_view': 10, 'warm_switch': 2, 'small_preview': 2, 'small_apply': 5, 'batch_preview': 10,
           'batch_apply': 15, 'undo_small': 5, 'redo_small': 5, 'undo_batch': 5, 'redo_batch': 5, 'export_readback': 45}
OPEN = """
import sys, time, json, resource
t = time.time()
sys.path.insert(0, {root!r})
from sovereign_editor.core import Project
p = Project({project!r})
header = next(a['header'] for a in p.world_areas()['areas'] if a['identity'] == 'ec_ridge')
view = p.map_view(header=header, cell=[0, 0])
lib = p.story_library()
print(json.dumps({{'seconds': time.time() - t, 'rss_mb': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20}}))
"""


def summary(samples, target=None):
    out = {'samples': [round(s, 2) for s in samples], 'median': round(statistics.median(samples), 2),
           'max': round(max(samples), 2)}
    if target:
        out.update(target=target, median_ok=out['median'] <= target, max_ok=out['max'] <= 2 * target)
    return out


def timed(call):
    t = time.time()
    call()
    return time.time() - t


def batch_ops(p, i):
    """A representative mixed batch: trainer, states, shop and record data, encounters, NPCs, carved cave."""
    areas = {a['identity']: a['header'] for a in p.world_areas()['areas']}
    ridge, cave = {'header': areas['ec_ridge'], 'cell': [0, 0]}, {'header': areas['ec_cave'], 'cell': [0, 0]}
    r29 = {'header': 33, 'cell': [19, 12]}
    from sovereign_editor import game_data as gd
    from sovereign_editor.formats import digest
    state = p.composed()
    sp = gd._members(p, state, gd.ITEMS)(26)
    enc = p.gameplay_data(areas['ec_cave'])['encounters']
    ops = [{'kind': 'story', 'context': r29, 'request': {'kind': 'trainer', 'key': 'ranger_05', 'action': 'put', 'value': {
        'name': 'Perf', 'character': None, 'stock_class': 4, 'party': [{'species': 16, 'level': 5 + i, 'moves': None,
                                                                          'held_item': 0}],
        'policy': 'ordinary-v2', 'before': ['Perf battle!'], 'after': ['Done.'], 'revisit': ['Again?'], 'defeat': ['Lost.']}}}]
    ops += [{'kind': 'story', 'context': r29, 'request': {'kind': 'state', 'key': f'perf_state_{k}', 'action': 'put',
                                                          'value': {'name': f'Perf state {k}', 'switch': True}}} for k in range(4)]
    ops.append({'kind': 'data', 'context': r29, 'request': {'operations': [
        {'kind': 'shop', 'name': 'perf_shop', 'before': None, 'items': [17, 18][: 1 + i % 2]},
        {'kind': 'item', 'id': 26, 'before_sha256': digest(sp), 'changes': {'price': 700 + 10 * (i + 1)}}], 'label': 'perf data'}})
    ops.append({'kind': 'gameplay', 'context': cave, 'request': {'operations': [
        {'kind': 'encounters', 'header': areas['ec_cave'], 'before_sha256': enc['before_sha256'],
         'edits': [{'method': 'grass', 'field': 'level', 'slot': 0, 'value': 6 + i}]}]}})
    for k in range(12):
        x, z = 15 + (k % 6), 9 + (k // 6)
        ops.append({'kind': 'story', 'context': ridge, 'request': {'kind': 'sequence', 'key': f'perf_npc_{k}', 'action': 'put',
                    'value': {'kind': 'npc', 'x': x, 'z': z, 'donor_id': None, 'facing': 1, 'movement': 0, 'range_x': 0,
                              'range_z': 0, 'character': None, 'stock_sprite': 322, 'once_state': None,
                              'nodes': [{'id': 'hi', 'op': 'say', 'pages': [f'Perf NPC {k} ({i})'], 'next': 'done'},
                                        {'id': 'done', 'op': 'end', 'complete': False}]}}})
    ops.append({'kind': 'elevation', 'context': cave, 'request': {'action': 'cave_room', 'rects': [[3, 17, 4 + i % 2, 4]],
                                                                  'exits': [], 'encounters': True, 'label': 'perf room'}})
    assert len(ops) == 20, len(ops)      # the contract's representative 20-operation mixed batch
    return ops


def main():
    root, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    import numpy
    from sovereign_editor import snapshots
    from sovereign_editor.core import Project
    report = {'project': str(root), 'machine': platform.machine(), 'platform': platform.platform(),
              'python': platform.python_version(), 'numpy': numpy.__version__, 'cache_dir': str(snapshots.directory()),
              'notes': ['wall-clock seconds on this Intel Mac; OS file cache not flushed; no other heavy job running'],
              'operations': {}}
    ops = report['operations']
    for label, env in (('open_first_view', os.environ), ('open_first_view_cold_cache', None)):
        runs = []
        for k in range(3):
            e = dict(os.environ)
            cold = Path(os.environ.get('PERF_SCRATCH', '/tmp')) / f'perf-cold-{k}'
            if env is None:
                shutil.rmtree(cold, ignore_errors=True)
                e['SOVEREIGN_EDITOR_CACHE'] = str(cold)
            r = subprocess.run([PY, '-c', OPEN.format(root=str(ROOT), project=str(root))], capture_output=True, text=True,
                               check=True, env=e)
            runs.append(json.loads(r.stdout.strip().splitlines()[-1]))
            shutil.rmtree(cold, ignore_errors=True)
        ops[label] = {**summary([r['seconds'] for r in runs], TARGETS['open_first_view']),
                      'rss_mb': round(max(r['rss_mb'] for r in runs), 1)}
    p = Project(root)
    areas = {a['identity']: a['header'] for a in p.world_areas()['areas']}
    views = ((33, [19, 12]), (areas['ec_ridge'], [0, 0]), (areas['ec_cave'], [0, 0]), (areas['ec_hut'], [0, 0]),
             (542, [3, 2]), (67, [17, 12]))
    for header, cell in views:
        p.map_view(header=header, cell=cell)
    ops['warm_switch'] = summary([timed(lambda h=h, c=c: p.map_view(header=h, cell=c)) for h, c in views],
                                 TARGETS['warm_switch'])
    healer = p.composed()['story']['sequence']['ec_healer']
    value = {k: healer[k] for k in ('kind', 'x', 'z', 'donor_id', 'facing', 'movement', 'range_x', 'range_z', 'character',
                                    'nodes', 'once_state', 'stock_sprite')}
    small = []
    for i in range(3):
        v = json.loads(json.dumps(value))
        v['nodes'][3]['pages'] = [f'All rested! ({i + 1})']
        small.append([{'kind': 'story', 'context': healer['context'] | {}, 'request': {
            'kind': 'sequence', 'key': 'ec_healer', 'action': 'put', 'value': v}}])
        small[-1][0]['context'] = {'header': healer['context']['header'], 'cell': healer['context']['cell']}
    ops['small_preview'] = summary([timed(lambda s=s: p.plan_area_edit(s)) for s in small], TARGETS['small_preview'])
    applies, undos, redos = [], [], []
    for s in small:
        applies.append(timed(lambda s=s: p.apply_area_edit(p.doc['revision'], operations=s, label='perf small')))
        undos.append(timed(lambda: p.undo(p.doc['revision'])))
        redos.append(timed(lambda: p.redo(p.doc['revision'])))
        undos.append(timed(lambda: p.undo(p.doc['revision'])))
    ops['small_apply'] = summary(applies, TARGETS['small_apply'])
    ops['undo_small'] = summary(undos, TARGETS['undo_small'])
    ops['redo_small'] = summary(redos, TARGETS['redo_small'])
    batches = [batch_ops(p, i) for i in range(3)]
    report['batch_operations'] = len(batches[0])
    ops['batch_preview'] = summary([timed(lambda b=b: p.plan_area_edit(b)) for b in batches], TARGETS['batch_preview'])
    applies, undos, redos = [], [], []
    for i in range(3):
        b = batch_ops(p, i)
        applies.append(timed(lambda b=b: p.apply_area_edit(p.doc['revision'], operations=b, label='perf batch')))
        undos.append(timed(lambda: p.undo(p.doc['revision'])))
        redos.append(timed(lambda: p.redo(p.doc['revision'])))
        undos.append(timed(lambda: p.undo(p.doc['revision'])))
    ops['batch_apply'] = summary(applies, TARGETS['batch_apply'])
    ops['undo_batch'] = summary(undos, TARGETS['undo_batch'])
    ops['redo_batch'] = summary(redos, TARGETS['redo_batch'])
    if '--skip-export' not in sys.argv:
        from sovereign_editor import world_readback
        scratch = Path(os.environ.get('PERF_SCRATCH', '/tmp')) / 'perf-export'
        samples, parts = [], []
        for _ in range(3):
            shutil.rmtree(scratch, ignore_errors=True)
            t0 = time.time()
            p.export(scratch, p.doc['revision'])
            t1 = time.time()
            check = world_readback.check((scratch / 'game.nds').read_bytes(), p)
            t2 = time.time()
            samples.append(t2 - t0)
            parts.append({'export': round(t1 - t0, 2), 'readback': round(t2 - t1, 2), 'passed': check['passed'],
                          'failed': check['failed'], 'rom_sha256': check['rom_sha256']})
        shutil.rmtree(scratch, ignore_errors=True)
        ops['export_readback'] = {**summary(samples, TARGETS['export_readback']), 'parts': parts}
    runs = [timed(lambda: subprocess.run([PY, '-m', 'sovereign_editor.cli', 'capacity', '--project', str(root)], cwd=str(ROOT),
                                         capture_output=True, check=True)) for _ in range(3)]
    ops['cli_capacity_fresh_process'] = summary(runs)
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.gui import STYLE
    from sovereign_editor.map_inspector import MapInspectorWindow
    from sovereign_editor.story_ui import StoryEditor
    from sovereign_editor.world_ui import WorldEditor
    from sovereign_editor.workspace_ui import ProjectBrowser
    app = QApplication.instance() or QApplication([])
    app.setStyle('Fusion'); app.setStyleSheet(STYLE)
    ui = Project(root)
    t = time.time(); w = MapInspectorWindow(ui, context=(areas['ec_ridge'], [0, 0])); w.show(); app.processEvents()
    ops['ui_window_first_view'] = summary([time.time() - t], TARGETS['open_first_view'])
    switches = []
    for header, cell in views:
        t = time.time(); w.load_context(header, cell); app.processEvents(); switches.append(time.time() - t)
    ops['ui_area_switch'] = summary(switches, TARGETS['warm_switch'])
    for name, cls in (('story_editor', StoryEditor), ('world_editor', WorldEditor), ('project_browser', ProjectBrowser)):
        samples = []
        for _ in range(3):
            t = time.time(); d = cls(w); d.show(); app.processEvents(); samples.append(time.time() - t); d.close()
        ops[f'ui_open_{name}'] = summary(samples)
    w.close(); app.processEvents()
    report['project_json_bytes'] = (root / 'project.json').stat().st_size
    report['transactions'] = len(p.doc['map_edits'])
    report['history_entries'] = len(p.doc['history'])
    report['rss_mb_this_process'] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 1)
    report['revision'] = p.doc['revision']
    misses = [k for k, v in ops.items() if v.get('target') and not (v['median_ok'] and v['max_ok'])]
    report['misses'] = misses
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1) + '\n')
    print(json.dumps({'misses': misses, **{k: {kk: v.get(kk) for kk in ('median', 'max', 'target')} for k, v in ops.items()}}))


if __name__ == '__main__':
    main()
