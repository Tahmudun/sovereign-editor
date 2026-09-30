"""Whole-area visual review for created areas (PROD-VIS-001). Evidence, not native acceptance.

* Composes every cell of an area (terrain + placed models) at its matrix offset and
  stock altitude (8 model units per altitude step) into one top-down render.
* Flood-fills the walkable region from an arrival tile using the composed
  collision and one-way ledge jumps (again with Surf, reported separately), then checks camera enclosure: the native field view around each
  reachable tile (VIEW, measured from a user native screenshot: ~16 tiles across,
  more to the north) must stay inside populated cells of the area.
* Writes crops at native scale (16 px/tile) and enlarged (32 px/tile) with the view
  box drawn, for arrivals, seams, perimeter sectors and named points.

Usage (library): review(project, header, arrival, points, out_dir) -> report dict
"""
import json
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sovereign_editor import mapscene, world  # noqa: E402
from sovereign_editor.formats import EditorError  # noqa: E402
from sovereign_editor.raster import render  # noqa: E402

UNITS = 16
ALTITUDE_UNITS = 8
# View box around the player tile (west, north, east, south), tiles. The DS top
# screen is 16x12 tiles at the player's depth; the tilted camera sees further north.
VIEW = (10, 9, 10, 6)


def oblique(prims, box, pitch=40, scale=2, headroom=96):
    """Camera-like view of tile box (x0, z0, x1, z1) seen from the south at `pitch` degrees
    elevation (90 = the top-down plan). Shows facades and canopy fronts the plan omits;
    still unlit and without sprites, so it is evidence, not native framing."""
    import math
    x0, z0, x1, z1 = box
    e = math.radians(pitch)
    lo = np.array([x0 * UNITS - 64, -np.inf, z0 * UNITS - 64])
    hi = np.array([x1 * UNITS + 64, np.inf, z1 * UNITS + 64 + headroom])
    keep = [replace(p, vertices=p.vertices - [x0 * UNITS, 0, z0 * UNITS]) for p in prims
            if (p.vertices.max(0) >= lo).all() and (p.vertices.min(0) <= hi).all()]
    top = headroom * math.cos(e)
    # Lower every vertex by the headroom so tall fronts at the box's north edge stay in frame.
    keep = [replace(p, vertices=p.vertices - [0, headroom, 0]) for p in keep]
    projection = np.array([[1, 0, 0], [0, -math.cos(e), math.sin(e)], [0, math.sin(e), math.cos(e)]]) * scale
    width = int((x1 - x0) * UNITS * scale)
    height = int(((z1 - z0) * UNITS * math.sin(e) + top) * scale)
    return render(keep, (width, height), projection)


def cells(project, header):
    grid = project.matrix_data(project.header(header)['matrix'])
    out = []
    for y in range(grid['height']):
        for x in range(grid['width']):
            if grid['maps'][y][x] == world.EMPTY:
                continue
            if grid['has_headers'] and grid['headers'][y][x] != header:
                continue
            out.append((x, y))
    return grid, out


def primitives(project, header):
    grid, used = cells(project, header)
    prims, skipped = [], []
    for x, y in used:
        ctx = project.context(header=header, cell=[x, y])
        description = mapscene.scene(project, header=header, cell=[x, y])
        refs, blobs = mapscene.tilesets(project, ctx)
        parts = mapscene.scene_primitives(project, ctx, description['placements'], blobs,
                                          refs['building_models']['archive'])
        skipped += [p['key'] for p in description['placements'] if p['status'] != 'ok']
        altitude = (ctx['cell']['altitude'] or 0) * ALTITUDE_UNITS
        offset = np.array([x * 32 * UNITS, altitude, y * 32 * UNITS])
        prims += [replace(p, vertices=p.vertices + offset) for p in parts]
    return grid, used, prims, skipped


# Stock one-way ledges (behavior -> step jumped over) and surfable water behaviors,
# from the pinned metatile_behavior tables (work/map-area-authoring-1).
LEDGES = {0x38: (1, 0), 0x39: (-1, 0), 0x3A: (0, -1), 0x3B: (0, 1)}
SURFABLE = {0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x19, 0x2A, 0x50, 0x51, 0x52, 0x53, 0x73, 0x78, 0x7C}


