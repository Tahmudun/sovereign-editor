import argparse
import json
import sys
from pathlib import Path

from . import __version__, authoring
from .core import Project
from .formats import EditorError


def cell_type(text):
    parts = text.replace(" ", "").split(",")
    if len(parts) != 2 or not all(p.lstrip("-").isdigit() for p in parts):
        raise argparse.ArgumentTypeError("Use X,Y integer matrix coordinates")
    return [int(parts[0]), int(parts[1])]


def tile_type(text):
    return cell_type(text)


def _hex_pair(value, what):
    if len(value) != 4 or any(c not in "0123456789abcdefABCDEF" for c in value):
        raise argparse.ArgumentTypeError(
            f"Permission {what} must be a two-byte type/collision hex value such as 0080, got {value!r}")
    return value.lower()


def permission_type(text):
    """``X,Z=AFTER`` or ``X,Z=AFTER:BEFORE`` with two-byte type/collision hex values."""
    cell, assigned, values = text.replace(" ", "").partition("=")
    if not assigned:
        raise argparse.ArgumentTypeError("Use X,Z=AFTER or X,Z=AFTER:BEFORE, e.g. 550,398=0000:0080")
    after, guarded, before = values.partition(":")
    x, z = cell_type(cell)
    result = {"x": x, "z": z, "after": _hex_pair(after, "after")}
    if guarded:
        result["before"] = _hex_pair(before, "before")
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description="Sovereign Editor: native Mac UI and agentic JSON CLI")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="Create a separate project from an immutable ROM copy")
    create.add_argument("--rom", required=True, type=Path)
    create.add_argument("--project", required=True, type=Path)
    create.add_argument("--name", default="Cherrygrove workspace")
    clone = sub.add_parser("clone", help="Copy a verified project with its full history and project-owned assets")
    clone.add_argument("--project", required=True, type=Path)
    clone.add_argument("--output", required=True, type=Path, help="New project folder (must not exist)")
    clone.add_argument("--name")
    data_catalog = sub.add_parser('data-catalog', help='Qualified move/item/ability/effect/TM/shop/respawn catalogs with refusal reasons')
    data_catalog.add_argument('--project', required=True, type=Path)
    data_catalog.add_argument('--kind', required=True, choices=('moves', 'items', 'abilities', 'effects', 'machines', 'shops', 'spawns'))
    data_catalog.add_argument('--search', default='')
    data_catalog.add_argument('--offset', type=int, default=0)
    data_catalog.add_argument('--limit', type=int, default=40)
    data_record = sub.add_parser('data-record', help='Decoded move/item record (before_sha256) or a shop inventory (before)')
    data_record.add_argument('--project', required=True, type=Path)
    data_record.add_argument('--kind', required=True, choices=('move', 'item', 'shop'))
    data_record.add_argument('--id', type=int)
    data_record.add_argument('--name', help='Shop name (kind shop)')
    data_edit = sub.add_parser('data-edit', help='Preview/apply move, item, TM and shop changes (one batch)')
    data_edit.add_argument('--project', required=True, type=Path)
    data_edit.add_argument('--request', required=True, type=Path, help='JSON {operations, label}')
    data_edit.add_argument('--dry-run', action='store_true')
    data_edit.add_argument('--revision', type=int)
    pack = sub.add_parser('package', help='Write a portable, verified project package (ZIP)')
    pack.add_argument('--project', required=True, type=Path)
    pack.add_argument('--output', required=True, type=Path)
    pack.add_argument('--include-baseline', action='store_true', help='Carry the baseline ROM inside the package')
    unpack = sub.add_parser('unpack', help='Open a package into a new folder, verifying files and the baseline binding')
    unpack.add_argument('--package', required=True, type=Path)
    unpack.add_argument('--output', required=True, type=Path)
    unpack.add_argument('--baseline', type=Path, help='The baseline ROM when the package does not carry it')
    cp = sub.add_parser('checkpoint', help='Save a named checkpoint of the project and its asset packages')
    cp.add_argument('--project', required=True, type=Path)
    cp.add_argument('--name', required=True)
    cp.add_argument('--revision', required=True, type=int)
    cp.add_argument('--note')
    cps = sub.add_parser('checkpoints', help='List checkpoints and how they differ from the current edits')
    cps.add_argument('--project', required=True, type=Path)
    rs = sub.add_parser('restore', help='Restore a checkpoint as one undoable edit (automatic backup first)')
    rs.add_argument('--project', required=True, type=Path)
    rs.add_argument('--name', required=True)
    rs.add_argument('--revision', required=True, type=int)
    rc = sub.add_parser('recover', help='Replace a project.json that no longer opens with a checkpoint')
    rc.add_argument('--project', required=True, type=Path)
    rc.add_argument('--name', required=True)
    cov = sub.add_parser('coverage', help='Supported / limited / read-only / unsupported features and live limits')
    cov.add_argument('--project', required=True, type=Path)
    ws_search = sub.add_parser('workspace-search', help='Search every authored resource with its cross-links')
    ws_search.add_argument('--project', required=True, type=Path)
    ws_search.add_argument('--query', default='')
    ws_search.add_argument('--kind', action='append', choices=('area', 'character', 'trainer', 'state', 'event', 'shop',
                                                               'record', 'asset', 'connection', 'terrain'))
    ws_search.add_argument('--limit', type=int, default=200)
    ws_refs = sub.add_parser('workspace-refs', help='Uses and shared users of one resource (kind:key)')
    ws_refs.add_argument('--project', required=True, type=Path)
    ws_refs.add_argument('--ref', required=True)
    progress = sub.add_parser('progression-report', help='Warp/travel graph and graded findings (errors, conditional, warnings)')
    progress.add_argument('--project', required=True, type=Path)
    reach = sub.add_parser('reach-report', help='Static walk/warp/travel reach from a tile or a save (read-only)')
    reach.add_argument('--project', required=True, type=Path)
    reach.add_argument('--save', type=Path)
    reach.add_argument('--header', type=int)
    reach.add_argument('--x', type=int)
    reach.add_argument('--z', type=int)
    reach.add_argument('--surf', action='store_true')
    reach.add_argument('--unconditional-only', action='store_true')
    impact = sub.add_parser('impact-report', help='Resources a staged batch changes and who shares them (no write)')
    impact.add_argument('--project', required=True, type=Path)
    impact.add_argument('--request', required=True, type=Path, help='JSON {operations, label}')
    story = sub.add_parser('story-library', help='List imported characters, trainers, persistent state and events')
    story.add_argument('--project', required=True, type=Path)
    for command in ('gameplay-data', 'gameplay-edit', 'gameplay-catalog', 'gameplay-areas', 'gameplay-species'):
        cmd = sub.add_parser(command, help='Inspect or author qualified trainer teams, wild encounters and species data')
        cmd.add_argument('--project', required=True, type=Path)
        if command in ('gameplay-data', 'gameplay-edit'):
            cmd.add_argument('--header', type=int, default=34)
        if command == 'gameplay-species':
            cmd.add_argument('--species', required=True, type=int)
            cmd.add_argument('--form', type=int, default=0)
        if command == 'gameplay-areas':
            cmd.add_argument('--search', default='')
            cmd.add_argument('--offset', type=int, default=0)
            cmd.add_argument('--limit', type=int, default=40)
        if command == 'gameplay-edit':
            cmd.add_argument('--request', required=True, type=Path)
            cmd.add_argument('--dry-run', action='store_true')
            cmd.add_argument('--revision', type=int)
        elif command == 'gameplay-catalog':
            cmd.add_argument('--kind', choices=('species', 'moves', 'items', 'types', 'abilities', 'growth', 'machines',
                                                      'species_forms', 'evolution_methods', 'evolution_items'), required=True)
            cmd.add_argument('--search', default='')
            cmd.add_argument('--offset', type=int, default=0)
            cmd.add_argument('--limit', type=int, default=40)
    cmd = sub.add_parser('field-view', help='Placed field-move features (place/remove via area-edit kind field)')
    cmd.add_argument('--project', required=True, type=Path)
    cmd = sub.add_parser('travel-view', help='Authored respawn and Fly points (define/revise/retire via area-edit '
                                            'kind travel)')
    cmd.add_argument('--project', required=True, type=Path)
    cmd = sub.add_parser('petal-view', help='Areas with airborne petals and their runtime numbers (set/remove via '
                                           'area-edit kind petals)')
    cmd.add_argument('--project', required=True, type=Path)
    cmd = sub.add_parser('repair-view', help='Qualified base-ROM repairs and whether they are applied (apply/remove '
                                            'via area-edit kind repair)')
    cmd.add_argument('--project', required=True, type=Path)
    cmd = sub.add_parser('pokemon-view', help='Imported Pokémon packages and form identities (import/revise/bind/'
                                             'remove via area-edit kind pokemon)')
    cmd.add_argument('--project', required=True, type=Path)
    cmd = sub.add_parser('group-view', help='Map group templates and placed copies (capture/forget/place/move/remove '
                                           'via area-edit kind map_group)')
    cmd.add_argument('--project', required=True, type=Path)
    cmd = sub.add_parser('env-view', help='Reusable environments: ground/prop members, presets and donors (define/'
                                         'revise/retire/place via area-edit kind environment)')
    cmd.add_argument('--project', required=True, type=Path)
    cmd = sub.add_parser('reuse', help='Copy selected definitions (kind:key) and their dependencies from another '
                                      'project into this one by their ordinary operations; dry run without --revision')
    cmd.add_argument('--project', required=True, type=Path)
    cmd.add_argument('--source', required=True, type=Path)
    cmd.add_argument('--item', action='append', required=True, help='state|character|trainer|prop|ground|pokemon|identity:key')
    cmd.add_argument('--revision', type=int)
    cmd = sub.add_parser('pokemon-stage', help='Validate a prepared Pokémon package folder (no writes) and print the '
                                              'import/revise operation')
    cmd.add_argument('--project', required=True, type=Path)
    cmd.add_argument('--source', required=True, type=Path)
    cmd.add_argument('--key', required=True)
    cmd.add_argument('--display', required=True)
    cmd.add_argument('--template', required=True, type=int, help='base species whose records set the layout')
    cmd.add_argument('--icon-palette', required=True, type=int, choices=(0, 1, 2))
    for command in ('ground-view', 'ground-stage'):
        cmd = sub.add_parser(command, help='Custom ground materials: registered materials/variants/users, or bake '
                                           'an import folder (register/revise via area-edit kind ground)')
        cmd.add_argument('--project', required=True, type=Path)
        if command == 'ground-stage':
            cmd.add_argument('--source', required=True, type=Path, help='material.json + indexed PNG folder')
    for command in ('prop-view', 'prop-stage', 'prop-edit'):
        cmd = sub.add_parser(command, help='Custom static props: inspect, bake/preview an import folder, or apply operations')
        cmd.add_argument('--project', required=True, type=Path)
        if command == 'prop-stage':
            cmd.add_argument('--source', required=True, type=Path, help='asset.json + OBJ + indexed PNG folder')
        if command == 'prop-edit':
            cmd.add_argument('--request', required=True, type=Path,
                             help='JSON {operations:[...], label?, context?}; register/revise may name source instead of package')
            cmd.add_argument('--dry-run', action='store_true')
            cmd.add_argument('--revision', type=int)
    report = sub.add_parser('scene-report', help='Validate and trace scene positions, gaze, presence and approach routes')
    report.add_argument('--project', required=True, type=Path)
    report.add_argument('--key', required=True)
    area = sub.add_parser('area-edit', help='Preview/apply one atomic batch of area operations')
    area.add_argument('--project', required=True, type=Path)
    area.add_argument('--request', required=True, type=Path, help='JSON object containing operations and optional label')
    area.add_argument('--dry-run', action='store_true')
    area.add_argument('--revision', type=int)
    workspace = sub.add_parser('workspace-edit', help='Preview/apply semantic staged map actions')
    workspace.add_argument('--project', required=True, type=Path)
    workspace.add_argument('--request', required=True, type=Path, help='JSON actions and optional label')
    workspace.add_argument('--dry-run', action='store_true')
    workspace.add_argument('--revision', type=int)
    for name in ('linked-groups', 'surface-sample', 'surface-palette'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--project', required=True, type=Path)
        cmd.add_argument('--header', required=True, type=int)
        cmd.add_argument('--cell', required=True, type=cell_type)
        if name == 'surface-sample':
            cmd.add_argument('--x', required=True, type=int)
            cmd.add_argument('--z', required=True, type=int)
        if name == 'surface-palette':
            cmd.add_argument('--images', action='store_true')
    areas = sub.add_parser('world-areas', help='List created areas, their private resources, connections and header capacity')
    areas.add_argument('--project', required=True, type=Path)
    ident = sub.add_parser('world-identity', help='Created-area names, popups, music, weather, Pokégear markers, parents, '
                           'static forest cells and Headbutt tiles, with valid values; edit them with area-edit '
                           'world identity/animation/trees operations')
    ident.add_argument('--project', required=True, type=Path)
    terrain = sub.add_parser('terrain-inspect', help='Terrain authoring v1: supported family, limits, terraces/ponds '
                             'and sound plates (rain conflicts) of a created area; edit with area-edit kind elevation')
    terrain.add_argument('--project', required=True, type=Path)
    terrain.add_argument('--header', required=True, type=int)
    for name, text in (('capacity', 'Used/available limits for characters, trainers, states, visibility, headers, cells and encounters'),
                       ('runtime-report', 'Consolidated resident/field runtime allocation after all additions')):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument('--project', required=True, type=Path)
    appearances = sub.add_parser('npc-appearances', help='List supported stock NPC appearances and behaviors')
    appearances.add_argument('--project', required=True, type=Path)
    library = sub.add_parser('area-library', help='Browse compatible stock donor maps and templates')
    library.add_argument('--project', required=True, type=Path)
    library.add_argument('--header', required=True, type=int)
    library.add_argument('--cell', required=True, type=cell_type)
    library.add_argument('--source-header', type=int)
    library.add_argument('--source-cell', type=cell_type)
    library.add_argument('--search', default='')
    library.add_argument('--images', action='store_true')
    for name in ("inspect", "scene", "placements", "placement", "validate", "diff", "undo", "redo", "move-npc", "move-placement", "export", "preview"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--project", required=True, type=Path)
        if name in ("placement", "move-placement"):
            cmd.add_argument("--map", required=True, type=int, dest="map_id", help="Map archive member (4 or 5)")
            cmd.add_argument("--slot", required=True, type=int, help="Placement slot within the map member")
        if name == "preview":
            cmd.add_argument("--terrain-only", action="store_true")
        if name in ("undo", "redo", "move-npc", "move-placement", "export"):
            cmd.add_argument("--revision", type=int, required=True, help="Current revision returned by inspect")
        if name == "move-npc":
            cmd.add_argument("--id", type=int, required=True)
            cmd.add_argument("--x", type=int, required=True, help="Global tile X")
            cmd.add_argument("--z", type=int, required=True, help="Global tile Z")
        if name == "move-placement":
            cmd.add_argument("--x", type=float, required=True, help="Global anchor X; query placement for the five allowed anchors")
            cmd.add_argument("--z", type=float, required=True, help="Global anchor Z; whole-tile translations preserve the .5 anchor")
        if name == "export":
            cmd.add_argument("--output", type=Path, required=True, help="New export directory")
            cmd.add_argument("--save", type=Path, help="Copy this ordinary save alongside the ROM")
    for name in ("map-contexts", "map-view", "map-permissions", "map-edit", "map-scene", "map-neighborhood", "map-sign", "map-palette", "map-scenery", "map-events", "event-edit"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--project", required=True, type=Path)
        if name == "map-contexts":
            cmd.add_argument("--matrix", type=int, help="List this matrix's populated cells")
            cmd.add_argument("--header", type=int, help="Restrict to one map header")
            cmd.add_argument("--map-member", type=int, dest="map_member", help="Cells reusing one a/0/6/5 member")
            cmd.add_argument("--search", help="Filter cells by internal map name, or by map member number")
            cmd.add_argument("--limit", type=int, default=40)
            cmd.add_argument("--offset", type=int, default=0, help="Page offset; total is reported")
        else:
            cmd.add_argument("--header", type=int, help="Map header id")
            cmd.add_argument("--matrix", type=int, help="Matrix id, when no header is given")
            cmd.add_argument("--cell", type=cell_type, help="Matrix cell as X,Y")
        if name == "event-edit":
            cmd.add_argument("--kind", choices=("npc", "background", "warp"), required=True)
            cmd.add_argument("--id", type=int, required=True, dest="event_id")
            for field in ("x", "z", "facing", "range-x", "range-z", "destination", "destination-warp"):
                cmd.add_argument("--" + field, type=int)
            cmd.add_argument("--reciprocal", action="store_true", help="Also connect the destination warp back to this source")
            cmd.add_argument("--label")
            cmd.add_argument("--dry-run", action="store_true")
            cmd.add_argument("--revision", type=int)
        if name == "map-palette":
            cmd.add_argument("--images", action="store_true", help="Include thumbnail image paths")
        if name == "map-scenery":
            cmd.add_argument("--action", required=True, choices=("add", "duplicate", "delete", "transfer", "move"))
            cmd.add_argument("--slot", required=True, type=int, help="Stable authoring slot, or baseline template slot for add")
            cmd.add_argument("--to-x", type=float)
            cmd.add_argument("--to-z", type=float)
            cmd.add_argument("--destination-header", type=int)
            cmd.add_argument("--destination-cell", type=cell_type)
            cmd.add_argument("--permission", action="append", default=[], type=permission_type)
            cmd.add_argument("--destination-permission", action="append", default=[], type=permission_type)
            cmd.add_argument("--move-collision", action="append", default=[], type=tile_type)
            cmd.add_argument("--label")
            cmd.add_argument("--dry-run", action="store_true")
            cmd.add_argument("--revision", type=int)
        if name == "map-view":
            cmd.add_argument("--no-grid", action="store_true", help="Omit the 32x32 permission grid")
        if name in ("map-scene", "map-neighborhood"):
            cmd.add_argument("--no-image", action="store_true",
                             help="Resolve models and textures without rendering the scene")
        if name == "map-scene":
            cmd.add_argument("--pixels-per-tile", type=int, dest="pixels_per_tile",
                             help="Render resolution, 4..64 pixels per tile (default 32)")
            cmd.add_argument("--thumbnail", type=int, metavar="MODEL",
                             help="Also render this building model's preview image")
        if name == "map-permissions":
            cmd.add_argument("--x", type=int, help="Window origin, global tile X")
            cmd.add_argument("--z", type=int, help="Window origin, global tile Z")
            cmd.add_argument("--width", type=int, default=1)
            cmd.add_argument("--height", type=int, default=1)
        if name == "map-edit":
            cmd.add_argument("--align-sign", action="store_true", help="Align the identified New Bark town-sign interaction with its model")
            cmd.add_argument("--slot", type=int, help="Placement slot inside this map member")
            cmd.add_argument("--to-x", type=float, dest="to_x", help="New global anchor X")
            cmd.add_argument("--to-z", type=float, dest="to_z", help="New global anchor Z")
            # Height is preserved in this milestone; the flag exists only to refuse clearly.
            cmd.add_argument("--to-y", type=float, dest="to_y", help=argparse.SUPPRESS)
            cmd.add_argument("--permission", action="append", default=[], metavar="X,Z=AFTER[:BEFORE]",
                             type=permission_type,
                             help="Explicit permission cell, e.g. 550,398=0000 or 550,398=0000:0080")
            cmd.add_argument("--move-collision", action="append", default=[], dest="move_collision",
                             type=tile_type, metavar="X,Z",
                             help="Selected blocked cell that moves with the placement")
            cmd.add_argument("--label", help="Short description stored with the transaction")
            cmd.add_argument("--dry-run", action="store_true", help="Preview the transaction without saving")
            cmd.add_argument("--revision", type=int, help="Current revision; required unless --dry-run")
    ui = sub.add_parser("ui")
    ui.add_argument("--project", type=Path)
    ui.add_argument("--map-inspector", action="store_true", dest="map_inspector",
                    help="Open the map/permission inspector window")
    ui.add_argument('--gameplay', action='store_true', help='Open teams, wild encounters and species data')
    ui.add_argument('--smoke-test', action='store_true', dest='smoke',
                    help='Open the windows, process events briefly, print a JSON launch report and exit')
    sprite = sub.add_parser("aseprite")
    sprite.add_argument("action", choices=("doctor", "export"))
    sprite.add_argument("--source", type=Path)
    sprite.add_argument("--output", type=Path)
    args = p.parse_args(argv)
    try:
        if args.command == "ui":
            from .gui import launch
            if args.gameplay and (not args.project or args.map_inspector):
                p.error('--gameplay requires --project and cannot combine with --map-inspector')
            return launch(args.project, map_inspector=args.map_inspector, gameplay=args.gameplay, smoke=args.smoke)
        if args.command == "create":
            result = Project.create(args.rom, args.project, args.name).inspect()
        elif args.command == 'unpack':
            from .recovery import unpack
            result = unpack(args.package, args.output, args.baseline)
        elif args.command == 'recover':
            from .recovery import recover
            result = recover(args.project, args.name)
        elif args.command == "aseprite":
            from .aseprite import doctor, export_sheet
            if args.action == "doctor":
                result = doctor()
            else:
                if args.source is None or args.output is None:
                    p.error("aseprite export requires --source and --output")
                result = export_sheet(args.source, args.output)
        else:
            project = Project(args.project)
            if args.command == "clone":
                result = project.clone(args.output, args.name).inspect()
            elif args.command == 'package':
                result = project.package(args.output, args.include_baseline)
            elif args.command == 'checkpoint':
                result = project.checkpoint(args.name, args.revision, args.note)
            elif args.command == 'checkpoints':
                result = project.checkpoints()
            elif args.command == 'restore':
                result = project.restore_checkpoint(args.name, args.revision)
            elif args.command == 'data-catalog':
                result = project.data_catalog(args.kind, args.search, args.offset, args.limit)
            elif args.command == 'data-record':
                result = project.data_record(args.kind, args.name if args.kind == 'shop' else args.id)
            elif args.command == 'data-edit':
                request = json.loads(args.request.read_text())
                if args.dry_run:
                    plan = project.plan_data_edit(**request)
                    result = {'revision': project.doc['revision'], 'empty': plan['empty'], 'preview': plan['preview']}
                else:
                    if args.revision is None:
                        p.error('data-edit requires --revision unless --dry-run is given')
                    result = project.apply_data_edit(args.revision, **request)
            elif args.command == 'coverage':
                result = project.coverage()
            elif args.command == 'workspace-search':
                result = project.workspace_search(args.query, args.kind, args.limit)
            elif args.command == 'workspace-refs':
                result = project.workspace_references(args.ref)
            elif args.command == 'progression-report':
                result = project.progression()
            elif args.command == 'reach-report':
                result = project.reach(args.header, args.x, args.z, save=args.save, surf=args.surf,
                                       conditional=not args.unconditional_only)
            elif args.command == 'impact-report':
                result = project.impact(**json.loads(args.request.read_text()))
            elif args.command == "story-library":
                result = project.story_library()
            elif args.command == 'gameplay-data':
                result = project.gameplay_data(args.header)
            elif args.command == 'gameplay-areas':
                result = project.gameplay_areas(args.search, args.offset, args.limit)
            elif args.command == 'gameplay-species':
                result = project.gameplay_species(args.species, args.form)
            elif args.command == 'gameplay-catalog':
                result = project.gameplay_catalog(args.kind, args.search, args.offset, args.limit)
            elif args.command == 'gameplay-edit':
                request = json.loads(args.request.read_text())
                request.setdefault('header', args.header)
                if args.dry_run:
                    plan = project.plan_gameplay_edit(**request)
                    result = {'revision': project.doc['revision'], 'empty': plan['empty'], 'preview': plan['preview']}
                    if 'impact' in plan['transaction']:
                        result['impact'] = plan['transaction']['impact']
                else:
                    if args.revision is None:
                        p.error('gameplay-edit requires --revision unless --dry-run is given')
                    result = project.apply_gameplay_edit(args.revision, **request)
            elif args.command == 'field-view':
                result = project.field_view()
            elif args.command == 'travel-view':
                result = project.travel_view()
            elif args.command == 'petal-view':
                result = project.petal_view()
            elif args.command == 'repair-view':
                result = project.repair_view()
            elif args.command == 'pokemon-view':
                result = project.pokemon_view()
            elif args.command == 'group-view':
                result = project.map_group_view()
            elif args.command == 'env-view':
                result = project.environment_view()
            elif args.command == 'reuse':
                from . import reuse
                source = Project(args.source)
                result = (reuse.plan(project, source, args.item) if args.revision is None
                          else reuse.apply(project, source, args.item, args.revision))
                result.pop('operations', None)
            elif args.command == 'pokemon-stage':
                result = project.stage_pokemon_source(args.source, args.key, args.display, args.template,
                                                      args.icon_palette)
            elif args.command == 'ground-view':
                result = project.ground_view()
            elif args.command == 'ground-stage':
                result = project.stage_ground_source(args.source)
            elif args.command == 'prop-view':
                result = project.prop_view()
            elif args.command == 'prop-stage':
                result = project.stage_prop_source(args.source)
            elif args.command == 'prop-edit':
                request = json.loads(args.request.read_text())
                operations = []
                for op in request['operations']:
                    if op.get('action') in ('register', 'revise') and 'source' in op:
                        # Bake the folder now (same call as prop-stage); the package is written on apply.
                        operations.append(project.stage_prop_source(Path(op['source']))['operation'])
                    else:
                        operations.append(op)
                if args.dry_run:
                    plan = project.plan_prop_edit(operations, request.get('label'), request.get('context'))
                    result = {'revision': project.doc['revision'], 'empty': plan['empty'], 'preview': plan['preview']}
                else:
                    if args.revision is None:
                        p.error('prop-edit requires --revision unless --dry-run is given')
                    result = project.apply_prop_edit(args.revision, operations, request.get('label'), request.get('context'))
            elif args.command == 'scene-report':
                result = project.scene_report(args.key)
            elif args.command == "move-npc":
                result = project.move_npc(args.id, args.x, args.z, args.revision)
            elif args.command == "move-placement":
                result = project.move_placement(args.map_id, args.slot, args.x, args.z, args.revision)
            elif args.command == "undo":
                result = project.undo(args.revision)
            elif args.command == "redo":
                result = project.redo(args.revision)
            elif args.command == "export":
                result = project.export(args.output, args.revision, args.save)
            elif args.command == "preview":
                result = project.preview(include_models=not args.terrain_only)
            elif args.command == "placement":
                result = project.placement(args.map_id, args.slot)
            elif args.command == "map-contexts":
                result = project.contexts(matrix=args.matrix, map_member=args.map_member,
                                          header=args.header, search=args.search,
                                          limit=args.limit, offset=args.offset)
            elif args.command == "map-events":
                result = project.map_events(header=args.header, matrix=args.matrix, cell=args.cell)
            elif args.command == 'world-areas':
                result = project.world_areas()
            elif args.command == 'world-identity':
                result = project.world_identity()
            elif args.command == 'terrain-inspect':
                result = project.terrain_view(args.header)
            elif args.command == 'capacity':
                result = project.capacity()
            elif args.command == 'runtime-report':
                result = project.runtime_report()
            elif args.command == 'npc-appearances':
                from .npc_behavior import BEHAVIORS
                result = {'appearances': project.npc_appearances(), 'behaviors': BEHAVIORS}
            elif args.command == 'area-edit':
                request = json.loads(args.request.read_text())
                if args.dry_run:
                    plan = project.plan_area_edit(**request)
                    result = {'revision': project.doc['revision'], 'empty': plan['empty'], 'preview': plan['preview']}
                else:
                    if args.revision is None:
                        p.error('area-edit requires --revision unless --dry-run is given')
                    result = project.apply_area_edit(args.revision, **request)
            elif args.command == 'workspace-edit':
                request = json.loads(args.request.read_text())
                if args.dry_run:
                    plan = project.plan_workflow(**request)
                    result = {'revision': project.doc['revision'], 'empty': plan['empty'], 'actions': plan['actions'], 'preview': plan['preview']}
                else:
                    if args.revision is None:
                        p.error('workspace-edit requires --revision unless --dry-run is given')
                    result = project.apply_workflow(args.revision, **request)
            elif args.command == 'linked-groups':
                result = {'groups': project.linked_groups(header=args.header, cell=args.cell)}
            elif args.command == 'surface-sample':
                result = project.sample_surface(args.x, args.z, header=args.header, cell=args.cell)
            elif args.command == 'surface-palette':
                result = {'surfaces': project.surface_palette(header=args.header, cell=args.cell, images=args.images)}
            elif args.command == 'area-library':
                from . import area_layout
                context = project.context(header=args.header, cell=args.cell)
                if args.source_header is None:
                    result = {'sources': area_layout.library_sources(project, context)}
                else:
                    if args.source_cell is None:
                        p.error('Choose --source-cell with --source-header')
                    result = {'templates': area_layout.templates(project, context,
                              dict(header=args.source_header, cell=args.source_cell), args.search, args.images)}
            elif args.command == "event-edit":
                values = {k: getattr(args, k) for k in ("x", "z", "facing", "range_x", "range_z", "destination", "destination_warp")
                          if getattr(args, k) is not None}
                request = dict(header=args.header, matrix=args.matrix, cell=args.cell, kind=args.kind,
                               event_id=args.event_id, values=values, reciprocal=args.reciprocal, label=args.label)
                if args.dry_run:
                    plan = project.plan_event_edit(**request)
                    result = {"revision": project.doc["revision"], "empty": plan["empty"], "preview": plan["preview"]}
                else:
                    if args.revision is None:
                        p.error("event-edit requires --revision unless --dry-run is given")
                    result = project.apply_event_edit(args.revision, **request)
            elif args.command == "map-palette":
                result = project.map_palette(header=args.header, matrix=args.matrix, cell=args.cell, images=args.images)
            elif args.command == "map-scenery":
                if (args.destination_header is None) != (args.destination_cell is None):
                    p.error("Destination requires both --destination-header and --destination-cell")
                destination = None if args.destination_header is None else {
                    "header": args.destination_header, "cell": args.destination_cell}
                request = dict(operation=args.action, slot=args.slot, header=args.header, matrix=args.matrix,
                               cell=args.cell, x=args.to_x, z=args.to_z, destination=destination,
                               permissions=args.permission, destination_permissions=args.destination_permission,
                               move_collision=[{"x": c[0], "z": c[1]} for c in args.move_collision], label=args.label)
                if args.dry_run:
                    plan = project.plan_scenery_edit(**request)
                    result = {"revision": project.doc["revision"], "empty": plan["empty"], "preview": plan["preview"]}
                else:
                    if args.revision is None:
                        p.error("map-scenery requires --revision unless --dry-run is given")
                    result = project.apply_scenery_edit(args.revision, **request)
            elif args.command == "map-view":
                result = project.map_view(header=args.header, matrix=args.matrix, cell=args.cell,
                                          include_grid=not args.no_grid)
            elif args.command == "map-scene":
                result = project.map_scene(header=args.header, matrix=args.matrix, cell=args.cell,
                                           image=not args.no_image, pixels_per_tile=args.pixels_per_tile,
                                           thumbnail=args.thumbnail)
            elif args.command == "map-neighborhood":
                result = project.map_neighborhood(header=args.header, matrix=args.matrix, cell=args.cell,
                                                  image=not args.no_image, summary=True)
            elif args.command == "map-sign":
                result = project.map_sign(header=args.header, matrix=args.matrix, cell=args.cell)
            elif args.command == "map-permissions":
                result = project.permission_cells(header=args.header, matrix=args.matrix, cell=args.cell,
                                                  x=args.x, z=args.z, width=args.width, height=args.height)
            elif args.command == "map-edit":
                if args.to_y is not None:
                    raise EditorError("UNSUPPORTED_EDIT", authoring.HEIGHT_PRESERVED)
                placement = None
                if args.slot is not None:
                    placement = {"slot": args.slot}
                    for axis, value in (("x", args.to_x), ("z", args.to_z)):
                        if value is not None:
                            placement[axis] = value
                cells = [{"x": c[0], "z": c[1]} for c in args.move_collision]
                request = dict(header=args.header, matrix=args.matrix, cell=args.cell, placement=placement,
                               permissions=args.permission, move_collision=cells, label=args.label, align_sign=args.align_sign)
                if args.dry_run:
                    result = project.plan_map_edit(**request)
                else:
                    if args.revision is None:
                        p.error("map-edit requires --revision unless --dry-run is given")
                    result = project.apply_map_edit(args.revision, **request)
            else:
                result = getattr(project, args.command)()
        print(json.dumps({"ok": True, "result": result}, separators=(",", ":")))
        return 0
    except EditorError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": str(exc)}}))
        return 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": {"code": "INVALID_INPUT", "message": str(exc)}}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
