"""Cave family ``cave-d41-v1`` (TERRAIN-03): carved rooms, corridors, walls and exits.

Every piece is cut from the pinned stock cave room D41R0104 (map member 582, stock header
461 matrix 268 cell 0,0, map tileset 80) and placed by whole-tile translation only: the
walls use direction-specific materials (dwall_n/s/e/w and the dwall_wn/ne/es/sw corner
blocks), so nothing is rotated. Measured donor profile (tile units, model y):

* floor ``droad01`` at y 16 (height 1.0), permission ``08 0a`` (TILE_BEHAVIOR_CAVE_FLOOR,
  encounter flag; footstep 10) or ``00 0a`` for encounter-free floor;
* a wall band two tiles deep in two stepped tiers (y 16..32 and 32..48), blocked ``00 80``,
  then a one-tile rim ``dcliff01_r`` flat at y 48; beyond it the stock cave shows void;
* convex floor corners: the donor's 3x3 corner blocks (band + rim);
* concave floor corners: the two straight bands mitred on the diagonal through the concave
  vertex (the stock room has none; the same technique as terrain v2 inner corners);
* exit: the donor's south-wall hole (``chole_in`` with chamfered wall ends), three columns
  wide, with the donor floor only under the hole row (hidden by the wall and rim beyond): floor tile ``6f 0a`` (TILE_BEHAVIOR_WARP_SOUTH, the warp tile) and ``65 80`` inside the
  hole, exactly as the donor's own exit at (5, 8).

UVs: each material is shifted by its linear donor phase per tile (gradient along x and z), so
any translation reproduces the donor's own texture field and runs, corners and exits stay
continuous. The stock darker floor border (``droad01_r``) is not reproduced; floors are plain
``droad01``. Pure geometry; terrain_authoring plans and Project writes.
"""
import functools

import numpy as np

from . import nitro, mapscene
from .formats import digest, map_data, require
from . import terrain_geometry as tg

FAMILY = 'cave-d41-v1'
DONOR = {'member': 582, 'header': 461, 'cell': [0, 0]}
DONOR_SHA256 = '3227ec891ae0cb4f57d856f66d481b888f94788e03eb4cdd45ba467feff1408a'
FLOOR_Y = 16
BAND, RIM = 2, 1
ZONE = BAND + RIM
MATERIALS = ('droad01', 'dwall_n', 'dwall_s', 'dwall_e', 'dwall_w', 'dwall_wn', 'dwall_ne', 'dwall_es', 'dwall_sw',
             'dcliff01_r', 'chole_in')
# Flat rock top the stock room leaves around itself (y 48): replaced where a room is carved.
CLEARABLE = {'d_ro', 'dcliff01_r'}
PAIRS = {'floor': bytes((0x08, 0x0A)), 'quiet_floor': bytes((0x00, 0x0A)), 'wall': bytes((0x00, 0x80)),
         'exit': bytes((0x6F, 0x0A)), 'hole': bytes((0x65, 0x80))}
OUT = tg.OUTWARD
# Donor anchors: the floor tile each piece belongs to.
STRAIGHT = {'north': ((4, 2), (4, -1, 5, 2)), 'south': ((7, 8), (7, 9, 8, 12)),
            'west': ((2, 5), (-1, 5, 2, 6)), 'east': ((8, 5), (9, 5, 12, 6))}
CORNERS = {('north', 'west'): ((2, 2), (-1, -1, 2, 2)), ('north', 'east'): ((8, 2), (9, -1, 12, 2)),
           ('south', 'east'): ((8, 8), (9, 9, 12, 12)), ('south', 'west'): ((2, 8), (-1, 9, 2, 12))}
EXIT_ANCHOR = (5, 8)
EXIT_WALLS = (4, 8.7, 7, 12)
# Only the floor under the hole row: in the donor the strip continues to z 12 beneath flat rock
# (d_ro) that a carved room does not have, so the rest hung in the void below the rim (R82-CAVE-01).
EXIT_FLOOR = (4, 9, 7, 12)
# Version 2 keeps the donor floor only under the hole row. In the donor the strip continues to
# z 12 beneath flat rock (d_ro) that a carved room does not have, so version 1 left it hanging in
# the void below the rim as isolated floor tiles (R82-CAVE-01, native screenshot 7).
EXIT_FLOOR_V2 = (4, 9, 7, 10)


