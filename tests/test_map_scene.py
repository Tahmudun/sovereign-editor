"""Textured cross-area scenes: resource resolution, decoding, picking and editing.

Everything runs against the copied baseline ROM of this workspace. Resource ids are
recomputed here from ``AreaData`` and the NARC tables rather than taken from the
module under test, and geometry is checked against each model's own stored bounding
box and counts, so a wrong transform cannot pass quietly.
"""
import copy
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from sovereign_editor import mapscene, nitro, world
from sovereign_editor.core import Project, atomic_json
from sovereign_editor.formats import EditorError, digest, map_data, resource

# Town, route and indoor contexts of the pinned ROM. Members/origins are asserted.
NEW_BARK = {"header": 60, "cell": [21, 12], "member": 0, "origin": [672, 384], "terrain": "map21_12c",
            "title": "New Bark Town", "unsupported_models": {28}}
CHERRYGROVE = {"header": 67, "cell": [17, 12], "member": 5, "origin": [544, 384], "terrain": "map17_12c"}
ROUTE_29 = {"header": 33, "cell": [18, 12], "member": 1, "origin": [576, 384], "terrain": "map18_12c"}
ROUTE_30 = {"header": 34, "cell": [17, 9], "member": 6, "origin": [544, 288], "terrain": "map17_09c"}
ROUTE_33 = {"header": 37, "cell": [14, 14], "member": 32, "origin": [448, 448], "terrain": "map14_14c"}
ELM_LAB = {"header": 61, "cell": [0, 0], "member": 244, "origin": [0, 0], "terrain": "m_labo01_00_00c"}
POKE_CENTER = {"header": 2, "cell": [0, 0], "member": 222, "origin": [0, 0], "terrain": "m_pc02_00_00c"}
CONTEXTS = [NEW_BARK, CHERRYGROVE, ROUTE_29, ROUTE_30, ROUTE_33, ELM_LAB, POKE_CENTER]
# New Bark's wooden town sign (model 29) and the cells of its stock/target tiles.
NB_SLOT, NB_FROM, NB_TO = 13, {"x": 685.5, "z": 400.5}, {"x": 684.5, "z": 400.5}
NB_CELL, NB_TARGET_CELL = (685, 400), (684, 400)
# Building models that previously refused: the wind streaks use a texture-matrix
# translation, wk_sp1 uses a full 3x3 node rotation.
WIND, WK_SP1, BOARD_A = 28, 27, 29


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    source = Path(os.environ.get("SG_TEST_ROM", "projects/cherrygrove/baseline.nds"))
    if not source.is_file():
        pytest.skip("Set SG_TEST_ROM to a qualified local HeartGold ROM")
    root = tmp_path_factory.mktemp("map-scene") / "project"
    project = Project.create(source, root)
    return project.root, copy.deepcopy(project.doc)


@pytest.fixture
def project(workspace):
    root, original = workspace
    atomic_json(root / "project.json", original)
    yield Project(root)
    atomic_json(root / "project.json", original)


def area_of(project, header):
    """Area data read straight from the archives, independent of mapscene."""
    head = world.read_header(project.blob, header, project.arm9)
    return world.read_area_data(project.blob, head["area_data"])


def open_window(project, context=NEW_BARK):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.map_inspector import MapInspectorWindow
    app = QApplication.instance() or QApplication([])
    window = MapInspectorWindow(project.root, context=(context["header"], context["cell"]))
    app.processEvents()
    return app, window


# ---- resource resolution -----------------------------------------------------


@pytest.mark.parametrize("context", CONTEXTS, ids=lambda c: c["terrain"])
def test_scene_resolves_each_context_from_its_own_area_data(project, context):
    scene = project.map_scene(header=context["header"], cell=context["cell"], image=False)
    area = area_of(project, context["header"])
    assert scene["context"]["map_member"] == context["member"]
    assert scene["origin"] == context["origin"]
    resources = scene["resources"]
    # The tilesets are the ones AreaData names, not a Cherrygrove default.
    assert resources["map_tileset"]["archive"] == world.MAP_TEXTURE_ARCHIVE
    assert resources["map_tileset"]["member"] == area["map_tileset"]
    assert resources["map_tileset"]["status"] == "ok"
    assert resources["building_tileset"]["archive"] == world.BUILDING_TEXTURE_ARCHIVE
    assert resources["building_tileset"]["member"] == area["buildings_tileset"]
    assert resources["building_models"]["archive"] == (world.INTERIOR_MODEL_ARCHIVE
                                                       if area["area_type"] == 0
                                                       else world.BUILDING_MODEL_ARCHIVE)
    assert resources["terrain"]["name"] == context["terrain"]
    assert resources["terrain"]["status"] == "ok" and resources["terrain"]["triangles"] > 0
    # Terrain models carry no embedded TEX0: the area tileset is what textures them.
    assert resources["terrain"]["texture_source"] == "area tileset"
    assert resources["terrain"]["textured_materials"] >= 1
    # Only New Bark's wind streaks refuse (unverified texture-matrix convention);
    # every other resource of these contexts decodes.
    expected = context.get("unsupported_models", set())
    assert {u["member"] for u in scene["unsupported"]} == expected
    assert all(u["resource"] == "building model" for u in scene["unsupported"])
    assert {m["model_id"] for m in scene["models"] if m["status"] != "ok"} == expected
    assert all(p["status"] == "ok" for p in scene["placements"] if p["model_id"] not in expected)
    assert scene["content"] and scene["content"]["tiles"][2] > scene["content"]["tiles"][0]


def test_indoor_and_outdoor_contexts_use_different_model_archives(project):
    outdoor = project.map_scene(header=NEW_BARK["header"], cell=NEW_BARK["cell"], image=False)
    indoor = project.map_scene(header=ELM_LAB["header"], cell=ELM_LAB["cell"], image=False)
    assert outdoor["area_data"]["area_type_name"] == "outdoor"
    assert indoor["area_data"]["area_type_name"] == "indoor"
    assert outdoor["resources"]["building_models"]["archive"] == world.BUILDING_MODEL_ARCHIVE
    assert indoor["resources"]["building_models"]["archive"] == world.INTERIOR_MODEL_ARCHIVE
    assert {m["archive"] for m in indoor["models"]} == {world.INTERIOR_MODEL_ARCHIVE}
    assert indoor["placements"] and outdoor["placements"]
    # Different areas really do resolve different tilesets.
    assert indoor["resources"]["map_tileset"]["member"] != outdoor["resources"]["map_tileset"]["member"]


