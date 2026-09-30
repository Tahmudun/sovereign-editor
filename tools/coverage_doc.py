"""Write docs/EDITOR_V1_COVERAGE.md from sovereign_editor/coverage.py (plus a project's live limits).

Usage: coverage_doc.py [PROJECT]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sovereign_editor import coverage  # noqa: E402

project = None
if len(sys.argv) > 1:
    from sovereign_editor.core import Project
    project = Project(sys.argv[1])
(ROOT / 'docs/EDITOR_V1_COVERAGE.md').write_text(coverage.markdown(project))
print('docs/EDITOR_V1_COVERAGE.md')
