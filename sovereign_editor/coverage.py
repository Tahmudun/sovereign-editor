"""Supported / limited / read-only / unsupported coverage of the editor (RELEASE-02).

One table serves the app (Project browser → Coverage), the CLI (``coverage``) and the generated
docs/EDITOR_V1_COVERAGE.md, so the three never disagree. ``report(project)`` adds the live
resource limits of the open project. Every "supported" row is software evidence only: native
melonDS acceptance stays with the user's checklist.
"""

SUPPORTED, LIMITED, READ_ONLY, UNSUPPORTED = 'supported', 'limited', 'read-only', 'unsupported'

ROWS = [
    # (area, feature, status, limits / notes, evidence)
    ('Field', 'Cut tree, Rock Smash rock, Strength boulder obstacles', SUPPORTED,
     'Stock objects and native actions; reset (map-temporary flag) or permanent (named on/off state); authority by '
     'move plus badge, named state, item or money; Strength resets on map reload (puzzle reset helper).',
     'tests/test_editor_completion.py; field-authority qualification'),
    ('Field', 'Surf', LIMITED,
     'Native field code only: Fog Badge (3) + a party member knowing Surf, facing surfable water (byte-qualified). '
     'The editor authors water (ponds, water shapes), surf encounters and the badge reward; it does not change Surf.',
     'field_moves.qualify_field_authority'),
    ('Field', 'Waterfall, Whirlpool, Rock Climb, Flash, Fly (original content v1)', SUPPORTED,
     'Native actions with their stock badge checks (Rising 7, Glacier 6, Earth 15, Storm 4): waterfalls carved '
     'into created areas (3..5 wide, pools 2..4 deep), whirlpools placed on qualified surf water, rock-climb faces on '
     'terraces, dark areas (identity weather 11) lit by Flash, Fly destinations discovered on the first visit '
     '(5 qualified flags). No teleport substitutes.',
     'tests/test_field_features.py; tests/test_travel_points.py; tests/test_terrain_shapes.py'),
    ('Field', 'Requirements, badges, gates, pickups, rewards, guides, rematch presets', SUPPORTED,
     'Move/badge/item/state/money/trainer requirements; give_badge; remove; presets expand to ordinary events.',
     'presets.py; chapter composition'),
    ('Battles', 'Ordinary v2 trainers: nature, IV/EV, ability, ball, AI, items, singles and doubles', SUPPORTED,
     'Team-wide nature/IV/EV/ball layout (engine format); abilities first/second/hidden qualified per species/form; '
     '64 library slots (reuse unreferenced ones when full).', 'tools/trainer_party_qualification.py (63 checks)'),
    ('Battles', 'Native sight trainers (single and double pairs), conditional rematches', SUPPORTED,
     'Stock trainer objects and std flow; clear walkable sight lines; slot 740 excluded.', 'test_editor_completion.py'),
    ('Battles', 'New battle effects, custom AI programs, battle-only transformations', UNSUPPORTED,
     'Out of v1 scope; existing effects and AI flags only.', ''),
    ('Data', 'Move records (power, accuracy, PP, type, category, priority, effect, chance)', SUPPORTED,
     'Effects limited to those used by implemented stock moves; unimplemented moves refused.', 'game_data.py'),
    ('Data', 'Item price, HP/PP restore, EV gain, friendship; TM001–TM092 mapping', LIMITED,
     'Use parameters only where the item already has that use family; HM mappings stay stock.', 'game_data.py'),
    ('Data', 'Expanded catalogs', LIMITED,
     'Moves 842/928, abilities 309/331, hold effects 204/220 offered with per-entry refusal reasons.',
     'assets/engine-catalog.json'),
    ('Data', 'New moves, items, abilities or effect code', UNSUPPORTED, 'Existing records only.', ''),
    ('Services', 'Shops', SUPPORTED,
     'Native special marts 30+ (≤32 authored shops, ≤48 items each); priced, sellable items only; Poké Ball stays '
     'hidden until the game sets flag 0x9A; lists live in the field extension (overlay 131).',
     'tools/service_qualification.py; tools/mart_qualification.py'),
    ('Services', 'Healing, respawn and blackout destinations', SUPPORTED,
     'Stock rest heal; authored respawn points (spawn rows 31+ with qualified bounds, blackout arrival script) and '
     'Fly points in created areas; a respawn becomes the blackout point when its map is entered.',
     'tests/test_travel_points.py'),
    ('Services', 'Tutor, relearner, gifts, money, evolution items', SUPPORTED,
     'Payment only after a successful lesson; existing r64 coverage.', 'test_assets_gameplay.py'),
    ('Terrain', 'Rectangular terraces and ponds (canopy-coast-v1)', SUPPORTED,
     'One stock step; 3-wide stairs; ponds 2..8; cells holding the donor materials.', 'docs/TERRAIN_AUTHORING.md'),
    ('Terrain', 'Shaped terraces: concave outlines, second level, ledges', SUPPORTED,
     '≤8 rectangles within 24×24; inner corners are mitred stock walls; one-tile notches refused; ledges one step.',
     'tests/test_terrain_shapes.py'),
    ('Terrain', 'Shaped water (river bends)', SUPPORTED,
     'Water ≥2 wide; no one-tile land necks; only in cells holding the stock sea/shore materials.',
     'tests/test_terrain_shapes.py'),
    ('Terrain', 'Caves (cave-d41-v1): rooms, corridors, walls, exits, encounters', LIMITED,
     'Carved into empty space of areas made from the stock D41 room; floors ≥2 wide; walls need 3 tiles of rock; '
     'exits on straight south walls; the stock floor-edge decal is not reproduced.', 'tests/test_cave_terrain.py'),
    ('Terrain', '3D model import, bridges/overlapping floors, arbitrary donor tilesets', UNSUPPORTED, 'Refused.', ''),
    ('World', 'Created areas, connections (door, exit mat, gate, cave exit; same-area holes), moving entrances',
     SUPPORTED, 'Editor bound 152 created headers; resident memory is the binding limit (14 bytes per header).',
     'tests/test_world_*.py; test_cave_terrain.py'),
    ('World', 'Static custom props (import, place, revise)', SUPPORTED,
     '≤512 triangles, ≤15 materials, ≤16 KiB textures; no animation or rotation; 32 objects per map cell; each area '
     'carries only the prop textures its maps show (stock outdoor texture maximum). Kits: blossom, framed beds, '
     'picket fences.', 'docs/CUSTOM_PROPS.md; tests/test_map_groups.py'),
    ('World', 'Custom ground materials: paving with rims, decals, area variants, decorative tall grass', SUPPORTED,
     'Paths repaint cleanly (rims follow, stop at terrace cliffs, pave terrace tops); ≤1024 authored tiles per cell '
     'within the polygon budget and 60 KiB map buffer; ≤64 decals per map; rims fade into stock grass/dirt, so two '
     'custom materials cannot meet; decorative tall grass is walkable ground without encounters.',
     'tests/test_ground_materials.py; tests/test_border_authoring.py'),
    ('World', 'Map groups: capture, copy, move, remove (groves, beds, fence runs, buildings with entrances)', SUPPORTED,
     'One edit per placement; a copied entrance gets its own two-way connection; moves stay in one cell; signs/NPCs '
     'copy within their map or into created areas; map-model visuals are not group elements.',
     'tests/test_map_groups.py'),
    ('World', 'Environments: named kits of materials, props and placeable presets', SUPPORTED,
     'Terrace (+paving), pond, cave room, prop and decal arrangements, areas from stock donor cells; placement expands '
     'to ordinary operations (not a live link); reusable between projects.', 'tests/test_environments.py'),
    ('World', 'Airborne petals (one area-scoped effect)', LIMITED,
     'Weather 14 per area with density/speed/direction; refused where the area uses dark/lighting weathers 11..13; one '
     'qualified effect, not a particle system.', 'tests/test_petal_effect.py'),
    ('Workspace', 'Project browser, cross-links, progression, reach from a save, impact reports', SUPPORTED,
     'Static analysis; stock scripts are not interpreted; findings never block export. Progression names missing '
     'warp targets, unconnected areas, unreachable entrances and entrance tiles with no connection.',
     'tests/test_workspace.py; tests/test_cave_terrain.py'),
    ('Recovery', 'Portable packages, checkpoints, undoable restore, recovery', SUPPORTED,
     'Baseline bound by SHA-256; no partial projects on refusal.', 'tests/test_recovery.py'),
    ('Release', 'Intel Mac app bundle', LIMITED,
     'Tested on macOS 15.7.9 x86_64 with the python.org Python 3.12 framework; unsigned, not notarized; other '
     'platforms not verified.', 'evidence/editor-completion-v1/release/launch.json'),
    ('Release', 'Timing at the original-content load (738 edits, 15.6 MB project)', LIMITED,
     'Measured on this Intel Mac only (3 samples each): open 1.0 s, small preview 0.9 / apply 1.9, 20-operation batch '
     'preview 3.1 / apply 3.6, undo/redo 1.3-1.4, export with readback 38 s, content batch (environment terrace, grove, '
     'decals) 1.2 / 2.0; the cold-cache first open misses its 10 s target (about 13 s after caching fixes).',
     'work/original-content-v1/impl/evidence/perf-contract-r101.json; perf-content-r101.json'),
    ('Pokémon', 'Species, forms, evolution (r64)', LIMITED,
     '52 of 56 regional forms use the base-species follower; expanded cries unqualified.', 'ASSETS_GAMEPLAY_COVERAGE.md'),
    ('Pokémon', 'Custom Pokémon packages and persistent identities (e.g. Jacob\'s Eevee)', LIMITED,
     'Import/revise/remove art packages; an identity takes a free form slot and personal index 1490+; stats, moves, '
     'cry, footprint and Pokédex are inherited from the template species; own follower sprite; no new evolutions.',
     'tests/test_pokemon_packages.py'),
    ('Production', 'Clean projects, reuse between projects, retirement', SUPPORTED,
     'Explicit selection with dependencies; the clean project allocates its own IDs; retired IDs are never reused. '
     'Measured library limits: 64 trainer teams, 32 characters, 60 number states.',
     'tests/test_reuse.py; tests/test_retirement.py; tools/capacity_measure.py'),
]


