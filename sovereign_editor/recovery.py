"""Portable projects and user checkpoints (RECOVERY-01/02).

Package: one ZIP holding ``project.json``, every project-owned asset package (content-addressed
under ``assets/``) and ``package.json`` (schema, name, revision, baseline binding sha256/size/
profile, per-file SHA-256, editor code fingerprint). The baseline ROM is optional: without it the
person chooses a ROM and it must match the recorded binding exactly (the baseline-selection flow).
Unpacking verifies every file, refuses path tricks, missing or extra files, then opens the result
in a sibling scratch folder (full replay validation) and only then renames it into place: a bad
package never leaves a partial project.

Checkpoints: named copies of ``project.json`` plus the asset packages it needs, under
``checkpoints/<name>/`` with a manifest. Restoring is one undoable commit: the pre-restore state
goes onto the undo stack, the revision advances (a stale client's write is refused), composed
caches reset, and missing asset packages are copied back first. A restore first records an
automatic ``before-restore-r<revision>`` checkpoint. ``recover`` rebuilds a project.json that no
longer opens from a checkpoint, again validating in scratch before replacing.
"""
import copy
import hashlib
import json
import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path

from .formats import EditorError, digest, require

PACKAGE_SCHEMA = 'sovereign-project-package-v1'
CHECKPOINT_SCHEMA = 'sovereign-project-checkpoint-v1'
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}')


def _files(root):
    """Project-owned files that travel: project.json and every asset package file."""
    root = Path(root)
    out = ['project.json']
    assets = root / 'assets'
    if assets.is_dir():
        for path in sorted(assets.rglob('*')):
            require(not path.is_symlink(), f'Asset {path} is a symbolic link', 'INVALID_PACKAGE')
            if path.is_file():
                out.append(path.relative_to(root).as_posix())
    return out


def _sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


# ---- packages ---------------------------------------------------------------------------------

def package(project, output, include_baseline=False):
    from . import snapshots
    output = Path(output)
    require(not output.exists(), 'Package destination already exists', 'EXISTS')
    root = project.root
    files = {rel: _sha(root / rel) for rel in _files(root)}
    manifest = {'schema': PACKAGE_SCHEMA, 'name': project.doc['name'], 'revision': project.doc['revision'],
                'baseline': dict(project.doc['baseline']), 'baseline_included': bool(include_baseline),
                'files': files, 'editor_code': snapshots.CODE, 'created': time.strftime('%Y-%m-%dT%H:%M:%S')}
    temp = output.with_name(f'.{output.name}.{uuid.uuid4().hex}.tmp')
    try:
        with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('package.json', json.dumps(manifest, indent=1, sort_keys=True))
            for rel in files:
                z.write(root / rel, rel)
            if include_baseline:
                z.write(root / 'baseline.nds', 'baseline.nds', compress_type=zipfile.ZIP_STORED)
        os.replace(temp, output)
    finally:
        if temp.exists():
            temp.unlink()
    return {'package': str(output), 'files': len(files), 'baseline_included': bool(include_baseline),
            'bytes': output.stat().st_size, 'revision': manifest['revision']}


