"""Terrain family geometry: donor pieces, placement, model clearing and BDHC (TERRAIN-HEIGHT-001).

Pure functions over decoded polygons and BDHC tables; terrain_authoring.py owns planning
and Project owns writes. Every piece is cut from the pinned stock donor map member 168
(Canopy Walk's coastal donor, stock header 22 cell 42,13) and placed by whole-tile
translation and quarter-turn rotation, so materials, UV density, vertex colors and
lighting normals stay the stock values. See docs/TERRAIN_AUTHORING.md.

Coordinates: pieces use tile units in x/z (global tiles once placed) and model units in y
(16 per tile). Polygons are rows [x, y, z, u, v, r, g, b, normal, shade] like
surface_native. BDHC uses cell-local tile units centred on the cell (fx32).
"""
import functools
import math
import struct

import numpy as np

from . import mapscene, nitro, surface_native, world
from .formats import EditorError, digest, map_data, map_sections, require, resource

DONOR = {'archive': world.MAP_ARCHIVE, 'member': 168, 'header': 22, 'cell': [42, 13]}
# Pinned stock bytes of the donor member (US HeartGold baseline b1ea4b20…); verified on every build.
DONOR_SHA256 = '0ac3e5258227ca7a1d569c3131c5eda55ecfc50edcaba59715ff9756af4eb16f'
RISE = 16                          # one terrace step: 16 model units = 1.0 BDHC tile unit
WATER_DROP, UNDER_DROP = 8, 9      # stock sea_on / sea_un below ground (Canopy: 16 → 8 / 7)
STAIR_WIDTH = 3
DONOR_GROUND = 16
FX = 65536
BDHC_BUFFER = 0x9000
EPS = 1e-6
UNDERLAY = 4
# Materials a terrace or pond may replace inside its footprint: flat ground and its flat
# one-unit decals. Everything else (tall grass, fences, trees, walls, water) refuses.
CLEARABLE = {'grass01gs', 'grass02', 'road01', 'road01_r', 'road01_sub', 'road02', 'road02_r',
             'grass01_a', 'grass02_a', 'flower01', 'flower02', 'flower03',
             'grass02_r'}   # v2: Route 39 flat ground (only flat faces at ground level are ever cleared)
TERRACE_MATERIALS = ('grass01gs', 'wall01_g', 'slope')
WATER_MATERIALS = ('sea_on', 'sea_un', 'pond_line', 'sea_line02')
# Quarter turns R(a, b) = (-b, a) of the canonical pieces. Walls and shores are cut from
# a WEST-facing donor side, corners from the SOUTH-WEST corner, stairs from a SOUTH stair.
SIDE_TURNS = {'west': 0, 'north': 1, 'east': 2, 'south': 3}
STAIR_TURNS = {'south': 0, 'west': 1, 'north': 2, 'east': 3}
CORNER_TURNS = {'south_west': 0, 'north_west': 1, 'north_east': 2, 'south_east': 3}
OUTWARD = {'west': (-1, 0), 'north': (0, -1), 'east': (1, 0), 'south': (0, 1)}
STEP = 0x8000                      # permission collision bit (u16 attribute bit 15)
# Low 7 bits of the flag byte select the footstep sound (ARM9 0x020FCB98): 4 grass, 6 stone/stairs.
PAIRS = {'ground': bytes((0x00, 0x04)), 'rim': bytes((0x00, 0x80)), 'stair': bytes((0x00, 0x06)),
         'water': bytes((0x15, 0x00)), 'decorative_water': bytes((0x00, 0x80))}
BLOCK_DELTA = 1.25                 # sub_02054954: |dh| >= 1.25 tile blocks a step
HALF = 1 / math.sqrt(2)


# ---- polygon helpers ---------------------------------------------------------------------

def rows(prim):
    arr = np.column_stack([prim.vertices, prim.uvs, prim.colors, prim.normals, prim.shade_commands]).astype(float)
    return [arr[i].copy() for i in prim.polygons]


def to_tiles(poly, origin=(0, 0)):
    out = poly.copy()
    out[:, 0] = (poly[:, 0] + 256) / 16 + origin[0]
    out[:, 2] = (poly[:, 2] + 256) / 16 + origin[1]
    return out


def to_model(poly, origin):
    out = poly.copy()
    out[:, 0] = (poly[:, 0] - origin[0]) * 16 - 256
    out[:, 2] = (poly[:, 2] - origin[1]) * 16 - 256
    return out


def area_xz(poly):
    x, z = poly[:, 0], poly[:, 2]
    return 0.5 * float(np.dot(x, np.roll(z, -1)) - np.dot(z, np.roll(x, -1)))


def split(poly, a, b, c):
    """Split by the line a*x + b*z + c = 0 into (<= 0, >= 0) parts. Positions, UVs and
    colors interpolate; a packed normal/shade is taken from the nearer endpoint."""
    keep, drop = [], []
    n = len(poly)
    for i in range(n):
        p, q = poly[i], poly[(i + 1) % n]
        dp = a * p[0] + b * p[2] + c
        dq = a * q[0] + b * q[2] + c
        if dp <= EPS:
            keep.append(p)
        if dp >= -EPS:
            drop.append(p)
        if (dp < -EPS and dq > EPS) or (dp > EPS and dq < -EPS):
            t = dp / (dp - dq)
            r = p + (q - p) * t
            r[8:10] = (p if t < 0.5 else q)[8:10]
            keep.append(r)
            drop.append(r)
    clean = lambda pts: np.array(pts) if len(pts) >= 3 else None
    return clean(keep), clean(drop)


def clip_rect(poly, x0, z0, x1, z1):
    """Inside part of a polygon for an axis-aligned rectangle (None if empty)."""
    for a, b, c in ((-1, 0, x0), (1, 0, -x1), (0, -1, z0), (0, 1, -z1)):
        if poly is None:
            return None
        poly, _ = split(poly, a, b, c)
    return poly


def outside_rect(poly, x0, z0, x1, z1):
    """Parts of a polygon outside an axis-aligned rectangle."""
    result, rest = [], poly
    for a, b, c in ((-1, 0, x0), (1, 0, -x1), (0, -1, z0), (0, 1, -z1)):
        if rest is None:
            break
        inside, out = split(rest, a, b, c)
        if out is not None and abs(area_xz(out)) > 1e-7:
            result.append(out)
        rest = inside
    return result


def native(polys):
    """Stock quads stay quads; larger clipped polygons become fans of triangles."""
    out = []
    for poly in polys:
        if poly is None:
            continue
        pts = [poly[0]]
        for v in poly[1:]:
            if np.linalg.norm(v[:3] - pts[-1][:3]) > 1e-7:
                pts.append(v)
        if len(pts) > 1 and np.linalg.norm(pts[0][:3] - pts[-1][:3]) < 1e-7:
            pts.pop()
        if len(pts) < 3:
            continue
        pts = np.array(pts)
        if len(pts) <= 4:
            out.append(pts)
        else:
            for i in range(1, len(pts) - 1):
                tri = np.array([pts[0], pts[i], pts[i + 1]])
                if np.linalg.norm(np.cross(tri[1, :3] - tri[0, :3], tri[2, :3] - tri[0, :3])) > 1e-6:
                    out.append(tri)
    return out


def unpack_normal(value):
    value = int(value)
    parts = [(value >> s) & 0x3FF for s in (0, 10, 20)]
    return [p - 1024 if p & 0x200 else p for p in parts]


def pack_normal(x, y, z):
    clamp = lambda v: max(-512, min(511, int(v)))
    return sum((clamp(v) & 0x3FF) << s for v, s in ((x, 0), (y, 10), (z, 20)))


def turn(poly, k):
    """Quarter turns R(a, b) = (-b, a) about the tile origin; normals rotate with them."""
    out = poly.copy()
    for _ in range(k % 4):
        x, z = out[:, 0].copy(), out[:, 2].copy()
        out[:, 0], out[:, 2] = -z, x
        for i in range(len(out)):
            if int(out[i, 9]) == 0x21:
                nx, ny, nz = unpack_normal(out[i, 8])
                out[i, 8] = pack_normal(-nz, ny, nx)
    return out


