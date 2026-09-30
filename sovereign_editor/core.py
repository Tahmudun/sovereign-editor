"""Transactional project edits and guarded ROM byte patches, shared by UI/CLI."""
import copy
import fcntl
import json
import math
import os
import shutil
import struct
import tempfile
from contextlib import contextmanager
from pathlib import Path

from . import authoring, world, sign_interaction, scenery, event_authoring, surface_authoring, simple_interactions, interiors, linked_groups, story_authoring, scene_authoring
from . import gameplay, world_authoring, snapshots, terrain_authoring, game_data
from . import props as custom_props
from . import ground_materials
from . import decals as ground_decals
from . import travel_points
from . import field_features
from . import petal_effect
from . import runtime_repairs
from . import pokemon_packages
from . import map_groups
from . import environments
from .formats import (ASSETS, EditorError, arm9_code, digest, immutable_digest, events, map_data, map_sections,
                      qualify, require, resource)
from .decoration import (BASELINE as DECORATION_BASELINE, KEY as DECORATION_KEY, BEFORE,
                         qualify_move, qualify_area, translation_proof, authored_move, footprint, require_target)

SCHEMA = "sovereign-editor-project-v1"
ORIGIN = (512, 384)
EVENT_ARCHIVE = "a/0/3/2"
# The legacy Cherrygrove scene: header 67 of the pinned ROM, two matrix cells.
CHERRYGROVE = {"header": 67, "cells": {4: [16, 12], 5: [17, 12]}}
CHERRYGROVE_CELL = {"header": 67, "cell": [17, 12]}
LABELS = {0: "Guide", 1: "Road trainer", 2: "Resident", 3: "Shore resident", 4: "Scene actor", 5: "Scene actor"}


def _require_disjoint(authored, qualified):
    """Legacy and authored edits never mix silently: compose apart or refuse.

    Both domains are resource-local (map member plus slot or byte offset), so a
    second matrix cell showing the same member cannot hide the overlap.
    """
    overlap = sorted(authoring.describe_domain(item) for item in authored & qualified)
    require(not overlap,
            f"This authored edit overlaps the qualified planter operation at {overlap[:3]}. "
            "Undo that operation, or author records and cells it does not own.",
            "LEGACY_CONFLICT")