def donor_context(project):
    return project.context(header=DONOR['header'], cell=list(DONOR['cell']))


def _grads(polys, axis):
    """Per-material UV phase per tile along ``axis`` from donor faces (0 where undefined)."""
    out = {}
    for name, poly in polys:
        pts = poly[:, [0, 2]]
        spread = np.ptp(pts[:, 0 if axis == 0 else 1])
        if spread < 0.5:
            continue
        other = 1 if axis == 0 else 0
        try:
            a = np.linalg.lstsq(np.column_stack([pts[:, 0], pts[:, 1], np.ones(len(pts))]), poly[:, 3:5], rcond=None)[0]
        except np.linalg.LinAlgError:
            continue
        g = np.round(a[0 if axis == 0 else 1] * 64) / 64
        out.setdefault(name, []).append(g)
    return {k: np.median(np.array(v), axis=0) for k, v in out.items()}


@functools.lru_cache(maxsize=2)
def _library(raw, tileset):
    require(digest(raw) == DONOR_SHA256, 'The pinned cave donor map differs; this build is not qualified',
            'UNQUALIFIED_DONOR')
    _, prims = nitro.decode_model(map_data(raw)[2], tileset=tileset, render=False)
    mats = {p.material['texture_name']: p.material for p in prims}
    walls = set(MATERIALS) - {'droad01', 'chole_in'}
    cut = lambda box, names, centroid=False: tg._cut(prims, names, box, centroid=centroid)
    pieces = {}
    for side, (anchor, box) in STRAIGHT.items():
        pieces[side] = (anchor, cut(box, walls))
    for key, (anchor, box) in CORNERS.items():
        pieces[key] = (anchor, cut(box, walls))
    walls_only = cut(EXIT_WALLS, {'dwall_s', 'dcliff01_r', 'chole_in'})
    pieces['exit'] = (EXIT_ANCHOR, walls_only + cut(EXIT_FLOOR, {'droad01'}))
    pieces['exit_v2'] = (EXIT_ANCHOR, walls_only + cut(EXIT_FLOOR_V2, {'droad01'}))
    gx, gz = {}, {}
    for strip, axis in (((2, -1, 9, 2), 0), ((2, 9, 9, 12), 0), ((-1, 2, 2, 9), 2), ((9, 2, 12, 9), 2)):
        for name, g in _grads(cut(strip, walls | {'droad01'}), axis).items():
            (gx if axis == 0 else gz).setdefault(name, g)
    for name, g in _grads(cut((3, 3, 9, 9), {'droad01'}), 0).items():
        gx.setdefault(name, g)
    for name, g in _grads(cut((3, 3, 9, 9), {'droad01'}), 2).items():
        gz.setdefault(name, g)
    flats = {}
    for prim in prims:
        if prim.material['texture_name'] != 'droad01':
            continue
        for poly in tg.rows(prim):
            poly = tg.to_tiles(poly)
            if np.ptp(poly[:, 1]) < 1e-4 and abs(tg.area_xz(poly)) > 0.5:
                flats.setdefault(('droad01', float(FLOOR_Y)), (poly[0].copy(), tg._affine(poly), np.sign(tg.area_xz(poly))))
    require(all(pieces[k][1] for k in pieces) and ('droad01', float(FLOOR_Y)) in flats,
            'The cave donor lacks a required stock piece', 'UNQUALIFIED_DONOR')
    return {'pieces': pieces, 'gx': gx, 'gz': gz, 'flats': flats,
            'materials': {k: {f: v[f] for f in ('width', 'height', 'repeat')} for k, v in mats.items()}}


def library(project):
    ctx = donor_context(project)
    require(ctx['map_member'] == DONOR['member'], 'Cave donor context moved', 'UNQUALIFIED_DONOR')
    _, blobs = mapscene.tilesets(project, ctx)
    return _library(project.member_raw(ctx['map_member']), blobs['map_tileset'])


