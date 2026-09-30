# Development and verification

## Start with the shared project boundary

`sovereign_editor/core.py` owns project writes. The desktop UI and JSON CLI call the same operations. Resource modules plan and validate changes; they should not create a second mutation path around `Project`.

A project keeps its immutable baseline ROM separate from authored state. Operations check the expected revision and resource dependencies, then record a transaction with history. Export composes a separate candidate; validation and readback check supported resources. Unknown bytes and untouched members should remain preserved.

## Small test entry point

After following the installation commands in the README, run:

```sh
python -m pytest -q tests/test_containers.py tests/test_raster.py
```

These existing tests construct synthetic archive/ROM structures and colored geometry in memory. They exercise container preservation, malformed-data refusal, file relocation, depth, transparency and texture sampling without loading private game fixtures. This is a small test subset, not complete editor verification.

The broader suite includes integration tests tied to local project paths and the qualified baseline. Some skip missing inputs; others require specific historical projects. A clean checkout cannot currently reproduce the complete local integration suite. Do not publish ROMs or historical project packages to make those tests pass.

## Read-only CLI inspection

```sh
python -m sovereign_editor.cli --help
python -m sovereign_editor.cli create --help
python -m sovereign_editor.cli ui --help
```

For an existing qualified project, `inspect`, `validate`, `diff` and the relevant resource queries provide context before editing. Mutations require the current revision. Review a plan before applying it and export to a new destination.

## Verification boundaries

- Format tests and readback establish specific software properties; they do not prove every native runtime path.
- Static previews and reach/progression reports have narrower scope than gameplay.
- Native acceptance is recorded for a particular exported ROM/save pair through user-run melonDS checks.
- Platform support and packaging must be verified separately. The documented release workflow currently targets Intel macOS and remains unsigned.

No full integration run, native emulator session or clean public-install qualification is implied by the small test command above.

## Public snapshot boundary

This public snapshot excludes the legacy game-derived PNG/GLB previews, engine catalogs, repair/effect images, encoded character fixture and generated native helper. The CLI help and synthetic test entry point work without them. Full authoring still needs a reviewed resource-bootstrap workflow or a synthetic demo; supplying an arbitrary ROM does not supply the missing resources. Some integration tests also retain local project assumptions.

The native helper can be rebuilt from `sovereign_editor/native/scene_collect.c` using `tools/build_scene_collect.py`, with an ARM-target-capable clang and `pyelftools` installed separately. The generated JSON payload is ignored. Rebuilding the helper alone does not establish baseline compatibility or complete the missing-resource setup.

Keep game inputs, outputs, extracted assets, art/photo references, local sessions, transfer archives, environments and caches outside the public commit set. The original eight checkpoints were sanitized into this independent publication history; the private originals remain outside the public repository. Ignore rules cannot remove newly tracked files or historical objects. Recheck staged files and all reachable history before later pushes.
