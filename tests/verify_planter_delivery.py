"""Independent M4 delivery verification; read ROM/save inputs, write evidence only."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import sys

import ndspy.narc
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sovereign_editor.formats import file_span

evidence = Path("evidence/m4")
pair = Path("projects/cherrygrove/exports/planter-area-r5")
baseline_path = Path("projects/cherrygrove/baseline.nds")
old_pair = Path("projects/cherrygrove/exports/planter-west-r4")
baseline, candidate = baseline_path.read_bytes(), (pair / "game.nds").read_bytes()
original = (old_pair / "game.nds").read_bytes()

# Absolute offsets were established from the native container/record layout;
# this table does not use the export report or decoration patch generator.
changes = {57611220: (42, 43), 66280427: (128, 0), 66280491: (128, 0),
           66280555: (128, 0), 66280487: (0, 128), 66280551: (0, 128),
           66280615: (0, 128), 66281894: (5, 3), 66281902: (4, 5)}
expected = bytearray(baseline)
for offset, (before, after) in changes.items():
    assert expected[offset] == before
    expected[offset] = after
assert candidate == expected

def changed_bytes(a, b):
    assert len(a) == len(b)
    offsets = np.flatnonzero(np.frombuffer(a, dtype=np.uint8) != np.frombuffer(b, dtype=np.uint8))
    return [{"offset": int(i), "before": a[i], "after": b[i]} for i in offsets]

changed = changed_bytes(baseline, candidate)
versus_m3 = changed_bytes(original, candidate)
assert len(changed) == 9 and len(versus_m3) == 8
before_archive = ndspy.narc.NARC(file_span(baseline, "a/0/6/5")[1])
after_archive = ndspy.narc.NARC(file_span(candidate, "a/0/6/5")[1])
assert [i for i, (a,b) in enumerate(zip(before_archive.files, after_archive.files)) if a != b] == [5]
assert struct.unpack_from("<3i", after_archive.files[5], 2760) == (229376, 65536, 360448)
assert before_archive.files[5][2796:2804] == after_archive.files[5][2796:2804]
save, source_save = (pair / "game.sav").read_bytes(), (old_pair / "game.sav").read_bytes()
assert save == source_save and len(save) == 524288

protected = json.loads((evidence / "preservation-before.json").read_text())
after = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in protected}
assert after == protected
(evidence / "preservation-after.json").write_text(json.dumps(after, indent=2) + "\n")

old_doc = json.loads((evidence / "before/projects/cherrygrove/project.json").read_text())
doc = json.loads(Path("projects/cherrygrove/project.json").read_text())
assert doc["revision"] == old_doc["revision"] + 1 == 5
assert doc["positions"] == old_doc["positions"] == {"1": {"x":555, "z":399}}
assert doc["history"][:-1] == old_doc["history"]
assert doc["history"][-1] == {"operation":"placement.move", "positions":old_doc["positions"], "placement_moves":old_doc["placement_moves"]}
assert doc["placement_moves"]["5:14"]["after"] == {"x":563.5,"z":405.5}
expected_doc = copy.deepcopy(old_doc)
for key in ("revision", "history", "placement_moves"):
    expected_doc[key] = doc[key]
assert doc == expected_doc
report = {"candidate_sha256":hashlib.sha256(candidate).hexdigest(),
          "baseline_sha256":hashlib.sha256(baseline).hexdigest(), "rom_bytes":len(candidate),
          "save_sha256":hashlib.sha256(save).hexdigest(), "save_source":str(old_pair / "game.sav"),
          "unchanged_ordinary_save_copy":True, "all_other_rom_bytes_equal":True,
          "baseline_changes":changed, "m3_changes":versus_m3, "protected_files":len(protected),
          "all_protected_files_equal":True, "legacy_history_and_trainer_preserved":True,
          "native_readback_xyz_fixed":[229376,65536,360448],
          "native_acceptance":"pending for revision 5; guide scene explicitly untested",
          "emulator_automation":False}
(evidence / "delivery-verification.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps({k:v for k,v in report.items() if k not in ("baseline_changes", "m3_changes")}))