def walkable(project, header, grid, used):
    """Composed (behavior, blocked) grids over the whole area."""
    state = project.composed()
    shape = (grid['height'] * 32, grid['width'] * 32)
    behaviour, blocked = np.zeros(shape, int), np.ones(shape, bool)
    for x, y in used:
        ctx = project.context(header=header, cell=[x, y])
        raw = project.member_raw(ctx['map_member'])
        start = ctx['sections']['permissions_offset']
        for tz in range(32):
            for tx in range(32):
                at = start + 2 * (tz * 32 + tx)
                pair = state['permissions'].get((ctx['map_member'], at), raw[at:at + 2])
                behaviour[y * 32 + tz, x * 32 + tx] = pair[0]
                blocked[y * 32 + tz, x * 32 + tx] = bool(pair[1] & 0x80)
    return behaviour, blocked


def reachable(behaviour, blocked, start, surf=False):
    """Tiles the player can stand on from `start`: open ground, one-way ledge jumps
    (landing two tiles on), and surfable water only when `surf` is set."""
    height, width = behaviour.shape

    def standable(x, z):
        if not (0 <= z < height and 0 <= x < width) or blocked[z, x]:
            return False
        return surf or behaviour[z, x] not in SURFABLE
    seen = np.zeros(behaviour.shape, bool)
    stack = [tuple(start)]
    while stack:
        x, z = stack.pop()
        if not standable(x, z) or seen[z, x]:
            continue
        seen[z, x] = True
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, nz = x + dx, z + dz
            if 0 <= nz < height and 0 <= nx < width and LEDGES.get(behaviour[nz, nx]) == (dx, dz):
                stack.append((nx + dx, nz + dz))
            else:
                stack.append((nx, nz))
    return seen


def enclosure(grid, used, region):
    """Reachable tiles whose view box leaves the area's populated cells."""
    inside = np.zeros((grid['height'] * 32, grid['width'] * 32), bool)
    for x, y in used:
        inside[y * 32:(y + 1) * 32, x * 32:(x + 1) * 32] = True
    w, n, e, s = VIEW
    bad = []
    zs, xs = np.nonzero(region)
    for x, z in zip(xs, zs):
        x0, z0, x1, z1 = x - w, z - n, x + e, z + s
        if x0 < 0 or z0 < 0 or x1 >= inside.shape[1] or z1 >= inside.shape[0] or not inside[z0:z1 + 1, x0:x1 + 1].all():
            bad.append([int(x), int(z)])
    margins = {'west': int(xs.min()), 'north': int(zs.min()),
               'east': int(inside.shape[1] - 1 - xs.max()), 'south': int(inside.shape[0] - 1 - zs.max())}
    return bad, margins


def perimeter(region, sectors=16):
    """Reachable tiles on the region's edge, sampled by angle around its centre."""
    edge = region & ~(np.roll(region, 1, 0) & np.roll(region, -1, 0) & np.roll(region, 1, 1) & np.roll(region, -1, 1))
    zs, xs = np.nonzero(edge)
    cx, cz = xs.mean(), zs.mean()
    angle = np.arctan2(zs - cz, xs - cx)
    picks = []
    for k in range(sectors):
        lo, hi = -np.pi + 2 * np.pi * k / sectors, -np.pi + 2 * np.pi * (k + 1) / sectors
        idx = np.nonzero((angle >= lo) & (angle < hi))[0]
        if len(idx):
            far = idx[np.argmax((xs[idx] - cx) ** 2 + (zs[idx] - cz) ** 2)]
            picks.append((f'perimeter-{k:02d}', int(xs[far]), int(zs[far])))
    return picks


