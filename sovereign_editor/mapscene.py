"""Textured scene resolution for any resolvable HGSS map context.

Everything here is read-only. The terrain mesh is the map member's own ``BMD0``
section (``a/0/6/5``), its textures are the area's map tileset (``a/0/4/4``), and
building models come from ``a/0/4/0`` outdoors or ``a/1/4/8`` indoors with the
area's building tileset (``a/0/7/0``) whenever a model carries no embedded
``TEX0``. Resource ids come from ``AreaData``; nothing is hardcoded to Cherrygrove
and no texture, colour or background is invented when a resource is missing.

An individual model that this decoder cannot support is reported by name with the
exact refusal, and the rest of the scene still renders: a single unsupported
building never hides a usable map or its permission grid.
"""
import json
from dataclasses import replace

import numpy as np

from . import world
from .formats import EditorError, digest, map_data, member_count, require, resource
from .nitro import decode_model, model_names
from .raster import render

RENDERER_VERSION = "map-scene-v1"
UNITS_PER_TILE = 16
CELL_UNITS = world.MAP_SIZE * UNITS_PER_TILE
PIXELS_PER_TILE = 32
BACKGROUND = (24, 34, 41)

# Display labels for internal model names. The exact-name entries are the ones
# reviewed in earlier milestones; the suffix rules only restate what the internal
# name already says. Anything else keeps its internal name and member id, because
# an honest "model 27 (wk_sp1)" is better than an invented description.
KNOWN_MODELS = {"board_a": "Town sign", "board_b": "Mailbox", "pc": "Pokémon Center",
                "fs": "Poké Mart", "en_fs": "Poké Mart entrance", "p_door": "Centre/shop door",
                "yo_h01": "House", "yo_h02": "Tall house", "yo_door1": "House door",
                "yo_sp2": "Flower planter", "wk_labo": "Laboratory", "wk_hhouse": "House",
                "wk_h01": "House", "wind": "Ambient wind streaks"}
NAME_RULES = (("door", "Door"), ("board", "Sign"), ("shelf", "Shelf"), ("plant", "Potted plant"),
              ("tree", "Tree"), ("kanban", "Signboard"), ("h0", "House"), ("hh", "House"))
# Readable titles for the contexts demonstrated and checked in this milestone, keyed
# by the header's internal name. Every other context shows its internal name.
AREA_TITLES = {"T20": "New Bark Town", "T21": "Cherrygrove City", "R29": "Route 29",
               "R30": "Route 30", "R33": "Route 33", "T20R0101": "Elm's Lab"}
# A drawn extent larger than a quarter of the 32x32 cell is ambient geometry (New
# Bark's wind streaks span 2213 tiles²); clicking open ground inside it is not a hit.
AMBIENT_TILES = 256


def area_title(internal_name):
    return AREA_TITLES.get(internal_name)


def describe_model(model_id, name):
    """A readable label plus the honest internal name and member id."""
    label, basis = KNOWN_MODELS.get(name), "reviewed model name"
    if label is None:
        for fragment, guess in NAME_RULES:
            if fragment in name.lower():
                label, basis = guess, "internal model name"
                break
    if label is None:
        basis = None
    return {"model_id": model_id, "name": name, "label": label, "label_basis": basis,
            "display": f"{label} · {name} ({model_id})" if label else f"{name} (model {model_id})"}


def tilesets(project, context, state=None):
    """Area tileset bytes plus their exact archive/member references.

    With project ground materials (ground_materials.py) the map tileset is the superset
    the export builds for the area data; ``state`` is the composed state during
    composition (defaults to the project's)."""
    blobs_refs = _stock_tilesets(project, context)
    from . import ground_materials
    if state is None and getattr(project, '_composed_cache', None) is not None:
        state = project._composed_cache
    if (state is not None and blobs_refs[1]['map_tileset'] is not None
            and (ground_materials.materials(state) or state.get('terrain_features') and
                 context['area_data']['id'] in ground_materials.fall_areas(project, state))):
        refs, blobs = blobs_refs
        blobs = dict(blobs)
        blobs['map_tileset'] = ground_materials.editor_tileset(project, state, context['area_data']['id'],
                                                               blobs['map_tileset'])
        return refs, blobs
    return blobs_refs


