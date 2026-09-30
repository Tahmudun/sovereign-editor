"""Relocated clean-launch verification of the Mac bundle (RELEASE-01). Evidence, not native acceptance.

Clones the built ``Sovereign Editor.app`` to a different folder (with a space in its path), then with an
empty environment (only HOME and PATH=/usr/bin:/bin; no PYTHONPATH) and an unrelated working directory:
* ``--smoke-test`` of the editor window and of the map inspector on a scratch project clone
  (offscreen, and once on the native cocoa platform);
* CLI commands through ``Contents/Resources/bin/sovereign``.
Every report must show the editor package and interpreter inside the relocated bundle.

Usage: verify_mac_app.py APP PROJECT SCRATCH OUT.json
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def run(cmd, env, cwd, timeout=600):
    t = time.time()
    r = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    lines = [l for l in r.stdout.splitlines() if l.startswith('{')]
    return {'cmd': [str(c) for c in cmd], 'exit': r.returncode, 'seconds': round(time.time() - t, 2),
            'report': json.loads(lines[-1]) if lines else None, 'stderr_tail': r.stderr[-600:]}


def main(app, project, scratch, out):
    # Commands run from / with an empty environment, so every path is made absolute first.
    app, project, scratch = Path(app).resolve(), Path(project).resolve(), Path(scratch).resolve()
    place = scratch / 'Relocated Apps'
    if place.exists():
        shutil.rmtree(place)
    place.mkdir(parents=True)
    moved = place / app.name
    subprocess.run(['cp', '-c', '-R', str(app), str(moved)], check=True)
    clone = scratch / 'launch-project'
    if clone.exists():
        shutil.rmtree(clone)
    env = {'HOME': os.environ['HOME'], 'PATH': '/usr/bin:/bin', 'SOVEREIGN_EDITOR_CACHE': str(scratch / 'cache')}
    cli = moved / 'Contents/Resources/bin/sovereign'
    gui = moved / 'Contents/MacOS/Sovereign Editor'
    checks, results = [], {}
    results['clone'] = run([cli, 'clone', '--project', project, '--output', clone], env, '/')
    results['editor_offscreen'] = run([gui, '--smoke-test'], {**env, 'QT_QPA_PLATFORM': 'offscreen'}, '/')
    results['inspector_offscreen'] = run([gui, '--smoke-test', '--project', clone, '--map-inspector'],
                                         {**env, 'QT_QPA_PLATFORM': 'offscreen'}, '/')
    results['inspector_cocoa'] = run([gui, '--smoke-test', '--project', clone, '--map-inspector'], env, '/')
    results['cli_search'] = run([cli, 'workspace-search', '--project', clone, '--query', 'cut', '--limit', '5'], env, '/')
    results['cli_checkpoints'] = run([cli, 'checkpoints', '--project', clone], env, '/')
    inside = lambda path: str(path).startswith(str(moved))
    for name in ('editor_offscreen', 'inspector_offscreen', 'inspector_cocoa'):
        r = results[name]['report'] or {}
        checks.append({'check': f'{name}: launched, package and interpreter inside the relocated bundle',
                       'pass': results[name]['exit'] == 0 and r.get('ok') and inside(r.get('package', ''))
                       and inside(r.get('executable', ''))})
    checks.append({'check': 'native platform is cocoa', 'pass': (results['inspector_cocoa']['report'] or {}).get('platform') == 'cocoa'})
    for name in ('clone', 'cli_search', 'cli_checkpoints'):
        checks.append({'check': f'{name} via bundled CLI', 'pass': results[name]['exit'] == 0
                       and (results[name]['report'] or {}).get('ok', False)})
    report = {'schema': 'sovereign-mac-launch-v1', 'relocated_app': str(moved), 'checks': checks,
              'passed': sum(c['pass'] for c in checks), 'failed': sum(not c['pass'] for c in checks), 'runs': results,
              'note': 'Software launch evidence on this Mac; the bundle is unsigned and not notarized.'}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(report, indent=1))
    shutil.rmtree(place)
    shutil.rmtree(clone, ignore_errors=True)
    print(json.dumps({'passed': report['passed'], 'failed': report['failed']}))
    return report


if __name__ == '__main__':
    main(*sys.argv[1:5])