def turn_tiles(tiles, k):
    for _ in range(k % 4):
        tiles = [(-j - 1, i) for i, j in tiles]
    return tiles


# ---- donor library -----------------------------------------------------------------------

class Piece:
    """Polygons in anchor-local tile coordinates with an optional per-tile UV gradient."""

    def __init__(self, polys, tiles, grad=None, atomic=False):
        self.polys, self.tiles, self.grad, self.atomic = polys, tiles, grad, atomic

    def place(self, k, target_min, along, dy, repeat):
        tiles = turn_tiles(self.tiles, k)
        mx, mz = min(t[0] for t in tiles), min(t[1] for t in tiles)
        tx, tz = target_min[0] - mx, target_min[1] - mz
        placed = []
        for material, poly in self.polys:
            p = turn(poly, k)
            p[:, 0] += tx
            p[:, 2] += tz
            p[:, 1] += dy
            if self.grad is not None and along:
                shift = self.grad[material] * along
                shift = np.where(repeat[material], shift - np.floor(shift), shift)
                p[:, 3:5] += shift
            placed.append((material, p))
        return placed


def _affine(poly):
    """uv = [x, z, 1] @ A for a planar, non-vertical polygon in tile units."""
    v = poly[:3]
    return np.linalg.solve(np.column_stack([v[:, 0], v[:, 2], np.ones(3)]), v[:, 3:5])


def _cut(prims, materials, box, clip=True, centroid=False):
    x0, z0, x1, z1 = box
    out = []
    for prim in prims:
        name = prim.material['texture_name']
        if name not in materials:
            continue
        for poly in rows(prim):
            poly = to_tiles(poly)
            if centroid:
                cx, cz = poly[:, 0].mean(), poly[:, 2].mean()
                if x0 <= cx <= x1 and z0 <= cz <= z1:
                    out.append((name, poly))
                continue
            part = clip_rect(poly, x0, z0, x1, z1)
            if part is not None and abs(area_xz(part)) > 1e-7:
                out.extend((name, p) for p in native([part]))
    return out


def _anchor(polys, ax, az):
    result = []
    for name, poly in polys:
        p = poly.copy()
        p[:, 0] -= ax
        p[:, 2] -= az
        result.append((name, p))
    return result


def _gradient(polys, axis):
    """Per-material UV change per +1 tile along x (0) or z (2), from the planar donor faces."""
    grads = {}
    for name, poly in polys:
        try:
            a = _affine(poly)
        except np.linalg.LinAlgError:
            continue
        # Stock UVs are quantised to 1/16 texel: snap the gradient to 1/64 of a repeat.
        g = np.round((a[0] if axis == 0 else a[1]) * 64) / 64
        prev = grads.setdefault(name, g)
        require(np.allclose(prev, g, atol=2e-3), f'Donor {name} UVs are not a single repeating strip',
                'UNSUPPORTED_TERRAIN')
    return grads


def donor_context(project):
    return project.context(header=DONOR['header'], cell=list(DONOR['cell']))


@functools.lru_cache(maxsize=4)
def _library(raw_donor, tileset):
    require(digest(raw_donor) == DONOR_SHA256,
            'The pinned terrain donor map differs; this build is not qualified', 'UNQUALIFIED_DONOR')
    _, prims = nitro.decode_model(map_data(raw_donor)[2], tileset=tileset, render=False)
    mats = {p.material['texture_name']: p.material for p in prims}
    # Straight west-facing cliff: rim column x=7 of the donor walkway, z 1..8.
    wall = _anchor(_cut(prims, {'wall01_g'}, (6.9, 3, 8.1, 4)), 7, 3)
    wall_grad = _gradient(_cut(prims, {'wall01_g'}, (6.9, 1, 8.1, 8)), 2)
    # South-west outer corner tile (7, 11): rounded foot plus the grass filling the tile.
    corner = _anchor(_cut(prims, {'wall01_g', 'grass01gs'}, (7, 11, 8, 12)), 7, 11)
    # South-facing stair unit over rim tiles x 11..13, z 11 (side rails overhang by 0.19).
    stair = _anchor(_cut(prims, {'slope'}, (10.7, 10.9, 14.3, 12.1), centroid=True), 11, 11)
    # West-shore strip: land x <= 15, water x >= 16, rows 13..21 (bank lip + foam).
    shore_polys = _cut(prims, {'pond_line', 'sea_line02'}, (15.5, 13, 16.7, 21))
    shore_grad = _gradient(shore_polys, 2)
    flats = {}
    for prim in prims:
        name = prim.material['texture_name']
        if name not in ('grass01gs', 'sea_on', 'sea_un'):
            continue
        for poly in rows(prim):
            poly = to_tiles(poly)
            if np.ptp(poly[:, 1]) < 1e-4 and abs(area_xz(poly)) > 0.5:
                key = (name, round(float(poly[0, 1]), 3))
                flats.setdefault(key, (poly[0].copy(), _affine(poly), np.sign(area_xz(poly))))
    require(len(wall) and len(corner) and len(stair) and len(shore_polys)
            and ('grass01gs', 32.0) in flats and ('sea_on', 8.0) in flats and ('sea_un', 7.0) in flats,
            'The terrain donor lacks a required stock piece', 'UNQUALIFIED_DONOR')
    return {'wall': Piece(wall, [(0, 0)], wall_grad), 'corner': Piece(corner, [(0, 0)]),
            'stair': Piece(stair, [(0, 0), (1, 0), (2, 0)], atomic=True),
            'shore_grad': shore_grad, 'shore_source': shore_polys, 'flats': flats,
            'materials': {k: {f: v[f] for f in ('width', 'height', 'repeat')} for k, v in mats.items()}}


def library(project):
    ctx = donor_context(project)
    raw = project.member_raw(ctx['map_member'])
    require(ctx['map_member'] == DONOR['member'], 'Terrain donor context moved', 'UNQUALIFIED_DONOR')
    _, blobs = mapscene.tilesets(project, ctx)
    return _library(raw, blobs['map_tileset'])


def shore_piece(lib, a, b):
    """West-shore strip covering donor rows 15+a .. 15+b (a >= -2, b <= 6) anchored at the
    water tile (16, 15)."""
    require(-2 <= a < b <= 6, 'Shore strip outside the donor coast', 'UNSUPPORTED_WATER')
    polys = []
    for name, poly in lib['shore_source']:
        part = clip_rect(poly, 0, 15 + a, 40, 15 + b)
        if part is not None and abs(area_xz(part)) > 1e-7:
            polys.extend((name, p - np.array([16, 0, 15, 0, 0, 0, 0, 0, 0, 0]) * 1) for p in native([part]))
    return Piece(polys, [(0, 0)], lib['shore_grad'])


def repeat_flags(lib):
    return {k: np.array(v['repeat'], dtype=bool) for k, v in lib['materials'].items()}


def flat_quad(lib, material, donor_y, rect, y):
    """A flat stock-mapped quad over a global tile rectangle at model height y."""
    first, affine, sign = lib['flats'][(material, float(donor_y))]
    x0, z0, x1, z1 = rect
    corners = [(x0, z0), (x1, z0), (x1, z1), (x0, z1)]
    quad = np.zeros((4, 10))
    quad[:, 0] = [c[0] for c in corners]
    quad[:, 2] = [c[1] for c in corners]
    if np.sign(area_xz(quad)) != sign:
        quad = quad[::-1].copy()
    quad[:, 1] = y
    uv = np.column_stack([quad[:, 0], quad[:, 2], np.ones(4)]) @ affine
    uv -= np.floor(uv.min(axis=0))
    quad[:, 3:5] = uv
    quad[:, 5:8] = first[5:8]
    quad[:, 8] = first[8]
    quad[:, 9] = first[9]
    return quad


# ---- feature geometry (global tiles) -----------------------------------------------------