def _place(lib, key, target, dy=0.0):
    """Translate a donor piece so its anchor floor tile lands on ``target``; shift UVs by the
    donor's own per-tile phase so the texture field continues."""
    anchor, polys = lib['pieces'][key]
    dx, dz = target[0] - anchor[0], target[1] - anchor[1]
    rep = {k: np.array(v['repeat'], dtype=bool) for k, v in lib['materials'].items()}
    out = []
    for name, poly in polys:
        p = poly.copy()
        p[:, 0] += dx
        p[:, 2] += dz
        p[:, 1] += dy
        shift = lib['gx'].get(name, np.zeros(2)) * dx + lib['gz'].get(name, np.zeros(2)) * dz
        shift = np.where(rep.get(name, np.array([True, True])), shift - np.floor(shift), shift)
        p[:, 3:5] += shift
        out.append((name, p))
    return out


# ---- layout ----------------------------------------------------------------------------------

def layout(spec):
    """Floor, wall zone and piece placements of a carved cave feature; refuses shapes the
    stock pieces cannot express (parts closer than their walls allow, one-tile floors)."""
    floor = tg.rect_tiles(spec['rects'])
    for t in sorted(floor):
        require(any(all((t[0] + a + i, t[1] + b + j) in floor for i in (0, 1) for j in (0, 1))
                    for a in (-1, 0) for b in (-1, 0)),
                f'Floor tile {t[0]},{t[1]} is in a one-tile-wide passage; cave floors are at least 2 tiles wide',
                'UNSUPPORTED_TERRAIN')
    claims = {}
    straights, corners, inner = [], [], {}

    def claim(tile, tag):
        claims.setdefault(tile, []).append(tag)

    for t in sorted(floor):
        for side, n in OUT.items():
            if (t[0] + n[0], t[1] + n[1]) in floor:
                continue
            straights.append((t, side))
            for k in range(1, ZONE + 1):
                claim((t[0] + n[0] * k, t[1] + n[1] * k), ('band', side, t))
    for t in sorted(floor):
        for (a, b) in CORNERS:
            na, nb = OUT[a], OUT[b]
            if (t[0] + na[0], t[1] + na[1]) in floor or (t[0] + nb[0], t[1] + nb[1]) in floor:
                continue
            if (t[0] + na[0] + nb[0], t[1] + na[1] + nb[1]) in floor:
                continue
            corners.append((t, (a, b)))
            for i in range(1, ZONE + 1):
                for j in range(1, ZONE + 1):
                    claim((t[0] + na[0] * i + nb[0] * j, t[1] + na[1] * i + nb[1] * j), ('corner', (a, b), t))
    zone = set(claims)
    require(not zone & floor, 'Floor parts are too close: walls need three tiles of rock between them',
            'UNSUPPORTED_TERRAIN')
    for tile, tags in sorted(claims.items()):
        kinds = sorted({(tag[0], tag[1]) for tag in tags})
        if len(kinds) == 1:
            continue
        sides = [k[1] for k in kinds if k[0] == 'band']
        ok = (len(kinds) == 2 and len(sides) == 2 and {tuple(OUT[s]) for s in sides}
              not in ({(0, -1), (0, 1)}, {(-1, 0), (1, 0)}))
        require(ok, f'Rock tile {tile[0]},{tile[1]} would carry conflicting walls; keep floor parts apart or '
                'join them with at least a two-tile corridor away from corners', 'UNSUPPORTED_TERRAIN')
    concave = _concave_vertices(floor)
    exits = []
    for e in spec['exits']:
        x, z = e['x'], e['z']
        run = [(x + i, z) for i in (-2, -1, 0, 1, 2)]
        require(all(t in floor and (t[0], t[1] + 1) not in floor for t in run),
                f'Exit {x},{z} needs a straight south wall run of five floor tiles centred on it', 'UNSUPPORTED_ACCESS')
        exits.append((x, z))
    for a, b in zip(sorted(exits), sorted(exits)[1:]):
        require(a[1] != b[1] or b[0] - a[0] >= 5, 'Exits on one wall need four tiles between them', 'UNSUPPORTED_ACCESS')
    return {'floor': floor, 'zone': zone, 'footprint': floor | zone, 'straights': straights, 'corners': corners,
            'concave': concave, 'exits': exits, 'ring': zone, 'landings': set()}


