"""Content-keyed, signed snapshots of validated compositions (PROD-PERF-001).

Replaying and validating every stored transaction is the dominant cost of opening,
previewing, applying, undoing and redoing. A snapshot records what composition
leaves on a Project (the composed state plus created/copied resource registries
and the ARM9 rebinding) after the FULL validation of one exact input:

  key = sha256(canonical positions/placement moves/map edits
               + the verified baseline SHA-256 + the editor code fingerprint)

so a different project history, baseline or editor build is always a miss and
takes the full replay/validation path. Snapshots live in memory (bounded LRU) and
in a per-user cache directory, never inside project folders. Each file carries
an HMAC-SHA256 over its key and payload with a per-user secret; a tampered or
foreign file fails verification and is ignored. Project still reads project.json
and the baseline from disk and refuses stale revisions on every write.
"""
import contextlib
import hashlib
import hmac
import json
import os
import pickle
import secrets
import tempfile
from collections import OrderedDict
from pathlib import Path

MAGIC = b'SESNAP01'
ATTRS = ('_room_members', '_room_matrices', '_room_headers', '_world_headers', '_world_members', '_terrain_bdhc')
DERIVED = ('_area_resource_users', '_library_source_cache', '_event_users', '_header_names', '_level_script_users',
           '_gameplay_wild_users', '_resource_users_versions')
MEMORY_ITEMS = 8
DISK_BYTES = 192 << 20


def _code_fingerprint():
    root = Path(__file__).parent
    h = hashlib.sha256()
    for path in sorted([*root.glob('*.py'), *root.glob('native/*')]):
        h.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
    return h.hexdigest()


CODE = _code_fingerprint()
_state = {'dir': None, 'enabled': os.environ.get('SOVEREIGN_EDITOR_CACHE', '') != 'off'}
_memory = OrderedDict()


def configure(path):
    _state['dir'] = Path(path)


def directory():
    if _state['dir'] is None:
        env = os.environ.get('SOVEREIGN_EDITOR_CACHE')
        _state['dir'] = Path(env) if env and env != 'off' else Path.home() / 'Library/Caches/sovereign-editor/composition'
    _state['dir'].mkdir(parents=True, exist_ok=True)
    return _state['dir']


@contextlib.contextmanager
def disabled():
    previous = _state['enabled']
    _state['enabled'] = False
    try:
        yield
    finally:
        _state['enabled'] = previous


def clear_memory():
    _memory.clear()


def key(project, state):
    inputs = json.dumps({'positions': state.get('positions', {}), 'placement_moves': state.get('placement_moves', {}),
                         'map_edits': state.get('map_edits', [])}, sort_keys=True, separators=(',', ':'))
    h = hashlib.sha256(inputs.encode())
    h.update(project.baseline_sha256.encode() + CODE.encode())
    return h.hexdigest()


def capture(project):
    arm9 = None if project.arm9 is project._base_arm9 else project.arm9
    values = {name: getattr(project, name) for name in ATTRS}
    return pickle.dumps((project._composed_cache, arm9, values), protocol=5)


def restore(project, blob):
    composed, arm9, values = pickle.loads(blob)
    project.arm9 = project._base_arm9 if arm9 is None else arm9
    for name, value in values.items():
        setattr(project, name, value)
    project._composed_cache = composed
    project._context_cache, project._member_cache, project._event_cache = {}, {}, {}
    for name in DERIVED:
        project.__dict__.pop(name, None)
    project._snapshot_blob = blob


def _secret():
    path = directory() / 'cache.key'
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(secrets.token_bytes(32))
    return path.read_bytes()


def _mac(name, payload):
    return hmac.new(_secret(), name.encode() + b'\0' + payload, hashlib.sha256).digest()


def lookup(name):
    if not _state['enabled']:
        return None
    if name in _memory:
        _memory.move_to_end(name)
        return _memory[name]
    path = directory() / f'{name}.snapshot'
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    payload = raw[len(MAGIC) + 32:]
    if raw[:len(MAGIC)] != MAGIC or not hmac.compare_digest(raw[len(MAGIC):len(MAGIC) + 32], _mac(name, payload)):
        return None
    _remember(name, payload)
    try:
        os.utime(path)
    except OSError:
        pass                                    # pruned by another process meanwhile
    return payload


def _remember(name, payload):
    _memory[name] = payload
    _memory.move_to_end(name)
    while len(_memory) > MEMORY_ITEMS:
        _memory.popitem(last=False)


def store(name, payload):
    if not _state['enabled']:
        return
    _remember(name, payload)
    folder = directory()
    fd, temp = tempfile.mkstemp(prefix='.snapshot-', dir=folder)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(MAGIC + _mac(name, payload) + payload)
        os.replace(temp, folder / f'{name}.snapshot')
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    # Another editor process may prune the shared cache at the same time: a file that vanishes
    # between listing and stat is simply skipped.
    entries = []
    for path in folder.glob('*.snapshot'):
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        entries.append((info.st_mtime, info.st_size, path))
    total = 0
    for _, size, path in sorted(entries, key=lambda e: e[0], reverse=True):
        total += size
        if total > DISK_BYTES:
            path.unlink(missing_ok=True)


def structure_digest(project):
    """Order-sensitive digest of everything composition leaves on a Project.

    Dict order is significant (library order allocates slots); shared container
    references are encoded; identity of immutable str/bytes is not (pickle
    memoization of equal immutables differs between equivalent compositions).
    """
    h = hashlib.sha256()
    seen = {}

    def feed(tag, data=b''):
        h.update(tag + len(data).to_bytes(8, 'little') + data)

    def walk(x):
        if isinstance(x, (dict, list, set, bytearray)):
            if id(x) in seen:
                feed(b'R', str(seen[id(x)]).encode())
                return
            seen[id(x)] = len(seen)
        if x is None or isinstance(x, (bool, int, float)):
            feed(b'S', repr(x).encode())
        elif isinstance(x, str):
            feed(b's', x.encode())
        elif isinstance(x, (bytes, bytearray)):
            feed(b'b', bytes(x))
        elif isinstance(x, dict):
            feed(b'D', str(len(x)).encode())
            for k, v in x.items():
                walk(k)
                walk(v)
        elif isinstance(x, (list, tuple)):
            feed(b'L' if isinstance(x, list) else b'T', str(len(x)).encode())
            for v in x:
                walk(v)
        elif isinstance(x, (set, frozenset)):
            items = sorted(x, key=repr)
            feed(b'E', str(len(items)).encode())
            for v in items:
                walk(v)
        else:
            raise TypeError(f'Unsupported composition value {type(x).__name__}')
    project.composed()
    composed, arm9, values = pickle.loads(capture(project))
    walk(composed)
    walk(arm9)
    walk(values)
    return h.hexdigest()