def _stock_tilesets(project, context):
    """Area tileset bytes plus their exact archive/member references.

    Each tileset is resolved independently and never raises: one missing texture
    archive must not erase a terrain mesh or the models that carry their own
    ``TEX0``. A tileset that cannot be read is reported with its exact reason, and
    the models that actually needed it refuse individually.
    """
    area = context["area_data"]
    refs = {"map_tileset": {"archive": world.MAP_TEXTURE_ARCHIVE, "member": area["map_tileset"]},
            "building_tileset": {"archive": world.BUILDING_TEXTURE_ARCHIVE, "member": area["buildings_tileset"]},
            "building_models": {"archive": world.INTERIOR_MODEL_ARCHIVE if area["area_type"] == 0
                                else world.BUILDING_MODEL_ARCHIVE}}
    blobs = {}
    for key in ("map_tileset", "building_tileset"):
        ref = refs[key]
        try:
            require(ref["member"] < member_count(project.blob, ref["archive"]),
                    f"Area {area['id']} names {ref['archive']} member {ref['member']}, "
                    "which this ROM lacks", "NOT_FOUND")
            offset, raw = resource(project.blob, ref["archive"], ref["member"])
        except EditorError as exc:
            ref.update(status="unsupported", reason=str(exc), code=exc.code, sha256=None)
            blobs[key] = None
            continue
        ref.update(status="ok", rom_offset=offset, bytes=len(raw), sha256=digest(raw))
        blobs[key] = raw
    return refs, blobs


def terrain_model(project, context, tileset):
    """Decode the map member's own terrain model with the area's map tileset."""
    from .surface_authoring import model
    section = model(project, context, project.composed())
    require(section[:4] == b"BMD0", "Map terrain model section is not a Nitro model")
    summary, primitives = decode_model(section, tileset=tileset)
    offset = np.array([CELL_UNITS / 2, 0, CELL_UNITS / 2])
    return summary, [replace(p, vertices=p.vertices + offset) for p in primitives]


def building_model(project, archive, model_id, tileset):
    from . import props
    custom = props.editor_model(project, model_id) if archive == world.BUILDING_MODEL_ARCHIVE else None
    if custom is not None:
        # Custom props bind by name to the project-owned extended building tileset the
        # export builds; stock resolution (and its digests) stays unchanged.
        summary, primitives = decode_model(custom, tileset=props.editor_tileset(project, tileset))
        return {"archive": archive, "member": model_id, "rom_offset": -1, "bytes": len(custom),
                "sha256": digest(custom), "custom_prop": True}, summary, primitives
    require(model_id < member_count(project.blob, archive),
            f"Building model {model_id} is absent from {archive}", "NOT_FOUND")
    offset, raw = resource(project.blob, archive, model_id)
    summary, primitives = decode_model(raw, tileset=tileset)
    return {"archive": archive, "member": model_id, "rom_offset": offset, "bytes": len(raw),
            "sha256": digest(raw)}, summary, primitives


def _placement_transform(placement):
    """Y-rotation and scale words of a placement record, or an exact refusal reason."""
    rx, ry, rz = placement["rotation_raw"]
    require(rx & 0xFFFF == 0 and rz & 0xFFFF == 0,
            f"Placement {placement['key']} rotates around X/Z ({placement['rotation_raw']}); "
            "only the Y axis is drawn")
    require(placement["scale_raw"] == [4096, 4096, 4096],
            f"Placement {placement['key']} stores non-neutral size words {placement['scale_raw']}")
    angle = np.radians((ry & 0xFFFF) * 360 / 65536)
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]]), float(np.degrees(angle))


def footprint(position, bounds, rotation=None):
    """Axis-aligned XZ extent of a model's drawn geometry at its anchor, in tiles.

    The same transform the renderer applies is used here, so the highlight and the
    pick region follow a rotated placement instead of its unrotated box. This is a
    geometric extent only: it never states which permission cells a model owns.
    """
    if bounds is None:
        return None
    (x0, _, z0), (x1, _, z1) = bounds
    corners = np.array([[x0, 0, z0], [x1, 0, z0], [x0, 0, z1], [x1, 0, z1]], dtype=float)
    if rotation is not None:
        corners = corners @ np.asarray(rotation).T
    lo, hi = corners.min(0), corners.max(0)
    return [float(position["x"] + lo[0] / UNITS_PER_TILE), float(position["z"] + lo[2] / UNITS_PER_TILE),
            float(position["x"] + hi[0] / UNITS_PER_TILE), float(position["z"] + hi[2] / UNITS_PER_TILE)]