def _concave_vertices(floor):
    """(vertex, (side_a, side_b)) for every concave floor corner. Floor tile t has rock on side a,
    its neighbour on side b is floor and that neighbour's side-a tile is floor too: the wall of side
    a ends there and the wall of the opposite of b (on the next floor part) begins at the vertex."""
    out = set()
    for (x, z) in sorted(floor):
        for a, na in OUT.items():
            for b, nb in OUT.items():
                if na[0] * nb[0] + na[1] * nb[1] != 0 or a == b:
                    continue
                if (x + na[0], z + na[1]) in floor:
                    continue
                if (x + nb[0], z + nb[1]) in floor and (x + na[0] + nb[0], z + na[1] + nb[1]) in floor:
                    vx = x + (1 if na[0] > 0 or nb[0] > 0 else 0)
                    vz = z + (1 if na[1] > 0 or nb[1] > 0 else 0)
                    other = next(s for s, v in OUT.items() if v == (-nb[0], -nb[1]))
                    out.add(((vx, vz), tuple(sorted((a, other)))))
    return sorted(out)


def runs(straights, skip=()):
    """Maximal straight wall runs: (side, first floor tile, length) along x (north/south) or z."""
    by_line = {}
    for t, side in straights:
        if (t, side) in skip:
            continue
        along = 0 if side in ('north', 'south') else 1
        by_line.setdefault((side, t[1 - along]), []).append(t[along])
    out = []
    for (side, fixed), values in sorted(by_line.items()):
        values.sort()
        start = prev = values[0]
        for v in values[1:] + [None]:
            if v is not None and v == prev + 1:
                prev = v
                continue
            first = (start, fixed) if side in ('north', 'south') else (fixed, start)
            out.append((side, first, prev - start + 1))
            if v is not None:
                start = prev = v
    return out


def _stretch(lib, polys, side, first, length):
    """A one-tile band piece at ``first`` stretched along its run to ``length`` tiles: the donor
    walls are extruded profiles, so vertices at the far tile edge move and UVs follow the phase."""
    axis = 0 if side in ('north', 'south') else 2
    base = first[0] if axis == 0 else first[1]
    grads = lib['gx'] if axis == 0 else lib['gz']
    out = []
    for name, poly in polys:
        p = poly.copy()
        offset = (p[:, axis] - base) * (length - 1)
        p[:, axis] += offset
        p[:, 3:5] += np.outer(offset, grads.get(name, np.zeros(2)))
        out.append((name, p))
    return out


def pieces(lib, spec, ground):
    lay = layout(spec)
    dy = ground - FLOOR_Y
    exit_cols = {((x + i, z), 'south') for x, z in lay['exits'] for i in (-1, 0, 1)}
    keeps = []
    for (vx, vz), (a, b) in lay['concave']:
        for mine, other in ((a, b), (b, a)):
            n, m = OUT[mine], OUT[other]
            keeps.append((mine, (vx, vz), lambda px, pz, n=n, m=m, vx=vx, vz=vz:
                          (m[0] - n[0]) * (px - vx) + (m[1] - n[1]) * (pz - vz)))
    placed = []
    for side, first, length in runs(lay['straights'], exit_cols):
        polys = _stretch(lib, _place(lib, side, first, dy), side, first, length)
        axis = 0 if side in ('north', 'south') else 1
        lo, hi = first[axis], first[axis] + length
        # The run's floor edge line; a concave vertex belongs to this run only when it lies on that
        # line at one of the run's ends.
        edge = {'north': first[1], 'south': first[1] + 1, 'west': first[0], 'east': first[0] + 1}[side]
        near = [f for mine, (vx, vz), f in keeps if mine == side
                and (vz if axis == 0 else vx) == edge and (vx if axis == 0 else vz) in (lo, hi)]
        for name, poly in polys:
            for f in near:
                poly = tg._keep(poly, f)
                if poly is None:
                    break
            if poly is not None and (abs(tg.area_xz(poly)) > 1e-7 or np.ptp(poly[:, 1]) > 1e-6):
                placed.extend((name, q) for q in tg.native([poly]))
    for t, key in lay['corners']:
        placed.extend(_place(lib, key, t, dy))
    for x, z in lay['exits']:
        placed.extend(_place(lib, 'exit_v2' if spec.get('version') else 'exit', (x, z), dy))
    flats = [('droad01', float(FLOOR_Y), rect, ground) for rect in tg.rectangles(lay['floor'])]
    return {'layout': lay, 'clipped': placed, 'atomic': [], 'flats': flats}