def terrace_layout(spec):
    """Tile roles of a terrace: top, rim sides, corners, stair tiles and landings."""
    x, z, w, h = spec['x'], spec['z'], spec['width'], spec['height']
    top = {(xx, zz) for xx in range(x, x + w) for zz in range(z, z + h)}
    ring = {(xx, zz) for xx in range(x - 1, x + w + 1) for zz in range(z - 1, z + h + 1)} - top
    corners = {'north_west': (x - 1, z - 1), 'north_east': (x + w, z - 1),
               'south_west': (x - 1, z + h), 'south_east': (x + w, z + h)}
    stairs, landings = [], set()
    for access in spec['access']:
        side, off = access['side'], access['offset']
        if side in ('north', 'south'):
            row = z - 1 if side == 'north' else z + h
            tiles = [(x + off + i, row) for i in range(STAIR_WIDTH)]
            land = [(t[0], row + (-1 if side == 'north' else 1)) for t in tiles]
        else:
            col = x - 1 if side == 'west' else x + w
            tiles = [(col, z + off + i) for i in range(STAIR_WIDTH)]
            land = [(col + (-1 if side == 'west' else 1), t[1]) for t in tiles]
        stairs.append({'side': side, 'offset': off, 'tiles': tiles, 'landing': land})
        landings.update(land)
    return {'top': top, 'ring': ring, 'corners': corners, 'stairs': stairs, 'landings': landings,
            'footprint': top | ring}


def terrace_pieces(lib, spec, ground):
    layout = terrace_layout(spec)
    x, z, w, h = spec['x'], spec['z'], spec['width'], spec['height']
    dy = ground - DONOR_GROUND
    rep = repeat_flags(lib)
    stair_tiles = {t for s in layout['stairs'] for t in s['tiles']}
    placed, atomic = [], []
    sides = {'west': [(x - 1, zz) for zz in range(z, z + h)], 'east': [(x + w, zz) for zz in range(z, z + h)],
             'north': [(xx, z - 1) for xx in range(x, x + w)], 'south': [(xx, z + h) for xx in range(x, x + w)]}
    for side, tiles in sides.items():
        k = SIDE_TURNS[side]
        axis = turn(np.array([[0, 0, 1, 0, 0, 0, 0, 0, 0, 0.0]]), k)[0]
        for t in tiles:
            if t in stair_tiles:
                continue
            along = int(round(t[0] * axis[0] + t[1] * axis[2]))
            placed.extend(lib['wall'].place(k, t, along, dy, rep))
    for name, t in layout['corners'].items():
        placed.extend(lib['corner'].place(CORNER_TURNS[name], t, 0, dy, rep))
    for stair in layout['stairs']:
        tmin = (min(t[0] for t in stair['tiles']), min(t[1] for t in stair['tiles']))
        atomic.append(lib['stair'].place(STAIR_TURNS[stair['side']], tmin, 0, dy, rep))
    top = ('grass01gs', 32.0, (x, z, x + w, z + h), ground + RISE)
    return {'layout': layout, 'clipped': placed, 'atomic': atomic, 'flats': [top]}


# ---- shaped features (terrain v2: TERRAIN-01/02) -----------------------------------------------
# A shape is a union of rectangles [x, z, width, height] in global tiles. Terraces classify
# every rim tile as a straight side, an outer corner (the stock rounded corner) or an inner
# (concave) corner, built from the two stock straight walls mitred on the tile diagonal
# through the concave vertex. Water places the stock shore strip on every land-facing edge
# and mitres it at convex (end cap on the land tile) and concave (extension over the next
# water tile) corners. Shapes the stock pieces cannot express refuse with the tile named.

OPPOSITE = {'north': 'south', 'south': 'north', 'west': 'east', 'east': 'west'}
# Stock one-way ledge pairs (behavior = jump direction, collision bit set; stock R29 3b80/3980).
JUMPS = {'east': 0x38, 'west': 0x39, 'north': 0x3A, 'south': 0x3B}
# Rock Climb tiles (pret MetatileBehavior_IsRockClimbInDirection): facing north/south needs 0x4B,
# facing east/west 0x4C; stock climb tiles are blocked (flag 0x80) like Cherrygrove's (13, 6..7).
CLIMB_BEHAVIOR = {'north': 0x4B, 'south': 0x4B, 'east': 0x4C, 'west': 0x4C}
CLIMB_MATERIAL, CLIMB_SIZE = 'r_climb', (16, 16)
JUMP_DIRECTION = {v: OUTWARD[k] for k, v in JUMPS.items()}


def rect_tiles(rects):
    tiles = set()
    for x, z, w, h in rects:
        tiles |= {(xx, zz) for xx in range(x, x + w) for zz in range(z, z + h)}
    return tiles


def rectangles(tiles):
    """Row runs merged downward into rectangles (x0, z0, x1, z1) that exactly cover ``tiles``."""
    rows = {}
    for x, z in tiles:
        rows.setdefault(z, []).append(x)
    runs = []
    for z in sorted(rows):
        xs = sorted(rows[z])
        start = prev = xs[0]
        for x in xs[1:]:
            if x != prev + 1:
                runs.append((start, prev + 1, z))
                start = x
            prev = x
        runs.append((start, prev + 1, z))
    done, open_ = [], {}
    for x0, x1, z in runs:
        rect = open_.get((x0, x1))
        if rect is not None and rect[3] == z:
            rect[3] = z + 1
        else:
            if rect is not None:
                done.append(tuple(rect))
            open_[(x0, x1)] = [x0, z, x1, z + 1]
    done.extend(tuple(r) for r in open_.values())
    return sorted(done)


def _neighbourhood(tiles):
    return {(x + dx, z + dz) for x, z in tiles for dx in (-1, 0, 1) for dz in (-1, 0, 1)} - tiles


def classify_rim(top):
    """Role of every rim tile around a terrace top: ('side', facing), ('outer', corner) or
    ('inner', (facing_a, facing_b), vertex)."""
    roles = {}
    for t in sorted(_neighbourhood(top)):
        orth = [s for s, (dx, dz) in OUTWARD.items() if (t[0] + dx, t[1] + dz) in top]
        diag = [(dx, dz) for dx in (-1, 1) for dz in (-1, 1) if (t[0] + dx, t[1] + dz) in top]
        shares = lambda d: any((OUTWARD[s][0] and d[0] == OUTWARD[s][0]) or (OUTWARD[s][1] and d[1] == OUTWARD[s][1])
                               for s in orth)
        if len(orth) == 1 and all(shares(d) for d in diag):
            roles[t] = ('side', OPPOSITE[orth[0]])
        elif not orth and len(diag) == 1:
            dx, dz = diag[0]
            roles[t] = ('outer', ('south' if dz < 0 else 'north') + '_' + ('west' if dx > 0 else 'east'))
        elif len(orth) == 2 and set(orth) not in ({'north', 'south'}, {'east', 'west'}):
            a, b = orth
            d = (OUTWARD[a][0] + OUTWARD[b][0], OUTWARD[a][1] + OUTWARD[b][1])
            require(d in diag and all(shares(e) for e in diag),
                    f'Rim tile {t[0]},{t[1]} would join two separate top parts diagonally', 'UNSUPPORTED_TERRAIN')
            vertex = (t[0] + (1 if d[0] > 0 else 0), t[1] + (1 if d[1] > 0 else 0))
            roles[t] = ('inner', (OPPOSITE[a], OPPOSITE[b]), vertex)
        else:
            raise EditorError('UNSUPPORTED_TERRAIN', f'Rim tile {t[0]},{t[1]} sits in a one-tile notch or between '
                              'two top parts; widen the shape (stock pieces need straight sides and corners)')
    return roles


def _run(role_of, start, facing, length):
    """Tiles of a run along a rim side starting at ``start`` (the minimum x or z)."""
    along = (1, 0) if facing in ('north', 'south') else (0, 1)
    tiles = [(start[0] + along[0] * i, start[1] + along[1] * i) for i in range(length)]
    for t in tiles:
        require(role_of.get(t) == ('side', facing), f'Tile {t[0]},{t[1]} is not a straight {facing} rim tile',
                'UNSUPPORTED_ACCESS')
    return tiles


