"""Aseprite document interchange; sprite synthesis belongs to Sprite Forge."""
import json
import subprocess
from pathlib import Path

from .formats import require

DEFAULT = Path("/Applications/Aseprite.app/Contents/MacOS/aseprite")


def doctor(executable=DEFAULT):
    executable = Path(executable)
    require(executable.is_file(), "Aseprite executable not found", "ASEPRITE_UNAVAILABLE")
    result = subprocess.run([str(executable), "--version"], capture_output=True, text=True, timeout=20)
    require(result.returncode == 0, "Aseprite could not run. The host may require execution approval.", "ASEPRITE_UNAVAILABLE")
    return {"executable": str(executable), "version": result.stdout.strip(), "bridge": "Aseprite CLI; Sprite Forge handles character templates and continuity"}


def export_sheet(source, output, executable=DEFAULT):
    source, output = Path(source).resolve(), Path(output).resolve()
    require(source.is_file() and source.suffix.lower() in (".ase", ".aseprite"), "Choose an Aseprite source document")
    require(not output.exists(), "Choose a new sprite export folder", "EXISTS")
    doctor(executable)
    output.mkdir(parents=True, exist_ok=False)
    result = subprocess.run([str(executable), "--batch", str(source), "--sheet", str(output / "sheet.png"),
                             "--data", str(output / "frames.json"), "--format", "json-array", "--list-tags",
                             "--sheet-type", "horizontal"], capture_output=True, text=True, timeout=60)
    require(result.returncode == 0 and (output / "sheet.png").is_file() and (output / "frames.json").is_file(),
            f"Aseprite export failed: {result.stderr[:300]}", "ASEPRITE_EXPORT_FAILED")
    metadata = json.loads((output / "frames.json").read_text())
    return {"source": str(source), "output": str(output), "frames": len(metadata["frames"]),
            "tags": metadata.get("meta", {}).get("frameTags", []), "rom_import": "not implemented"}