def atomic_json(path, value):
    """Durable replace. A dict is written one top-level key per line with compact values:
    the C encoder is several times faster than indent=2 for a history-bearing project."""
    fd, temp = tempfile.mkstemp(prefix=".project-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            if isinstance(value, dict) and value:
                items = [f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in value.items()]
                f.write("{\n" + ",\n".join(items) + "\n}")
            else:
                json.dump(value, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _clone(value):
    """Independent copy of plain JSON-like data (dict/list/tuple of scalars); several times
    faster than copy.deepcopy for the contexts that whole-state validation requests."""
    kind = type(value)
    if kind is dict:
        return {k: _clone(v) for k, v in value.items()}
    if kind is list:
        return [_clone(v) for v in value]
    if kind is tuple:
        return tuple(_clone(v) for v in value)
    if kind in (int, str, float, bool, bytes) or value is None:
        return value
    return copy.deepcopy(value)


class Project:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / "project.json"
        require(self.path.is_file() and not self.path.is_symlink(), "Choose a Sovereign Editor project folder", "PROJECT_NOT_FOUND")
        self._read()

    @property
    def baseline_sha256(self):
        """Cache only an immutable in-memory ROM; disk rereads replace that object."""
        blob = self.blob
        if type(blob) is not bytes:
            return digest(blob)
        cached = getattr(self, '_baseline_digest_cache', None)
        if cached is None or cached[0] is not blob:
            # Shared with formats.immutable_digest: one hash per ROM object, not two.
            cached = (blob, immutable_digest(blob))
            self._baseline_digest_cache = cached
        return cached[1]

    @classmethod
    def create(cls, rom_path, root, name="Cherrygrove workspace"):
        root = Path(root).resolve()
        require(not root.exists(), "Project destination already exists", "EXISTS")
        blob = Path(rom_path).read_bytes()
        profile = qualify(blob)
        root.mkdir(parents=True, exist_ok=False)
        try:
            (root / "baseline.nds").write_bytes(blob)
            manifest = {"schema": SCHEMA, "name": name, "revision": 0,
                        "baseline": {"sha256": digest(blob), "size": len(blob), "profile": profile["id"]},
                        "positions": {}, "placement_moves": {}, "map_edits": [], "history": []}
            atomic_json(root / "project.json", manifest)
        except BaseException:
            shutil.rmtree(root)
            raise
        return cls(root)

    def _read(self):
        self.doc = json.loads(self.path.read_text())
        require(self.doc.get("schema") == SCHEMA, "Unknown project version")
        require(type(self.doc.get("revision")) is int and self.doc["revision"] >= 0, "Invalid project revision")
        require(isinstance(self.doc.get("positions"), dict) and isinstance(self.doc.get("history"), list), "Invalid project edits")
        baseline = self.root / "baseline.nds"
        require(not baseline.is_symlink(), "Baseline must be a project-owned file")
        self.blob = baseline.read_bytes()
        require(self.baseline_sha256 == self.doc["baseline"]["sha256"] and len(self.blob) == self.doc["baseline"]["size"],
                "Baseline ROM has changed. Restore the original project baseline.", "BASELINE_CHANGED")
        self.profile = qualify(self.blob)
        self.arm9 = arm9_code(self.blob)
        self._base_arm9 = self.arm9
        self._composing = False
        interiors.reset(self)
        self._context_cache, self._member_cache, self._composed_cache = {}, {}, None
        self._snapshot_blob = None
        self._event_cache = {}
        self._gameplay_archives, self._gameplay_names = {}, {}
        self.event_offset, raw = resource(self.blob, EVENT_ARCHIVE, 64)
        self.base_events = events(raw)
        self.maps = {index: self.member_data(index) for index in (4, 5)}
        self.permission_start = {index: map_sections(self.member_raw(index))["permissions_offset"]
                                 for index in (4, 5)}
        self.legacy_decoration_proof = qualify_move(self) if self.doc["baseline"]["sha256"] == DECORATION_BASELINE else None
        self.decoration_proof = qualify_area(self, self.legacy_decoration_proof) if self.legacy_decoration_proof else None
        # Older projects remain byte-preserved on read; add the new state only on a write.
        self.doc.setdefault("placement_moves", {})
        self.doc.setdefault("map_edits", [])
        # The validated composition is kept (it was discarded before, so the first
        # read replayed every transaction a second time).
        self._validate_state(self.doc)
        # Prop packages live beside project.json: verify them even when the composition
        # came from the snapshot cache (a moved or partial project must not open silently).
        self.__dict__.pop('_prop_packages', None)
        if self._composed_cache is not None:
            for aid, asset in custom_props.assets(self._composed_cache).items():
                custom_props.verify_package(self, aid, asset['package'])
            self.__dict__.pop('_ground_packages', None)
            for mid, entry in ground_materials.materials(self._composed_cache).items():
                ground_materials.verify_package(self, mid, entry['package'])
            self.__dict__.pop('_pokemon_packages', None)
            for key, entry in pokemon_packages.packages(self._composed_cache).items():
                pokemon_packages.verify_package(self, key, entry['package'])
        require(isinstance(self.doc.get('redo', []), list), 'Invalid redo history')

    def clone(self, root, name=None):
        """Copy a verified project and its complete accepted history to a new root."""
        root = Path(root).resolve()
        with self._locked(self.doc['revision']):
            require(not root.exists(), 'Project destination already exists', 'EXISTS')
            root.mkdir(parents=True, exist_ok=False)
            try:
                (root / 'baseline.nds').write_bytes(self.blob)
                if (self.root / 'assets').is_dir():
                    # Project-owned prop packages travel with the project (content-addressed).
                    shutil.copytree(self.root / 'assets', root / 'assets')
                doc = copy.deepcopy(self.doc)
                if name is not None:
                    require(isinstance(name, str) and 0 < len(name) <= 160, 'Invalid project name')
                    doc['name'] = name
                atomic_json(root / 'project.json', doc)
                return Project(root)
            except BaseException:
                shutil.rmtree(root)
                raise

    # ---- generic map contexts -------------------------------------------------

    def member_raw(self, member):
        """Cached bytes of one a/0/6/5 map member; never mutated."""
        if member not in self._member_cache:
            raw = self._room_members.get(member)
            if raw is None:
                _, raw = resource(self.blob, world.MAP_ARCHIVE, member)
            self._member_cache[member] = (raw, map_data(raw))
        return self._member_cache[member][0]

    def member_data(self, member):
        self.member_raw(member)
        return self._member_cache[member][1]

    def context(self, header=None, matrix=None, cell=None):
        """Explicit map context: resource member plus matrix-cell origin."""
        if not self._composing and self._composed_cache is None and any(
                t.get('schema') in (interiors.SCHEMA, *world_authoring.SCHEMAS) for t in self.doc.get('map_edits', [])):
            self.composed()
        key = (header, matrix, tuple(cell) if cell is not None else None)
        if key not in self._context_cache:
            self._context_cache[key] = world.resolve_context(
                self.blob, header=header, matrix=matrix, cell=cell, arm9=self.arm9,
                matrix_reader=self.matrix_data, map_reader=self.member_raw, header_reader=self.header)
        return _clone(self._context_cache[key])

    def matrix_data(self, matrix_id):
        if matrix_id in self._room_matrices:
            return world.decode_matrix(self._room_matrices[matrix_id], matrix_id)
        return world.read_matrix(self.blob, matrix_id)

    # ---- composed headers and resources ----------------------------------------

    def header(self, header_id):
        """A stock header (with interior rebinding) or a created header."""
        if not self._composing and self._composed_cache is None and any(
                t.get('schema') in world_authoring.SCHEMAS for t in self.doc.get('map_edits', [])):
            self.composed()
        return world.read_header(self.blob, header_id, self.arm9, self._world_headers)

    def header_count(self):
        if not self._composing and self._composed_cache is None and any(
                t.get('schema') in world_authoring.SCHEMAS for t in self.doc.get('map_edits', [])):
            self.composed()
        return world.header_count(self.blob) + len(self._world_headers)

    def world_signature(self):
        """Cache key for header-derived indexes: ARM9 rebinding plus created headers."""
        arm9 = self.arm9
        cached = getattr(self, '_arm9_digest_cache', None)
        if cached is None or cached[0] is not arm9 or type(arm9) is not bytes:
            cached = self._arm9_digest_cache = (arm9, digest(arm9))
        return (cached[1], tuple((h, v['raw']) for h, v in sorted(self._world_headers.items())))

    def resource(self, archive, member):
        """(ROM offset or -1, bytes) of a stock or project-created archive member."""
        if archive == world.MAP_ARCHIVE and member in self._room_members:
            return -1, self._room_members[member]
        if archive == world.MATRIX_ARCHIVE and member in self._room_matrices:
            return -1, self._room_matrices[member]
        if (archive, member) in self._world_members:
            return -1, self._world_members[(archive, member)]
        return resource(self.blob, archive, member)

    def created_member(self, archive, member):
        return (archive, member) in self._world_members

    def world_areas(self):
        return world_authoring.view(self)

    def contexts(self, matrix=None, map_member=None, header=None, search=None, limit=40, offset=0):
        """List matrices, or the populated cells of one matrix. No full enumeration.

        ``search`` matches an internal map name (case-insensitive substring) or, for
        an all-digit term, a map member number. It is applied before paging, so a
        context beyond the first page stays reachable.
        """
        self.composed()
        require(type(limit) is int and 0 < limit <= 400 and type(offset) is int and offset >= 0,
                "Use a limit of 1..400 and a non-negative offset")
        require(search is None or isinstance(search, str), "Search term must be text")
        if matrix is None and header is not None:
            matrix = self.header(header)["matrix"]
        if matrix is None:
            total = world.member_count(self.blob, world.MATRIX_ARCHIVE) + len(self._room_matrices)
            listed = []
            for index in range(offset, min(total, offset + limit)):
                grid = self.matrix_data(index)
                cells = world.matrix_cells(grid)
                listed.append({"matrix": index, "name": grid["name"], "width": grid["width"],
                               "height": grid["height"], "has_headers": grid["has_headers"],
                               "populated_cells": len(cells),
                               "distinct_map_members": len({c["map_member"] for c in cells})})
            return {"kind": "matrices", "total": total, "offset": offset, "matrices": listed,
                    "hint": "pass --matrix (or --header) to list that matrix's cells"}
        grid = self.matrix_data(matrix)
        cells = world.matrix_cells(grid, map_member)
        # A matrix without a header section carries no per-cell header: the caller's
        # header applies to every cell, so it must not filter them all away.
        if header is not None and grid["has_headers"]:
            cells = [c for c in cells if c["header"] == header]
        if not hasattr(self, "_header_names"):
            # One read of the internal-name table per project, not one per listed cell.
            count = world.header_count(self.blob)
            _, table = world.span_named(self.blob, world.NAME_TABLE)
            self._header_names = [table[i * world.NAME_LENGTH:(i + 1) * world.NAME_LENGTH]
                                  .split(b"\x00")[0].decode("ascii", errors="replace").strip()
                                  for i in range(count)]
            self._header_names += [self._world_headers[h]['name'] for h in sorted(self._world_headers)]
        for cell in cells:
            header = cell["header"]
            cell["name"] = (self._header_names[header] if header is not None and header < len(self._header_names)
                            else self.header(header)['name'] if header is not None else None)
        if search:
            term = search.strip().lower()
            cells = [c for c in cells
                     if (term in (c["name"] or "").lower()
                         or (term.isdigit() and c["map_member"] == int(term)))]
        window = cells[offset:offset + limit]
        return {"kind": "cells", "matrix": matrix, "matrix_name": grid["name"],
                "size": [grid["width"], grid["height"]], "has_headers": grid["has_headers"],
                "total": len(cells), "offset": offset, "limit": limit,
                "returned": len(window), "search": search, "cells": window,
                "header_required": None if grid["has_headers"] else
                "this matrix has no header section: pass an explicit --header to open a cell"}

    def _legacy_state(self, moves):
        """The qualified planter operation expressed as composed resource-local state.

        Its records and cells are addressed exactly like authored ones, so an overlap
        is detected on the resource bytes rather than on a matrix-cell coordinate.
        """
        if not moves:
            return {}, {}, set()
        proof = self._placement_proof(moves)
        record = dict(zip(("x", "y", "z"), self.maps[5][1][14]["xyz_raw"]))
        context = self.context(**CHERRYGROVE_CELL)
        record = authoring.record_from_global(context, proof["after"], record)
        permissions = {(5, cell["offset"]): bytes.fromhex(cell["after"]) for cell in proof["collision_cells"]}
        return {(5, 14): record}, permissions, authoring.legacy_domain(5, proof)

    def _compose(self, state):
        self._composing = True
        interiors.reset(self)
        try:
            return self._compose_transactions(state)
        finally:
            self._composing = False

    def _compose_transactions(self, state):
        """Legacy plus authored transactions, validated and composed in stored order.

        Placement state is the 16.16 record words and permission state is the byte
        offset inside the map member: both independent of which matrix cell shows them.
        """
        moves = state.get("placement_moves", {})
        edits = state.get("map_edits", [])
        require(isinstance(edits, list), "Invalid authored map edits", "UNSUPPORTED_EDIT")
        legacy_placements, legacy_permissions, legacy_owned = self._legacy_state(moves)
        placements, permissions = dict(legacy_placements), dict(legacy_permissions)
        generic = {"placements": {}, "permissions": {}}
        contexts, interactions = [], {}
        composed = {"placements": placements, "permissions": permissions,
                    "generic": generic, "contexts": contexts, "legacy_domain": legacy_owned,
                    "interactions": interactions, "objects": {}, "structural_members": set()}
        event_authoring.initialise(self, composed, state["positions"])
        for index, transaction in enumerate(edits):
            self._replay_transaction(composed, transaction, index)
        return composed

    def _replay_transaction(self, composed, transaction, index):
        """Replay one stored transaction onto a composed state (in stored order)."""
        # Planners that qualify references (custom Pokémon identities) read the state being composed.
        self._replaying = composed
        placements, permissions, generic = composed['placements'], composed['permissions'], composed['generic']
        interactions, contexts = composed['interactions'], composed['contexts']
        require(isinstance(transaction, dict), 'Invalid map transaction', 'INVALID_INPUT')
        if transaction.get('schema') in world_authoring.SCHEMAS:
            world_authoring.replay(self, composed, transaction, index)
            return
        if transaction.get('schema') in gameplay.SCHEMAS:
            gameplay.replay(self, composed, transaction, index)
            return
        if transaction.get('schema') in game_data.SCHEMAS:
            game_data.replay(self, composed, transaction, index)
            return
        if transaction.get('schema') in story_authoring.SCHEMAS:
            story_authoring.replay(self, composed, transaction, index)
            return
        if transaction.get('schema') == interiors.SCHEMA:
            interiors.replay(self, composed, transaction, index)
            return
        if transaction.get('schema') == linked_groups.SCHEMA:
            linked_groups.replay(self, composed, transaction, index)
            return
        if transaction.get('schema') in surface_authoring.SCHEMAS:
            surface_authoring.replay(self, composed, transaction, index)
            return
        if transaction.get('schema') in terrain_authoring.SCHEMAS:
            terrain_authoring.replay(self, composed, transaction, index)
            return
        if transaction.get('schema') in simple_interactions.SCHEMAS:
            simple_interactions.replay(self, composed, transaction, index)
            return
        if event_authoring.is_transaction(transaction):
            event_authoring.replay(self, composed, transaction, index)
            return
        if custom_props.is_transaction(transaction):
            custom_props.replay(self, composed, transaction, index)
            return
        if ground_materials.is_transaction(transaction):
            ground_materials.replay(self, composed, transaction, index)
            return
        if ground_decals.is_transaction(transaction):
            ground_decals.replay(self, composed, transaction, index)
            return
        if travel_points.is_transaction(transaction):
            travel_points.replay(self, composed, transaction, index)
            return
        if field_features.is_transaction(transaction):
            field_features.replay(self, composed, transaction, index)
            return
        if petal_effect.is_transaction(transaction):
            petal_effect.replay(self, composed, transaction, index)
            return
        if runtime_repairs.is_transaction(transaction):
            runtime_repairs.replay(self, composed, transaction, index)
            return
        if pokemon_packages.is_transaction(transaction):
            pokemon_packages.replay(self, composed, transaction, index)
            return
        if map_groups.is_transaction(transaction):
            map_groups.replay(self, composed, transaction, index)
            return
        if environments.is_transaction(transaction):
            environments.replay(self, composed, transaction, index)
            return
        if scenery.is_transaction(transaction):
            scenery.replay(self, composed, transaction, index)
            return
        authoring.require_shape(transaction)
        require(transaction["index"] == index, "Authored transactions were reordered", "STALE_EDIT")
        ref = transaction["context"]
        context = self.context(header=ref["header"], cell=list(ref["cell"]))
        require(context["map_member"] == ref["map_member"] and list(context["origin"]) == list(ref["origin"])
                and context["matrix"]["id"] == ref["matrix"] and context["id"] == ref["id"],
                "This map context no longer resolves to the authored cell", "CONTEXT_MISMATCH")
        expected = authoring.dependencies(context, index)
        require(transaction["dependencies"] == expected
                and transaction["dependencies_sha256"] == authoring.canonical(expected),
                "Authored transaction dependencies changed", "UNQUALIFIED_DEPENDENCIES")
        _require_disjoint(authoring.transaction_domain(transaction), composed['legacy_domain'])
        member = context["map_member"]
        raw = self.member_raw(member)
        _, props, _ = self.member_data(member)
        for change in transaction["placements"]:
            slot = change["slot"]
            require(type(slot) is int and 0 <= slot < len(props),
                    f"No placement {member}:{slot} in this map context", "NOT_FOUND")
            if member in composed["objects"]:
                require(slot in composed["objects"][member], "Object was removed or transferred", "NOT_FOUND")
            # Everything the patch is derived from is checked against the resolved slot.
            authoring.require_record_identity(change, props[slot], context)
            current = placements.get((member, slot), authoring.record_state(props[slot]))
            require(change["record_before"] == current,
                    "Placement before-value differs", "BEFORE_VALUE_MISMATCH")
            require(change["before"] == authoring.global_from_record(context, change["record_before"])
                    and change["after"] == authoring.global_from_record(context, change["record_after"]),
                    "Placement anchors disagree with their records", "BEFORE_VALUE_MISMATCH")
            authoring.require_anchor(context, change["after"])
            placements[(member, slot)] = change["record_after"]
            generic["placements"][(member, slot)] = change["record_after"]
            if member in composed["objects"]:
                obj = composed["objects"][member][slot]
                data = bytearray(obj["raw"])
                struct.pack_into("<3i", data, 4, *(change["record_after"][a] for a in ("x", "y", "z")))
                obj["raw"] = bytes(data)
        owned = composed.get('prop_collision', {})
        for cell in transaction["permissions"]:
            authoring.require_cell_bounds(cell, context)
            offset = cell["offset"]
            require((member, offset) not in owned, f"Permission cell {cell['x']},{cell['z']} is owned by prop instance(s) "
                    f"{owned.get((member, offset), {}).get('owners')}; edit the prop's collision instead", "COLLISION_CONFLICT")
            current = permissions.get((member, offset), raw[offset:offset + 2])
            require(cell["before"] == current.hex(),
                    f"Permission cell {cell['x']},{cell['z']} is {current.hex()}, not {cell['before']}",
                    "BEFORE_VALUE_MISMATCH")
            after = bytes.fromhex(cell["after"])
            require(after != current, "Empty permission change", "UNSUPPORTED_EDIT")
            permissions[(member, offset)] = after
            generic["permissions"][(member, offset)] = after
        if "sign_interaction" in transaction:
            expected_sign = sign_interaction.plan(self, context, placements, interactions)
            require(expected_sign is not None and transaction["sign_interaction"] == expected_sign,
                    "Sign interaction before-values, target or dependencies changed", "BEFORE_VALUE_MISMATCH")
            interactions[(expected_sign["event_member"], expected_sign["event_id"])] = bytes.fromhex(expected_sign["after"])
            composed["event_records"][(expected_sign["event_member"], expected_sign["record_offset"])] = bytes.fromhex(expected_sign["after"])
        contexts.append(context)

    def _append_composed(self, transaction):
        """Extend a cached composition by one appended transaction (batch planning).

        Equivalent to recomposing in stored order; plan_area_edit still validates the
        whole final state from scratch before anything is saved.
        """
        if self._composed_cache is None:
            return
        self._snapshot_blob = None
        self._composing = True
        try:
            self._replay_transaction(self._composed_cache, transaction, len(self.doc['map_edits']) - 1)
        except BaseException:
            self._composed_cache = None
            raise
        finally:
            self._composing = False

    def composed(self):
        if self._composed_cache is None:
            blob = snapshots.lookup(snapshots.key(self, self.doc))
            if blob is not None:
                snapshots.restore(self, blob)
            else:
                self._composed_cache = self._compose(self.doc)
        return self._composed_cache

    def _fork(self, doc=None):
        """An isolated Project for trials: own registries, caches and composition.

        copy.copy alone shares every registry dict with this Project, so a trial
        could leak created headers or contexts into it (or the reverse).
        """
        trial = copy.copy(self)
        trial.doc = self.doc if doc is None else doc
        trial._composed_cache = None
        trial._snapshot_blob = None
        interiors.reset(trial)
        trial._event_cache = {}
        for name in snapshots.DERIVED:
            trial.__dict__.pop(name, None)
        return trial

    def _validate_state(self, state):
        # A proposed state needs its own runtime overlays and composed cache.
        # Validating a preview/undo snapshot must not change reads of self.doc.
        # The copy keeps the full document (composition reads e.g. its baseline);
        # only ``state`` is composed.
        target = self if state is self.doc else self._fork()
        name = snapshots.key(self, state)
        blob = snapshots.lookup(name)
        if blob is not None:
            snapshots.restore(target, blob)
            return target._composed_cache
        composed = target._validate_composed_state(state)
        target._snapshot_blob = snapshots.capture(target)
        snapshots.store(name, target._snapshot_blob)
        return composed

    def _validate_composed_state(self, state):
        composed = self._compose(self._checked_moves(state))
        self._composed_cache = composed
        return self._validate_composition(state, composed)

    def _checked_moves(self, state):
        moves = state.get("placement_moves", {})
        require(isinstance(moves, dict) and set(moves).issubset({DECORATION_KEY}),
                "Only the qualified south planter translation is supported", "UNSUPPORTED_EDIT")
        if moves:
            self._placement_proof(moves)
        return state

    def _validate_composition(self, state, composed):
        """Whole-state checks on a composition built in stored order (fully or
        incrementally: both replay the same transactions with the same code)."""
        # An event-edited legacy NPC is validated at its composed position below.
        # Keeping the earlier legacy position here would create a phantom blocker.
        legacy_positions = {key: value for key, value in state["positions"].items()
                            if key != "1" or (64, 1) not in composed["event_npcs"]}
        self._validate_positions(legacy_positions, composed["permissions"])
        event_authoring.validate_final(self, composed)
        simple_interactions.validate(self, composed)
        linked_groups.validate(self, composed)
        story_authoring.validate(self, composed)
        gameplay.validate(self, composed)
        world_authoring.validate(self, composed)
        return composed

    def _placement_proof(self, moves=None):
        moves = self.doc["placement_moves"] if moves is None else moves
        require(self.decoration_proof is not None, "Placement dependencies are not qualified", "UNQUALIFIED_DEPENDENCIES")
        record = moves[DECORATION_KEY]
        require(isinstance(record, dict), "Invalid placement record", "BEFORE_VALUE_MISMATCH")
        if record.get("id") == self.legacy_decoration_proof["id"]:
            proof = self.legacy_decoration_proof
        else:
            try:
                proof = translation_proof(self.decoration_proof, record.get("after"))
            except EditorError as exc:
                raise EditorError("BEFORE_VALUE_MISMATCH", "Placement target guard differs") from exc
        require(record == authored_move(proof) and record["after"] != BEFORE,
                "Placement before-values, target or dependency guard differs", "BEFORE_VALUE_MISMATCH")
        return proof

    def _snapshot(self, operation):
        result = {"operation": operation, "positions": _clone(self.doc["positions"]),
                  "placement_moves": _clone(self.doc["placement_moves"]),
                  "map_edits": _clone(self.doc["map_edits"])}
        if "map_selection" in self.doc:
            result["map_selection"] = _clone(self.doc["map_selection"])
        return result

    @staticmethod
    def _history_entry(snapshot, following):
        """A history snapshot whose map edits are stored as a delta against the state
        that follows it (kept prefix + tail): a stored edit list is no longer repeated
        once per history entry. Undo expands only the newest entry, against the doc."""
        edits, after = snapshot["map_edits"], following["map_edits"]
        keep, limit = 0, min(len(edits), len(after))
        while keep < limit and edits[keep] == after[keep]:
            keep += 1
        entry = {k: v for k, v in snapshot.items() if k != "map_edits"}
        entry["map_edits_delta"] = {"keep": keep, "tail": edits[keep:]}
        return entry

    @staticmethod
    def _expand_history(entry, following):
        """Full snapshot of a stored history entry (legacy entries are already full)."""
        if "map_edits_delta" not in entry:
            return entry
        delta = entry["map_edits_delta"]
        require(isinstance(delta, dict) and set(delta) == {"keep", "tail"} and type(delta["keep"]) is int
                and 0 <= delta["keep"] <= len(following["map_edits"]) and isinstance(delta["tail"], list)
                and "map_edits" not in entry,
                "Undo history differs from the current edits", "BEFORE_VALUE_MISMATCH")
        full = {k: v for k, v in entry.items() if k != "map_edits_delta"}
        full["map_edits"] = _clone(following["map_edits"][:delta["keep"]]) + _clone(delta["tail"])
        return full

    def _commit(self, operation, changes):
        """One save unit: snapshot, apply, bump the revision, write atomically."""
        snapshot = self._snapshot(operation)
        self.doc.update(changes)
        self.doc["history"].append(self._history_entry(snapshot, self.doc))
        self.doc.pop('redo', None)
        self.doc["revision"] += 1
        self._composed_cache = self._snapshot_blob = None
        interiors.reset(self)
        atomic_json(self.path, self.doc)

    @contextmanager
    def _locked(self, expected_revision):
        lock = self.root / ".project.lock"
        require(not lock.is_symlink(), "Project lock must not be a symbolic link")
        with lock.open("a") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            self._read()
            require(type(expected_revision) is int and expected_revision == self.doc["revision"],
                    f"Project changed; expected revision {expected_revision}, current {self.doc['revision']}. Inspect and retry.", "STALE_REVISION")
            yield

    def scene(self):
        view = events(event_authoring.raw_member(self, 64, self.composed()))
        for npc in view["npcs"]:
            npc["label"] = LABELS[npc["id"]]
            npc["editable"] = npc["id"] == 1 and (64, 1) not in self.composed()["event_npcs"]
            baseline_npc = next(n for n in self.base_events["npcs"] if n["id"] == npc["id"])
            npc["changed"] = any(npc[key] != baseline_npc[key] for key in ("x", "z", "facing", "range_x", "range_z"))
            npc.pop("offset")
        view.update(name="Cherrygrove City", origin=list(ORIGIN), width=64, height=32,
                    header=67, event_member=64, cells=[{"map": 4, "x": 0}, {"map": 5, "x": 32}])
        view["collision"] = [[self.tile(x + ORIGIN[0], z + ORIGIN[1]) for x in range(64)] for z in range(32)]
        view["props"] = []
        composed = self.composed()["placements"]
        for index, (_, props, _) in self.maps.items():
            for prop in props:
                prop = copy.deepcopy(prop)
                # Legacy and authored moves compose into one current record.
                record = composed.get((index, prop["slot"]))
                if record:
                    prop["xyz_raw"] = [record[axis] for axis in ("x", "y", "z")]
                    prop["xyz"] = [v / 65536 for v in prop["xyz_raw"]]
                view["props"].append({**prop, "map": index,
                    # Placement 16.16 coordinates are in tiles; GLB vertices use
                    # 16 world units per tile. Both origins are the cell center.
                    "tile_x": (0 if index == 4 else 32) + prop["xyz"][0] + 16,
                    "tile_z": prop["xyz"][2] + 16})
        return view

    def stock_models(self):
        """Qualified immutable source models. No extraction/import side effects."""
        from .nitro import decode_model
        refs = json.loads((ASSETS / "stock-models.json").read_text())
        require(refs.get("schema") == "stock-placed-models-v1", "Unsupported stock model manifest")
        result = {}
        ids = {p["model_id"] for _, props, _ in self.maps.values() for p in props}
        for model_id in sorted(ids):
            ref = refs["models"].get(str(model_id))
            require(ref is not None and ref["archive"] == "a/0/4/0" and ref["member"] == model_id,
                    f"Unresolved placed model {model_id}")
            _, raw = resource(self.blob, ref["archive"], ref["member"])
            require(digest(raw) == ref["sha256"], f"Placed model source changed: {model_id}")
            summary, primitives = decode_model(raw)
            require(summary["name"] == ref["name"], f"Model name disagrees: {model_id}")
            result[model_id] = {"source": ref, "summary": summary, "primitives": primitives}
        return result

    def placements(self):
        """Explicit detailed query shared by the Mac inspector and agent CLI."""
        models = self.stock_models()
        view = self.scene()
        placements = []
        for prop in view["props"]:
            require(prop["rotation_raw"] == [0, 0, 0] and prop["scale_raw"] == [4096] * 3,
                    "Only stock neutral placement transforms are qualified", "UNSUPPORTED_TRANSFORM")
            model = models[prop["model_id"]]
            bounds = model["summary"]["bounds"]
            x, z = prop["tile_x"], prop["tile_z"]
            box = [x + bounds[0][0] / 16, z + bounds[0][2] / 16,
                   x + bounds[1][0] / 16, z + bounds[1][2] / 16]
            # Nearby is a geometric observation, never a proven event dependency.
            def nearby(px, pz):
                return box[0] - 1 <= px - ORIGIN[0] <= box[2] + 1 and box[1] - 1 <= pz - ORIGIN[1] <= box[3] + 1
            blocked = sum(self.tile(xx + ORIGIN[0], zz + ORIGIN[1])["blocked"]
                          for xx in range(max(0, math.floor(box[0])), min(64, math.ceil(box[2])))
                          for zz in range(max(0, math.floor(box[1])), min(32, math.ceil(box[3]))))
            key = f"{prop['map']}:{prop['slot']}"
            editable = key == DECORATION_KEY and self.decoration_proof is not None
            placements.append({**prop, "key": key, "editable": editable,
                "changed": key in self.doc["placement_moves"]
                           or (prop["map"], prop["slot"]) in self.composed()["generic"]["placements"],
                "qualified_move": ({"id": self.decoration_proof["id"], "before": dict(BEFORE),
                    "allowed_anchors": copy.deepcopy(self.decoration_proof["allowed_anchors"]), "tile_step": 1,
                    "collision_updates_required": True, "footprint_tiles": 3,
                    "basis": "native permission controls, shared BDHC plate and complete event records",
                    "direct_event_conflicts": [], "native_acceptance": "pending per exported ROM"} if editable else None),
                "label": model["source"]["label"], "model_name": model["summary"]["name"],
                "global_position": [x + ORIGIN[0], prop["xyz"][1], z + ORIGIN[1]],
                "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1],
                "transform_scope": "qualified stock neutral rotation/unit scale only",
                "tile_bounds": box, "model_source": dict(model["source"]),
                "record_source": {"archive": "a/0/6/5", "member": prop["map"], "offset": prop["record_offset"],
                                  "bytes": 48, "sha256": prop["record_sha256"]},
                "geometry": {k: model["summary"][k] for k in ("vertices", "triangles", "shapes", "nodes")},
                "textures": [{"material": m["name"], "texture": m["texture_name"], "palette": m["palette_name"],
                              "source": "embedded TEX0"} for m in model["summary"]["materials"]],
                "dependencies": {"status": "unqualified for placement writes",
                    "basis": "geometry bounding box; nearby events use one-tile margin",
                    "blocked_tiles_in_bounds": blocked,
                    "nearby_doors": [w["id"] for w in view["warps"] if nearby(w["x"], w["z"])],
                    "nearby_triggers": [t["id"] for t in view["triggers"]
                        if t["x"] - ORIGIN[0] <= box[2] + 1 and t["x"] - ORIGIN[0] + t["width"] >= box[0] - 1
                        and t["z"] - ORIGIN[1] <= box[3] + 1 and t["z"] - ORIGIN[1] + t["height"] >= box[1] - 1],
                    "limitations": ["bounds include roofs and shadows, not a collision footprint",
                                    "door ownership, scripted movement and collision relocation remain unknown"]}})
            if editable:
                placements[-1]["dependencies"].update(
                    status="qualified only for five explicit tile-snapped anchors",
                    basis="native permission controls, horizontal BDHC plate and complete event records",
                    collision_updates_required=True, direct_event_conflicts=[],
                    limitations=["nearby-event and mesh-bounds values remain observations",
                                 "scripted runtime behavior requires user-run native acceptance; guide scene remains untested"])
        return {"count": len(placements), "unique_models": len(models), "placements": placements}

    def placement(self, map_id, slot):
        require(type(map_id) is int and type(slot) is int, "Map and slot must be integers")
        for item in self.placements()["placements"]:
            if item["map"] == map_id and item["slot"] == slot:
                return item
        require(False, f"No placement {map_id}:{slot}", "NOT_FOUND")

    # ---- general map authoring ------------------------------------------------

    def map_view(self, header=None, matrix=None, cell=None, include_grid=True):
        """Placement + permission inspector data for any resolvable map context."""
        context = self.context(header=header, matrix=matrix, cell=cell)
        member = context["map_member"]
        raw = self.member_raw(member)
        composed = self.composed()
        props = scenery.properties(self, context, composed)
        baseline_props = self.member_data(member)[1]
        overrides = {offset: value for (m, offset), value in composed["permissions"].items() if m == member}
        legacy = {d for d in composed["legacy_domain"] if d[1] == member}
        placements = []
        for prop in props:
            baseline_prop = baseline_props[prop["slot"]] if prop["slot"] < len(baseline_props) else prop
            stock_record = authoring.record_state(baseline_prop)
            record = composed["placements"].get((member, prop["slot"]), stock_record)
            # Composed state is resource-local; this cell's origin only renders it.
            stock = authoring.global_from_record(context, stock_record)
            current = authoring.global_from_record(context, record)
            owned = ("placement", member, prop["slot"]) in legacy
            placements.append({"slot": prop["slot"], "key": f"{member}:{prop['slot']}",
                               "object_id": prop["object_id"], "export_slot": prop["export_slot"],
                               "authored_new": prop["authored_new"],
                               "model_id": prop["model_id"], "stock_position": stock, "position": current,
                               "record": record, "changed": record != stock_record or prop["authored_new"],
                               "record_offset": baseline_prop["record_offset"],
                               "record_sha256": baseline_prop["record_sha256"],
                               "rotation_raw": prop["rotation_raw"], "scale_raw": prop["scale_raw"],
                               "unknown_hex": prop["unknown_hex"],
                               "editable": not owned,
                               "locked_by": "qualified planter operation (undo it first)" if owned else None})
        ox, oz = context["origin"]
        events_view = events(self.resource(world.EVENT_ARCHIVE, context["event_member"])[1])
        view = {"context": {k: context[k] for k in ("id", "name", "origin", "map_member", "map_archive",
                                                    "map_sha256", "event_member", "shared_cells", "resources")},
                "header": {k: context["header"][k] for k in ("id", "name", "matrix", "area_data", "event_file",
                                                             "script_file", "level_script", "location_type")},
                "matrix": context["matrix"], "cell": context["cell"], "area_data": context["area_data"],
                "sections": context["sections"], "size": [world.MAP_SIZE, world.MAP_SIZE],
                "placements": placements,
                # Changed cells are stored by resource offset and rendered at this
                # cell's origin, so every matrix cell of a shared member agrees.
                "changed_permission_cells": sorted(
                    [{**dict(zip(("x", "z"), world.offset_cell(context, offset))), "offset": offset,
                      "value": value.hex(),
                      "owner": "qualified planter operation" if ("permission", m, offset) in legacy else "authored"}
                     for (m, offset), value in composed["permissions"].items() if m == member],
                    key=lambda c: c["offset"]),
                "events": {"member": context["event_member"], "warps": len(events_view["warps"]),
                           "triggers": len(events_view["triggers"]), "npcs": len(events_view["npcs"]),
                           "backgrounds": len(events_view["backgrounds"]),
                           "note": "doors and scripts never follow a model; only the identified New Bark town-sign interaction has explicit alignment"},
                "limitations": ["collision is an aggregate grid: bounds never identify ownership",
                                "terrain mesh, BDHC/height, BGS, warps, triggers and scripts are preserved unchanged",
                                "rotation, scale, new models and terrain authoring remain unsupported"]}
        if include_grid:
            view["permissions"] = {"encoding": "type:collision byte pair, hex, row-major from the cell origin",
                                   "origin": [ox, oz], "rows": world.permission_rows(raw, context, overrides)}
        return view

    def map_scene(self, header=None, matrix=None, cell=None, image=True, pixels_per_tile=None,
                  thumbnail=None):
        """Textured scene description for a context, optionally with rendered images.

        The scene is resolved from the selected context's own map member, area data
        and tilesets; it shares :meth:`map_view`'s composed placement state, so the
        render, the permission grid and the CLI never disagree. Unsupported models
        are reported by name and skipped rather than hiding the map.
        """
        from . import mapscene
        description = mapscene.scene(self, header=header, matrix=matrix, cell=cell)
        context = self.context(header=header, matrix=matrix, cell=cell)
        if image:
            pixels = mapscene.PIXELS_PER_TILE if pixels_per_tile is None else pixels_per_tile
            description["image"] = str(mapscene.scene_image(self, context, description, pixels))
            description["pixels_per_tile"] = pixels
            description["image_size"] = [world.MAP_SIZE * pixels] * 2
        if thumbnail is not None:
            description["thumbnail"] = mapscene.model_thumbnail(self, context, thumbnail)
        return description

    def map_thumbnail(self, model_id, header=None, matrix=None, cell=None):
        """One building model's preview image, resolved through the context's tilesets."""
        from . import mapscene
        require(type(model_id) is int and model_id >= 0, "Model id must be a non-negative integer")
        context = self.context(header=header, matrix=matrix, cell=cell)
        return mapscene.model_thumbnail(self, context, model_id)

    def map_neighborhood(self, header=None, matrix=None, cell=None, image=True, include_grid=False, summary=False, scenes=True):
        from . import neighborhood
        result = neighborhood.describe(self, header, matrix, cell, image, include_grid, scenes)
        return neighborhood.summarize(result) if summary else result

    def map_events(self, header=None, matrix=None, cell=None):
        context = self.context(header=header, matrix=matrix, cell=cell)
        return event_authoring.view(self, context, self.composed())

    def plan_area_edit(self, operations, label=None):
        """Stage a group of explicit operations, validated together, without writes."""
        require(isinstance(operations, list) and 1 <= len(operations) <= 64,
                'An area edit needs 1..64 operations', 'INVALID_INPUT')
        require(label is None or isinstance(label, str) and 0 < len(label) <= 160, 'Invalid area label')
        self.composed()
        # Presets expand into the ordinary story operations an author could write by hand.
        from . import presets
        expanded = []
        for operation in operations:
            if (isinstance(operation, dict) and operation.get('kind') == 'environment'
                    and isinstance(operation.get('request'), dict) and operation['request'].get('action') == 'place'):
                # KIT-02: an environment preset placement is the ordinary operations it stands for.
                require(set(operation) == {'kind', 'context', 'request'} and isinstance(operation['context'], dict),
                        'Each operation needs kind, context and request', 'INVALID_INPUT')
                expanded += environments.expand(self, operation['context'], operation['request'])
            elif isinstance(operation, dict) and operation.get('kind') == 'preset':
                require(set(operation) == {'kind', 'context', 'request'} and isinstance(operation['context'], dict),
                        'Each operation needs kind, context and request', 'INVALID_INPUT')
                expanded += presets.expand(self, operation['context'], operation['request'])
            elif (isinstance(operation, dict) and operation.get('kind') == 'ground'
                  and isinstance(operation.get('request'), dict) and 'source' in operation['request']):
                # A ground-material register/revise may name its import folder: it is baked
                # here (in memory) and becomes the ordinary package operation.
                request = dict(operation['request'])
                staged = ground_materials.stage(self, request.pop('source'))
                require(set(request) <= {'action'} and request.get('action', staged['operation']['action'])
                        == staged['operation']['action'], 'A staged ground import names only its source (and action)',
                        'INVALID_INPUT')
                expanded.append({**operation, 'request': staged['operation']})
            elif (isinstance(operation, dict) and operation.get('kind') == 'prop'
                  and isinstance(operation.get('request'), dict) and 'source' in operation['request']
                  and operation['request'].get('action') in (None, 'register', 'revise')):
                # A prop register/revise may name its import folder, like ground materials: baked
                # here (in memory, staged for the commit) into the ordinary package operation.
                request = dict(operation['request'])
                staged = self.stage_prop_source(request.pop('source'))
                require(set(request) <= {'action'} and request.get('action', staged['operation']['action'])
                        == staged['operation']['action'], 'A staged prop import names only its source (and action)',
                        'INVALID_INPUT')
                require(not staged['unchanged'], f"Prop {staged['asset']} is unchanged", 'NO_CHANGE')
                expanded.append({**operation, 'request': staged['operation']})
            elif (isinstance(operation, dict) and operation.get('kind') == 'pokemon'
                  and isinstance(operation.get('request'), dict) and 'source' in operation['request']):
                # A Pokémon package import/revise may name its prepared folder: validated here
                # (in memory) and turned into the ordinary package operation.
                request = dict(operation['request'])
                require(set(request) <= {'action', 'key', 'source', 'display', 'template', 'icon_palette'},
                        'A staged Pokémon import names key, source, display, template and icon_palette',
                        'INVALID_INPUT')
                staged = pokemon_packages.stage(self, request['source'], request.get('key'), request.get('display'),
                                                request.get('template'), request.get('icon_palette'))
                require(request.get('action', staged['operation']['action']) == staged['operation']['action'],
                        f"This package is a {staged['operation']['action']}", 'INVALID_INPUT')
                expanded.append({**operation, 'request': staged['operation']})
            else:
                expanded.append(operation)
        operations = expanded
        # The trial starts from this Project's validated composition (a snapshot
        # hit, not a replay) and composes only the new transactions in order.
        trial = self._fork({**self.doc, 'map_edits': list(self.doc['map_edits'])})
        snapshots.restore(trial, self._current_snapshot())
        start = len(trial.doc['map_edits'])
        for operation in operations:
            require(isinstance(operation, dict) and set(operation) == {'kind', 'context', 'request'},
                    'Each operation needs kind, context and request', 'INVALID_INPUT')
            kind, request = operation['kind'], operation['request']
            require(isinstance(request, dict), 'Operation request must be an object', 'INVALID_INPUT')
            require(all(isinstance(k,str) and not k.startswith('_') for k in request),
                    'Internal replay options are not authoring fields','INVALID_INPUT')
            state, index = trial.composed(), len(trial.doc['map_edits'])
            context = trial.context(**operation['context'])
            if kind == 'border':
                # A stock-bordered path or tall-grass patch: one surface transaction per
                # touched cell (seam continuity), then each cell's behaviors.
                from . import border_authoring
                require(set(request) <= {'family', 'tiles', 'label', 'window', 'material', 'erase', 'behavior'},
                        'Border fields are family, tiles, window, material, erase, behavior and label', 'INVALID_INPUT')
                ground = border_authoring.behavior_of(request.get('family'), request.get('behavior'))
                header = context['header']['id']
                for cx, cy in border_authoring.touched_cells(trial, header, request.get('family'), request.get('tiles'),
                                                             request.get('window'), trial.composed(),
                                                             request.get('material'), request.get('erase', False)):
                    cell_ctx = trial.context(header=header, cell=[cx, cy])
                    t = border_authoring.plan(trial, cell_ctx, trial.composed(), len(trial.doc['map_edits']), **request)
                    if border_authoring.changed(t):
                        trial.doc['map_edits'].append(t)
                        trial._append_composed(t)
                # A paved terrace top or stair landing keeps the behavior its terrace gave it.
                kept = set().union(*border_authoring.terrace_tiles(trial.composed(), header)[:2]) \
                    if request.get('material') else set()
                for (cx, cy), coords in sorted(border_authoring.interior_by_cell(request['tiles']).items()):
                    coords = [c for c in coords if tuple(c) not in kept]
                    if not coords:
                        continue
                    cell_ctx = trial.context(header=header, cell=[cx, cy])
                    # 'path' is ordinary walkable ground (also for an erased patch or decorative grass);
                    # 'grass' is encounter tall grass.
                    spec = {'coords': coords, 'ground': ground, 'blocked': False, 'stamp': None,
                            'decorative': request.get('behavior') == 'ground'}
                    cells = world_authoring.terrain_permissions(trial, cell_ctx, trial.composed(), spec)
                    if cells:
                        t = trial.plan_map_edit(header=header, cell=[cx, cy], permissions=cells,
                                                label=request.get('label') or 'Border behavior')['transaction']
                        trial.doc['map_edits'].append(t)
                        trial._append_composed(t)
                continue
            if kind == 'map_group' and request.get('action') in ('place', 'move', 'remove'):
                # GROUP-01/02: a group copy/move/removal is the ordinary transaction of every
                # element (objects, prop instances, tiles, signs/NPCs, entrance) plus one record.
                ops, record = map_groups.expand(trial, context, state, request)
                produced = []
                for sub_kind, ref, sub in ops:
                    sub_ctx = trial.context(header=ref['header'], cell=ref['cell'])
                    sub_state, sub_index = trial.composed(), len(trial.doc['map_edits'])
                    if sub_kind == 'scenery':
                        t = scenery.plan(trial, sub_ctx, sub_state, sub_index, **sub)
                    elif sub_kind == 'prop':
                        t = custom_props.plan(trial, sub_ctx, sub_state, sub_index, **sub)
                    elif sub_kind == 'interaction':
                        t = simple_interactions.plan(trial, sub_ctx, sub_state, sub_index, **sub)
                    elif sub_kind == 'world':
                        t = world_authoring.plan(trial, sub_ctx, sub_state, sub_index, **sub)
                    else:
                        t = trial.plan_map_edit(header=ref['header'], cell=ref['cell'], permissions=sub['permissions'],
                                                label=request.get('label') or 'Group tiles')['transaction']
                    trial.doc['map_edits'].append(t)
                    trial._append_composed(t)
                    produced.append(sub_index)
                name = request['name'] if request['action'] == 'place' else \
                    map_groups.placed(state)[request['instance']]['template']
                t = map_groups.plan(trial, context, trial.composed(), len(trial.doc['map_edits']), 'record', name=name,
                                    label=request.get('label'), produced=produced, **record)
                trial.doc['map_edits'].append(t)
                trial._append_composed(t)
                continue
            if kind == 'world' and request.get('action') == 'trees':
                # Decorative forest: one ordinary permission transaction per cell that still
                # has Headbutt-behavior tiles; collision and visuals are unchanged.
                from . import world_identity
                area = world_identity.tree_request(state, request)
                added = 0
                for cell in area['cells']:
                    cell_ctx = trial.context(header=area['header'], cell=cell['cell'])
                    cells = world_identity.tree_cells(trial, cell_ctx, trial.composed(), request.get('retain') or [])
                    if cells:
                        t = trial.plan_map_edit(header=area['header'], cell=cell['cell'], permissions=cells,
                                                label=request.get('label') or f"Decorative trees: {request['area']}")['transaction']
                        trial.doc['map_edits'].append(t)
                        trial._append_composed(t)
                        added += 1
                require(added, 'This area has no Headbutt-behavior trees left', 'NO_CHANGE')
                continue
            if kind == 'elevation':
                # Terrain authoring v1: a terrace/pond is validated globally once, then one
                # terrain transaction (model + height table) and one permission transaction
                # per touched cell; ambient edits remove inherited sound plates in one cell.
                spec = terrain_authoring.normalise(request)
                header = context['header']['id']
                if spec['action'] == 'ambient':
                    cell_ctx = trial.context(header=header, cell=spec['cell'])
                    t = terrain_authoring.plan_ambient(trial, cell_ctx, trial.composed(), len(trial.doc['map_edits']), spec)
                    trial.doc['map_edits'].append(t)
                    trial._append_composed(t)
                    continue
                feature = terrain_authoring.plan_feature(trial, trial.composed(), header, spec)
                for cell, cell_ctx in sorted(feature['cells'].items()):
                    t = terrain_authoring.plan_cell(trial, cell_ctx, trial.composed(), len(trial.doc['map_edits']),
                                                    spec, feature['ground'], feature['report'])
                    trial.doc['map_edits'].append(t)
                    trial._append_composed(t)
                for cell, cell_ctx in sorted(feature['cells'].items()):
                    cells = terrain_authoring.permission_cells(trial, trial.composed(), cell_ctx, feature)
                    if cells:
                        t = trial.plan_map_edit(header=header, cell=list(cell), permissions=cells,
                                                label=(spec.get('label') or spec['action'].title()) + ' collision')['transaction']
                        trial.doc['map_edits'].append(t)
                        trial._append_composed(t)
                continue
            if kind == 'terrain':
                # Visible ground (surface v4), then collision/encounter behavior judged
                # against the new visuals: two transactions, one atomic batch.
                spec = world_authoring.terrain_request(trial, context, request)
                if spec['surface']:
                    t = surface_authoring.plan(trial, context, state, index, **spec['surface'], _version=4)
                    if surface_authoring.changed(t):
                        trial.doc['map_edits'].append(t)
                        trial._append_composed(t)
                if spec['ground'] is not None or spec['blocked'] is not None or spec['stamp']:
                    cells = world_authoring.terrain_permissions(trial, context, trial.composed(), spec)
                    if cells:
                        t = trial.plan_map_edit(header=context['header']['id'],
                                                cell=[context['cell']['x'], context['cell']['y']],
                                                permissions=cells, label=spec['label'] or 'Terrain behavior')['transaction']
                        trial.doc['map_edits'].append(t)
                        trial._append_composed(t)
                continue
            try:
                if kind == 'world':
                    t = world_authoring.plan(trial, context, state, index, **request)
                    changed = True
                elif kind == 'story':
                    t = story_authoring.plan(trial, context, state, index, **request)
                    changed = story_authoring.changed(state, t)
                elif kind == 'group':
                    t = linked_groups.plan(trial, context, state, index, **request)
                    changed = t['before'] != t['after']
                elif kind == 'interior':
                    t = interiors.plan(trial, context, state, index, **request)
                    changed = True
                elif kind == 'scenery':
                    t = scenery.plan(trial, context, state, index, **request)
                    changed = bool(t['objects'] or t['permissions'])
                elif kind == 'event':
                    t = event_authoring.plan(trial, context, state, index, **request)
                    changed = bool(t['changes'])
                elif kind == 'gameplay':
                    t = gameplay.plan_v3(trial, state, index, context['header']['id'], **request)
                    changed = bool(t['changes'])
                elif kind == 'data':
                    t = game_data.plan(trial, context, state, index, **request)
                    changed = bool(t['changes'])
                elif kind == 'prop':
                    t = custom_props.plan(trial, context, state, index, **request)
                    changed = True
                elif kind == 'ground':
                    t = ground_materials.plan(trial, state, index, **request)
                    changed = True
                elif kind == 'travel':
                    t = travel_points.plan(trial, state, index, **request)
                    changed = True
                elif kind == 'field':
                    t = field_features.plan(trial, context, state, index, **request)
                    changed = True
                elif kind == 'petals':
                    t = petal_effect.plan(trial, state, index, **request)
                    changed = True
                elif kind == 'repair':
                    t = runtime_repairs.plan(trial, state, index, **request)
                    changed = True
                elif kind == 'pokemon':
                    t = pokemon_packages.plan(trial, state, index, **request)
                    changed = True
                elif kind == 'map_group':
                    t = map_groups.plan(trial, context, state, index, **request)
                    changed = True
                elif kind == 'environment':
                    t = environments.plan(trial, state, index, **request)
                    changed = True
                elif kind == 'decal':
                    t = ground_decals.plan(trial, context, state, index, **request)
                    changed = t['model_before_sha256'] != t['model_after_sha256'] or t['before'] != t['after']
                elif kind == 'surface':
                    t = surface_authoring.plan(trial, context, state, index, **request)
                    changed = surface_authoring.changed(t)
                elif kind == 'interaction':
                    t = simple_interactions.plan(trial, context, state, index, **request)
                    changed = t['before'] != t['after']
                elif kind == 'map':
                    t = trial.plan_map_edit(**operation['context'], **request)['transaction']
                    terrain_authoring.require_free(state, context['header']['id'],
                                                   [(c['x'], c['z']) for c in t['permissions']], 'Permission edit')
                    changed = bool(t['placements'] or t['permissions'] or t.get('sign_interaction'))
                else:
                    require(False, 'Unknown area operation', 'INVALID_INPUT')
            except TypeError as exc:
                raise EditorError('INVALID_INPUT', 'Invalid area operation fields') from exc
            if changed:
                trial.doc['map_edits'].append(t)
                trial._append_composed(t)
            if kind == 'world' and request.get('action') == 'identity':
                # Parented interiors follow their parent's marker, region and shared name in
                # the same atomic batch (one ordinary identity transaction per child).
                from . import world_identity
                for child in world_identity.children(trial.composed(), request.get('area')):
                    area = world_authoring.areas(trial.composed())[child]
                    cell_ctx = trial.context(header=area['header'], cell=area['cells'][0]['cell'])
                    try:
                        t = world_identity.plan_identity(trial, cell_ctx, trial.composed(), len(trial.doc['map_edits']),
                                                         area=child, parent=request['area'],
                                                         label=f'Follow parent: {child}')
                    except EditorError as exc:
                        if exc.code != 'NO_CHANGE':
                            raise
                        continue
                    trial.doc['map_edits'].append(t)
                    trial._append_composed(t)
        trial._validate_composition(trial._checked_moves(trial.doc), trial.composed())
        trial._snapshot_blob = snapshots.capture(trial)
        snapshots.store(snapshots.key(trial, trial.doc), trial._snapshot_blob)
        transactions = trial.doc['map_edits'][start:]
        return {'transactions': transactions, 'empty': not transactions, 'label': label or 'Area edit',
                'preview': trial.diff()[len(self.diff()):]}

    def area_preview_project(self, plan):
        trial = self._fork({**self.doc, 'map_edits': self.doc['map_edits'] + copy.deepcopy(plan['transactions'])})
        trial._validate_state(trial.doc)
        return trial

    def _current_snapshot(self):
        """Snapshot bytes of this Project's current validated composition."""
        blob = getattr(self, '_snapshot_blob', None)
        if blob is None:
            blob = snapshots.lookup(snapshots.key(self, self.doc))
            if blob is None:
                self._validate_state(self.doc)
                blob = self._snapshot_blob
            self._snapshot_blob = blob
        return blob

    def apply_area_edit(self, expected_revision, **request):
        with self._locked(expected_revision):
            plan = self.plan_area_edit(**request)
            if not plan['empty']:
                self._write_staged_packages(plan['transactions'])
                self._commit('map.transaction', {'map_edits': self.doc['map_edits'] + plan['transactions']})
        return {'revision': self.doc['revision'], 'changed': not plan['empty'], 'preview': plan['preview']}

    def surface_palette(self, header=None, matrix=None, cell=None, images=False):
        context = self.context(header=header, matrix=matrix, cell=cell)
        entries = surface_authoring.palette(self, context)
        if images:
            from PIL import Image
            from . import mapscene, nitro
            from .preview import _save_cache
            _, blobs = mapscene.tilesets(self, context)
            _, prims = nitro.decode_model(surface_authoring.model(self, context, self.composed()), tileset=blobs['map_tileset'])
            for entry in entries:
                texture = next(p.texture for p in prims if p.material['name'] == entry['material'])
                if texture is not None:
                    key = digest(texture.tobytes())[:20]
                    entry['image'] = str(_save_cache(self, f'surface-{key}.png', lambda t=texture: Image.fromarray(t)))
        return entries

    def sample_surface(self, x, z, header=None, matrix=None, cell=None):
        from .workflow import sample
        return sample(self, self.context(header=header, matrix=matrix, cell=cell), x, z)

    def plan_workflow(self, actions, label=None):
        from .workflow import plan
        return plan(self, actions, label)

    def apply_workflow(self, expected_revision, actions, label=None):
        with self._locked(expected_revision):
            plan = self.plan_workflow(actions, label)
            if not plan['empty']:
                last = actions[-1]['context']
                context = self.area_preview_project(plan).context(**last)
                selection = {'header': context['header']['id'], 'cell': [context['cell']['x'], context['cell']['y']], 'workspace': True}
                self._commit('map.transaction', {'map_edits': self.doc['map_edits'] + plan['transactions'], 'map_selection': selection})
        return {'revision': self.doc['revision'], 'changed': not plan['empty'], 'actions': plan['actions'], 'preview': plan['preview']}

    def linked_groups(self, header=None, cell=None):
        groups = self.composed().get('groups', {})
        return copy.deepcopy([g for g in groups.values() if (header is None or g['context']['header'] == header)
                              and (cell is None or g['context']['cell'] == list(cell))])

    def story_library(self):
        state = self.composed()
        return {'characters': story_authoring.character_rows(state),
                'trainers': copy.deepcopy(story_authoring.catalog(state, 'trainer')),
                'states': copy.deepcopy(story_authoring.catalog(state, 'state')),
                'sequences': copy.deepcopy(story_authoring.catalog(state, 'sequence')),
                'stock_sprites': [{'stock_sprite': tag, 'name': v['name']} for tag, v in scene_authoring.STOCK_SPRITES.items()]
                                 + [{'stock_sprite': e['sprite'], 'name': e['name']} for e in self.npc_appearances()]}

    def capacity(self):
        from . import capacity
        return capacity.report(self)

    def world_identity(self):
        """Player-visible identity, Pokégear placement, parents, static forest cells and
        remaining Headbutt-behavior tiles of every created area, plus valid values/limits."""
        from . import world_identity
        state = self.composed()
        return {**world_identity.view(self, state), 'headbutt_tiles': world_identity.headbutt_tiles(self, state)}

    def prop_view(self):
        """Custom prop assets, placed instances and collision ownership (read-only)."""
        return custom_props.view(self)

    def _write_staged_packages(self, transactions):
        """Write the content-addressed packages a committing batch registers or revises."""
        for t in transactions:
            if pokemon_packages.is_transaction(t) and t['action'] in ('import', 'revise'):
                key = (t['request']['key'], t['request']['package'])
                staged = pokemon_packages.pending(self).get(key)
                if staged is not None:
                    pokemon_packages.write_package(self, *key, staged['files'])
                    pokemon_packages.pending(self).pop(key)
                pokemon_packages.verify_package(self, *key)
            for module, key_name in ((custom_props, 'asset'), (ground_materials, 'material')):
                if module.is_transaction(t) and t['action'] in ('register', 'revise'):
                    key = (t['request'][key_name], t['request']['package'])
                    staged = module.pending(self).get(key)
                    if staged is not None:
                        module.write_package(self, *key, staged['files'])
                        module.pending(self).pop(key)
                    module.verify_package(self, *key)

    def field_view(self):
        """Placed native field-move features (whirlpools) and their crossings."""
        return field_features.view(self.composed())

    def environment_view(self):
        """Reusable named environments: members with their package ids, presets and donors (KIT-02)."""
        return environments.view(self, self.composed())

    def map_group_view(self):
        """Reusable group templates (shared vs copied report) and placed group instances (GROUP-01/02)."""
        return map_groups.view(self.composed())

    def pokemon_view(self):
        """Imported Pokémon packages and their persistent form identities (POKE-01..04)."""
        return pokemon_packages.view(self, self.composed())

    def stage_pokemon_source(self, folder, key, display, template, icon_palette):
        """Validate a prepared Pokémon package folder in memory (no writes); returns the operation."""
        return pokemon_packages.stage(self, folder, key, display, template, icon_palette)

    def repair_view(self):
        """Qualified base-ROM repairs, whether this baseline supports them and which are applied."""
        return runtime_repairs.view(self, self.composed())

    def petal_view(self):
        """Areas with airborne petals, their settings and derived runtime numbers (EFFECT-01)."""
        return petal_effect.view(self, self.composed())

    def travel_view(self):
        """Authored respawn/Fly points: spawn IDs, flags, tiles and capacity (TRAVEL-01, FIELD-05)."""
        return travel_points.view(self, self.composed())

    def ground_view(self):
        """Registered ground materials: package, roles, variants per area data and users."""
        state = self.composed()
        out = {}
        for mid, entry in ground_materials.materials(state).items():
            body = ground_materials.verify_package(self, mid, entry['package'])
            out[mid] = {'display': body['display'], 'package': entry['package'], 'revision': entry['revision'],
                        'roles': body['roles'], 'variants': body['variants'],
                        'area_variants': entry.get('variants', {}), 'users': ground_materials.users(self, state, mid),
                        'textures': {r: ground_materials.texture_name(mid, r) for r in body['roles']}}
        return {'materials': out, 'shown_areas': ground_materials.shown_areas(self, state)}

    def stage_ground_source(self, folder):
        """Bake a ground-material folder in memory (no writes); returns the operation to apply."""
        return ground_materials.stage(self, folder)

    def stage_prop_source(self, folder):
        """Bake an import/reimport folder in memory (no writes) and stage its package for
        previews. Returns the package id, bake report and impact on existing instances."""
        source = custom_props.load_source(folder)
        sha, files, body = custom_props.package(source)
        custom_props.pending(self)[(body['id'], sha)] = {'files': files, 'body': body}
        state = self.composed(); registry = custom_props.assets(state)
        current = registry.get(body['id'])
        impact = sorted(k for k, i in custom_props.instances(state).items() if i['asset'] == body['id'])
        return {'asset': body['id'], 'package': sha, 'report': body['report'], 'display': body['display'],
                'collision': body['collision'], 'registered': current is not None,
                'revision': current['revision'] if current else None,
                'unchanged': bool(current and current['package'] == sha), 'affected_instances': impact,
                'operation': ({'action': 'revise', 'asset': body['id'], 'package': sha, 'expected_revision': current['revision']}
                              if current else {'action': 'register', 'asset': body['id'], 'package': sha})}

    def plan_prop_edit(self, operations, label=None, context=None):
        """Preview prop operations (register/revise/place/duplicate/move/remove/collision) as one batch."""
        ctx = context or {'header': 67, 'cell': [17, 12]}
        return self.plan_area_edit([{'kind': 'prop', 'context': op.get('context', ctx),
                                     'request': {k: v for k, v in op.items() if k != 'context'}} for op in operations],
                                   label or 'Custom props')

    def apply_prop_edit(self, expected_revision, operations, label=None, context=None):
        with self._locked(expected_revision):
            plan = self.plan_prop_edit(operations, label, context)
            if not plan['empty']:
                for t in plan['transactions']:
                    if t['action'] in ('register', 'revise'):
                        key = (t['request']['asset'], t['request']['package'])
                        staged = custom_props.pending(self).get(key)
                        if staged is not None:
                            custom_props.write_package(self, *key, staged['files'])
                            custom_props.pending(self).pop(key)
                        custom_props.verify_package(self, *key)
                self._commit('map.transaction', {'map_edits': self.doc['map_edits'] + plan['transactions']})
        return {'revision': self.doc['revision'], 'changed': not plan['empty'], 'preview': plan['preview']}

    def terrain_view(self, header):
        """Terrain authoring v1 inspect: family, limits, features and sound plates of an area."""
        return terrain_authoring.view(self, header)

    def runtime_report(self):
        from . import capacity
        return capacity.runtime_report(self)

    def gameplay_data(self, header=34):
        return gameplay.inspect(self, header)

    def gameplay_areas(self, search='', offset=0, limit=40):
        return gameplay.areas(self, search, offset, limit)

    def gameplay_species(self, species, form=0):
        return gameplay.species_data(self, species, form)

    def gameplay_catalog(self, kind, search='', offset=0, limit=40):
        return gameplay.catalog(self, kind, search, offset, limit)

    def plan_gameplay_edit(self, operations, header=34):
        t = gameplay.plan_v3(self, self.composed(), len(self.doc['map_edits']), header, operations)
        if t['changes']:
            self._validate_state({**self.doc, 'map_edits': self.doc['map_edits'] + [t]})
        return {'transaction': t, 'preview': t['preview'], 'empty': not t['changes']}

    def apply_gameplay_edit(self, expected_revision, **request):
        with self._locked(expected_revision):
            plan = self.plan_gameplay_edit(**request)
            if not plan['empty']:
                self._commit('map.transaction', {'map_edits': self.doc['map_edits'] + [plan['transaction']]})
        return {'revision': self.doc['revision'], 'changed': not plan['empty'], 'preview': plan['preview']}

    # Records are global; the transaction is anchored to Cherrygrove's stock context
    # (every pinned-baseline project has it), as area-edit anchors any operation.
    DATA_CONTEXT = {'header': 67, 'cell': [17, 12]}

    def data_catalog(self, kind, search='', offset=0, limit=40):
        return game_data.catalog(self, kind, search, offset, limit)

    def data_record(self, kind, ident):
        """Current decoded record plus the before_sha256 an edit must quote."""
        state = self.composed()
        require(kind in ('move', 'item', 'shop'), 'Record kind is move, item or shop', 'INVALID_INPUT')
        if kind == 'shop':
            row = next((r for r in game_data.shop_rows(self, state) if r['name'] == ident), None)
            return {'kind': 'shop', 'name': ident, 'exists': row is not None, 'before': row['items'] if row else None,
                    'record': row}
        require(type(ident) is int, 'Choose a record ID', 'INVALID_INPUT')
        path = game_data.MOVES if kind == 'move' else game_data.ITEMS
        raw = game_data._members(self, state, path)(ident)
        view = game_data.decode_move(raw) if kind == 'move' else game_data.decode_item(raw)
        entry = (game_data.move_entry if kind == 'move' else game_data.item_entry)(self, state, ident)
        return {'kind': kind, 'id': ident, 'name': entry['name'], 'supported': entry['supported'], 'reason': entry['reason'],
                'record': view, 'before_sha256': digest(raw)}

    def plan_data_edit(self, operations, label=None):
        return self.plan_area_edit([{'kind': 'data', 'context': self.DATA_CONTEXT,
                                     'request': {'operations': operations, 'label': label}}], label=label)

    def apply_data_edit(self, expected_revision, operations, label=None):
        return self.apply_area_edit(expected_revision, operations=[{'kind': 'data', 'context': self.DATA_CONTEXT,
                                                                   'request': {'operations': operations, 'label': label}}],
                                    label=label)

    # ---- portability and checkpoints (RECOVERY-01/02), shared by UI and CLI ----
    def package(self, output, include_baseline=False):
        from . import recovery
        return recovery.package(self, output, include_baseline)

    def checkpoint(self, name, expected_revision, note=None):
        from . import recovery
        return recovery.checkpoint(self, name, expected_revision, note)

    def checkpoints(self):
        from . import recovery
        return recovery.checkpoints(self)

    def restore_checkpoint(self, name, expected_revision):
        from . import recovery
        return recovery.restore(self, name, expected_revision)

    def coverage(self):
        from . import coverage
        return coverage.report(self)

    # ---- workspace (WORKSPACE-01..03, ACCESS-01): read-only, shared by UI and CLI ----
    def workspace_search(self, text='', kinds=None, limit=200):
        from . import workspace
        return workspace.search(self, text, kinds, limit)

    def workspace_references(self, ref):
        from . import workspace
        return workspace.references(self, ref)

    def progression(self):
        from . import workspace
        return workspace.progression(self)

    def reach(self, header=None, x=None, z=None, save=None, surf=False, conditional=True):
        from . import workspace, save_read
        flags = ()
        if save is not None:
            found = save_read.read(save)
            header, x, z, flags = found['header'], found['x'], found['z'], found['flags']
        require(all(type(v) is int for v in (header, x, z)), 'Choose a start tile or a save', 'INVALID_INPUT')
        result = workspace.reach(self, header, x, z, flags=flags, surf=surf, conditional=conditional)
        if save is not None:
            result['save'] = {k: v for k, v in found.items() if k != 'flags'} | {'flags_set': len(found['flags'])}
        return result

    def impact(self, operations, label=None):
        from . import workspace
        return workspace.impact(self, operations, label)

    def scene_report(self, key):
        """Read-only map-qualified beat trace, shared by the UI and agent CLI."""
        state = self.composed()
        spec = story_authoring.catalog(state, 'sequence').get(key)
        require(spec is not None, 'Unknown scene', 'NOT_FOUND')
        trace = []
        scene_authoring.validate_routes(self, state, spec, state.get('scene_versions', {}).get(key, 1), trace)
        routes = scene_authoring.gather_routes(self, state, spec)
        return {'scene': key, 'revision': self.doc['revision'], 'context': spec['context'],
                'directions': {'0':'north','1':'south','2':'west','3':'east'},
                'approaches': {k:[{'start':list(p),'path':[list(q) for q in path]} for p,path in paths.items()] for k,paths in routes.items()},
                'beats': trace, 'native_acceptance': 'not established by this report'}

    def character_package(self, key):
        package = story_authoring.catalog(self.composed(), 'character').get(key)
        require(package is not None, 'Unknown character', 'NOT_FOUND')
        return copy.deepcopy(package)

    def npc_appearances(self):
        from . import npc_behavior
        return npc_behavior.palette(self)

    def simple_interactions(self, header=None, matrix=None, cell=None):
        context = self.context(header=header, matrix=matrix, cell=cell)
        return [{'identity': key, **value} for key, value in simple_interactions.specs(self.composed()).items()
                if value['event_member'] == context['event_member']]

    def plan_event_edit(self, header=None, matrix=None, cell=None, kind=None, event_id=None,
                        values=None, reciprocal=False, label=None):
        context = self.context(header=header, matrix=matrix, cell=cell)
        transaction = event_authoring.plan(self, context, self.composed(), len(self.doc["map_edits"]),
                                           kind, event_id, {} if values is None else values, reciprocal, label)
        preview = event_authoring.summary(transaction)
        if transaction["changes"]:
            candidate = {**self.doc, "map_edits": self.doc["map_edits"] + [transaction]}
            composed = self._validate_state(candidate)
        else:
            composed = self.composed()
        record = event_authoring.lookup(self, context["event_member"], kind, event_id, composed)
        preview["association"] = event_authoring.association(context, record)
        if kind == "warp":
            preview["connection"] = event_authoring.connection(self, context["header"]["id"], record, composed)
            preview["note"] = "Only listed endpoints change. Other incoming links and door models/collision remain in place."
        else:
            preview["note"] = "Scripts, dialogue, sprite, movement behavior, height, collision and model placement preserved."
        return {"transaction": transaction, "preview": preview, "empty": not transaction["changes"]}

    def apply_event_edit(self, expected_revision, **request):
        with self._locked(expected_revision):
            plan = self.plan_event_edit(**request)
            if not plan["empty"]:
                t = plan["transaction"]
                selection = {"header": t["context"]["header"], "cell": t["context"]["cell"],
                             "event_kind": t["request"]["kind"], "event_id": t["request"]["event_id"]}
                self._commit("map.transaction", {"map_edits": self.doc["map_edits"] + [t], "map_selection": selection})
        return {"revision": self.doc["revision"], "changed": not plan["empty"], "preview": plan["preview"]}

    def map_sign(self, header=None, matrix=None, cell=None):
        """Inspect only the identified town-sign binding; never guess nearby ownership."""
        context = self.context(header=header, matrix=matrix, cell=cell)
        if not sign_interaction.supported(context):
            return {"supported": False}
        raw, _, _ = sign_interaction.binding(self, context)
        record = self.composed()["interactions"].get((57, 2), raw[44:64])
        x, z = struct.unpack_from("<2i", record, 4)
        return {"supported": True, "placement_slot": 13, "event_member": 57, "event_id": 2,
                "script": 15, "position": [x, z]}

    def map_palette(self, header=None, matrix=None, cell=None, images=False):
        """Compatible baseline templates from the selected map, with donor provenance."""
        from . import mapscene
        context = self.context(header=header, matrix=matrix, cell=cell)
        raw = self.member_raw(context["map_member"])
        state = self.composed()
        entries, seen = [], set()
        for prop in self.member_data(context["map_member"])[1]:
            offset = prop["record_offset"]
            record = raw[offset:offset + 48]
            key = (prop["model_id"], record[8:12], record[16:])
            if key in seen:
                continue
            seen.add(key)
            entry = {"slot": prop["slot"], "model_id": prop["model_id"], "status": "ok",
                     "donor": authoring.context_ref(context), "record_sha256": digest(record),
                     "height": prop["xyz"][1]}
            try:
                obj = {"id": f"baseline:{context['map_member']}:{prop['slot']}"}
                reason = scenery.protected(obj, context["map_member"], prop["slot"], state)
                require(reason is None, reason or "Bound object", "BOUND_OBJECT")
                asset = scenery.asset_dependency(self, context, record)
                entry.update(mapscene.describe_model(prop["model_id"], asset["name"]))
                if images:
                    entry["thumbnail"] = self.map_thumbnail(prop["model_id"], header=context["header"]["id"],
                                                            cell=[context["cell"]["x"], context["cell"]["y"]])
            except EditorError as exc:
                entry.update(status="unsupported", reason=str(exc), code=exc.code,
                             display=f"Model {prop['model_id']}")
            entries.append(entry)
        return {"context": authoring.context_ref(context), "templates": entries,
                "scope": "existing records in this map; copied height/transform; events unchanged",
                "shared_cells": context["shared_cells"]}

    def plan_scenery_edit(self, operation, slot, header=None, matrix=None, cell=None,
                          x=None, z=None, destination=None, permissions=(),
                          destination_permissions=(), move_collision=(), label=None):
        context = self.context(header=header, matrix=matrix, cell=cell)
        state = copy.deepcopy(self.composed())
        transaction = scenery.plan(self, context, state, len(self.doc["map_edits"]),
                                    operation, slot, x, z, destination, permissions,
                                    destination_permissions, move_collision, label)
        preview = scenery.summary(transaction)
        preview["shared_cells"] = context["shared_cells"]
        preview["note"] = "Full donor record preserved except X/Z; events and height unchanged. Collision cells are explicit."
        return {"transaction": transaction, "empty": not transaction["objects"] and not transaction["permissions"],
                "preview": preview, "patches": []}

    def apply_scenery_edit(self, expected_revision, **request):
        with self._locked(expected_revision):
            plan = self.plan_scenery_edit(**request)
            if plan["empty"]:
                return {"revision": self.doc["revision"], "changed": False, "preview": plan["preview"]}
            edits = copy.deepcopy(self.doc["map_edits"]) + [plan["transaction"]]
            self._validate_state({"positions": self.doc["positions"],
                                  "placement_moves": self.doc["placement_moves"], "map_edits": edits})
            target = plan["transaction"]["destination_context"]
            survivor = next((o for o in reversed(plan["transaction"]["objects"]) if o["after"] is not None), None)
            selection = {"header": target["header"], "cell": target["cell"], "object_id": survivor["id"]} if survivor else None
            self._commit("map.transaction", {"map_edits": edits, "map_selection": selection})
        return {"revision": self.doc["revision"], "changed": True,
                "preview": plan["preview"], "changes": self.diff()}

    def permission_cells(self, header=None, matrix=None, cell=None, x=None, z=None, width=1, height=1):
        """A small explicit window of permission cells, for selection and review."""
        context = self.context(header=header, matrix=matrix, cell=cell)
        raw = self.member_raw(context["map_member"])
        composed = self.composed()["permissions"]
        ox, oz = context["origin"]
        x = ox if x is None else x
        z = oz if z is None else z
        require(type(width) is int and type(height) is int and 0 < width <= 32 and 0 < height <= 32,
                "Window must be 1..32 tiles on each axis")
        cells = []
        for zz in range(z, z + height):
            for xx in range(x, x + width):
                offset = world.cell_offset(context, xx, zz)
                pair = composed.get((context["map_member"], offset), raw[offset:offset + 2])
                cells.append({"x": xx, "z": zz, "offset": offset, "value": pair.hex(),
                              "type": pair[0], "collision": pair[1], "blocked": world.is_blocked(pair)})
        return {"context": context["id"], "map_member": context["map_member"],
                "window": {"x": x, "z": z, "width": width, "height": height}, "cells": cells}

    def plan_map_edit(self, header=None, matrix=None, cell=None, placement=None,
                      permissions=(), move_collision=(), label=None, align_sign=False):
        """Build (never save) one transaction; the same call backs preview and apply."""
        context = self.context(header=header, matrix=matrix, cell=cell)
        member = context["map_member"]
        raw = self.member_raw(member)
        _, props, _ = self.member_data(member)
        composed = self.composed()
        if placement is not None:
            require(isinstance(placement, dict), "Placement request must be an object", "UNSUPPORTED_EDIT")
            slot = placement.get("slot")
            if align_sign:
                require(slot == sign_interaction.SLOT, "Select the identified New Bark town sign to align its interaction",
                        "UNSUPPORTED_INTERACTION")
            require(type(slot) is int and slot >= 0 and slot in scenery.table_for(self, context, composed),
                    f"No placement {member}:{slot}; object absent, removed or transferred", "NOT_FOUND")
            if slot >= len(props):
                require(not align_sign and set(placement) <= {"slot", "x", "z"},
                        "Created scenery supports X/Z and explicit collision only", "UNSUPPORTED_EDIT")
                current = authoring.global_from_record(context, scenery.words(composed["objects"][member][slot]["raw"]))
                return self.plan_scenery_edit(header=context["header"]["id"], cell=[context["cell"]["x"], context["cell"]["y"]],
                    operation="move", slot=slot, x=placement.get("x", current["x"]), z=placement.get("z", current["z"]),
                    permissions=permissions, move_collision=move_collision, label=label)
        overrides = {offset: value for (m, offset), value in composed["permissions"].items() if m == member}
        require(isinstance(permissions, (list, tuple)) and isinstance(move_collision, (list, tuple)),
                "Permission and collision selections must be lists")
        explicit = []
        for cell in permissions:
            authoring.require_cell_request(cell)
            explicit.append(dict(cell))
        translated = []
        if move_collision:
            require(isinstance(placement, dict) and any(k in placement for k in ("x", "z")),
                    "Moving collision with a placement needs a placement target", "UNSUPPORTED_EDIT")
            slot = placement.get("slot")
            require(type(slot) is int and 0 <= slot < len(props), f"No placement {member}:{slot}", "NOT_FOUND")
            record = composed["placements"].get((member, slot), authoring.record_state(props[slot]))
            before = authoring.global_from_record(context, record)
            authoring.require_anchor(context, {axis: placement.get(axis, before[axis]) for axis in ("x", "z")})
            delta = (placement.get("x", before["x"]) - before["x"], placement.get("z", before["z"]) - before["z"])
            require(all(float(value).is_integer() for value in delta),
                    "Collision follows a placement only in whole tiles", "UNQUALIFIED_TARGET")
            translated = authoring.translate_selection(context, raw, overrides,
                                                       [dict(c) for c in move_collision],
                                                       (int(delta[0]), int(delta[1])))
        cells = authoring.merge_cells(explicit, translated)
        index = len(self.doc["map_edits"])
        transaction = authoring.build(context, raw, props, placement, cells, index, label,
                                      composed["placements"], composed["permissions"])
        require(type(align_sign) is bool, "align_sign must be a boolean", "UNSUPPORTED_EDIT")
        if align_sign:
            require(placement is None or placement.get("slot") == sign_interaction.SLOT,
                    "Select the identified New Bark town sign to align its interaction", "UNSUPPORTED_INTERACTION")
            placement_records = dict(composed["placements"])
            for change in transaction["placements"]:
                placement_records[(member, change["slot"])] = change["record_after"]
            interaction = sign_interaction.plan(self, context, placement_records, composed["interactions"])
            if interaction is not None:
                transaction.update(schema="sovereign-map-transaction-v2", version=2, sign_interaction=interaction)
        _require_disjoint(authoring.transaction_domain(transaction), composed["legacy_domain"])
        events_view = events(self.resource(world.EVENT_ARCHIVE, context["event_member"])[1])
        preview = authoring.summarise(transaction, events_view, context)
        patches = authoring.patches(context, transaction)
        if transaction.get("sign_interaction"):
            patches.extend(sign_interaction.patches(self, transaction["sign_interaction"]))
            preview["sign_interaction"] = transaction["sign_interaction"]
            if not transaction["permissions"]:
                preview["permissions"] = "movement permissions unchanged; town-sign interaction aligned"
            preview["unchanged"]["note"] = "Identified town-sign X/Z aligned; its script, text, kind, height and direction preserved. Other events unchanged."
        return {"transaction": transaction, "empty": authoring.is_empty(transaction),
                "preview": preview, "patches": patches}

    def apply_map_edit(self, expected_revision, header=None, matrix=None, cell=None, placement=None,
                       permissions=(), move_collision=(), label=None, align_sign=False):
        with self._locked(expected_revision):
            plan = self.plan_map_edit(header=header, matrix=matrix, cell=cell, placement=placement,
                                      permissions=permissions, move_collision=move_collision, label=label,
                                      align_sign=align_sign)
            if plan["empty"]:
                return {"revision": self.doc["revision"], "changed": False,
                        "patch_count": 0, "preview": plan["preview"]}
            edits = copy.deepcopy(self.doc["map_edits"]) + [plan["transaction"]]
            self._validate_state({"positions": self.doc["positions"],
                                  "placement_moves": self.doc["placement_moves"], "map_edits": edits})
            self._commit("map.transaction", {"map_edits": edits})
        return {"revision": self.doc["revision"], "changed": True, "transaction": plan["transaction"],
                "patch_count": len(plan["patches"]), "preview": plan["preview"], "changes": self.diff()}

    def preview(self, include_models=True):
        from .preview import map_preview
        return {"image": str(map_preview(self, include_models)),
                "kind": "stock terrain and placed models" if include_models else "stock terrain",
                "projection": "top-down", "lighting": "unlit inspection preview"}

    def placement_preview(self, map_id, slot):
        from .preview import model_preview
        placement = self.placement(map_id, slot)
        return model_preview(self, placement["model_id"])

    def tile(self, x, z, permission_state=None):
        """Composed permission of one Cherrygrove tile, addressed by resource offset."""
        xx, zz = x - ORIGIN[0], z - ORIGIN[1]
        if not (0 <= xx < 64 and 0 <= zz < 32):
            return {"blocked": True, "type": None}
        member = 4 if xx < 32 else 5
        local = 2 * (zz * 32 + xx % 32)
        pair = self.maps[member][0][local:local + 2]
        state = self.composed()["permissions"] if permission_state is None else permission_state
        pair = state.get((member, self.permission_start[member] + local), pair)
        return {"blocked": world.is_blocked(pair), "type": pair[0]}

    def _validate_positions(self, positions, permission_state=None):
        require(isinstance(positions, dict), "Invalid NPC positions")
        require(set(positions).issubset({"1"}), "This release only qualifies movement of NPC 1", "UNSUPPORTED_EDIT")
        npcs = {n["id"]: n for n in self.base_events["npcs"]}
        for key, position in positions.items():
            require(isinstance(position, dict) and set(position) == {"x", "z"}, "Invalid position fields")
            x, z = position["x"], position["z"]
            require(type(x) is int and type(z) is int, "Coordinates must be integer tiles")
            # Keep this actor in its original map cell and height domain.
            require(544 <= x < 576 and 384 <= z < 416, "NPC must stay within the east Cherrygrove cell", "OUTSIDE_MAP")
            npc = npcs[int(key)]
            for xx in range(x - npc["range_x"], x + npc["range_x"] + 1):
                for zz in range(z - npc["range_z"], z + npc["range_z"] + 1):
                    require(not self.tile(xx, zz, permission_state)["blocked"], "NPC or its movement range intersects blocked terrain/water", "BLOCKED_TILE")
                    require(not any(n["id"] != int(key) and n["x"] == xx and n["z"] == zz for n in npcs.values()),
                            "NPC movement range intersects another actor", "OCCUPIED_TILE")
                    require(not any(w["x"] == xx and w["z"] <= zz <= w["z"] + 1 for w in self.base_events["warps"]),
                            "NPC movement range blocks a door or its approach", "DOOR_CONFLICT")
                    require(not any(t["x"] <= xx < t["x"] + t["width"] and t["z"] <= zz < t["z"] + t["height"] for t in self.base_events["triggers"]),
                            "NPC movement range intersects a scene trigger", "TRIGGER_CONFLICT")

    def inspect(self):
        return {"name": self.doc["name"], "project": str(self.root), "revision": self.doc["revision"],
                "baseline": self.doc["baseline"], "map": "Cherrygrove City", "editable_npcs": [1],
                "changes": self.diff(), "runtime_acceptance": "recorded separately per exported ROM",
                "placed_models": {"placements": sum(len(p) for _, p, _ in self.maps.values()),
                                  "unique_models": len({v['model_id'] for _, p, _ in self.maps.values() for v in p}),
                                  "editable": [DECORATION_KEY] if self.decoration_proof else []},
                "map_authoring": {"schema": authoring.SCHEMA, "transactions": len(self.doc["map_edits"]),
                                  "supported_versions": [1, 2], "scenery_schema": scenery.SCHEMA, "event_schema": event_authoring.SCHEMA,
                                  # Project-wide transactions (e.g. prop register/revise) have no map context.
                                  "contexts": sorted({t["context"]["id"] for t in self.doc["map_edits"] if t.get("context")}),
                                  "scope": "placement movement, scenery add/duplicate/delete/transfer, explicit permissions, existing NPC/background/warp editing and identified sign alignment"},
                "capabilities": ["terrain-preview", "stock-model-preview", "placement-inspect", "event-inspect", "npc-1-move", "undo", "exact-rom-export",
                                 "map-context-select", "map-permission-inspect", "map-transaction-author",
                                 "map-neighborhood", "new-bark-sign-align", "scenery-palette", "scenery-author", "structural-rom-export", "event-edit", "warp-connect",
                                 "gameplay-data", "gameplay-catalog", "gameplay-edit", "gameplay-areas", "gameplay-species"]
                                + (["qualified-planter-move"] if self.decoration_proof else [])}

    def diff(self):
        result = []
        for npc in self.base_events["npcs"]:
            target = self.doc["positions"].get(str(npc["id"]))
            if target:
                result.append({"operation": "npc.move", "id": npc["id"],
                               "before": {"x": npc["x"], "z": npc["z"]}, "after": target,
                               "archive": EVENT_ARCHIVE, "member": 64})
        if self.doc["placement_moves"]:
            proof = self._placement_proof()
            result.append({"operation": "placement.move", "map": 5, "slot": 14,
                           "before": dict(BEFORE), "after": dict(proof["after"]), "archive": "a/0/6/5", "member": 5,
                           "qualification": proof["id"],
                           "collision_flag_updates": sum(p["kind"] == "collision.flag" for p in proof["patches"])})
        for transaction in self.doc["map_edits"]:
            if transaction.get('schema') in world_authoring.SCHEMAS:
                result.append(world_authoring.summary(transaction))
                continue
            if transaction.get('schema') in gameplay.SCHEMAS:
                result.append(gameplay.summary(transaction))
                continue
            if transaction.get('schema') in story_authoring.SCHEMAS:
                result.append(story_authoring.summary(transaction))
                continue
            if transaction.get('schema') == interiors.SCHEMA:
                result.append(interiors.summary(transaction))
                continue
            if transaction.get('schema') == linked_groups.SCHEMA:
                result.append(linked_groups.summary(transaction))
                continue
            if transaction.get('schema') in surface_authoring.SCHEMAS:
                result.append(surface_authoring.summary(transaction))
                continue
            if transaction.get('schema') in terrain_authoring.SCHEMAS:
                result.append(terrain_authoring.summary(transaction))
                continue
            if transaction.get('schema') in simple_interactions.SCHEMAS:
                result.append(simple_interactions.summary(transaction))
                continue
            if transaction.get('schema') in game_data.SCHEMAS:
                result.append(game_data.summary(transaction))
                continue
            if event_authoring.is_transaction(transaction):
                result.append(event_authoring.summary(transaction))
                continue
            if custom_props.is_transaction(transaction):
                result.append(custom_props.summary(transaction))
                continue
            if ground_materials.is_transaction(transaction):
                result.append(ground_materials.describe(transaction))
                continue
            if ground_decals.is_transaction(transaction):
                result.append(ground_decals.summary(transaction))
                continue
            if travel_points.is_transaction(transaction):
                result.append(travel_points.summary(transaction))
                continue
            if field_features.is_transaction(transaction):
                result.append(field_features.summary(transaction))
                continue
            if petal_effect.is_transaction(transaction):
                result.append(petal_effect.summary(transaction))
                continue
            if runtime_repairs.is_transaction(transaction):
                result.append(runtime_repairs.summary(transaction))
                continue
            if pokemon_packages.is_transaction(transaction):
                result.append(pokemon_packages.summary(transaction))
                continue
            if map_groups.is_transaction(transaction):
                result.append(map_groups.summary(transaction))
                continue
            if environments.is_transaction(transaction):
                result.append(environments.summary(transaction))
                continue
            if scenery.is_transaction(transaction):
                result.append(scenery.summary(transaction))
                continue
            result.append({"operation": "map.transaction", "index": transaction["index"],
                           "label": transaction["label"], "context": transaction["context"],
                           "archive": world.MAP_ARCHIVE, "member": transaction["context"]["map_member"],
                           "placements": [{"slot": p["slot"], "before": p["before"], "after": p["after"]}
                                          for p in transaction["placements"]],
                           "permission_cells": [{"x": c["x"], "z": c["z"], "before": c["before"], "after": c["after"]}
                                                for c in transaction["permissions"]],
                           "qualification": transaction["qualification"]})
            if transaction.get("sign_interaction"):
                change = transaction["sign_interaction"]
                result[-1]["sign_interaction"] = {k: change[k] for k in
                                                 ("event_member", "event_id", "script", "from", "to")}
        return result

    def move_npc(self, npc_id, x, z, expected_revision):
        with self._locked(expected_revision):
            require(type(npc_id) is int and npc_id == 1, "Only NPC 1 is qualified for editing in this release", "UNSUPPORTED_EDIT")
            require((64, npc_id) not in self.composed()["event_npcs"],
                    "Use the Events editor for this NPC, or undo its event edits first", "EVENT_CONFLICT")
            positions = copy.deepcopy(self.doc["positions"])
            positions[str(npc_id)] = {"x": x, "z": z}
            self._validate_positions(positions)
            original = next(n for n in self.base_events["npcs"] if n["id"] == npc_id)
            if (x, z) == (original["x"], original["z"]):
                positions.pop(str(npc_id))
            if positions == self.doc["positions"]:
                return {"revision": self.doc["revision"], "changed": False}
            self._validate_state({**self.doc, "positions": positions})
            self._commit("npc.move", {"positions": positions})
        return {"revision": self.doc["revision"], "changed": True, "changes": self.diff()}

    def move_placement(self, map_id, slot, x, z, expected_revision):
        with self._locked(expected_revision):
            require(type(map_id) is int and type(slot) is int and (map_id, slot) == (5, 14),
                    "Only south flower planter 5:14 is qualified", "UNSUPPORTED_EDIT")
            require(self.decoration_proof is not None, "Placement dependencies are not qualified", "UNQUALIFIED_DEPENDENCIES")
            target = {"x": x, "z": z}
            require_target(target)
            current = self.doc["placement_moves"].get(DECORATION_KEY, {}).get("after", BEFORE)
            # Preserve an accepted legacy M3 record byte-for-byte on no-op.
            if target == current:
                return {"revision": self.doc["revision"], "changed": False}
            moves = {DECORATION_KEY: authored_move(translation_proof(self.decoration_proof, target))} if target != BEFORE else {}
            self._validate_state({"positions": self.doc["positions"], "placement_moves": moves,
                                  "map_edits": self.doc["map_edits"]})
            if moves == self.doc["placement_moves"]:
                return {"revision": self.doc["revision"], "changed": False}
            self._commit("placement.move", {"placement_moves": moves})
        return {"revision": self.doc["revision"], "changed": True, "changes": self.diff()}

    def undo(self, expected_revision):
        with self._locked(expected_revision):
            require(bool(self.doc["history"]), "No edit to undo", "NO_UNDO")
            previous = self._expand_history(self.doc["history"][-1], self.doc)
            require(isinstance(previous, dict)
                    and previous.get("operation") in ("npc.move", "placement.move", "map.transaction"),
                    "Unknown undo operation", "UNSUPPORTED_EDIT")
            self._validate_state({**previous, "map_edits": previous.get("map_edits", [])})
            snapshot = self._snapshot(previous['operation'])
            self.doc.setdefault('redo', []).append({'snapshot': snapshot, 'sha256': authoring.canonical(snapshot)})
            self.doc["history"].pop()
            self.doc["positions"] = previous["positions"]
            self.doc["placement_moves"] = previous.get("placement_moves", {})
            self.doc["map_edits"] = previous.get("map_edits", [])
            if "map_selection" in previous:
                self.doc["map_selection"] = previous["map_selection"]
            else:
                self.doc.pop("map_selection", None)
            self.doc["revision"] += 1
            self._composed_cache = self._snapshot_blob = None
            interiors.reset(self)
            atomic_json(self.path, self.doc)
        return {"revision": self.doc["revision"], "changes": self.diff()}

    def redo(self, expected_revision):
        with self._locked(expected_revision):
            require(bool(self.doc.get('redo')), 'No edit to redo', 'NO_REDO')
            entry = self.doc['redo'][-1]
            require(isinstance(entry, dict) and set(entry) == {'snapshot', 'sha256'}
                    and entry['sha256'] == authoring.canonical(entry['snapshot']),
                    'Redo snapshot differs', 'BEFORE_VALUE_MISMATCH')
            following = entry['snapshot']
            require(following.get('operation') in ('npc.move', 'placement.move', 'map.transaction'),
                    'Unknown redo operation', 'UNSUPPORTED_EDIT')
            self._validate_state(following)
            current = self._snapshot(following['operation'])
            self.doc['redo'].pop()
            if not self.doc['redo']:
                self.doc.pop('redo')
            for key in ('positions', 'placement_moves', 'map_edits'):
                self.doc[key] = _clone(following[key])
            self.doc['history'].append(self._history_entry(current, self.doc))
            if 'map_selection' in following:
                self.doc['map_selection'] = _clone(following['map_selection'])
            else:
                self.doc.pop('map_selection', None)
            self.doc['revision'] += 1
            self._composed_cache = self._snapshot_blob = None
            interiors.reset(self)
            atomic_json(self.path, self.doc)
        return {'revision': self.doc['revision'], 'changes': self.diff()}

    def validate(self):
        self._read()
        placements = self.placements()
        return {"valid": True, "revision": self.doc["revision"], "changes": self.diff(),
                "checks": ["baseline hash", "qualified resource hashes", "coordinate bounds", "movement-range collision", "actor/door/trigger overlap",
                           "stock model/texture resolution", "neutral placement transforms",
                           "qualified planter before-values and dependency guards", "composed collision",
                           "authored map-context resolution", "authored transaction dependency digests",
                           "authored placement/permission before-values", "legacy/authored domain separation",
                           "bounded flat surface reconstruction", "simple interaction identity and script/text isolation"],
                "placed_models": placements["count"],
                "map_transactions": len(self.doc["map_edits"]),
                "limitations": ["game acceptance applies to each exported ROM", "only planter 5:14 and five explicit anchors are qualified",
                                "authored map transactions are explicit edits, not a qualification of the resulting scene",
                                "events and collision move only through explicit selections; existing script bodies, triggers and BDHC heights are preserved",
                                "surface painting is limited to exposed single-layer flat tiles; no cliffs, water or baked scenery editing",
                                "new simple NPC/sign scripts append isolated plain dialogue; stock actor deletion and general scripting are unsupported",
                                "event checks cover records, actor ranges and approaches; scripted runtime behavior and guide scene remain untested",
                                "preview uses static unlit models; no game camera, lighting or animation emulation"]}

    def export(self, output, expected_revision, save_path=None):
        output = Path(output).resolve()
        with self._locked(expected_revision):
            require(not output.exists(), "Choose a new export folder; existing exports are preserved", "EXISTS")
            save = Path(save_path).read_bytes() if save_path else None
            require(save is None or len(save) == 524288, "Expected a 512 KiB ordinary HGSS save")
            if any(scenery.is_transaction(t) or event_authoring.is_transaction(t) or custom_props.is_transaction(t) or
                   ground_materials.is_transaction(t) or ground_decals.is_transaction(t) or travel_points.is_transaction(t) or field_features.is_transaction(t) or petal_effect.is_transaction(t) or runtime_repairs.is_transaction(t) or pokemon_packages.is_transaction(t) or map_groups.is_transaction(t) or environments.is_transaction(t) or
                   t.get('schema') in (*surface_authoring.SCHEMAS, *simple_interactions.SCHEMAS, interiors.SCHEMA, linked_groups.SCHEMA, *gameplay.SCHEMAS, *game_data.SCHEMAS, *story_authoring.SCHEMAS, *world_authoring.SCHEMAS) for t in self.doc["map_edits"]):
                from .scenery_export import export
                return export(self, output, save)
            data = bytearray(self.blob)
            patches = []
            for change in self.diff():
                if change["operation"] == "placement.move":
                    patches.extend(copy.deepcopy(self._placement_proof()["patches"]))
                    continue
                if change["operation"] == "map.transaction":
                    transaction = self.doc["map_edits"][change["index"]]
                    context = self.context(header=change["context"]["header"], cell=list(change["context"]["cell"]))
                    patches.extend(authoring.patches(context, transaction))
                    if transaction.get("sign_interaction"):
                        patches.extend(sign_interaction.patches(self, transaction["sign_interaction"]))
                    continue
                npc = next(n for n in self.base_events["npcs"] if n["id"] == change["id"])
                offset = self.event_offset + npc["offset"]
                before = struct.pack("<2H", change["before"]["x"], change["before"]["z"])
                after = struct.pack("<2H", change["after"]["x"], change["after"]["z"])
                patches.append({"rom_offset": offset, "before": before.hex(), "after": after.hex()})
            for patch in patches:
                pos = patch["rom_offset"]
                before, after = bytes.fromhex(patch["before"]), bytes.fromhex(patch["after"])
                require(len(before) == len(after) and data[pos:pos + len(before)] == before,
                        "Export before-value mismatch", "BEFORE_VALUE_MISMATCH")
                data[pos:pos + len(after)] = after
            # Prove the exported event records parse and that no other ROM byte changes.
            # Composed patches are unwound in reverse so overlapping edits restore exactly.
            restored = bytearray(data)
            for patch in reversed(patches):
                pos = patch["rom_offset"]
                before = bytes.fromhex(patch["before"])
                restored[pos:pos + len(before)] = before
            require(restored == self.blob, "Unexpected export changes")
            _, raw = resource(data, EVENT_ARCHIVE, 64)
            readback = events(raw)
            for change in self.diff():
                if change["operation"] == "placement.move":
                    _, raw_map = resource(data, "a/0/6/5", 5)
                    collision, props, _ = map_data(raw_map)
                    target = change["after"]
                    require(props[14]["xyz"] == [target["x"] - 560, 1, target["z"] - 400], "Export placement readback differs")
                    for cell in self._placement_proof()["collision_cells"]:
                        require(raw_map[cell["offset"]:cell["offset"] + 2].hex() == cell["after"],
                                "Export collision readback differs")
                    continue
                if change["operation"] == "map.transaction":
                    continue
                actual = next(n for n in readback["npcs"] if n["id"] == change["id"])
                require(all(actual[k] == v for k, v in change["after"].items()), "Export readback differs")
            # Authored transactions are verified against the composed final state, so
            # overlapping edits are checked once with an independent reparse.
            composed = self.composed()
            for (member, event_id), expected_record in composed["interactions"].items():
                _, raw_event = resource(data, EVENT_ARCHIVE, member)
                actual = events(raw_event)["backgrounds"][event_id]
                require(raw_event[4 + event_id * 20:24 + event_id * 20] == expected_record
                        and [actual["x"], actual["z"]] == list(struct.unpack_from("<2i", expected_record, 4)),
                        "Export sign interaction readback differs")
            for member in sorted({key[0] for key in composed["generic"]["placements"]}
                                 | {key[0] for key in composed["generic"]["permissions"]}):
                context = next(c for c in composed["contexts"] if c["map_member"] == member)
                _, raw_map = resource(data, world.MAP_ARCHIVE, member)
                _, props, _ = map_data(raw_map)
                for (owner, slot), record in composed["generic"]["placements"].items():
                    if owner == member:
                        require(authoring.record_state(props[slot]) == record,
                                "Export authored placement readback differs")
                for (owner, offset), value in composed["generic"]["permissions"].items():
                    if owner == member:
                        require(raw_map[offset:offset + 2] == value,
                                "Export authored permission readback differs")
            # Net count over the touched span: composed edits that cancel out count zero.
            touched = {pos + i for p in patches for i in range(len(p["before"]) // 2)
                       for pos in (p["rom_offset"],)}
            report = {"schema": "sovereign-editor-export-v1", "revision": self.doc["revision"],
                      "baseline_sha256": digest(self.blob), "candidate_sha256": digest(data),
                      "rom_bytes": len(data), "changes": self.diff(), "patches": patches,
                      "changed_byte_count": sum(data[i] != self.blob[i] for i in touched),
                      "patched_byte_span": len(touched),
                      "all_other_rom_bytes_equal": True, "native_acceptance": "pending",
                      "save_sha256": digest(save) if save else None}
            output.mkdir(parents=True, exist_ok=False)
            try:
                (output / "game.nds").write_bytes(data)
                if save is not None:
                    (output / "game.sav").write_bytes(save)
                (output / "export.json").write_text(json.dumps(report, indent=2) + "\n")
                checklist = (
                    "Open game.nds in native melonDS. Use ordinary in-game saving.\n"
                    "In Cherrygrove, confirm NPC 1 moved to the exported coordinates,\n"
                    "dialogue works, and movement is clear. Enter/leave a house and\n"
                    "return from a battle; confirm the position and map remain correct.\n"
                    "The supplied save, if any, is a copy; no savestate is supplied.\n")
                if self.doc["placement_moves"]:
                    target = self.doc["placement_moves"][DECORATION_KEY]["after"]
                    tx, tz = math.floor(target["x"]), math.floor(target["z"])
                    vacated = sorted(footprint(BEFORE) - footprint(target))
                    checklist = (
                        "M4 south flower planter — USER-RUN melonDS checks (acceptance pending)\n\n"
                        "1. Open this game.nds with its adjacent game.sav; use the ordinary save, not a savestate.\n"
                        "2. Find the planter WEST of the southern house (door tile 567,405). It should be centered at\n"
                        f"   ({target['x']:g},{target['z']:g}), height 1; stock was (565.5,404.5). Other models look normal.\n"
                        f"3. Try entering all three planter cells x={tx}, z={tz-1}..{tz+1} from the sides and ends: all must block.\n"
                        f"   Walk these vacated stock cells: {vacated}. They must be passable, with no invisible old blocker.\n"
                        "   Walk around all sides of the planter; check the gap to the house and routes north/south.\n"
                        "4. Enter/exit the southern house. Check the town sign/mailbox and trainer dialogue; trainer stays one tile east.\n"
                        "   Speak to the resident south of the planter. The guide scene was completed in the M3 save and remains\n"
                        "   UNTESTED; if unavailable, mark it untested again. Prior M3 acceptance does not establish new scene safety.\n"
                        "5. Return from a battle, then save normally, close/reopen and load: planter, collision and doors stay correct.\n\n"
                        "Report each check as pass/fail/untested. Preview/file checks do not establish gameplay acceptance.\n"
                        "This pair tests only its listed endpoint; other allowed anchors and dynamic scripts are not native-accepted.\n"
                        "The supplied save is an unchanged copy. Preserve the accepted M3 and v0.1 pairs.\n")
                for transaction in self.doc["map_edits"]:
                    context = transaction["context"]
                    checklist += (
                        f"\nAuthored map transaction {transaction['index']} — {transaction['label']}\n"
                        f"  Context {context['id']} · map member {context['map_member']} · cell origin {context['origin']}\n"
                        + "".join(f"  Placement {context['map_member']}:{p['slot']} "
                                  f"({p['before']['x']:g},{p['before']['z']:g}) -> ({p['after']['x']:g},{p['after']['z']:g}); "
                                  f"height {p['before']['y']:g} and its record word are unchanged\n"
                                  for p in transaction["placements"])
                        + (f"  Permission cells changed: "
                           f"{[(c['x'], c['z'], c['before'], c['after']) for c in transaction['permissions']]}\n"
                           if transaction["permissions"] else
                           "  No permission byte changed: walk the old and new spots and confirm collision is unchanged.\n")
                        + "  Walk every changed cell from all four sides; check nearby doors/signs still work.\n"
                        "  Doors, warps, triggers, scripts and terrain height were NOT moved. This is an authored edit,\n"
                        "  not a qualified operation: report pass/fail/untested per check.\n")
                    if transaction.get("sign_interaction"):
                        change = transaction["sign_interaction"]
                        checklist += (f"  New Bark town-sign interaction moved {change['from']} -> {change['to']}; "
                                      "script/text reference preserved. Read the moved sign, confirm the empty old spot "
                                      "does not show town text, and recheck source/destination collision.\n")
                (output / "PLAYTEST.txt").write_text(checklist)
            except BaseException:
                shutil.rmtree(output)
                raise
        return {**report, "output": str(output)}