def shaped_terrace_layout(spec):
    top = rect_tiles(spec['rects'])
    roles = classify_rim(top)
    ring = set(roles)
    stairs, ledges, landings = [], [], set()
    for a in spec['access']:
        tiles = _run(roles, (a['x'], a['z']), a['side'], STAIR_WIDTH)
        n = OUTWARD[a['side']]
        ends = [(tiles[0][0] - (1 if n[1] else 0), tiles[0][1] - (1 if n[0] else 0)),
                (tiles[-1][0] + (1 if n[1] else 0), tiles[-1][1] + (1 if n[0] else 0))]
        for e in ends:
            require(roles.get(e, ('outer',))[0] in ('side', 'outer'),
                    f"The {a['side']} stair at {a['x']},{a['z']} ends next to an inner corner; move it", 'UNSUPPORTED_ACCESS')
        land = [(t[0] + n[0], t[1] + n[1]) for t in tiles]
        stairs.append({'side': a['side'], 'offset': None, 'tiles': tiles, 'landing': land})
        landings.update(land)
    for a in spec['ledges']:
        tiles = _run(roles, (a['x'], a['z']), a['side'], a['length'])
        n = OUTWARD[a['side']]
        land = [(t[0] + n[0], t[1] + n[1]) for t in tiles]
        ledges.append({'side': a['side'], 'tiles': tiles, 'landing': land})
        landings.update(land)
    climbs = []
    for a in spec.get('climbs', []):
        # Rock Climb (FIELD-03): one straight rim tile; the player climbs from the outside
        # landing over it onto the top, as over Cherrygrove's stock r_climb face.
        tiles = _run(roles, (a['x'], a['z']), a['side'], 1)
        n = OUTWARD[a['side']]
        land = [(t[0] + n[0], t[1] + n[1]) for t in tiles]
        climbs.append({'side': a['side'], 'tiles': tiles, 'landing': land,
                       'behavior': CLIMB_BEHAVIOR[a['side']]})
        landings.update(land)
    used = [t for s in stairs + ledges + climbs for t in s['tiles']]
    require(len(used) == len(set(used)), 'Stairs, ledges and climbs overlap', 'UNSUPPORTED_ACCESS')
    result = {'top': top, 'ring': ring, 'roles': roles, 'stairs': stairs, 'ledges': ledges, 'landings': landings,
              'footprint': top | ring, 'corners': {}}
    if climbs:
        result['climbs'] = climbs
    return result


def shaped_terrace_pieces(lib, spec, ground):
    layout = shaped_terrace_layout(spec)
    dy = ground - DONOR_GROUND
    rep = repeat_flags(lib)
    stair_tiles = {t for s in layout['stairs'] for t in s['tiles']}
    placed, atomic = [], []

    def along(t, facing):
        axis = turn(np.array([[0, 0, 1, 0, 0, 0, 0, 0, 0, 0.0]]), SIDE_TURNS[facing])[0]
        return int(round(t[0] * axis[0] + t[1] * axis[2]))

    climb_tiles = {t: c['side'] for c in layout.get('climbs', []) for t in c['tiles']}
    climb = []
    for t, role in sorted(layout['roles'].items()):
        if t in stair_tiles:
            continue
        if t in climb_tiles:
            # The stock wall geometry of this tile, faced with the stock r_climb texture (16 px:
            # one repeat across the tile and per tile of height, planar on the wall).
            for _, poly in lib['wall'].place(SIDE_TURNS[role[1]], t, along(t, role[1]), dy, rep):
                q = poly.copy()
                q[:, 3] = (q[:, 0] - t[0]) if climb_tiles[t] in ('north', 'south') else (q[:, 2] - t[1])
                q[:, 4] = (ground + RISE - q[:, 1]) / 16
                climb.append((CLIMB_MATERIAL, q))
            continue
        if role[0] == 'side':
            placed.extend(lib['wall'].place(SIDE_TURNS[role[1]], t, along(t, role[1]), dy, rep))
        elif role[0] == 'outer':
            placed.extend(lib['corner'].place(CORNER_TURNS[role[1]], t, 0, dy, rep))
        else:
            (fa, fb), (cx, cz) = role[1], role[2]
            for mine, other in ((fa, fb), (fb, fa)):
                n, m = OUTWARD[mine], OUTWARD[other]
                keep = lambda px, pz, n=n, m=m: (m[0] - n[0]) * (px - cx) + (m[1] - n[1]) * (pz - cz)
                for name, poly in lib['wall'].place(SIDE_TURNS[mine], t, along(t, mine), dy, rep):
                    poly = _keep(poly, keep)
                    if poly is not None and abs(area_xz(poly)) > 1e-7:
                        placed.extend((name, q) for q in native([poly]))
    for stair in layout['stairs']:
        tmin = (min(t[0] for t in stair['tiles']), min(t[1] for t in stair['tiles']))
        atomic.append(lib['stair'].place(STAIR_TURNS[stair['side']], tmin, 0, dy, rep))
    flats = [('grass01gs', 32.0, rect, ground + RISE) for rect in rectangles(layout['top'])]
    result = {'layout': layout, 'clipped': placed, 'atomic': atomic, 'flats': flats}
    if climb:
        result['climb'] = climb
    return result


def shaped_water_layout(spec):
    water = rect_tiles(spec['rects'])
    for t in sorted(water):
        require(any(all((t[0] + a + i, t[1] + b + j) in water for i in (0, 1) for j in (0, 1))
                    for a in (-1, 0) for b in (-1, 0)),
                f'Water tile {t[0]},{t[1]} is in a one-tile-wide channel; water is at least 2 tiles wide',
                'UNSUPPORTED_WATER')
    ring = _neighbourhood(water)
    circle = [(-1, -1), (0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0)]
    for t in sorted(ring):
        wet = [(t[0] + dx, t[1] + dz) in water for dx, dz in circle]
        runs = sum(1 for i in range(8) if wet[i] and not wet[i - 1])
        require(runs <= 1, f'Shore tile {t[0]},{t[1]} separates two parts of the water (one-tile land neck '
                'or diagonal touch); widen the land or join the water', 'UNSUPPORTED_WATER')
    return {'water': water, 'ring': ring, 'footprint': set(water)}


def shore_edges(water):
    """(tile, side) for every water tile edge facing land."""
    return [(t, s) for t in sorted(water) for s, n in OUTWARD.items() if (t[0] + n[0], t[1] + n[1]) not in water]


def shaped_water_pieces(lib, spec, ground):
    lay = shaped_water_layout(spec)
    water = lay['water']
    dy = ground - DONOR_GROUND
    rep = repeat_flags(lib)
    strip = shore_piece(lib, 0, 1)
    placed = []

    def along(t, side):
        axis = turn(np.array([[0, 0, 1, 0, 0, 0, 0, 0, 0, 0.0]]), SIDE_TURNS[side])[0]
        return int(round(t[0] * axis[0] + t[1] * axis[2]))

    def emit(tile, side, keeps):
        for name, poly in strip.place(SIDE_TURNS[side], tile, along(tile, side), dy, rep):
            for keep in keeps:
                poly = _keep(poly, keep)
                if poly is None:
                    break
            if poly is not None and abs(area_xz(poly)) > 1e-7:
                placed.extend((name, q) for q in native([poly]))

    for t, side in shore_edges(water):
        n = OUTWARD[side]
        keeps = []
        for a in ((n[1], n[0]), (-n[1], -n[0])):
            u = (t[0] + a[0], t[1] + a[1])
            c = (t[0] + (1 if n[0] > 0 or a[0] > 0 else 0), t[1] + (1 if n[1] > 0 or a[1] > 0 else 0))
            if u in water and (u[0] + n[0], u[1] + n[1]) not in water:
                continue                                        # straight shore continues
            m = a if u not in water else (-a[0], -a[1])        # convex: end cap; concave: extension
            sign = 1 if u not in water else -1
            keep = lambda px, pz, n=n, m=m, c=c, sign=sign: sign * ((n[0] - m[0]) * (px - c[0]) + (n[1] - m[1]) * (pz - c[1]))
            keeps.append(keep)
            emit(u, side, [keep])
        emit(t, side, keeps)
    flats = []
    for rect in rectangles(water):
        flats.append(('sea_on', 8.0, rect, ground - WATER_DROP))
        flats.append(('sea_un', 7.0, rect, ground - UNDER_DROP))
    return {'layout': lay, 'clipped': placed, 'atomic': [], 'flats': flats}


