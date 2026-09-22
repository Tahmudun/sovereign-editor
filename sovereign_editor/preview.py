"""Cached top-down texture preview of pinned, independently decoded stock maps."""
import json
import os
import struct
import tempfile
from dataclasses import replace
from pathlib import Path

import numpy as np
from PIL import Image

from .formats import ASSETS, digest, require
from .nitro import Primitive
from .raster import render

RENDERER_VERSION = "stock-scene-v2"


def glb(path):
    blob = path.read_bytes()
    require(struct.unpack_from("<III", blob) == (0x46546C67, 2, len(blob)), "Invalid preview GLB")
    length, kind = struct.unpack_from("<II", blob, 12)
    require(kind == 0x4E4F534A, "GLB JSON absent")
    doc = json.loads(blob[20:20 + length])
    size, kind = struct.unpack_from("<II", blob, 20 + length)
    require(kind == 0x004E4942, "GLB binary absent")
    binary = blob[28 + length:28 + length + size]

    def access(index):
        a = doc["accessors"][index]
        v = doc["bufferViews"][a["bufferView"]]
        code = {5121: "B", 5123: "H", 5126: "f"}[a["componentType"]]
        count = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}[a["type"]]
        fmt = "<" + code * count
        offset = v.get("byteOffset", 0) + a.get("byteOffset", 0)
        return np.array([struct.unpack_from(fmt, binary, offset + i * v.get("byteStride", struct.calcsize(fmt))) for i in range(a["count"])])

    return doc, access


def _terrain_primitives():
    primitives = []
    for col, name in enumerate(("map16_12c.glb", "map17_12c.glb")):
        path = ASSETS / name
        doc, access = glb(path)
        for primitive in doc["meshes"][0]["primitives"]:
            attr = primitive["attributes"]
            vertices = access(attr["POSITION"]) + [256 + col * 512, 0, 256]
            colors = np.ones_like(vertices)
            if "COLOR_0" in attr:
                colors = access(attr["COLOR_0"])[:, :3].astype(float)
                if doc["accessors"][attr["COLOR_0"]]["componentType"] == 5121:
                    colors /= 255
            uvs = access(attr["TEXCOORD_0"]) if "TEXCOORD_0" in attr else np.zeros((len(vertices), 2))
            mat = doc["materials"][primitive["material"]]
            pbr = mat.get("pbrMetallicRoughness", {})
            factor = pbr.get("baseColorFactor", [1, 1, 1, 1])
            colors *= factor[:3]
            ref, texture = pbr.get("baseColorTexture"), None
            repeat, mirror = [True, True], [False, False]
            if ref:
                tex = doc["textures"][ref["index"]]
                image_path = path.parent / doc["images"][tex["source"]]["uri"]
                texture = np.asarray(Image.open(image_path).convert("RGBA"))
                sampler = doc.get("samplers", [])[tex["sampler"]] if "sampler" in tex else {}
                wraps = [sampler.get(axis, 10497) for axis in ("wrapS", "wrapT")]
                repeat = [v != 33071 for v in wraps]
                mirror = [v == 33648 for v in wraps]
            material = {"name": mat["name"], "alpha": factor[3], "repeat": repeat, "mirror": mirror}
            primitives.append(Primitive(vertices, colors, uvs, access(primitive["indices"]).ravel().reshape(-1, 3), material, texture))
    return primitives


def _save_cache(project, filename, draw):
    cache = project.root / "cache"
    require(not cache.is_symlink(), "Preview cache must be project-local")
    cache.mkdir(exist_ok=True)
    target = cache / filename
    require(not target.is_symlink(), "Preview cache file must not be a symbolic link")
    if not target.is_file():
        picture = draw()
        fd, temp = tempfile.mkstemp(prefix=".preview-", suffix=".png", dir=cache)
        os.close(fd)
        try:
            picture.save(temp)
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    return target


def map_preview(project, include_models=True):
    hashes = json.loads((ASSETS / "preview-hashes.json").read_text())
    for name, expected in hashes.items():
        require(digest((ASSETS / name).read_bytes()) == expected, f"Preview asset changed: {name}")
    models = project.stock_models() if include_models else {}
    placements = project.placements()["placements"] if include_models else []
    sources = {str(k): v["source"]["sha256"] for k, v in models.items()}
    key = digest(json.dumps({"renderer": RENDERER_VERSION, "terrain": hashes, "models": sources,
                             "placements": [(p["key"], p["record_sha256"], p["xyz"], p["rotation_raw"], p["scale_raw"])
                                            for p in placements]}, sort_keys=True).encode())[:20]
    def draw():
        primitives = _terrain_primitives()
        for p in placements:
            translation = np.array([p["tile_x"], p["xyz"][1], p["tile_z"]]) * 16
            primitives.extend(replace(item, vertices=item.vertices + translation) for item in models[p["model_id"]]["primitives"])
        return render(primitives, (1536, 768), [[1.5, 0, 0], [0, 0, 1.5], [0, 1, 0]])
    return _save_cache(project, f"{RENDERER_VERSION}-{key}.png", draw)


def model_preview(project, model_id):
    model = project.stock_models()[model_id]
    key = digest((RENDERER_VERSION + model["source"]["sha256"]).encode())[:20]
    angle, tilt = np.radians([25, 30])
    c, s, cp, sp = np.cos(angle), np.sin(angle), np.cos(tilt), np.sin(tilt)
    camera = [[c, 0, -s], [s * sp, -cp, c * sp], [s * cp, sp, c * cp]]
    return _save_cache(project, f"model-{model_id}-{key}.png",
                       lambda: render(model["primitives"], (280, 180), camera, fit=True))