def scene(project, header=None, matrix=None, cell=None):
    """Resolved, JSON-safe description of a context's textured scene.

    Placement coordinates come from :meth:`core.Project.map_view`, so the scene and
    the permission grid always describe the same composed state.
    """
    view = project.map_view(header=header, matrix=matrix, cell=cell)
    context = project.context(header=header, matrix=matrix, cell=cell)
    refs, blobs = tilesets(project, context)
    ox, oz = context["origin"]
    terrain = {"archive": world.MAP_ARCHIVE, "member": context["map_member"],
               "sha256": context["map_sha256"], "status": "ok"}
    from .surface_authoring import model
    terrain['model_sha256'] = digest(model(project, context, project.composed()))
    try:
        summary, _ = terrain_model(project, context, blobs["map_tileset"])
        (x0, _, z0), (x1, _, z1) = summary["bounds"]
        half = world.MAP_SIZE // 2
        terrain.update(name=summary["name"], triangles=summary["triangles"],
                       vertices=summary["vertices"], texture_source=summary["texture_source"],
                       textured_materials=summary["textured_materials"],
                       materials=len(summary["materials"]),
                       # Global tile extent of the mesh; it may overhang the cell.
                       tile_bounds=[float(ox + half + x0 / UNITS_PER_TILE), float(oz + half + z0 / UNITS_PER_TILE),
                                    float(ox + half + x1 / UNITS_PER_TILE), float(oz + half + z1 / UNITS_PER_TILE)])
    except EditorError as exc:
        terrain.update(status="unsupported", reason=str(exc), code=exc.code)
    archive = refs["building_models"]["archive"]
    models, placements = {}, []
    for item in view["placements"]:
        model_id = item["model_id"]
        if model_id not in models:
            entry = {"model_id": model_id, "archive": archive, "status": "ok", "placements": 0}
            try:
                source, summary, primitives = building_model(project, archive, model_id,
                                                             blobs["building_tileset"])
                entry.update(describe_model(model_id, summary["name"]), source=source,
                             triangles=summary["triangles"], vertices=summary["vertices"],
                             texture_source=summary["texture_source"],
                             bounds=[[round(v, 3) for v in b] for b in summary["bounds"]])
            except EditorError as exc:
                names = []
                try:
                    names = model_names(resource(project.blob, archive, model_id)[1])
                except EditorError:
                    pass
                entry.update(describe_model(model_id, names[0] if names else f"model{model_id}"),
                             status="unsupported", reason=str(exc), code=exc.code)
            models[model_id] = entry
        model = models[model_id]
        model["placements"] += 1
        status, reason, rotation, degrees = model["status"], model.get("reason"), None, 0.0
        if status == "ok":
            try:
                rotation, degrees = _placement_transform(item)
            except EditorError as exc:
                status, reason = "unsupported_transform", str(exc)
        placements.append({
            "slot": item["slot"], "key": item["key"], "model_id": model_id,
            "name": model["name"], "label": model["label"], "display": model["display"],
            "label_basis": model["label_basis"],
            "position": item["position"], "stock_position": item["stock_position"],
            "changed": item["changed"], "editable": item["editable"], "locked_by": item["locked_by"],
            "record_offset": item["record_offset"], "rotation_raw": item["rotation_raw"],
            "rotation_degrees": round(degrees, 4), "scale_raw": item["scale_raw"],
            "status": status, "reason": reason,
            "tile": [item["position"]["x"] - ox, item["position"]["z"] - oz],
            # Drawn extent of the rotated model, for highlighting and picking only.
            "footprint": footprint(item["position"], model.get("bounds"), rotation)})
        box = placements[-1]["footprint"]
        placements[-1]["ambient"] = bool(box and (box[2] - box[0]) * (box[3] - box[1]) > AMBIENT_TILES)
    unsupported = [{"resource": key, "archive": refs[key]["archive"], "member": refs[key]["member"],
                    "reason": refs[key]["reason"]}
                   for key in ("map_tileset", "building_tileset") if refs[key]["status"] != "ok"]
    unsupported += ([{"resource": "terrain", **{k: terrain[k] for k in ("member", "reason")}}]
                    if terrain["status"] != "ok" else [])
    unsupported += [{"resource": "building model", "member": m["model_id"], "name": m["name"],
                     "reason": m["reason"]} for m in models.values() if m["status"] != "ok"]
    unsupported += [{"resource": "placement", "key": p["key"], "reason": p["reason"]}
                    for p in placements if p["status"] == "unsupported_transform"]
    extents = ([terrain["tile_bounds"]] if terrain.get("tile_bounds") else []) + \
        [p["footprint"] for p in placements if p["footprint"] and not p["ambient"]]
    content = None
    if extents:
        # Clip to the cell: geometry beyond it belongs to a neighbouring cell's view.
        lo_x = max(ox, min(e[0] for e in extents))
        lo_z = max(oz, min(e[1] for e in extents))
        hi_x = min(ox + world.MAP_SIZE, max(e[2] for e in extents))
        hi_z = min(oz + world.MAP_SIZE, max(e[3] for e in extents))
        content = {"tiles": [lo_x, lo_z, hi_x, hi_z],
                   "overhangs_cell": any(e[0] < ox or e[1] < oz or e[2] > ox + world.MAP_SIZE
                                         or e[3] > oz + world.MAP_SIZE for e in extents)}
    return {"context": view["context"], "cell": view["cell"], "area_data": view["area_data"],
            "header": view["header"], "size": view["size"], "origin": [ox, oz],
            "title": area_title(view["header"]["name"]), "content": content,
            "resources": {**refs, "terrain": terrain},
            "models": [models[k] for k in sorted(models)], "placements": placements,
            "unsupported": unsupported,
            "units_per_tile": UNITS_PER_TILE, "pixels_per_tile": PIXELS_PER_TILE,
            "projection": "top-down orthographic, +X east and +Z south, unlit inspection render",
            "limitations": [
                "an inspection render, not the game's lighting, animation or camera",
                "unsupported models are named and skipped; the permission grid stays usable",
                "events remain separate; the identified New Bark sign has explicit interaction alignment"]}