def water_layout(spec):
    x, z, w, h = spec['x'], spec['z'], spec['width'], spec['height']
    water = {(xx, zz) for xx in range(x, x + w) for zz in range(z, z + h)}
    ring = {(xx, zz) for xx in range(x - 1, x + w + 1) for zz in range(z - 1, z + h + 1)} - water
    return {'water': water, 'ring': ring, 'footprint': set(water)}


def water_pieces(lib, spec, ground):
    """A rectangular pond: the stock west-shore strip on all four sides, one piece per
    water tile plus an end cap beyond each corner, mitred on the corner diagonals."""
    x, z, w, h = spec['x'], spec['z'], spec['width'], spec['height']
    dy = ground - DONOR_GROUND
    rep = repeat_flags(lib)
    strip = shore_piece(lib, 0, 1)
    anchors = {'west': lambda i: (x, z + i), 'east': lambda i: (x + w - 1, z + i),
               'north': lambda i: (x + i, z), 'south': lambda i: (x + i, z + h - 1)}
    corners = {'west': (((x, z), 'north'), ((x, z + h), 'south')),
               'east': (((x + w, z), 'north'), ((x + w, z + h), 'south')),
               'north': (((x, z), 'west'), ((x + w, z), 'east')),
               'south': (((x, z + h), 'west'), ((x + w, z + h), 'east'))}
    placed = []
    for side, k in SIDE_TURNS.items():
        length = h if side in ('west', 'east') else w
        axis = turn(np.array([[0, 0, 1, 0, 0, 0, 0, 0, 0, 0.0]]), k)[0]
        n = OUTWARD[side]
        for i in range(-1, length + 1):
            target = anchors[side](i)
            along = int(round(target[0] * axis[0] + target[1] * axis[2]))
            for name, poly in strip.place(k, target, along, dy, rep):
                for (cx, cz), other in corners[side]:
                    m = OUTWARD[other]
                    dx, dz = n[0] - m[0], n[1] - m[1]
                    poly = _keep(poly, lambda px, pz, cx=cx, cz=cz, dx=dx, dz=dz: dx * (px - cx) + dz * (pz - cz))
                    if poly is None:
                        break
                if poly is not None and abs(area_xz(poly)) > 1e-7:
                    placed.extend((name, q) for q in native([poly]))
    flats = [('sea_on', 8.0, (x, z, x + w, z + h), ground - WATER_DROP),
             ('sea_un', 7.0, (x, z, x + w, z + h), ground - UNDER_DROP)]
    return {'layout': water_layout(spec), 'clipped': placed, 'atomic': [], 'flats': flats}


def _keep(poly, f):
    """Keep the part of a polygon where f(x, z) >= 0 (f affine)."""
    if poly is None:
        return None
    c = f(0.0, 0.0)
    a = f(1.0, 0.0) - c
    b = f(0.0, 1.0) - c
    kept, _ = split(poly, -a, -b, -c)
    return kept


# ---- model application --------------------------------------------------------------------

@functools.lru_cache(maxsize=16)
def _tileset_sizes(tileset):
    names, _ = nitro.texture_set(tileset)
    return {n: (t.width, t.height) for n, t in names.items()}


def apply_model(raw, tileset, origin, features, lib, palettes=None):
    """Clear each feature footprint in a cell's model and add its stock pieces."""
    if not features:
        return raw
    summary, bo, mo, model, shape, draws, by_shape, polys = surface_native._decode(raw, tileset)
    first = {sid: np.array(pp[0]) for sid, pp in polys.items() if pp}
    by_texture = {}
    for sid, p in by_shape.items():
        by_texture.setdefault(p.material['texture_name'], []).append(sid)
    changed = set()
    extra = {}
    animate = set()
    ox, oz = origin
    default_lib = lib
    for feature in features:
        pieces = feature['geometry']
        # Materials drawn on the model's area-animation slots (waterfall v2): they join only a
        # shape already on that named slot, never a static copy of the same texture.
        animated = set(pieces.get('animated', ()))
        animate |= animated
        ground = feature['ground']
        lib = feature.get('lib', default_lib)
        needed = feature.get('needed') or (TERRACE_MATERIALS if feature['kind'] == 'terrace' else WATER_MATERIALS)
        clearable = feature.get('clearable', CLEARABLE)
        levels = feature.get('levels', (ground - UNDERLAY, ground + 1))
        doubled = [m for m in needed if len(by_texture.get(m, [])) > 1]
        require(not doubled, f"Cell model has several {', '.join(doubled)} shapes", 'UNSUPPORTED_TERRAIN')
        for m in needed:
            ref = lib['materials'].get(m)
            if m in by_texture:
                mat = by_shape[by_texture[m][0]].material
                size = (mat['width'], mat['height'])
            else:
                # Absent from this cell: added as a new material bound to the area tileset's own
                # stock texture (a static material: area animation binds only stock slot indices).
                size = _tileset_sizes(tileset).get(m)
                require(size is not None, f'Cell model lacks the stock {m} material this family needs and the '
                        'area tileset has no such texture', 'UNSUPPORTED_TERRAIN')
            require(ref and size == (ref['width'], ref['height']),
                    f'Cell material {m} differs from the donor texture size', 'UNSUPPORTED_TERRAIN')
        for x0, z0, x1, z1 in feature['clear']:
            rect = [(x0 - ox) * 16 - 256, (z0 - oz) * 16 - 256, (x1 - ox) * 16 - 256, (z1 - oz) * 16 - 256]
            rect = [max(-256, min(256, v)) for v in rect]
            if rect[2] - rect[0] < EPS or rect[3] - rect[1] < EPS:
                continue
            for sid, p in by_shape.items():
                name = p.material['texture_name'] or ''
                kept = []
                for poly in polys[sid]:
                    box = surface_native._box(poly)
                    if surface_native._apart(box, rect):
                        kept.append(poly)
                        continue
                    inside = clip_rect(poly, *rect)
                    covered = abs(area_xz(inside)) if inside is not None else 0.0
                    if covered < 1e-4:
                        # Degenerate or edge-touching faces: only vertical faces inside refuse.
                        if (abs(area_xz(poly)) < 1e-6 and box[0] < rect[2] - 1e-3 and box[1] > rect[0] + 1e-3
                                and box[2] < rect[3] - 1e-3 and box[3] > rect[1] + 1e-3):
                            raise EditorError('UNSUPPORTED_TERRAIN', f'Raised or vertical {name} geometry inside the footprint')
                        kept.append(poly)
                        continue
                    flat = np.ptp(poly[:, 1]) < 1e-4
                    level = float(poly[0, 1])
                    # Flat stock ground, its one-unit decals and hidden underlays a few units
                    # below it (Canopy has grass01gs at 14 under ground 16) are replaced.
                    require(name in clearable and flat and levels[0] - 1e-3 <= level <= levels[1] + 1e-3,
                            f'The footprint covers {name or "untextured"} geometry at height {level:g} '
                            f'(only flat ground {ground:g} and its decals can be reshaped)', 'UNSUPPORTED_TERRAIN')
                    kept.extend(native(outside_rect(poly, *rect)))
                    changed.add(sid)
                polys[sid] = kept
        for x0, z0, x1, z1 in feature.get('guard', ()):
            # Bands the new geometry rests on (e.g. the pond bank lip) must hold nothing
            # higher than ground decals; they are checked, not cleared.
            rect = [(x0 - ox) * 16 - 256, (z0 - oz) * 16 - 256, (x1 - ox) * 16 - 256, (z1 - oz) * 16 - 256]
            rect = [max(-256, min(256, v)) for v in rect]
            if rect[2] - rect[0] < EPS or rect[3] - rect[1] < EPS:
                continue
            for sid, p in by_shape.items():
                for poly in polys[sid]:
                    if surface_native._apart(surface_native._box(poly), rect):
                        continue
                    inside = clip_rect(poly, *rect)
                    if inside is not None and abs(area_xz(inside)) > 1e-4:
                        require(float(np.max(inside[:, 1])) <= ground + 1 + 1e-3,
                                f"Raised {p.material['texture_name'] or 'untextured'} geometry borders the new edge",
                                'UNSUPPORTED_TERRAIN')
        added = list(pieces['clipped'])
        for material, donor_y, (x0, z0, x1, z1), y in pieces['flats']:
            cx0, cz0, cx1, cz1 = max(x0, ox), max(z0, oz), min(x1, ox + 32), min(z1, oz + 32)
            if cx1 - cx0 > EPS and cz1 - cz0 > EPS:
                added.append((material, flat_quad(lib, material, donor_y, (cx0, cz0, cx1, cz1), y)))
        def put(material, poly):
            target = by_texture.get(material)
            if target and material in animated and by_shape[target[0]].material['name'] != material:
                target = None
            if target:
                polys[target[0]].append(to_model(poly, origin))
                changed.add(target[0])
            else:
                extra.setdefault(material, []).append(to_model(poly, origin))

        for material, poly in added:
            part = clip_rect(poly, ox, oz, ox + 32, oz + 32)
            if part is None or abs(area_xz(part)) < 1e-7:
                continue
            for q in native([part]):
                put(material, q)
        for group in pieces['atomic']:
            cx = np.mean([p[:, 0].mean() for _, p in group])
            cz = np.mean([p[:, 2].mean() for _, p in group])
            if not (ox <= cx < ox + 32 and oz <= cz < oz + 32):
                continue
            for material, poly in group:
                put(material, poly)
        for material, poly in pieces.get('climb', ()):
            part = clip_rect(poly, ox, oz, ox + 32, oz + 32)
            if part is None:
                continue
            for q in native([part]):
                put(material, q)
    if not changed and not extra:
        return raw
    for sid in changed:
        if not polys[sid] and sid in first:
            # Every face of this material lies in the footprint (e.g. a lawn's only flower decal).
            # The model keeps each material's shape and draw, so it keeps one zero-area triangle:
            # nothing is drawn, every other shape and the draw list stay intact.
            polys[sid] = [np.repeat(first[sid][:1], 3, axis=0)]
    if extra:
        # A cell without the stock face gets it as a new material bound to the stock texture.
        names, pal_names = nitro.texture_set(tileset)
        sizes = _tileset_sizes(tileset)
        new = {}
        for material, pp in extra.items():
            require(material in names, f'The area tileset has no {material} texture', 'UNSUPPORTED_TERRAIN')
            # The palette stock models of this tileset bind (irregular names, e.g. sea_on -> sea_f02_pl).
            palette = (palettes or {}).get(material)
            if palette is None:
                palette = next((p for p in (material + '_pl', material + 'pl', material) if p in pal_names), None)
            require(palette in pal_names, f'The area tileset has no palette for {material}', 'UNSUPPORTED_TERRAIN')
            new[material] = (sizes[material], pp, palette, True) if material in animate else (sizes[material], pp, palette)
        return surface_native._rewrite(raw, tileset, summary, bo, mo, model, shape, draws, by_shape, polys,
                                       changed, new)
    emptied = sorted(by_shape[sid].material['texture_name'] or 'untextured' for sid in changed if not polys[sid])
    require(not emptied, f"The footprint would remove every {', '.join(emptied)} face in this cell (the model format "
            'keeps each material shape); leave some of it outside the feature', 'UNSUPPORTED_TERRAIN')
    return surface_native._rewrite(raw, tileset, summary, bo, mo, model, shape, draws, by_shape, polys, changed)


