"""Bounded adjacent-cell context, sharing the single-resource editing operations."""
from . import world
from .formats import EditorError


def describe(project, header=None, matrix=None, cell=None, image=True, include_grid=False, scenes=True):
    active = project.context(header=header, matrix=matrix, cell=cell)
    grid = project.matrix_data(active["matrix"]["id"])
    cx, cz = active["cell"]["x"], active["cell"]["y"]
    entries = []
    for z in range(max(0, cz - 1), min(grid["height"], cz + 2)):
        for x in range(max(0, cx - 1), min(grid["width"], cx + 2)):
            member = grid["maps"][z][x]
            entry = {"cell": [x, z], "origin": [x * 32, z * 32], "active": [x, z] == [cx, cz],
                     "map_member": None if member == world.EMPTY else member}
            entries.append(entry)
            if member == world.EMPTY:
                entry["status"] = "empty"
                continue
            owner = grid["headers"][z][x] if grid["has_headers"] else active["header"]["id"]
            entry["header"] = owner
            try:
                view = project.map_view(header=owner, matrix=grid["id"], cell=[x, z], include_grid=include_grid)
                entry.update(status="ok", context=view["context"], view=view)
            except EditorError as exc:
                entry.update(status="unsupported", code=exc.code, reason=str(exc))
                continue
            if not scenes:
                continue
            try:
                entry["scene"] = project.map_scene(header=owner, matrix=grid["id"], cell=[x, z],
                                                    image=image, pixels_per_tile=16)
            except (EditorError, OSError, ValueError) as exc:
                # A failed render does not invalidate a resolved permission/placement view.
                entry["scene_error"] = str(exc)
    return {"active": active["id"], "matrix": grid["id"], "cells": entries,
            "scope": "Activate one cell to edit its resources. Scenery transfers use an explicit separate action; "
                     "reused members share edits. Neighbor heights are shown as metadata, not editable terrain."}


def summarize(result):
    """Compact agent output; full placement/permission lists stay opt-in elsewhere."""
    cells = []
    for entry in result["cells"]:
        item = {k: v for k, v in entry.items() if k not in ("view", "scene", "context")}
        if entry.get("view"):
            item["placement_count"] = len(entry["view"]["placements"])
            item["shared_cells"] = entry["context"]["shared_cells"]
            item["altitude"] = entry["view"]["cell"]["altitude"]
        if entry.get("scene"):
            scene = entry["scene"]
            item.update(title=scene["title"], unsupported=scene["unsupported"])
            if scene.get("image"):
                item["image"] = scene["image"]
        cells.append(item)
    return {**result, "cells": cells}