def scene_primitives(project, context, placements, blobs, archive):
    """Terrain plus every supported placement, in scene units (16 per tile)."""
    primitives = []
    try:
        _, terrain = terrain_model(project, context, blobs["map_tileset"])
        primitives += terrain
    except EditorError:
        terrain = []
    ox, oz = context["origin"]
    for item in placements:
        if item["status"] != "ok":
            continue
        try:
            _, _, model = building_model(project, archive, item["model_id"], blobs["building_tileset"])
            rotation, _ = _placement_transform(item)
        except EditorError:
            continue
        translation = np.array([(item["position"]["x"] - ox) * UNITS_PER_TILE,
                                item["position"]["y"] * UNITS_PER_TILE,
                                (item["position"]["z"] - oz) * UNITS_PER_TILE])
        primitives += [replace(p, vertices=p.vertices @ rotation.T + translation) for p in model]
    return primitives


def scene_image(project, context, description, pixels_per_tile=PIXELS_PER_TILE):
    """Render (and cache) the top-down textured image of one resolved scene."""
    from .preview import _save_cache
    refs, blobs = tilesets(project, context)
    archive = refs["building_models"]["archive"]
    require(type(pixels_per_tile) is int and 4 <= pixels_per_tile <= 64,
            "Scene resolution must be 4..64 pixels per tile")
    scale = pixels_per_tile / UNITS_PER_TILE
    side = world.MAP_SIZE * pixels_per_tile
    key = digest(json.dumps({"renderer": RENDERER_VERSION, "context": description["context"]["id"],
                             "map": description["context"]["map_sha256"],
                             "surface": description['resources']['terrain']['model_sha256'],
                             "tilesets": [refs[k]["sha256"] for k in ("map_tileset", "building_tileset")],
                             "pixels": pixels_per_tile,
                             "placements": [(p["key"], p["model_id"], p["position"]["x"], p["position"]["y"],
                                             p["position"]["z"], p["status"]) for p in description["placements"]]},
                            sort_keys=True).encode())[:20]

    def draw():
        primitives = scene_primitives(project, context, description["placements"], blobs, archive)
        require(primitives, "No supported geometry to render for this context")
        return render(primitives, (side, side), [[scale, 0, 0], [0, 0, scale], [0, 1, 0]],
                      background=BACKGROUND)

    return _save_cache(project, f"{RENDERER_VERSION}-{key}-{pixels_per_tile}.png", draw)


def thumbnail_key(archive, model_sha, tileset_sha, size):
    """Cache identity of one model preview.

    The same building model can be drawn with a different area building tileset, so
    the tileset the decode actually used belongs in the key. ``tileset_sha`` is
    ``None`` for a model whose textures are embedded, where reuse across contexts
    is correct.
    """
    return digest(json.dumps({"renderer": RENDERER_VERSION, "archive": archive, "model": model_sha,
                              "tileset": tileset_sha, "size": list(size)}, sort_keys=True).encode())[:20]


def model_thumbnail(project, context, model_id, size=(260, 190)):
    """Three-quarter preview of one building model, for the selected-object panel."""
    from .preview import _save_cache
    refs, blobs = tilesets(project, context)
    archive = refs["building_models"]["archive"]
    source, summary, primitives = building_model(project, archive, model_id, blobs["building_tileset"])
    external = summary["texture_source"] != "embedded TEX0"
    key = thumbnail_key(archive, source["sha256"],
                        refs["building_tileset"]["sha256"] if external else None, size)
    angle, tilt = np.radians([25, 30])
    c, s, cp, sp = np.cos(angle), np.sin(angle), np.cos(tilt), np.sin(tilt)
    camera = [[c, 0, -s], [s * sp, -cp, c * sp], [s * cp, sp, c * cp]]
    path = _save_cache(project, f"model-{archive.replace('/', '')}-{model_id}-{key}.png",
                       lambda: render(primitives, size, camera, fit=True, background=BACKGROUND))
    return {**describe_model(model_id, summary["name"]), "image": str(path), "size": list(size),
            "source": source, "triangles": summary["triangles"], "vertices": summary["vertices"],
            "texture_source": summary["texture_source"],
            "tileset": refs["building_tileset"] if external else None,
            "textures": [{"material": m["name"], "texture": m["texture_name"], "palette": m["palette_name"]}
                         for m in summary["materials"]]}