# ---- BDHC -----------------------------------------------------------------------------------

FORMATS = ("<2i", "<3i", "<i", "<4H", "<i2H", "<H")


def parse_bdhc(data):
    require(data[:4] == b'BDHC', 'Terrain height header absent', 'UNSUPPORTED_TERRAIN')
    counts = struct.unpack_from('<6H', data, 4)
    cursor, arrays = 16, []
    for count, fmt in zip(counts, FORMATS):
        size = struct.calcsize(fmt)
        arrays.append(list(struct.iter_unpack(fmt, data[cursor:cursor + count * size])))
        cursor += count * size
    require(cursor == len(data), 'Unexpected BDHC trailing data', 'UNSUPPORTED_TERRAIN')
    points, normals, constants, plates, strips, indices = arrays
    result = []
    for a, b, n, c in plates:
        (x0, z0), (x1, z1) = points[a], points[b]
        result.append({'rect': (x0, z0, x1, z1), 'normal': normals[n], 'd': constants[c][0]})
    return {'plates': result, 'strips': strips, 'indices': [i[0] for i in indices]}


def build_bdhc(plates):
    """Stock layout: two points per plate, deduplicated normals/constants in first-use order,
    strips at the sorted plate z-maxima listing plates with z0 <= bound and z1 > previous bound."""
    points, normals, constants, records = [], [], [], []
    for plate in plates:
        x0, z0, x1, z1 = plate['rect']
        points.extend([(x0, z0), (x1, z1)])
        if plate['normal'] not in normals:
            normals.append(plate['normal'])
        if plate['d'] not in constants:
            constants.append(plate['d'])
        records.append((len(points) - 2, len(points) - 1, normals.index(plate['normal']), constants.index(plate['d'])))
    bounds = sorted({p['rect'][3] for p in plates})
    strips, indices, previous = [], [], None
    for bound in bounds:
        members = [i for i, p in enumerate(plates) if p['rect'][1] <= bound and (previous is None or p['rect'][3] > previous)]
        strips.append((bound, len(members), len(indices)))
        indices.extend(members)
        previous = bound
    counts = (len(points), len(normals), len(constants), len(records), len(strips), len(indices))
    require(max(counts) <= 0xFFFF, 'Height table exceeds the BDHC format', 'RESOURCE_CAPACITY')
    out = bytearray(b'BDHC' + struct.pack('<6H', *counts))
    for p in points:
        out += struct.pack('<2i', *p)
    for n in normals:
        out += struct.pack('<3i', *n)
    for c in constants:
        out += struct.pack('<i', c)
    for r in records:
        out += struct.pack('<4H', *r)
    for s in strips:
        out += struct.pack('<i2H', *s)
    for i in indices:
        out += struct.pack('<H', i)
    # ov01_021FAC44 allocates a 0x9000-byte heap buffer per loaded map for the height table.
    require(len(out) <= BDHC_BUFFER, f'Height table needs {len(out)} bytes; the runtime buffer is {BDHC_BUFFER}',
            'RESOURCE_CAPACITY')
    return bytes(out)


def local_fx(v, origin_axis):
    return int(round((v - origin_axis - 16) * FX))


def horizontal(height_units):
    return {'normal': (0, 4096, 0), 'd': -int(round(height_units * FX))}


def slope(side, edge_local, top_units):
    """45-degree stair plane rising one tile toward the terrace; exact-unit-normal constant
    like stock (e.g. Canopy donor south stair d = 139022)."""
    nx, nz = {'south': (0, 1), 'north': (0, -1), 'east': (1, 0), 'west': (-1, 0)}[side]
    normal = (nx * 2896, 2896, nz * 2896)
    # A point on the plane: the inner (terrace) edge at top height.
    ex, ez = edge_local
    d = -(nx * HALF * ex + HALF * top_units + nz * HALF * ez)
    return {'normal': normal, 'd': int(round(d * FX))}


