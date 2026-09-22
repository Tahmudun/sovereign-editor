"""Map-context survey: the generic reader resolves contexts beyond the two edited ones.

Nothing here writes; it proves that header/matrix/area resolution and the DSPRE map
section layout hold across town, route and reused-member contexts of the pinned ROM,
so the authoring path is not wired to members 4/5 and header 67.
"""
import os
from pathlib import Path

import pytest

from sovereign_editor import world
from sovereign_editor.formats import map_data, member_count, resource

# header, matrix cell, map member, internal name. Two towns and three routes;
# the real headerless indoor context is covered in test_map_authoring.py.
SURVEY = [(60, [21, 12], 0, "T20"), (67, [16, 12], 4, "T21"), (67, [17, 12], 5, "T21"),
          (33, [18, 12], 1, "R29"), (34, [17, 9], 6, "R30"), (37, [14, 14], 32, "R33")]


@pytest.fixture(scope="module")
def blob():
    source = Path(os.environ.get("SG_TEST_ROM", "projects/cherrygrove/baseline.nds"))
    if not source.is_file():
        pytest.skip("Set SG_TEST_ROM to a qualified local HeartGold ROM")
    return source.read_bytes()


def test_rom_tables_are_read_from_the_rom(blob):
    assert world.header_count(blob) == 540
    assert member_count(blob, world.MATRIX_ARCHIVE) == 288
    assert member_count(blob, world.MAP_ARCHIVE) == 676
    assert member_count(blob, world.AREA_ARCHIVE) == 106
    grid = world.read_matrix(blob, 0)
    assert (grid["width"], grid["height"], grid["name"]) == (47, 17, "map")
    assert grid["has_headers"] and grid["has_altitudes"]


@pytest.mark.parametrize("header,cell,member,name", SURVEY)
def test_survey_contexts_resolve_and_parse(blob, header, cell, member, name):
    context = world.resolve_context(blob, header=header, cell=cell)
    assert context["map_member"] == member and context["header"]["name"] == name
    assert context["origin"] == [cell[0] * 32, cell[1] * 32]
    sections = context["sections"]
    # MapFile.cs invariants: 2048 permission bytes, 48-byte buildings, nothing lost.
    assert sections["permissions_bytes"] == 2048 and sections["buildings_bytes"] % 48 == 0
    assert sections["terrain_offset"] + sections["terrain_bytes"] == sections["total_bytes"]
    raw = resource(blob, world.MAP_ARCHIVE, member)[1]
    collision, props, model = map_data(raw)
    assert len(collision) == 2048 and len(props) == sections["building_count"]
    assert len(model) == sections["model_bytes"] and model[:4] == b"BMD0"
    rows = world.permission_rows(raw, context)
    assert len(rows) == 32 and all(len(row) == 32 for row in rows)
    assert world.cell_offset(context, *context["origin"]) == sections["permissions_offset"]


def test_reused_map_member_is_reported_rather_than_assumed(blob):
    grid = world.read_matrix(blob, 0)
    reused = {c["map_member"] for c in world.matrix_cells(grid)
              if len(world.matrix_cells(grid, c["map_member"])) > 1}
    assert reused, "the overworld matrix reuses at least one map member"
    member = sorted(reused)[0]
    cells = world.matrix_cells(grid, member)
    context = world.resolve_context(blob, matrix=0, cell=cells[0]["cell"])
    assert context["shared_cells"] == [c["cell"] for c in cells]
    assert len(context["shared_cells"]) > 1