def test_terrain_textures_come_from_the_rom_tileset_and_refuse_when_absent(project):
    context = project.context(header=NEW_BARK["header"], cell=NEW_BARK["cell"])
    area = context["area_data"]
    tileset = resource(project.blob, world.MAP_TEXTURE_ARCHIVE, area["map_tileset"])[1]
    section = map_data(project.member_raw(context["map_member"]))[2]
    summary, primitives = nitro.decode_model(section, tileset=tileset)
    textures, palettes = nitro.texture_set(tileset)
    bound = [m for m in summary["materials"] if m["texture_name"]]
    assert bound and all(m["texture_name"] in textures for m in bound)
    assert all(m["palette_name"] in palettes for m in bound if m["palette_name"])
    # The palettes actually carry colours; ndspy 4.1 empties shared-offset entries.
    assert all(len(palettes[m["palette_name"]]) >= 4 for m in bound if m["palette_name"])
    assert any(p.texture is not None and p.texture[..., :3].std() > 0 for p in primitives)
    # Without the tileset the same model refuses by name instead of inventing pixels.
    with pytest.raises(EditorError, match="Textures absent"):
        nitro.decode_model(section)


def test_a_missing_building_tileset_keeps_terrain_and_embedded_models(project, monkeypatch):
    """One unreadable texture archive must not erase a usable map."""
    original = Project.context

    def patched(self, header=None, matrix=None, cell=None):
        context = original(self, header=header, matrix=matrix, cell=cell)
        context["area_data"]["buildings_tileset"] = 9999
        return context

    monkeypatch.setattr(Project, "context", patched)
    scene = project.map_scene(header=NEW_BARK["header"], cell=NEW_BARK["cell"], image=False)
    assert scene["resources"]["building_tileset"]["status"] == "unsupported"
    assert "9999" in scene["resources"]["building_tileset"]["reason"]
    assert scene["resources"]["terrain"]["status"] == "ok"
    # New Bark's buildings embed their own TEX0, so they still resolve; only the
    # already-refused wind streaks stay unsupported, for their own reason.
    assert {m["model_id"] for m in scene["models"] if m["status"] != "ok"} == {WIND}
    assert [u["resource"] for u in scene["unsupported"]] == ["building_tileset", "building model"]
    # The permission grid stays available for the same context.
    assert len(project.map_view(header=NEW_BARK["header"], cell=NEW_BARK["cell"])["permissions"]["rows"]) == 32


def test_unsupported_model_is_named_without_hiding_the_scene(project, monkeypatch):
    real = mapscene.building_model

    def patched(project_, archive, model_id, tileset):
        if model_id == BOARD_A:
            raise EditorError("INVALID_DATA", "Unsupported material transform: board_a")
        return real(project_, archive, model_id, tileset)

    monkeypatch.setattr(mapscene, "building_model", patched)
    scene = project.map_scene(header=NEW_BARK["header"], cell=NEW_BARK["cell"], image=False)
    broken = next(m for m in scene["models"] if m["model_id"] == BOARD_A)
    assert broken["status"] == "unsupported" and "board_a" in broken["reason"]
    # The honest internal name survives a refusal, and the rest of the scene does not.
    assert broken["name"] == "board_a"
    assert [u["member"] for u in scene["unsupported"]] == [WIND, BOARD_A]
    assert sum(m["status"] == "ok" for m in scene["models"]) == len(scene["models"]) - 2
    assert next(p for p in scene["placements"] if p["slot"] == NB_SLOT)["footprint"] is None
    assert next(p for p in scene["placements"] if p["slot"] == NB_SLOT)["position"] == {
        "x": NB_FROM["x"], "y": 1, "z": NB_FROM["z"]}


# ---- geometry, textures and transforms ---------------------------------------


@pytest.mark.parametrize("model_id,name,triangles", [(WK_SP1, "wk_sp1", 59), (BOARD_A, "board_a", 22)])
def test_previously_refused_building_models_now_decode_exactly(project, model_id, name, triangles):
    raw = resource(project.blob, world.BUILDING_MODEL_ARCHIVE, model_id)[1]
    summary, primitives = nitro.decode_model(raw)
    assert summary["name"] == name and summary["triangles"] == triangles
    # decode_model checks counts and the stored bounding box itself; assert the same
    # relations here so a loosened internal check cannot pass silently.
    assert sum(len(p.triangles) for p in primitives) == triangles
    assert sum(len(p.vertices) for p in primitives) == summary["vertices"]
    quantum = summary["bounds_quantum"]
    for decoded, native in zip(summary["bounds"], summary["native_bounds"]):
        assert all(abs(a - b) <= 2 * quantum for a, b in zip(decoded, native))
    assert all(p.texture is not None for p in primitives)


def material_records(raw):
    """(name, absolute offset, size) of every material of a single-model container."""
    mdl = struct.unpack_from("<I", raw, 16)[0]
    model_offset = struct.unpack("<I", nitro.info(nitro.blocks(bytes(raw))[b"MDL0"], 8)[0][1])[0]
    base = mdl + model_offset
    materials = struct.unpack_from("<I", raw, base + 8)[0]
    records = []
    for name, value in nitro.info(bytes(raw[base:]), materials + 4):
        start = base + materials + struct.unpack("<I", value)[0]
        records.append((name, start, struct.unpack_from("<H", raw, start + 2)[0]))
    return records


def test_texture_matrix_transform_is_parsed_exactly_but_refused(project):
    """Model 28's wind materials store a translation whose convention is unverified.

    The field layout is established independently of the renderer: a 52-byte record
    with TRANS_ZERO clear is the 44-byte record plus one fx32 pair. Zeroing exactly
    those eight bytes yields an identity matrix, and the model then decodes and
    matches its own stored bounds. The non-identity values stay refused by name.
    """
    raw = resource(project.blob, world.BUILDING_MODEL_ARCHIVE, WIND)[1]
    with pytest.raises(EditorError, match=r"texture-matrix transform: wind_lm3 .*translation \[-0\.625, 1\.0\]"):
        nitro.decode_model(raw)
    records = material_records(raw)
    assert [(name, size) for name, _, size in records] == [("wind_lm3", 52), ("wind_lm4", 52), ("wind_lm5", 52)]
    patched = bytearray(raw)
    for _, start, _ in records:
        flags = struct.unpack_from("<H", patched, start + 30)[0]
        assert not flags & nitro.TEXMTX_TRANS_ZERO and flags & nitro.TEXMTX_SCALE_ONE
        assert struct.unpack_from("<2i", patched, start + 44) == (-2560, 4096)
        struct.pack_into("<2i", patched, start + 44, 0, 0)
    summary, primitives = nitro.decode_model(bytes(patched))
    assert summary["name"] == "wind" and summary["triangles"] == 154
    assert all(m["uv_translate"] == [0.0, 0.0] for m in summary["materials"])
    # The scene reports the refusal and keeps the rest of New Bark.
    scene = project.map_scene(header=NEW_BARK["header"], cell=NEW_BARK["cell"], image=False)
    wind = next(m for m in scene["models"] if m["model_id"] == WIND)
    assert wind["status"] == "unsupported" and "convention is not verified" in wind["reason"]
    assert wind["name"] == "wind" and wind["label"] == "Ambient wind streaks"