def unpack(source, root, baseline=None):
    """Verified extraction into ``root`` (must not exist). ``baseline`` is the chosen ROM when the
    package does not carry one."""
    from .core import Project
    source, root = Path(source), Path(root).resolve()
    require(not root.exists(), 'Project destination already exists', 'EXISTS')
    try:
        z = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError) as exc:
        raise EditorError('INVALID_PACKAGE', f'Not a project package: {exc}') from exc
    with z:
        names = z.namelist()
        require('package.json' in names, 'Package manifest missing', 'INVALID_PACKAGE')
        try:
            manifest = json.loads(z.read('package.json'))
        except ValueError as exc:
            raise EditorError('INVALID_PACKAGE', 'Package manifest is not JSON') from exc
        require(manifest.get('schema') == PACKAGE_SCHEMA and isinstance(manifest.get('files'), dict)
                and 'project.json' in manifest['files'], 'Unknown or incomplete package manifest', 'INVALID_PACKAGE')
        for name in names:
            require(not name.startswith('/') and '..' not in Path(name).parts and '\\' not in name,
                    f'Unsafe path in package: {name}', 'INVALID_PACKAGE')
        expected = set(manifest['files']) | {'package.json'} | ({'baseline.nds'} if manifest.get('baseline_included') else set())
        require(set(names) == expected, 'Package files differ from its manifest (missing or extra files)', 'INVALID_PACKAGE')
        for rel, sha in manifest['files'].items():
            require(hashlib.sha256(z.read(rel)).hexdigest() == sha, f'{rel} is damaged (hash differs)', 'INVALID_PACKAGE')
        binding = manifest['baseline']
        if manifest.get('baseline_included'):
            require(baseline is None, 'This package carries its baseline; do not choose another', 'INVALID_INPUT')
            blob_source = lambda dest: dest.write_bytes(z.read('baseline.nds'))
            size = z.getinfo('baseline.nds').file_size
            got = hashlib.sha256(z.read('baseline.nds')).hexdigest()
        else:
            require(baseline is not None, 'Choose the baseline ROM this project was made from '
                    f"(SHA-256 {binding['sha256'][:12]}…, {binding['size']} bytes)", 'BASELINE_REQUIRED')
            baseline = Path(baseline)
            size, got = baseline.stat().st_size, _sha(baseline)
            blob_source = lambda dest: shutil.copyfile(baseline, dest)
        require(got == binding['sha256'] and size == binding['size'],
                f"The chosen ROM is not this project's baseline (expected {binding['sha256'][:12]}…)", 'BASELINE_MISMATCH')
        doc = json.loads(z.read('project.json'))
        require(doc.get('baseline', {}).get('sha256') == binding['sha256'], 'Package manifest and project disagree '
                'on the baseline', 'INVALID_PACKAGE')
        temp = root.with_name(f'.{root.name}.unpack-{uuid.uuid4().hex}')
        try:
            temp.mkdir(parents=True)
            for rel in manifest['files']:
                dest = temp / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(rel))
            blob_source(temp / 'baseline.nds')
            opened = Project(temp)                     # full replay validation + asset package checks
            require(opened.doc['revision'] == manifest['revision'], 'Package revision differs', 'INVALID_PACKAGE')
            del opened
            os.rename(temp, root)
        except BaseException:
            shutil.rmtree(temp, ignore_errors=True)
            raise
    project = Project(root)
    return {'root': str(root), 'revision': project.doc['revision'], 'files': len(manifest['files']),
            'baseline_sha256': binding['sha256']}


# ---- checkpoints ------------------------------------------------------------------------------

def _dir(project): return project.root / 'checkpoints'


def _asset_dirs(doc_text):
    """Asset package folders (assets/<asset>/<package prefix>) a project document refers to."""
    return sorted(set(re.findall(r'"package": "([0-9a-f]{64})"', doc_text)))


def checkpoint(project, name, expected_revision, note=None):
    require(isinstance(name, str) and NAME.fullmatch(name), 'Checkpoint names use letters, digits, . _ - (1..64)',
            'INVALID_INPUT')
    require(note is None or isinstance(note, str) and len(note) <= 200, 'Invalid checkpoint note', 'INVALID_INPUT')
    with project._locked(expected_revision):
        target = _dir(project) / name
        require(not target.exists(), f'Checkpoint {name} already exists', 'EXISTS')
        temp = _dir(project) / f'.{name}.{uuid.uuid4().hex}'
        try:
            temp.mkdir(parents=True)
            shutil.copyfile(project.path, temp / 'project.json')
            if (project.root / 'assets').is_dir():
                shutil.copytree(project.root / 'assets', temp / 'assets')
            files = {rel: _sha(temp / rel) for rel in _files(temp)}
            manifest = {'schema': CHECKPOINT_SCHEMA, 'name': name, 'note': note, 'revision': project.doc['revision'],
                        'transactions': len(project.doc['map_edits']), 'history': len(project.doc['history']),
                        'created': time.strftime('%Y-%m-%dT%H:%M:%S'), 'files': files,
                        'baseline_sha256': project.doc['baseline']['sha256']}
            (temp / 'checkpoint.json').write_text(json.dumps(manifest, indent=1, sort_keys=True))
            os.rename(temp, target)
        except BaseException:
            shutil.rmtree(temp, ignore_errors=True)
            raise
    return {k: manifest[k] for k in ('name', 'revision', 'transactions', 'history', 'created', 'note')}


def _load(project_root, name):
    target = Path(project_root) / 'checkpoints' / name
    require(NAME.fullmatch(name or '') and target.is_dir(), f'No checkpoint named {name}', 'NOT_FOUND')
    try:
        manifest = json.loads((target / 'checkpoint.json').read_text())
    except (OSError, ValueError) as exc:
        raise EditorError('INVALID_CHECKPOINT', f'Checkpoint {name} manifest is unreadable') from exc
    require(manifest.get('schema') == CHECKPOINT_SCHEMA, 'Unknown checkpoint format', 'INVALID_CHECKPOINT')
    # Every field a restore or listing reads is checked before anything is changed.
    require(isinstance(manifest.get('files'), dict) and manifest['files'] and type(manifest.get('revision')) is int
            and isinstance(manifest.get('baseline_sha256'), str) and manifest.get('name') == name,
            f'Checkpoint {name} manifest is damaged', 'INVALID_CHECKPOINT')
    for rel, sha in manifest['files'].items():
        require((target / rel).is_file() and _sha(target / rel) == sha, f'Checkpoint file {rel} is damaged',
                'INVALID_CHECKPOINT')
    return target, manifest


