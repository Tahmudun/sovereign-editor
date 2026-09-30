# Sovereign Editor

[![Source checks](https://github.com/Tahmudun/sovereign-editor/actions/workflows/ci.yml/badge.svg)](https://github.com/Tahmudun/sovereign-editor/actions/workflows/ci.yml)

A native macOS world and gameplay editor with a shared Python API and JSON command-line interface for a qualified Nintendo DS HeartGold-based development ROM.

Sovereign Editor brings map layout, terrain, events, dialogue and gameplay data into one editing workflow. It addresses a practical tooling problem: a visual change can span several binary resources, and manual edits can break their relationships. The editor stages those changes as validated transactions, records them in a project, and exports a separate ROM while preserving the baseline input.

**Status:** active development, package version **0.5.0**. The local editor has implemented authoring workflows and user-tested scenarios, with unresolved runtime, visual, capacity and performance issues. It is not a general-purpose editor for every HeartGold/SoulSilver ROM or a finished production release.

**This repository is a source-development snapshot.** You can install the Python package, inspect the CLI and run the synthetic tests below without a ROM. Full game-editing workflows still require a qualified local development baseline and private resources. ROMs, saves, extracted game art and personal references are excluded from this repository and its published history. A self-contained editor demo is planned.

## Implemented features

These features are present in the working source. Support is limited to the resource formats and runtime paths the editor qualifies.

- **Visual map editing:** textured map inspection, neighboring-cell navigation, object selection and placement, collision painting, and staged layout changes with Apply/Cancel and Undo/Redo.
- **World and terrain authoring:** areas built from compatible templates, entrances and paired connections, shaped terrain, caves, water, borders, ground materials and decals.
- **Characters and events:** dialogue, NPC behavior, persistent story state, event sequences, trainers, rewards and reusable field-event presets.
- **Gameplay data:** trainer teams, wild encounters, supported item/move/shop records, travel points and custom Pokémon asset-package bindings.
- **Reusable content:** custom props, map groups, environment definitions, explicit cross-project reuse and dependency checks.
- **Project recovery and export:** checkpoints, package/unpack operations, project validation, revision guards and exports to a new output directory.
- **Scriptable workflows:** the Qt interface and CLI use the same `core.Project` operations. The CLI returns structured JSON and supports inspection, planning, mutation, validation and export. No model subscription or AI service is required to run it.

Preview rendering is an inspection aid. It does not reproduce all game lighting, camera behavior or animation, and a passing preview or automated check does not establish native gameplay correctness.

## Architecture

```text
Qt desktop interface        JSON CLI / automation
          \                    /
             core.Project
       validate → plan → apply
        revision + history + lock
                   |
        project.json + owned assets
                   |
     qualified binary readers/writers
                   |
 immutable baseline → separate ROM export
```

| Layer | Responsibility | Examples |
| --- | --- | --- |
| Interface | Visual editing and structured command requests | `gui.py`, `map_inspector.py`, `world_ui.py`, `cli.py` |
| Project | Write ownership, revision checks, durable state, history and export | `core.py` |
| Authoring | Resource-specific planning and dependency validation | `world_authoring.py`, `terrain_authoring.py`, `scene_authoring.py`, `gameplay.py` |
| Binary formats | Bounded reads, container replacement and qualified runtime integration | `formats.py`, `containers.py`, `nitro.py`, `nitro_writer.py`, `native/` |
| Rendering | Texture decoding, geometry and cached software previews | `mapscene.py`, `raster.py`, `preview.py` |
| Recovery and inspection | Checkpoints, packages, references and coverage reports | `snapshots.py`, `recovery.py`, `workspace.py`, `coverage.py` |

The key engineering constraint is preservation: check before-values and dependencies, reject stale revisions, retain unknown data, and keep authored changes separate from the baseline. UI and CLI edits follow the same transaction boundary.

## Tech stack

- **Python 3.11+**; the documented local development environment uses Python 3.12.4 on Intel macOS.
- **PySide6 Essentials 6.8.3 / Qt 6** for the desktop interface.
- **ndspy 4.1.0** for Nintendo DS formats, archives and compression.
- **NumPy 2.2.6** and **Pillow 11.3.0** for geometry, textures and previews.
- **pytest** for automated checks; small C runtime helpers support selected qualified integrations.
- **Aseprite**, installed separately, for optional editable sprite workflows.

Runtime dependencies are pinned in [pyproject.toml](pyproject.toml); [requirements-lock.txt](requirements-lock.txt) records the local development dependency versions. Apple Silicon, Windows and Linux desktop release support have not been established. The existing Mac packaging workflow produces an unsigned, unnotarized app.

## Development setup

With Python 3.12 available:

```sh
git clone https://github.com/Tahmudun/sovereign-editor.git
cd sovereign-editor
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt
python -m pip install --no-deps -e .
python -m sovereign_editor.cli --version
python -m sovereign_editor.cli --help
python -m pytest -q tests/test_containers.py tests/test_raster.py
```

This installs the editor dependencies. It does **not** supply a game ROM, private preview data or historical test projects. The editor currently validates a specific modified baseline and resource profile; an arbitrary retail dump or another hg-engine build is not a supported substitute.

The public checkout omits the legacy PNG/GLB preview resources, derived engine catalogs, repair/effect images, private character fixtures and generated native-helper payload. Supplying a ROM alone is therefore insufficient to enable every workflow. There is not yet an automatic public resource-bootstrap process.

For an already configured local development environment containing all qualified inputs:

```sh
python -m sovereign_editor.cli create \
  --rom /absolute/path/to/qualified-local-baseline.nds \
  --project projects/my-world --name "My world"
python -m sovereign_editor.cli inspect --project projects/my-world
python -m sovereign_editor.cli ui --project projects/my-world --map-inspector
```

Creation makes a project-owned baseline copy. Authored edits live in `project.json` and project-owned assets. Use the current revision returned by `inspect` for mutation/export commands. Keep local ROMs, saves and projects out of Git.

See [Development and verification](docs/DEVELOPMENT.md) for a small test entry point and the limits of the current setup.

## Current limitations and planned work

The latest recorded native feedback accepts many delivered scenarios but leaves open issues around shop/Fly interactions, waterfall behavior, terrain and environment visuals, and moving effects. New repair code in the working tree does not by itself establish acceptance of a new delivered build.

The next approved production milestone targets:

- Expanded capacity: 256 trainer teams, 64 character definitions and 128 numeric story states, with save continuity. The last recorded measurements were 64, 32 and 60 respectively.
- More complete reusable materials and environment composition, including path edges, slopes and field-traversal presentation.
- Resolution and native verification of the outstanding interaction and rendering issues.
- A cold-open time of at most 10 seconds; the last recorded measurements missed that target.

Public distribution also needs an asset-free bootstrap, synthetic examples and a reproducible test path that does not depend on private projects, and a complete resource-bootstrap workflow. These are planned requirements, not shipped capabilities. There is no claim of DSPRE feature parity, universal ROM compatibility or complete game-engine emulation.

## Project scope and upstream credit

Sovereign Editor is Tahmudun's editor/tooling project: the desktop workflow, shared project/CLI architecture, authoring operations, validation, previews and export tooling are the focus of this repository. Development has used AI-assisted implementation and review; features and verification are described by their actual status.

The underlying game engine, game data, stock art and upstream format research are separate work:

- [hg-engine](https://github.com/BluRosie/hg-engine) is an upstream HeartGold engine project. Sovereign Editor targets a qualified local development build; it does not claim authorship of hg-engine or its battle systems.
- [DSPRE](https://github.com/DS-Pokemon-Rom-Editor/DSPRE) supplies upstream format references. Parts of the map-format implementation explicitly identify DSPRE adaptation, and the reference snapshot carries its AGPL-3.0 license.
- Sprite Forge and Aseprite are separate sprite tools used in the development workflow, not components authored by this repository.
- Pokémon game content belongs to its respective rights holders. This is an independent, unofficial project.

The editor source is distributed under the [GNU Affero General Public License v3.0](LICENSE), with upstream rights and notices retained. See [Third-party notices](THIRD_PARTY_NOTICES.md). The license does not grant rights to game data, assets or trademarks.

The published history retains the eight original development checkpoints after removing excluded material; publication commit IDs differ from the private originals.