def test_truncated_material_transform_fields_refuse_by_name(project):
    raw = bytearray(resource(project.blob, world.BUILDING_MODEL_ARCHIVE, WIND)[1])
    name, start, _ = material_records(raw)[0]
    struct.pack_into("<H", raw, start + 2, 48)
    with pytest.raises(EditorError, match=f"Truncated material texture-matrix translation: {name}"):
        nitro.decode_model(bytes(raw))
    # Trailing bytes that no flag explains are refused too, never skipped.
    clean = bytearray(resource(project.blob, world.BUILDING_MODEL_ARCHIVE, BOARD_A)[1])
    name, start, size = material_records(clean)[0]
    assert size == 44
    struct.pack_into("<H", clean, start + 2, 48)
    with pytest.raises(EditorError, match=f"{name} has 4 unexplained trailing bytes"):
        nitro.decode_model(bytes(clean))


def tex0_offset(raw):
    return struct.unpack_from("<I", raw, 16)[0]


def test_palette_bounds_refuse_malformed_tileset_tables(project):
    context = project.context(header=NEW_BARK["header"], cell=NEW_BARK["cell"])
    raw = resource(project.blob, world.MAP_TEXTURE_ARCHIVE, context["area_data"]["map_tileset"])[1]
    base = tex0_offset(raw)
    data = raw[base:]
    palette_bytes, palette_dict, palette_data = struct.unpack_from("<H2xII", data, 0x30)
    starts = [palette_data + (struct.unpack("<H2x", value)[0] << 3)
              for _, value in nitro.info(data, palette_dict, 4)]
    # A palette starting exactly at the end of palette data has no colours and no
    # following boundary: that is an EditorError, not an IndexError.
    ends_at_last = bytearray(raw)
    struct.pack_into("<H", ends_at_last, base + 0x30, (max(starts) - palette_data) >> 3)
    with pytest.raises(EditorError, match="Palette outside TEX0 palette data"):
        nitro.texture_set(bytes(ends_at_last))
    oversized = bytearray(raw)
    struct.pack_into("<H", oversized, base + 0x30, 0xFFFF)
    with pytest.raises(EditorError, match="Palette data exceeds its TEX0 block"):
        nitro.texture_set(bytes(oversized))
    truncated = bytearray(raw)
    struct.pack_into("<I", truncated, base + 4, 0x20)
    with pytest.raises(EditorError):
        nitro.texture_set(bytes(truncated))
    # The unmodified tileset still decodes with non-empty palettes.
    textures, palettes = nitro.texture_set(raw)
    assert textures and palettes and all(len(colours) > 0 for colours in palettes.values())
    assert palette_bytes > 0


def test_texture_matrix_rotation_is_refused_rather_than_guessed(project):
    """Clearing ROT_ZERO on a real material must refuse, not invent an orientation."""
    raw = bytearray(resource(project.blob, world.BUILDING_MODEL_ARCHIVE, BOARD_A)[1])
    mdl = struct.unpack_from("<I", raw, 16)[0]
    model_offset = struct.unpack("<I", nitro.info(nitro.blocks(bytes(raw))[b"MDL0"], 8)[0][1])[0]
    base = mdl + model_offset
    materials = struct.unpack_from("<I", raw, base + 8)[0]
    first = base + materials + struct.unpack("<I", nitro.info(bytes(raw[base:]), materials + 4)[0][1])[0]
    flags = struct.unpack_from("<H", raw, first + 30)[0]
    assert flags & nitro.TEXMTX_ROT_ZERO
    struct.pack_into("<H", raw, first + 30, flags & ~nitro.TEXMTX_ROT_ZERO)
    with pytest.raises(EditorError, match="Unsupported texture-matrix rotation"):
        nitro.decode_model(bytes(raw))


def test_node_rotation_reading_matches_the_stored_bounding_box(project):
    """wk_sp1 has a pivot node and a 3x3 node; the wrong convention fails its box."""
    raw = resource(project.blob, world.BUILDING_MODEL_ARCHIVE, WK_SP1)[1]
    summary, _ = nitro.decode_model(raw)
    assert summary["nodes"] == 3
    original = nitro._nodes

    def transposed(model):
        result = []
        for matrix in original(model):
            flipped = matrix.copy()
            flipped[:3, :3] = matrix[:3, :3].T
            result.append(flipped)
        return result

    nitro.decode_model.cache_clear()
    nitro._nodes = transposed
    try:
        with pytest.raises(EditorError, match="disagrees with native bounds"):
            nitro.decode_model(raw)
    finally:
        nitro._nodes = original
        nitro.decode_model.cache_clear()
    # The supported reading still decodes after the cache was cleared.
    assert nitro.decode_model(raw)[0]["name"] == "wk_sp1"


def test_scene_render_is_cached_and_keyed_on_state(project):
    from PIL import Image
    scene = project.map_scene(header=CHERRYGROVE["header"], cell=CHERRYGROVE["cell"])
    image = Path(scene["image"])
    assert image.is_file() and scene["image_size"] == [1024, 1024]
    stamp = image.stat().st_mtime_ns
    again = project.map_scene(header=CHERRYGROVE["header"], cell=CHERRYGROVE["cell"])
    assert Path(again["image"]) == image and image.stat().st_mtime_ns == stamp
    picture = Image.open(image).convert("RGB")
    # A real textured render, not a flat background or a synthetic grid: many
    # distinct colours and no single colour covering the cell.
    colours = picture.getcolors(maxcolors=1 << 20)
    assert picture.size == (1024, 1024) and len(colours) > 60
    assert max(count for count, _ in colours) < .5 * 1024 * 1024
    assert tuple(picture.getpixel((512, 512))) != mapscene.BACKGROUND
    smaller = project.map_scene(header=CHERRYGROVE["header"], cell=CHERRYGROVE["cell"], pixels_per_tile=8)
    assert Path(smaller["image"]) != image and Image.open(smaller["image"]).size == (256, 256)
    other = project.map_scene(header=NEW_BARK["header"], cell=NEW_BARK["cell"])
    assert Path(other["image"]) != image
    with pytest.raises(EditorError, match="pixels per tile"):
        project.map_scene(header=NEW_BARK["header"], cell=NEW_BARK["cell"], pixels_per_tile=200)