def contact_sheet(views, columns=4):
    """Labelled grid of equally sized view crops."""
    from PIL import Image, ImageDraw
    cw, ch = views[0][1].size
    rows = (len(views) + columns - 1) // columns
    sheet = Image.new('RGB', (columns * cw, rows * (ch + 14)), (24, 34, 41))
    draw = ImageDraw.Draw(sheet)
    for i, (label, view) in enumerate(views):
        x, y = (i % columns) * cw, (i // columns) * (ch + 14)
        draw.text((x + 2, y + 1), label, fill=(255, 255, 0))
        sheet.paste(view, (x, y + 14))
    return sheet


def review(project, header, arrival, points, out, actors=()):
    from PIL import ImageDraw
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    grid, used, prims, skipped = primitives(project, header)
    width, height = grid['width'] * 32, grid['height'] * 32
    full = render(prims, (width * 16, height * 16), [[1, 0, 0], [0, 0, 1], [0, 1, 0]])
    full.save(out / 'area-full-16px.png')
    full.resize((width * 4, height * 4)).save(out / 'area-overview-4px.png')
    behaviour, blocked = walkable(project, header, grid, used)
    region = reachable(behaviour, blocked, tuple(arrival))
    bad, margins = enclosure(grid, used, region)
    surfing = reachable(behaviour, blocked, tuple(arrival), surf=True)
    surf_bad, _ = enclosure(grid, used, surfing)
    marked = full.resize((width * 4, height * 4)).convert('RGB')
    draw = ImageDraw.Draw(marked)
    zs, xs = np.nonzero(region)
    for x, z in zip(xs, zs):
        draw.point((int(x) * 4 + 2, int(z) * 4 + 2), fill=(255, 0, 255))
    for _, ax, az in actors:
        draw.rectangle((ax * 4, az * 4, ax * 4 + 3, az * 4 + 3), outline=(255, 255, 0))
    marked.save(out / 'area-reachable-4px.png')
    crops, views = [], []
    w, n, e, s = VIEW
    for label, x, z in list(points) + perimeter(region):
        box = ((x - w - 2) * 16, (z - n - 2) * 16, (x + e + 3) * 16, (z + s + 3) * 16)
        crop = full.crop(box).convert('RGB')
        d = ImageDraw.Draw(crop)
        d.rectangle(((2 * 16), (2 * 16), (2 + w + e + 1) * 16, (2 + n + s + 1) * 16), outline=(255, 0, 255), width=2)
        d.rectangle(((2 + w) * 16, (2 + n) * 16, (3 + w) * 16, (3 + n) * 16), outline=(255, 255, 0), width=2)
        for name, ax, az in actors:      # actor tiles (the renders have no sprites)
            px, pz = (ax - x + w + 2) * 16, (az - z + n + 2) * 16
            if 0 <= px < crop.width and 0 <= pz < crop.height:
                d.rectangle((px + 3, pz + 3, px + 12, pz + 12), outline=(255, 64, 64), width=2)
                d.text((px + 1, pz - 10), name, fill=(255, 64, 64))
        crop.save(out / f'{label}-plan.png')
        # Camera-like view of the same box: native scale (16 px per tile) and 2x.
        view = oblique(prims, (x - w, z - n, x + e + 1, z + s + 1), scale=1).convert('RGB')
        view.save(out / f'{label}-view.png')
        view.resize((view.width * 2, view.height * 2)).save(out / f'{label}-view-2x.png')
        views.append((label, view))
        crops.append({'label': label, 'tile': [x, z], 'reachable': bool(region[z, x]) if 0 <= z < height and 0 <= x < width else False})
    if views:
        contact_sheet(views).save(out / 'contact-sheet.png')
    report = {'header': header, 'cells': len(used), 'size_tiles': [width, height], 'placements_skipped': skipped,
              'arrival': list(arrival), 'actors': [list(a) for a in actors],
              'reachable_tiles': int(region.sum()), 'view_box': VIEW,
              'view_leaves_area': len(bad), 'view_leaves_examples': bad[:10], 'reachable_margins': margins,
              'reachable_with_surf': int(surfing.sum()), 'view_leaves_area_with_surf': len(surf_bad),
              'crops': crops,
              'limits': ['plan and 40-degree views are unlit renders; the engine camera, lighting, sprites, weather and animation are not modelled',
                         'the view box is an estimate from a native screenshot, not the engine camera frustum']}
    (out / 'area-review.json').write_text(json.dumps(report, indent=1) + '\n')
    return report
