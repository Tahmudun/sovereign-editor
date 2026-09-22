"""Verify the disposable Aseprite fixture's edit/export and animation continuity."""
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image

from sovereign_editor.aseprite import export_sheet

root = Path("evidence/aseprite")
sources = {name: (root / f"{name}.aseprite").read_bytes() for name in ("original", "edited")}
reports = {name: export_sheet(root / f"{name}.aseprite", root / f"{name}-export") for name in sources}
images = [Image.open(root / f"{name}-export/sheet.png") for name in sources]
assert all(image.size == (32, 16) for image in images)
a, b = [np.array(image.convert("RGBA")) for image in images]
mask = np.any(a != b, axis=-1)
assert mask.sum() == 80  # 8 by 5 jacket pixels across two frames.
assert np.all(a[mask] == [91, 183, 149, 255])
assert np.all(b[mask] == [76, 124, 204, 255])
assert np.array_equal(a[..., 3], b[..., 3])
assert np.array_equal(b[:, :16], b[:, 16:])
for name in sources:
    assert (root / f"{name}.aseprite").read_bytes() == sources[name]
    meta = json.loads((root / f"{name}-export/frames.json").read_text())
    assert [f["duration"] for f in meta["frames"]] == [120, 180]
    assert meta["meta"]["frameTags"][0]["name"] == "walk-test"
result = {"passed": True, "frames": 2, "changed_pixels": int(mask.sum()),
          "checks": ["Lua create/edit/save/reopen", "two-frame indexed source", "source preservation",
                     "exact jacket color change", "unchanged transparency", "consistent frame appearance", "frame timing", "animation tag"],
          "source_sha256": {k: hashlib.sha256(v).hexdigest() for k, v in sources.items()},
          "scope": "Synthetic interchange fixture; not an HGSS sprite import or likeness demonstration"}
(root / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