def test_applied_move_changes_the_rendered_scene(project):
    before = project.map_scene(header=NEW_BARK["header"], cell=NEW_BARK["cell"])
    project.apply_map_edit(0, header=NEW_BARK["header"], cell=NEW_BARK["cell"],
                           placement={"slot": NB_SLOT, **NB_TO}, label="scene render")
    after = Project(project.root).map_scene(header=NEW_BARK["header"], cell=NEW_BARK["cell"])
    assert Path(after["image"]) != Path(before["image"])
    assert Path(before["image"]).read_bytes() != Path(after["image"]).read_bytes()
    moved = next(p for p in after["placements"] if p["slot"] == NB_SLOT)
    assert moved["position"]["x"] == NB_TO["x"] and moved["changed"]
    stock = next(p for p in before["placements"] if p["slot"] == NB_SLOT)
    assert moved["footprint"][0] == pytest.approx(stock["footprint"][0] - 1)


# ---- labels, thumbnails and their dependencies -------------------------------


def test_model_names_and_labels_stay_honest(project):
    scene = project.map_scene(header=NEW_BARK["header"], cell=NEW_BARK["cell"], image=False)
    by_id = {m["model_id"]: m for m in scene["models"]}
    sign = by_id[BOARD_A]
    assert (sign["name"], sign["label"], sign["label_basis"]) == ("board_a", "Town sign", "reviewed model name")
    assert sign["display"] == "Town sign · board_a (29)"
    # An unmapped model keeps its internal name and member id instead of a guess.
    unknown = by_id[WK_SP1]
    assert unknown["name"] == "wk_sp1" and unknown["label"] is None
    assert unknown["display"] == "wk_sp1 (model 27)" and unknown["label_basis"] is None
    assert mapscene.describe_model(999, "shelf09b")["label_basis"] == "internal model name"
    assert mapscene.describe_model(999, "zzz_unknown")["display"] == "zzz_unknown (model 999)"


def test_thumbnail_cache_key_follows_the_tileset_it_used(project):
    first = project.map_thumbnail(BOARD_A, header=NEW_BARK["header"], cell=NEW_BARK["cell"])
    assert Path(first["image"]).is_file() and first["name"] == "board_a"
    assert first["texture_source"] == "embedded TEX0" and first["tileset"] is None
    same = project.map_thumbnail(BOARD_A, header=CHERRYGROVE["header"], cell=CHERRYGROVE["cell"])
    # Embedded textures do not depend on the area, so reuse is correct here.
    assert Path(same["image"]) == Path(first["image"])
    keys = {mapscene.thumbnail_key("a/0/4/0", "model-sha", sha, (260, 190)) for sha in ("a", "b")}
    assert len(keys) == 2
    assert mapscene.thumbnail_key("a/0/4/0", "model-sha", None, (260, 190)) \
        == mapscene.thumbnail_key("a/0/4/0", "model-sha", None, (260, 190))
    assert mapscene.thumbnail_key("a/1/4/8", "model-sha", None, (260, 190)) not in keys


def test_thumbnail_of_a_tileset_textured_model_is_keyed_on_that_tileset(project, monkeypatch):
    """A shared model drawn with two different area tilesets gets two pictures."""
    real = mapscene.decode_model

    def patched(raw, geometry_only=False, tileset=None, index=0):
        summary, primitives = real(raw, geometry_only, tileset, index)
        return {**summary, "texture_source": "area tileset"}, primitives

    monkeypatch.setattr(mapscene, "decode_model", patched)
    new_bark = project.map_thumbnail(BOARD_A, header=NEW_BARK["header"], cell=NEW_BARK["cell"])
    route = project.map_thumbnail(BOARD_A, header=ROUTE_33["header"], cell=ROUTE_33["cell"])
    assert new_bark["tileset"]["member"] != route["tileset"]["member"]
    assert new_bark["tileset"]["sha256"] != route["tileset"]["sha256"]
    assert Path(new_bark["image"]) != Path(route["image"])


# ---- picking and footprints --------------------------------------------------


def test_footprint_follows_the_drawn_rotation(project):
    bounds = [[-16.0, 0.0, -4.0], [16.0, 20.0, 4.0]]
    position = {"x": 100.0, "z": 200.0}
    assert mapscene.footprint(position, bounds) == [99.0, 199.75, 101.0, 200.25]
    quarter, degrees = mapscene._placement_transform(
        {"key": "t", "rotation_raw": [0, 0x4000, 0], "scale_raw": [4096] * 3})
    assert degrees == pytest.approx(90)
    turned = mapscene.footprint(position, bounds, quarter)
    # A quarter turn swaps the extents; the unrotated box would not.
    assert turned == pytest.approx([99.75, 199.0, 100.25, 201.0], abs=1e-9)
    assert mapscene.footprint(position, None) is None
    with pytest.raises(EditorError, match="rotates around X/Z"):
        mapscene._placement_transform({"key": "t", "rotation_raw": [7, 0, 0], "scale_raw": [4096] * 3})
    with pytest.raises(EditorError, match="non-neutral size words"):
        mapscene._placement_transform({"key": "t", "rotation_raw": [0, 0, 0], "scale_raw": [8192, 4096, 4096]})


def test_scene_picking_selects_the_object_under_the_point(project):
    app, window = open_window(project)
    ox, oz = NEW_BARK["origin"]
    sign = next(p for p in window.scene_data["placements"] if p["slot"] == NB_SLOT)
    x0, z0, x1, z1 = sign["footprint"]
    assert window.pick_placement((x0 + x1) / 2 - ox, (z0 + z1) / 2 - oz) == NB_SLOT
    assert window.pick_placement(NB_FROM["x"] - ox, NB_FROM["z"] - oz) == NB_SLOT
    # Open ground picks nothing, so a stray click cannot silently select a record.
    assert window.pick_placement(3.5, 29.5) is None
    # Selecting in the scene moves the list, and selecting in the list moves the scene.
    window.select_placement(NB_SLOT)
    assert window.selected_slot == NB_SLOT
    assert "board_a" in window.object_label.text()
    row = next(r for r in range(window.placement_list.count())
               if window.placement_list.item(r).text().startswith("0:0 "))
    window.placement_list.setCurrentRow(row)
    app.processEvents()
    assert window.selected_slot == 0 and "wk_labo" in window.object_label.text()
    window.close()
    app.processEvents()