def subtract(plates, rect):
    """Remove a rectangle from every horizontal plate (fragments keep normal and constant)."""
    x0, z0, x1, z1 = rect
    out = []
    for p in plates:
        a0, b0, a1, b1 = p['rect']
        if a1 <= x0 or a0 >= x1 or b1 <= z0 or b0 >= z1:
            out.append(p)
            continue
        require(p['normal'] == (0, 4096, 0), 'The footprint crosses a sloped height plate', 'UNSUPPORTED_TERRAIN')
        for r in ((a0, b0, a1, z0), (a0, z1, a1, b1), (a0, max(b0, z0), x0, min(b1, z1)), (x1, max(b0, z0), a1, min(b1, z1))):
            if r[2] > r[0] and r[3] > r[1]:
                out.append({**p, 'rect': r})
    return out


def height_at(bdhc, x, z, current=None, mode=0):
    """Python mirror of ov01_021FAE50 (closed rectangles, strip lookup, nearest height)."""
    X, Z = int(round(x * FX)), int(round(z * FX))
    strips = bdhc['strips']
    if not strips:
        return None
    # ov01_021FADEC: first strip whose bound exceeds z (binary search).
    index = next((i for i, s in enumerate(strips) if s[0] > Z), len(strips) - 1)
    bound, count, start = strips[index]
    found = []
    for i in bdhc['indices'][start:start + count]:
        p = bdhc['plates'][i]
        a0, b0, a1, b1 = p['rect']
        if min(a0, a1) <= X <= max(a0, a1) and min(b0, b1) <= Z <= max(b0, b1):
            nx, ny, nz = p['normal']
            y = -(((nx * X + 0x800) >> 12) + ((nz * Z + 0x800) >> 12) + p['d']) * 4096 / ny
            found.append(y / FX)
            if len(found) >= 10:
                break
    if not found:
        return None
    if len(found) == 1 or current is None:
        return found[0] if mode == 0 else (max(found) if mode == 1 else min(found))
    return min(found, key=lambda v: abs(v - current))


# ---- waterfall (FIELD-01) --------------------------------------------------------------------
# The stock Waterfall task (overlay 1 0x021F29E4 up / 0x021F2BC8 down) moves a surfing player
# exactly two tiles north (up) or south (down) onto the water plate there, from a facing tile of
# behavior 0x13; every stock waterfall is one row of 0x13 between a lower and an upper sea.
# Here: an upper pool cut into a one-tile terrace (stock terrace pieces around a one-tile land
# margin), a fall row on the terrace's south rim, and a lower pool below it. Both pools use the
# stock shore strips and sea surfaces; the fall is the pool's own animated sea surface (area
# animation track ``sea_on``) continued down a slope across the fall row.

FALL_WIDTH, FALL_POOL = (2, 6), (2, 6)
FALL_PAIR = bytes((0x13, 0x00))
# Waterfall geometry version 2 (R101-WATERFALL). v1's fall row was walkable (collision 00) on a
# one-level slope, so a surfing player could simply swim up it: no prompt, a two-step climb. v2
# blocks the row like stock D38 (13 80): moving north bumps, A runs std_field_waterfall's prompt,
# the stock task climbs; moving south onto it still starts the stock descent (ov01_021F24F4
# checks the facing behavior before collision). v1 water was added as new static materials; v2
# draws the pools on the model's area-animation slots (sea_on/sea_un) so they move like stock
# sea, the fall as a chute on the river_r slot (the area track scrolls it along S only: the
# texture is the stock waterfall cascade ``wfall`` transposed, S mapped down the face) following
# the cliff's own profile, walls closing both sides of the chute, and foam on the river slot
# (stock ``wfall_c.1``, drifting downstream) at its foot. Recorded v1 requests replay unchanged.
FALL_VERSION = 2
FALL_PAIR_V2 = bytes((0x13, 0x80))
CHUTE, FOAM = 'river_r', 'river'
FALL_ANIMATED = ('sea_on', 'sea_un', CHUTE, FOAM)
CHUTE_TEXELS_PER_UNIT = 2.0        # 64-texel cascade over 32 units of face: ~10.7 units/s downward
FOAM_DEPTH, FOAM_LIFT, FOAM_START = 0.75, 0.25, 0.125
UNDER_FOOT = 0.5                   # lower water continued under the chute's foot (tiles)


def fall_pair(spec):
    return FALL_PAIR_V2 if spec.get('version', 1) >= 2 else FALL_PAIR


def waterfall_layout(spec):
    x, z, w, hu, hl = spec['x'], spec['z'], spec['width'], spec['upper'], spec['lower']
    upper = {(x + i, z + j) for i in range(w) for j in range(hu)}
    top = {(x + i, z + j) for i in range(-1, w + 1) for j in range(-1, hu)}
    roles = classify_rim(top)
    fall = [(x + i, z + hu) for i in range(w)]
    for t in fall:
        require(roles.get(t) == ('side', 'south'), f'Fall tile {t[0]},{t[1]} is not on the straight south rim',
                'UNSUPPORTED_TERRAIN')
    lower = {(x + i, z + hu + 1 + j) for i in range(w) for j in range(hl)}
    rim = set(roles)
    ring = _neighbourhood(lower) - rim - lower
    return {'top': top, 'upper': upper, 'fall': fall, 'lower': lower, 'roles': roles, 'rim': rim,
            'ring': ring, 'footprint': top | rim | lower, 'water': upper | lower, 'landings': set()}


def _shores(lib, water, emit_tiles, ground):
    """Stock shore strips on the land edges of ``emit_tiles``; ``water`` decides which edges are
    land and where straight shores continue (a fall row counts as water: no shore, no cap)."""
    dy = ground - DONOR_GROUND
    rep = repeat_flags(lib)
    strip = shore_piece(lib, 0, 1)
    placed = []

    def along(t, side):
        axis = turn(np.array([[0, 0, 1, 0, 0, 0, 0, 0, 0, 0.0]]), SIDE_TURNS[side])[0]
        return int(round(t[0] * axis[0] + t[1] * axis[2]))

    def emit(tile, side, keeps):
        for name, poly in strip.place(SIDE_TURNS[side], tile, along(tile, side), dy, rep):
            for keep in keeps:
                poly = _keep(poly, keep)
                if poly is None:
                    break
            if poly is not None and abs(area_xz(poly)) > 1e-7:
                placed.extend((name, q) for q in native([poly]))

    for t, side in shore_edges(water):
        if t not in emit_tiles:
            continue
        n = OUTWARD[side]
        keeps = []
        for a in ((n[1], n[0]), (-n[1], -n[0])):
            u = (t[0] + a[0], t[1] + a[1])
            c = (t[0] + (1 if n[0] > 0 or a[0] > 0 else 0), t[1] + (1 if n[1] > 0 or a[1] > 0 else 0))
            if u in water and (u[0] + n[0], u[1] + n[1]) not in water:
                continue
            m = a if u not in water else (-a[0], -a[1])
            sign = 1 if u not in water else -1
            keep = lambda px, pz, n=n, m=m, c=c, sign=sign: sign * ((n[0] - m[0]) * (px - c[0]) + (n[1] - m[1]) * (pz - c[1]))
            keeps.append(keep)
            emit(u, side, [keep])
        emit(t, side, keeps)
    return placed