def checkpoints(project):
    rows = []
    base = _dir(project)
    for path in sorted(base.iterdir()) if base.is_dir() else []:
        if path.name.startswith('.') or not path.is_dir():
            continue
        try:
            _, m = _load(project.root, path.name)
            doc = json.loads((path / 'project.json').read_text())
        except EditorError as exc:
            rows.append({'name': path.name, 'valid': False, 'reason': str(exc)})
            continue
        now = project.doc['map_edits']
        keep = 0
        while keep < min(len(now), len(doc['map_edits'])) and now[keep] == doc['map_edits'][keep]:
            keep += 1
        rows.append({'name': m['name'], 'valid': True, 'note': m.get('note'), 'created': m['created'],
                     'revision': m['revision'], 'transactions': m['transactions'],
                     'compared_with_current': {'shared_transactions': keep,
                                               'only_in_checkpoint': len(doc['map_edits']) - keep,
                                               'only_in_current': len(now) - keep,
                                               'labels_only_in_checkpoint': [t.get('label') for t in doc['map_edits'][keep:]][:12]}})
    return {'revision': project.doc['revision'], 'checkpoints': rows}


def restore(project, name, expected_revision):
    """One undoable commit that makes the checkpoint's content current (with a new revision)."""
    target, manifest = _load(project.root, name)
    require(manifest['baseline_sha256'] == project.doc['baseline']['sha256'], 'Checkpoint belongs to another baseline',
            'BASELINE_MISMATCH')
    doc = json.loads((target / 'project.json').read_text())
    auto = f"before-restore-r{expected_revision}"
    if not (_dir(project) / auto).exists():
        checkpoint(project, auto, expected_revision, note=f'automatic, before restoring {name}')
    with project._locked(expected_revision):
        # Content-addressed asset packages the checkpoint needs come back first (never overwritten).
        if (target / 'assets').is_dir():
            for path in sorted((target / 'assets').rglob('*')):
                dest = project.root / path.relative_to(target)
                if path.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                elif not dest.exists():
                    shutil.copyfile(path, dest)
        changes = {'positions': doc['positions'], 'placement_moves': doc.get('placement_moves', {}),
                   'map_edits': doc.get('map_edits', [])}
        trial = {**copy.deepcopy(project.doc), **copy.deepcopy(changes)}
        if 'map_selection' in doc:
            trial['map_selection'] = doc['map_selection']
            changes['map_selection'] = doc['map_selection']
        project._validate_state(trial)
        project._commit('map.transaction', copy.deepcopy(changes))
    return {'revision': project.doc['revision'], 'restored': name, 'checkpoint_revision': manifest['revision'],
            'automatic_backup': auto, 'undo': 'Undo returns to the state before the restore'}


def recover(root, name):
    """Replace a project.json that no longer opens with a checkpoint's (validated in scratch first)."""
    from .core import Project
    root = Path(root).resolve()
    target, manifest = _load(root, name)
    temp = root.with_name(f'.{root.name}.recover-{uuid.uuid4().hex}')
    try:
        temp.mkdir()
        os.link(root / 'baseline.nds', temp / 'baseline.nds') if hasattr(os, 'link') else shutil.copyfile(root / 'baseline.nds', temp / 'baseline.nds')
        shutil.copyfile(target / 'project.json', temp / 'project.json')
        if (target / 'assets').is_dir():
            shutil.copytree(target / 'assets', temp / 'assets')
        Project(temp)                                           # the checkpoint must open cleanly
        broken = root / f'project.json.broken-{time.strftime("%Y%m%d-%H%M%S")}'
        if (root / 'project.json').exists():
            os.replace(root / 'project.json', broken)
        shutil.copyfile(target / 'project.json', root / '.project.json.recover')
        os.replace(root / '.project.json.recover', root / 'project.json')
        if (target / 'assets').is_dir():
            for path in sorted((target / 'assets').rglob('*')):
                dest = root / path.relative_to(target)
                if path.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                elif not dest.exists():
                    shutil.copyfile(path, dest)
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    project = Project(root)
    return {'root': str(root), 'revision': project.doc['revision'], 'recovered_from': name,
            'previous_file': broken.name if 'broken' in locals() else None}