# ---- real mouse/key events on the scene --------------------------------------


@pytest.fixture
def wind_geometry(monkeypatch):
    """Give model 28 its real geometry with the unverified translation words zeroed.

    Only the picking extent matters here: the wind streaks' drawn box spans most of
    New Bark, which is exactly the ambient case picking must not treat as solid.
    """
    real = mapscene.building_model

    def patched(project_, archive, model_id, tileset):
        if model_id != WIND:
            return real(project_, archive, model_id, tileset)
        raw = bytearray(resource(project_.blob, archive, model_id)[1])
        for _, start, size in material_records(raw):
            if size == 52:
                struct.pack_into("<2i", raw, start + 44, 0, 0)
        summary, primitives = nitro.decode_model(bytes(raw), tileset=tileset)
        return {"archive": archive, "member": model_id, "sha256": digest(bytes(raw))}, summary, primitives

    monkeypatch.setattr(mapscene, "building_model", patched)


def styled_window(project, context=NEW_BARK, size=(1440, 900)):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.gui import STYLE
    from sovereign_editor.map_inspector import MapInspectorWindow
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = MapInspectorWindow(project.root, context=(context["header"], context["cell"]))
    window.resize(*size)
    window.show()
    app.processEvents()
    return app, window


def mouse(window, kind, global_tile, button, buttons=None, offset=(0, 0)):
    """Send a real QMouseEvent to the scene viewport at a global tile position."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.map_inspector import TILE
    ox, oz = window.view_data["context"]["origin"]
    view = window.grid
    point = view.mapFromScene(QPointF((global_tile[0] - ox) * TILE, (global_tile[1] - oz) * TILE))
    local = QPointF(point.x() + offset[0], point.y() + offset[1])
    types = {"press": QEvent.Type.MouseButtonPress, "move": QEvent.Type.MouseMove,
             "release": QEvent.Type.MouseButtonRelease, "double": QEvent.Type.MouseButtonDblClick}
    held = button if buttons is None else buttons
    event = QMouseEvent(types[kind], local, view.viewport().mapToGlobal(local),
                        Qt.MouseButton.NoButton if kind == "move" else button,
                        held, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(view.viewport(), event)
    QApplication.processEvents()


def space(window, pressed):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication
    event = QKeyEvent(QEvent.Type.KeyPress if pressed else QEvent.Type.KeyRelease,
                      Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(window.grid, event)


def scroll(window):
    return window.grid.horizontalScrollBar().value(), window.grid.verticalScrollBar().value()


def untouched(window, project):
    return (window.selected_slot is None and window.staged_cells == {} and window.selected_cells == set()
            and window.staged_move() is None and Project(project.root).doc["revision"] == 0)


def test_open_ground_inside_ambient_wind_pans_instead_of_dragging(project, wind_geometry):
    from PySide6.QtCore import Qt
    app, window = styled_window(project)
    wind = next(p for p in window.scene_data["placements"] if p["model_id"] == WIND)
    assert wind["ambient"] and wind["status"] == "ok"
    sign = next(p for p in window.scene_data["placements"] if p["slot"] == NB_SLOT)
    assert not sign["ambient"]
    window.focus_tile(686, 398, 10)
    app.processEvents()
    ground = (686.5, 398.5)
    assert wind["footprint"][0] < ground[0] < wind["footprint"][2]
    assert wind["footprint"][1] < ground[1] < wind["footprint"][3]
    assert window.pick_placement(ground[0] - 672, ground[1] - 384) is None
    before = scroll(window)
    left = Qt.MouseButton.LeftButton
    mouse(window, "press", ground, left)
    mouse(window, "move", ground, Qt.MouseButton.NoButton, left, offset=(-90, -70))
    mouse(window, "release", ground, left, Qt.MouseButton.NoButton, offset=(-90, -70))
    assert scroll(window) != before, "dragging open ground pans the view"
    assert untouched(window, project)
    # The ambient effect stays selectable by its anchor marker and by the list.
    assert window.pick_placement(wind["position"]["x"] - 672, wind["position"]["z"] - 384) == wind["slot"]
    window.close()
    app.processEvents()


@pytest.mark.parametrize("gesture", ["middle", "space"])
def test_pan_gestures_over_a_model_never_select_or_stage(project, gesture):
    from PySide6.QtCore import Qt
    app, window = styled_window(project)
    window.focus_tile(685, 400, 8)
    app.processEvents()
    target = (NB_FROM["x"], NB_FROM["z"])
    assert window.pick_placement(target[0] - 672, target[1] - 384) == NB_SLOT
    button = Qt.MouseButton.MiddleButton if gesture == "middle" else Qt.MouseButton.LeftButton
    for tool in ("select", "block"):
        window.set_tool(tool)
        before = scroll(window)
        if gesture == "space":
            space(window, True)
        mouse(window, "press", target, button)
        mouse(window, "move", target, Qt.MouseButton.NoButton, button, offset=(80, 60))
        mouse(window, "release", target, button, Qt.MouseButton.NoButton, offset=(80, 60))
        # The second click of a pan must not select or toggle a cell either.
        mouse(window, "double", target, button, offset=(80, 60))
        mouse(window, "release", target, button, Qt.MouseButton.NoButton, offset=(80, 60))
        if gesture == "space":
            space(window, False)
        assert scroll(window) != before
        assert untouched(window, project), f"{gesture} pan changed state in {tool} mode"
        assert window.x_input.anchor_value() == 0 and window.z_input.anchor_value() == 0
    window.close()
    app.processEvents()


def test_left_drag_on_a_model_stages_a_move_through_real_events(project):
    from PySide6.QtCore import Qt
    from sovereign_editor.map_inspector import TILE
    app, window = styled_window(project)
    window.focus_tile(685, 400, 8)
    app.processEvents()
    scale = window.grid.transform().m11()
    left = Qt.MouseButton.LeftButton
    start = (NB_FROM["x"], NB_FROM["z"])
    mouse(window, "press", start, left)
    assert window.selected_slot == NB_SLOT and window.staged_move() is None, "a click alone moves nothing"
    assert not window.apply_button.isEnabled()
    mouse(window, "move", start, Qt.MouseButton.NoButton, left, offset=(-TILE * scale * 1.1, 0))
    mouse(window, "release", start, left, Qt.MouseButton.NoButton, offset=(-TILE * scale * 1.1, 0))
    move = window.staged_move()
    assert move and move["after"] == NB_TO and move["before"] == NB_FROM
    assert window.apply_button.isEnabled() and window.move_warning.isVisible()
    assert window.align_sign.isVisible() and "Tick the alignment option" in window.move_warning.text()
    assert [label.text() for label in window.move_labels] == ["old 685.5, 400.5", "new 684.5, 400.5"]
    assert Project(project.root).doc["revision"] == 0
    window.close()
    app.processEvents()


def open_cells(window, blocked, count):
    """`count` horizontally adjacent cells that all have (or lack) the 0x80 flag."""
    rows = window.view_data["permissions"]["rows"]
    ox, oz = window.view_data["context"]["origin"]
    for lz in range(1, 31):
        for lx in range(1, 31 - count):
            run = [bytes.fromhex(rows[lz][lx + i]) for i in range(count)]
            if all(bool(p[1] & 128) == blocked and p[0] not in (16, 21) for p in run):
                return [(ox + lx + i, oz + lz) for i in range(count)]
    raise AssertionError("no suitable run of cells")


@pytest.mark.parametrize("first,second,blocked", [("block", "unblock", False), ("unblock", "block", True)])
def test_paint_strokes_are_reversible_before_apply(project, first, second, blocked):
    from PySide6.QtCore import Qt
    app, window = styled_window(project)
    cells = open_cells(window, blocked, 4)
    window.focus_tile(cells[0][0] + 2, cells[0][1], 10)
    app.processEvents()
    left = Qt.MouseButton.LeftButton
    centre = lambda cell: (cell[0] + .5, cell[1] + .5)

    def stroke(tool):
        window.set_tool(tool)
        mouse(window, "press", centre(cells[0]), left)
        for cell in cells[1:]:
            mouse(window, "move", centre(cell), Qt.MouseButton.NoButton, left)
        mouse(window, "release", centre(cells[-1]), left, Qt.MouseButton.NoButton)

    stroke(first)
    assert set(window.staged_cells) == set(cells)
    for cell in cells:
        before, after = (bytes.fromhex(window.staged_cells[cell][k]) for k in ("before", "after"))
        assert before[0] == after[0] and before[1] & ~128 == after[1] & ~128
        assert bool(after[1] & 128) == (first == "block")
    assert window.apply_button.isEnabled() and window.selected_slot is None
    stroke(second)
    # Every cell of the second stroke returned to its saved pair, not just the first.
    assert window.staged_cells == {}
    assert not window.apply_button.isEnabled()
    assert Project(project.root).doc["revision"] == 0
    window.close()
    app.processEvents()


def test_selected_cell_batch_toggles_follow_the_latest_action(project):
    from PySide6.QtCore import Qt
    app, window = styled_window(project)
    cells = open_cells(window, False, 3)
    window.focus_tile(cells[1][0], cells[1][1], 10)
    app.processEvents()
    left = Qt.MouseButton.LeftButton
    for cell in cells:
        mouse(window, "press", (cell[0] + .5, cell[1] + .5), left)
        mouse(window, "release", (cell[0] + .5, cell[1] + .5), left, Qt.MouseButton.NoButton)
        mouse(window, "double", (cell[0] + .5, cell[1] + .5), left)
        mouse(window, "release", (cell[0] + .5, cell[1] + .5), left, Qt.MouseButton.NoButton)
    assert window.selected_cells == set(cells)
    window.paint_selection(True)
    assert set(window.staged_cells) == set(cells)
    assert all(bytes.fromhex(window.staged_cells[c]["after"])[1] & 128 for c in cells)
    assert all(window.staged_cells[c]["after"][:2] == window.staged_cells[c]["before"][:2] for c in cells)
    window.paint_selection(False)
    assert window.staged_cells == {}, "unblocking restores the saved pairs, so nothing stays staged"
    window.paint_selection(True)
    window.paint_selection(True)
    assert set(window.staged_cells) == set(cells)
    assert Project(project.root).doc["revision"] == 0
    window.close()
    app.processEvents()


def test_action_states_and_movement_warning_follow_the_selection(project):
    app, window = styled_window(project)
    assert not window.apply_button.isEnabled() and not window.cancel_button.isEnabled()
    assert not window.x_input.isEnabled() and not window.z_input.isEnabled()
    assert not window.move_warning.isVisible()
    assert window.tool_buttons["select"].isChecked() and not window.toggles["Collision"].isChecked()
    window.select_placement(NB_SLOT)
    assert window.x_input.isEnabled() and not window.apply_button.isEnabled()
    assert window.move_warning.isVisible()
    window.drag_placement(NB_SLOT, -1, 0)
    assert window.apply_button.isEnabled() and window.cancel_button.isEnabled()
    window.cancel_staged()
    assert not window.apply_button.isEnabled()
    # Painting a cell enables Apply without any object selection.
    window.load_context(NEW_BARK["header"], NEW_BARK["cell"])
    assert window.selected_slot is None and not window.x_input.isEnabled()
    window.set_tool("block")
    assert window.toggles["Collision"].isChecked(), "painting shows the collision grid"
    cell = open_cells(window, False, 1)[0]
    window.paint_tile(cell[0] - 672, cell[1] - 384)
    assert window.apply_button.isEnabled()
    window.set_tool("select")
    assert not window.toggles["Collision"].isChecked(), "selection mode restores the lighter view"
    window.apply_transaction()
    app.processEvents()
    assert Project(project.root).doc["revision"] == 1
    assert not window.apply_button.isEnabled()
    # The active tool is visibly distinct in the styled window.
    window.set_tool("unblock")
    app.processEvents()
    active, idle = window.tool_buttons["unblock"], window.tool_buttons["block"]
    colour = lambda button: button.grab().toImage().pixelColor(4, button.height() // 2).name()
    assert colour(active) != colour(idle)
    window.close()
    app.processEvents()


@pytest.mark.parametrize("size", [(1440, 900), (1180, 760)])
def test_essential_controls_fit_the_window(project, size):
    app, window = styled_window(project, size=size)
    window.select_placement(NB_SLOT)
    window.drag_placement(NB_SLOT, -1, 0)
    app.processEvents()
    assert window.minimumSizeHint().width() <= size[0] and window.width() == size[0]
    right = window.width()
    for widget in (window.apply_button, window.cancel_button, window.x_input, window.z_input,
                   *window.tool_buttons.values(), *window.toggles.values(), window.title_label):
        corner = widget.mapTo(window, widget.rect().bottomRight())
        assert corner.x() <= right and corner.y() <= window.height(), widget
        assert widget.isVisible()
    assert window.title_label.text() == "New Bark Town"
    window.close()
    app.processEvents()


def test_interior_opens_fitted_to_its_room(project):
    app, window = styled_window(project, ELM_LAB, size=(1180, 760))
    app.processEvents()
    assert window.title_label.text() == "Elm's Lab"
    content = window.scene_data["content"]
    # The lab mesh overhangs the cell by one tile to the west; that strip lies outside
    # this cell's permission grid and is reported rather than drawn as editable space.
    assert content["overhangs_cell"] is True
    assert window.scene_data["resources"]["terrain"]["tile_bounds"][0] == -1.0
    assert content["tiles"][0] == 0
    from sovereign_editor.map_inspector import TILE
    visible = window.grid.mapToScene(window.grid.viewport().rect()).boundingRect()
    x0, z0, x1, z1 = content["tiles"]
    assert visible.left() <= x0 * TILE and visible.right() >= x1 * TILE
    assert visible.top() <= z0 * TILE and visible.bottom() >= z1 * TILE
    assert visible.width() < 30 * TILE, "the room fills the view rather than a 32x32 canvas"
    window.fit_map()
    app.processEvents()
    full = window.grid.mapToScene(window.grid.viewport().rect()).boundingRect()
    assert full.width() >= 32 * TILE - 1 or full.height() >= 32 * TILE - 1
    window.close()
    app.processEvents()


# ---- native drag / paint transactions ----------------------------------------


def test_drag_stages_exact_coordinates_and_apply_commits_one_transaction(project):
    app, window = open_window(project)
    window.select_placement(NB_SLOT)
    # Drag one tile west and a little north; the snap keeps a representable record.
    window.drag_placement(NB_SLOT, -1.12, 0.2)
    move = window.staged_move()
    assert move["before"] == NB_FROM and move["after"] == NB_TO
    assert window.x_input.anchor_value() == NB_TO["x"] and window.z_input.anchor_value() == NB_TO["z"]
    # Stage the two explicit cells with the paint tools, not by inference.
    window.set_tool("unblock")
    window.paint_tile(NB_CELL[0] - NEW_BARK["origin"][0], NB_CELL[1] - NEW_BARK["origin"][1])
    window.set_tool("block")
    window.paint_tile(NB_TARGET_CELL[0] - NEW_BARK["origin"][0], NB_TARGET_CELL[1] - NEW_BARK["origin"][1])
    assert set(window.staged_cells) == {NB_CELL, NB_TARGET_CELL}
    assert window.staged_cells[NB_CELL]["before"] == "0080"
    assert window.staged_cells[NB_CELL]["after"] == "0000"
    assert "move 0:13" in window.pending_label.text()
    assert "cell 684,400: 0000 → 0080" in window.pending_label.text()
    window.preview()
    assert window.detail.toPlainText().startswith("PENDING")
    assert Project(project.root).doc["revision"] == 0, "preview never writes"
    window.apply_transaction()
    app.processEvents()
    assert window.detail.toPlainText().startswith("SAVED")
    assert window.staged_cells == {} and window.staged_move() is None

    saved = Project(project.root)
    assert saved.doc["revision"] == 1 and len(saved.doc["map_edits"]) == 1
    transaction = saved.doc["map_edits"][0]
    change = transaction["placements"][0]
    assert change["after"] == {"x": NB_TO["x"], "y": 1, "z": NB_TO["z"]}
    assert change["record_after"]["y"] == change["record_before"]["y"]
    assert change["record_after"]["z"] == change["record_before"]["z"]
    assert {(c["x"], c["z"]): (c["before"], c["after"]) for c in transaction["permissions"]} \
        == {NB_CELL: ("0080", "0000"), NB_TARGET_CELL: ("0000", "0080")}
    # The same request through core produces exactly this transaction body.
    reference = Project(project.root)
    reference.undo(1)
    plan = reference.plan_map_edit(
        header=NEW_BARK["header"], cell=NEW_BARK["cell"], placement={"slot": NB_SLOT, **NB_TO},
        permissions=[{"x": c["x"], "z": c["z"], "before": c["before"], "after": c["after"]}
                     for c in transaction["permissions"]], label=transaction["label"])
    assert plan["transaction"] == transaction
    window.close()
    app.processEvents()


def test_drag_preserves_every_other_word_and_survives_reopen_and_undo(project, tmp_path):
    app, window = open_window(project)
    stock = member_record(project, NEW_BARK["member"], NB_SLOT)
    window.select_placement(NB_SLOT)
    window.drag_placement(NB_SLOT, -1.0, 0.0)
    window.apply_transaction()
    app.processEvents()
    reopened = Project(project.root)
    assert reopened.doc["revision"] == 1
    report = reopened.export(tmp_path / "moved", 1)
    edited = (tmp_path / "moved/game.nds").read_bytes()
    record = narc_record(edited, NEW_BARK["member"], NB_SLOT)
    assert len(record) == 48
    expected = bytearray(stock)
    struct.pack_into("<i", expected, 4, struct.unpack_from("<i", stock, 4)[0] - 65536)
    # Only the X word changed: model id, Y, Z, rotation, size words and filler match.
    assert record == bytes(expected)
    assert report["changed_byte_count"] == sum(a != b for a, b in zip(stock, record))
    reopened.undo(1)
    undone = reopened.export(tmp_path / "undone", 2)
    assert undone["candidate_sha256"] == digest(project.blob)
    window.project = Project(project.root)
    window.refresh()
    app.processEvents()
    assert window.staged_move() is None and window.staged_cells == {}
    window.close()
    app.processEvents()


def test_paint_preserves_type_bits_and_refuses_a_no_op(project):
    app, window = open_window(project)
    ox, oz = NEW_BARK["origin"]
    rows = window.view_data["permissions"]["rows"]
    typed = next((x, z) for z in range(32) for x in range(32)
                 if bytes.fromhex(rows[z][x])[0] and bytes.fromhex(rows[z][x])[1] & 128)
    window.set_tool("unblock")
    window.paint_tile(*typed)
    staged = window.staged_cells[(ox + typed[0], oz + typed[1])]
    before, after = bytes.fromhex(staged["before"]), bytes.fromhex(staged["after"])
    assert before[0] == after[0] != 0, "the terrain type byte is preserved"
    assert before[1] & 128 and not after[1] & 128
    assert before[1] & ~128 == after[1] & ~128, "no other collision bit changes"
    # Painting a cell that already has the requested flag stages nothing.
    window.staged_cells.clear()
    window.set_tool("block")
    window.paint_tile(*typed)
    assert (ox + typed[0], oz + typed[1]) not in window.staged_cells
    assert "already blocked" in window.message.text()
    assert Project(project.root).doc["revision"] == 0
    window.close()
    app.processEvents()


def test_staged_edits_never_survive_a_context_or_revision_change(project):
    app, window = open_window(project)
    window.select_placement(NB_SLOT)
    window.drag_placement(NB_SLOT, -1.0, 0.0)
    window.set_tool("block")
    window.paint_tile(NB_TARGET_CELL[0] - NEW_BARK["origin"][0], NB_TARGET_CELL[1] - NEW_BARK["origin"][1])
    assert window.staged_cells and window.staged_move()
    # Switching context drops every staged edit, including the selection.
    window.load_context(CHERRYGROVE["header"], CHERRYGROVE["cell"])
    app.processEvents()
    assert window.staged_cells == {} and window.selected_slot is None
    assert window.staged_move() is None and window.selected_cells == set()
    assert window.view_data["context"]["map_member"] == CHERRYGROVE["member"]
    assert window.scene_data["resources"]["terrain"]["name"] == CHERRYGROVE["terrain"]
    # Cancel discards without writing.
    window.select_placement(0)
    window.drag_placement(0, 1.0, 0.0)
    assert window.staged_move()
    window.cancel_staged()
    assert window.staged_move() is None and Project(project.root).doc["revision"] == 0
    # A revision written elsewhere refuses this window's apply instead of racing it.
    other = Project(project.root)
    other.apply_map_edit(0, header=CHERRYGROVE["header"], cell=CHERRYGROVE["cell"],
                         permissions=[{"x": 550, "z": 397, "after": "0000"}], label="external")
    assert Project(project.root).doc["revision"] == 1
    window.select_placement(0)
    window.drag_placement(0, 1.0, 0.0)
    window.apply_transaction()
    app.processEvents()
    assert "revision" in window.message.text().lower()
    assert len(Project(project.root).doc["map_edits"]) == 1
    window.close()
    app.processEvents()


def test_indoor_context_edits_through_the_same_window(project, tmp_path):
    app, window = open_window(project, ELM_LAB)
    assert window.view_data["context"]["map_member"] == ELM_LAB["member"]
    assert window.scene_data["resources"]["building_models"]["archive"] == world.INTERIOR_MODEL_ARCHIVE
    placement = window.view_data["placements"][0]
    slot, before = placement["slot"], placement["position"]
    window.select_placement(slot)
    window.drag_placement(slot, 1.0, 0.0)
    window.set_tool("block")
    window.paint_tile(4, 4)
    window.apply_transaction()
    app.processEvents()
    saved = Project(project.root)
    assert saved.doc["revision"] == 1
    transaction = saved.doc["map_edits"][0]
    assert transaction["context"]["map_member"] == ELM_LAB["member"]
    assert transaction["placements"][0]["after"]["x"] == before["x"] + 1
    assert transaction["placements"][0]["after"]["z"] == before["z"]
    assert len(transaction["permissions"]) == 1
    report = saved.export(tmp_path / "indoor", 1)
    edited = (tmp_path / "indoor/game.nds").read_bytes()
    record = narc_record(edited, ELM_LAB["member"], slot)
    stock = member_record(project, ELM_LAB["member"], slot)
    expected = bytearray(stock)
    struct.pack_into("<i", expected, 4, struct.unpack_from("<i", stock, 4)[0] + 65536)
    assert record == bytes(expected)
    assert report["changed_byte_count"] > 0
    window.close()
    app.processEvents()


# ---- UI / CLI agreement ------------------------------------------------------


def member_record(project, member, slot):
    raw = resource(project.blob, world.MAP_ARCHIVE, member)[1]
    offset = map_data(raw)[1][slot]["record_offset"]
    return raw[offset:offset + 48]


def narc_record(rom_bytes, member, slot):
    import ndspy.narc
    import ndspy.rom
    raw = ndspy.narc.NARC(ndspy.rom.NintendoDSRom(rom_bytes).getFileByName(world.MAP_ARCHIVE)).files[member]
    permissions, buildings = struct.unpack_from("<2I", raw)
    extra = struct.unpack_from("<H", raw, 18)[0]
    base = 20 + extra + permissions + slot * 48
    return raw[base:base + 48]


def run_cli(*args):
    result = subprocess.run([sys.executable, "-m", "sovereign_editor.cli", *args],
                            capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def test_cli_map_scene_matches_core_and_the_window(project):
    code, out, _ = run_cli("map-scene", "--project", str(project.root), "--header", str(NEW_BARK["header"]),
                           "--cell", "21,12", "--no-image")
    assert code == 0
    result = json.loads(out)["result"]
    assert result == project.map_scene(header=NEW_BARK["header"], cell=NEW_BARK["cell"], image=False)
    app, window = open_window(project)
    assert window.scene_data["placements"] == \
        project.map_scene(header=NEW_BARK["header"], cell=NEW_BARK["cell"])["placements"]
    assert [p["display"] for p in result["placements"]] == [p["display"] for p in window.scene_data["placements"]]
    window.close()
    app.processEvents()
    # The rendered form and the thumbnail are reachable from the same command.
    code, out, _ = run_cli("map-scene", "--project", str(project.root), "--header", str(NEW_BARK["header"]),
                           "--cell", "21,12", "--pixels-per-tile", "8", "--thumbnail", str(BOARD_A))
    assert code == 0
    rendered = json.loads(out)["result"]
    assert Path(rendered["image"]).is_file() and rendered["image_size"] == [256, 256]
    assert rendered["thumbnail"]["name"] == "board_a" and Path(rendered["thumbnail"]["image"]).is_file()


def test_cli_reports_an_unresolvable_context_without_a_traceback(project):
    code, out, _ = run_cli("map-scene", "--project", str(project.root), "--header", "60", "--cell", "0,0")
    assert code == 2
    error = json.loads(out)["error"]
    assert error["code"] == "NOT_FOUND" and "cell" in error["message"]
