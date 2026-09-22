"""Record milestone screenshots of the real styled Qt window (run explicitly).

    .venv/bin/python -m pytest tests/record_textured_editor.py -o tmp_path_retention_count=1 \
        -o tmp_path_retention_policy=failed

Not collected by the regression suite: it writes review images into
evidence/map-textured-1/screens, never into an earlier milestone's evidence. The
project is a fresh pytest-scratch copy, so no user project is edited. A screenshot is
visual evidence only, not native melonDS acceptance.
"""
import os
from pathlib import Path

import pytest

from sovereign_editor.core import Project

SCREENS = Path("evidence/map-textured-1/screens")


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    source = Path(os.environ.get("SG_TEST_ROM", "projects/cherrygrove/baseline.nds"))
    if not source.is_file():
        pytest.skip("Set SG_TEST_ROM to a qualified local HeartGold ROM")
    return Project.create(source, tmp_path_factory.mktemp("screens") / "project")


def window_for(project, header, cell, size=(1440, 900)):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.gui import STYLE
    from sovereign_editor.map_inspector import MapInspectorWindow
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = MapInspectorWindow(project.root, context=(header, cell))
    window.resize(*size)
    window.show()
    app.processEvents()
    window.fit_content()
    app.processEvents()
    return app, window


def save(window, app, name):
    SCREENS.mkdir(parents=True, exist_ok=True)
    app.processEvents()
    target = SCREENS / name
    assert window.grab().save(str(target)) and target.stat().st_size > 20000
    return target


def slot_of(window, name):
    return next(p["slot"] for p in window.scene_data["placements"] if p["name"] == name)


def test_record(project):
    app, window = window_for(project, 60, [21, 12])
    save(window, app, "01-town-new-bark-clean-1440x900.png")
    # Selected object plus a staged move and two painted cells, collision visible.
    window.select_placement(13)
    window.drag_placement(13, -1.0, 0.0)
    window.set_tool("unblock")
    window.paint_tile(685 - 672, 400 - 384)
    window.set_tool("block")
    window.paint_tile(684 - 672, 400 - 384)
    window.focus_tile(685, 400, 11)
    save(window, app, "02-new-bark-staged-move-and-cells-1440x900.png")
    window.resize(1180, 760)
    app.processEvents()
    window.focus_tile(685, 400, 11)
    save(window, app, "03-new-bark-staged-compact-1180x760.png")
    window.close()

    app, window = window_for(project, 67, [17, 12])
    window.select_placement(slot_of(window, "pc"))
    save(window, app, "04-town-cherrygrove-selected-center-1440x900.png")
    window.cancel_staged()
    window.set_tool("block")
    save(window, app, "05-cherrygrove-collision-overlay-editing-1440x900.png")
    window.close()

    app, window = window_for(project, 33, [18, 12])
    window.select_placement(0)
    save(window, app, "06-route-29-selected-sign-1440x900.png")
    window.close()

    app, window = window_for(project, 61, [0, 0], size=(1180, 760))
    window.select_placement(slot_of(window, "labo_select"))
    save(window, app, "07-interior-elm-lab-fitted-1180x760.png")
    window.close()
    app.processEvents()