def table():
    return [{'area': a, 'feature': f, 'status': s, 'limits': l, 'evidence': e} for a, f, s, l, e in ROWS]


def report(project):
    """The table plus the open project's live resource limits."""
    from . import capacity
    limits = project.capacity()['limits']
    runtime = capacity.runtime_report(project)
    live = {k: {f: v[f] for f in ('used', 'limit', 'available') if f in v} for k, v in limits.items()}
    live['resident_overlay_129_free_bytes'] = runtime['overlay_129'].get('free_bytes')
    live['field_overlay_131_free_bytes'] = runtime['overlay_131'].get('free_bytes')
    from .field_services import shops, MAX_SHOPS
    live['authored_shops'] = {'used': len(shops(project.composed())), 'limit': MAX_SHOPS}
    counts = {}
    for row in ROWS:
        counts[row[2]] = counts.get(row[2], 0) + 1
    return {'revision': project.doc['revision'], 'rows': table(), 'counts': counts, 'limits': live,
            'note': 'Supported means software-verified; native acceptance is the user-run checklist.'}


def markdown(project=None):
    lines = ['# Editor v1 coverage and limits', '',
             'Generated from `sovereign_editor/coverage.py` (the same table the app and CLI show). Supported rows are '
             'software-verified; native melonDS acceptance is the user-run checklist.', '',
             '| Area | Feature | Status | Limits / notes | Evidence |', '| --- | --- | --- | --- | --- |']
    for a, f, s, l, e in ROWS:
        lines.append(f'| {a} | {f} | {s} | {l} | {e} |')
    if project is not None:
        r = report(project)
        lines += ['', f"## Live limits of `{project.root.name}` (revision {r['revision']})", '',
                  '| Resource | Used | Limit |', '| --- | ---: | ---: |']
        for k, v in sorted(r['limits'].items()):
            if isinstance(v, dict):
                lines.append(f"| {k} | {v.get('used', '')} | {v.get('limit', '')} |")
            else:
                lines.append(f'| {k} | {v} | |')
    return '\n'.join(lines) + '\n'