def waterfall_pieces(lib, spec, ground):
    lay = waterfall_layout(spec)
    top_y = ground + RISE
    fall = set(lay['fall'])
    terrace = shaped_terrace_pieces(lib, {'rects': [[spec['x'] - 1, spec['z'] - 1, spec['width'] + 2,
                                                      spec['upper'] + 1]], 'access': [], 'ledges': []}, ground)
    inside = lambda poly, tiles: (int(np.floor(poly[:, 0].mean())), int(np.floor(poly[:, 2].mean()))) in tiles
    placed = [(n, p) for n, p in terrace['clipped'] if not inside(p, fall)]
    placed += _shores(lib, lay['upper'] | fall, lay['upper'], top_y)
    placed += _shores(lib, lay['lower'] | fall, lay['lower'], ground)
    flats = [('grass01gs', 32.0, rect, top_y) for rect in rectangles(lay['top'] - lay['upper'])]
    for rect in rectangles(lay['upper']):
        flats.append(('sea_on', 8.0, rect, top_y - WATER_DROP))
        flats.append(('sea_un', 7.0, rect, top_y - UNDER_DROP))
    for rect in rectangles(lay['lower']):
        flats.append(('sea_on', 8.0, rect, ground - WATER_DROP))
        flats.append(('sea_un', 7.0, rect, ground - UNDER_DROP))
    if spec.get('version', 1) >= 2:
        # The lower water runs on under the chute's foot, so no view ray under its lip meets the void.
        row, x0, x1 = lay['fall'][0][1], lay['fall'][0][0], lay['fall'][-1][0] + 1
        flats.append(('sea_on', 8.0, (x0, row + 1 - UNDER_FOOT, x1, row + 1), ground - WATER_DROP))
        flats.append(('sea_un', 7.0, (x0, row + 1 - UNDER_FOOT, x1, row + 1), ground - UNDER_DROP))
        return {'layout': lay, 'clipped': placed + _chute(spec, lay, terrace['clipped'], top_y, ground),
                'atomic': [], 'flats': flats, 'animated': FALL_ANIMATED}
    # The fall: the sea surface continued from the upper water down to the lower water across
    # each fall tile (stock-mapped like the pools, so the texture and its animation run on).
    falls = []
    for fx, fz in lay['fall']:
        # The translucent surface (sea_on) over its opaque underlay (sea_un), as in the pools.
        for material, donor_y, drop in (('sea_on', 8.0, WATER_DROP), ('sea_un', 7.0, UNDER_DROP)):
            quad = flat_quad(lib, material, donor_y, (fx, fz, fx + 1, fz + 1), 0.0)
            quad[:, 1] = np.where(quad[:, 2] <= fz + 1e-6, top_y - drop, ground - drop)
            quad[:, 8] = pack_normal(0, round(511 * HALF), round(511 * HALF))
            falls.append((material, quad))
    return {'layout': lay, 'clipped': placed + falls, 'atomic': [], 'flats': flats}


def _cliff_profile(pieces, boundary, row):
    """(z, y) knots of the terrace rim's face where it meets the plane x = ``boundary`` in the fall
    row: the wall faces of the rim tile beside the chute, top first."""
    knots = set()
    for name, poly in pieces:
        if name != 'wall01_g':
            continue
        zs = poly[:, 2]
        if zs.min() < row - 1e-6 or zs.max() > row + 1 + 1e-6:
            continue
        for v in poly:
            if abs(v[0] - boundary) < 1e-6:
                knots.add((round(float(v[2]), 4), round(float(v[1]), 4)))
    require(len(knots) >= 2, 'The waterfall rim has no cliff face beside the fall row', 'UNSUPPORTED_TERRAIN')
    by_z = {}
    for z, y in knots:
        by_z[z] = max(y, by_z.get(z, y))
    return sorted(by_z.items())


def _chute(spec, lay, terrace, top_y, ground):
    """v2 fall faces: the chute on the river_r slot following the cliff profile at water height,
    side walls between cliff and chute, and foam on the river slot at the chute's foot."""
    fall = lay['fall']
    x0, x1, row = fall[0][0], fall[-1][0] + 1, fall[0][1]
    west = _cliff_profile(terrace, x0, row)
    east = _cliff_profile(terrace, x1, row)
    require([z for z, _ in west] == [z for z, _ in east], 'The waterfall rims have different profiles',
            'UNSUPPORTED_TERRAIN')
    top, bottom = top_y - WATER_DROP, ground - WATER_DROP
    cliff_top, cliff_bottom = west[0][1], west[-1][1]
    # The chute keeps the cliff's knots, scaled into the water band (lip to lower surface).
    knots = [(z, bottom + (y - cliff_bottom) * (top - bottom) / (cliff_top - cliff_bottom)) for z, y in west]
    colour = np.full(3, 0.806)
    out = []
    arc = [0.0]
    for (za, ya), (zb, yb) in zip(knots, knots[1:]):
        arc.append(arc[-1] + math.hypot((zb - za) * RISE, yb - ya))
    for i, ((za, ya), (zb, yb)) in enumerate(zip(knots, knots[1:])):
        dz, dy = (zb - za) * RISE, yb - ya
        length = math.hypot(dz, dy) or 1.0
        # The face normal points toward the camera side (south, up): (0, dz, -dy) normalised.
        n = np.array([0.0, dz, -dy]) / length
        normal = pack_normal(0, round(511 * n[1]), round(511 * n[2]))
        for xa in range(x0, x1):
            quad = np.zeros((4, 10))
            quad[:, 0] = [xa, xa + 1, xa + 1, xa]
            quad[:, 1] = [ya, ya, yb, yb]
            quad[:, 2] = [za, za, zb, zb]
            # S runs down the face (the area track scrolls S), T across; texture units of 64 texels.
            s0, s1 = arc[i] * CHUTE_TEXELS_PER_UNIT / 64, arc[i + 1] * CHUTE_TEXELS_PER_UNIT / 64
            t0, t1 = (xa - x0) * RISE * CHUTE_TEXELS_PER_UNIT / 64, (xa + 1 - x0) * RISE * CHUTE_TEXELS_PER_UNIT / 64
            quad[:, 3] = [s0, s0, s1, s1]
            quad[:, 4] = [t0, t1, t1, t0]
            quad[:, 5:8] = colour
            quad[:, 8] = normal
            quad[:, 9] = 33.0
            out.append((CHUTE, _facing(quad, n)))
    # Side walls: the rim face texture between the cliff profile and the chute, facing the fall.
    for boundary, profile, facing in ((x0, west, 1), (x1, east, -1)):
        for (za, ca), (zb, cb), (_, wa), (_, wb) in zip(profile, profile[1:], knots, knots[1:]):
            quad = np.zeros((4, 10))
            quad[:, 0] = boundary
            quad[:, 1] = [ca, cb, wb, wa]
            quad[:, 2] = [za, zb, zb, za]
            quad[:, 3] = [(za - row) / 2, (zb - row) / 2, (zb - row) / 2, (za - row) / 2]
            quad[:, 4] = [(cliff_top - ca) / 32, (cliff_top - cb) / 32, (cliff_top - wb) / 32, (cliff_top - wa) / 32]
            quad[:, 5:8] = colour
            quad[:, 8] = pack_normal(511 * facing, 0, 0)
            quad[:, 9] = 33.0
            out.append(('wall01_g', _facing(quad, (facing, 0, 0))))
    # Foam: a band just above the lower water at the chute's foot; T runs downstream (+z).
    z0, z1, y = row + 1 - FOAM_START, row + 1 - FOAM_START + FOAM_DEPTH, bottom + FOAM_LIFT
    for xa in range(x0, x1):
        quad = np.zeros((4, 10))
        quad[:, 0] = [xa, xa + 1, xa + 1, xa]
        quad[:, 1] = y
        quad[:, 2] = [z0, z0, z1, z1]
        quad[:, 3] = [0, 1, 1, 0]
        quad[:, 4] = [0, 0, FOAM_DEPTH, FOAM_DEPTH]
        quad[:, 5:8] = colour
        quad[:, 8] = pack_normal(0, 511, 0)
        quad[:, 9] = 33.0
        out.append((FOAM, _facing(quad, (0, 1, 0))))
    return out


def _facing(poly, outward):
    """``poly`` wound as stock front faces (Newell normal, tiles scaled to model units, toward
    ``outward``)."""
    v = np.column_stack([poly[:, 0] * RISE, poly[:, 1], poly[:, 2] * RISE])
    n = np.zeros(3)
    for a, b in zip(v, np.roll(v, -1, axis=0)):
        n += ((a[1] - b[1]) * (a[2] + b[2]), (a[2] - b[2]) * (a[0] + b[0]), (a[0] - b[0]) * (a[1] + b[1]))
    return poly if float(n @ np.asarray(outward, float)) >= 0 else poly[::-1].copy()
