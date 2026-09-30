"""Reproducible Intel-Mac build of ``Sovereign Editor.app`` (RELEASE-01). Offline; unsigned.

Recipe (docs/RELEASE_MAC.md):
1. A fresh ``--copies --without-pip`` venv of the python.org Python 3.12 framework is created inside
   the bundle (``Contents/Resources/venv``); it does not see system or user site-packages.
2. The pinned runtime distributions (pyproject: ndspy 4.1.0, Pillow 11.3.0, numpy 2.2.6,
   PySide6-Essentials 6.8.3 + shiboken6) are copied from the build venv by their installed file lists
   (APFS clones, so the ~450 MB bundle costs almost no disk), then the ``sovereign_editor`` package
   (sources and package data only) is copied and byte-compiled.
3. ``Contents/MacOS/Sovereign Editor`` starts the editor; ``Contents/Resources/bin/sovereign`` is the
   same CLI for agents. Both clear PYTHONPATH/PYTHONHOME and ignore the user site, so a developer
   checkout is never imported.
4. ``build-manifest.json`` records the platform, interpreter, dependency versions, the SHA-256 of every
   packaged editor file, the editor code fingerprint and the git revision. The bundle is NOT signed or
   notarized; Gatekeeper asks for Open on first launch.

Usage: build_mac_app.py [--output DIR]   (default: work/editor-completion-v1/release)
"""
import argparse
import compileall
import hashlib
import importlib.metadata as md
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
__version__ = next(l.split('"')[1] for l in (ROOT / 'pyproject.toml').read_text().splitlines()
                   if l.startswith('version = '))
APP = 'Sovereign Editor.app'
DISTRIBUTIONS = {'ndspy': '4.1.0', 'pillow': '11.3.0', 'numpy': '2.2.6', 'PySide6_Essentials': '6.8.3',
                 'shiboken6': '6.8.3'}
LAUNCH = '''#!/bin/bash
# Sovereign Editor launcher: the bundle's own Python environment only.
VENV="$(cd "$(dirname "$0")/{venv}" && pwd)"
unset PYTHONPATH PYTHONHOME
export PYTHONNOUSERSITE=1
exec "$VENV/bin/python3" -s -m sovereign_editor.cli {command}"$@"
'''


def clone(src, dest):
    """APFS copy-on-write clone when possible (cp -c), else an ordinary copy."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if subprocess.run(['cp', '-c', '-R', str(src), str(dest)], capture_output=True).returncode != 0:
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, dest)


def top_level(dist):
    names = set()
    for f in md.distribution(dist).files or []:
        parts = Path(f).parts
        if parts and parts[0] not in ('..', 'bin') and not parts[0].startswith('__'):
            names.add(parts[0])
    return sorted(names)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(output):
    output = Path(output).resolve()
    app = output / APP
    if app.exists():
        shutil.rmtree(app)
    contents = app / 'Contents'
    (contents / 'MacOS').mkdir(parents=True)
    resources = contents / 'Resources'
    base = Path(getattr(sys, '_base_executable', sys.executable))
    venv = resources / 'venv'
    subprocess.run([str(base), '-m', 'venv', '--copies', '--without-pip', str(venv)], check=True)
    site = next((venv / 'lib').glob('python3.*/site-packages'))
    here = Path(md.distribution('numpy').locate_file(''))
    versions = {}
    for dist, want in DISTRIBUTIONS.items():
        got = md.version(dist)
        assert got == want, f'{dist} {got} is installed; the pinned build needs {want}'
        versions[dist] = got
        for name in top_level(dist):
            src = here / name
            if src.exists() and not (site / name).exists():
                clone(src, site / name)
    package = site / 'sovereign_editor'
    files = {}
    for src in sorted((ROOT / 'sovereign_editor').rglob('*')):
        rel = src.relative_to(ROOT / 'sovereign_editor')
        if '__pycache__' in rel.parts or src.suffix == '.pyc' or src.is_dir():
            continue
        dest = package / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        files[rel.as_posix()] = sha(src)
    compileall.compile_dir(str(package), quiet=1)
    for path, command, rel in ((contents / 'MacOS' / 'Sovereign Editor', 'ui ', '../Resources/venv'),
                               (resources / 'bin' / 'sovereign', '', '../venv')):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(LAUNCH.replace('{command}', command).replace('{venv}', rel))
        path.chmod(0o755)
    with open(contents / 'Info.plist', 'wb') as f:
        plistlib.dump({'CFBundleName': 'Sovereign Editor', 'CFBundleDisplayName': 'Sovereign Editor',
                       'CFBundleIdentifier': 'local.sovereign-editor', 'CFBundleExecutable': 'Sovereign Editor',
                       'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': __version__,
                       'CFBundleVersion': time.strftime('%Y%m%d%H%M'), 'LSMinimumSystemVersion': '13.0',
                       'NSHighResolutionCapable': True}, f)
    sys.path.insert(0, str(ROOT))
    from sovereign_editor import snapshots
    git = lambda *a: subprocess.run(['git', *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    manifest = {'schema': 'sovereign-mac-build-v1', 'app': APP, 'version': __version__,
                'built': time.strftime('%Y-%m-%dT%H:%M:%S'), 'platform': {'machine': platform.machine(),
                'macos': platform.mac_ver()[0]}, 'python': {'version': platform.python_version(), 'base': str(base)},
                'dependencies': versions, 'editor_files': files, 'editor_code_fingerprint': snapshots.CODE,
                'git': {'head': git('rev-parse', 'HEAD'), 'dirty': bool(git('status', '--porcelain'))},
                'signed': False, 'notarized': False,
                'requires': 'macOS on Intel (x86_64) with the python.org Python 3.12 framework at '
                            '/Library/Frameworks/Python.framework/Versions/3.12'}
    (output / 'build-manifest.json').write_text(json.dumps(manifest, indent=1))
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default=str(ROOT / 'work/editor-completion-v1/release'))
    args = parser.parse_args()
    m = build(args.output)
    print(json.dumps({k: m[k] for k in ('app', 'version', 'platform', 'python', 'dependencies', 'git')}))
