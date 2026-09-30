# Third-party notices

## Sovereign Editor

Copyright (C) 2026 Tahmudun and the respective contributors.

Sovereign Editor's original tooling and its DSPRE-derived map-format adaptations are distributed under the GNU Affero General Public License, version 3 (AGPL-3.0-only). The complete license is in [LICENSE](LICENSE). This software is provided without warranty, as described in that license. No rights to game content or trademarks are granted.

The work in this repository concerns desktop editing workflows, the shared Project/CLI layer, authoring operations, previews, validation and export tooling. Development has used AI-assisted implementation and review. Upstream components retain their authorship and rights.

## DSPRE

[DS-Pokemon-Rom-Editor/DSPRE](https://github.com/DS-Pokemon-Rom-Editor/DSPRE), reference commit `249a278186d80c35f04485b2c1b612bd81b90a74`, is licensed under AGPL-3.0. Its license and recorded file hashes remain in [references/dspre/](references/dspre/).

The six included C# files are unmodified format references. The Python implementations are adaptations, not unmodified copies: `world.py` identifies the header, matrix, area and map layouts it uses; `scenery.py` identifies the placement layout. Format decoding in `formats.py` and event/map authoring use the recorded reference layouts. Their implementation has been adapted for the editor's validation and transaction model. These adaptations were developed in September 2026. Original notices and attribution are retained.

## hg-engine and game content

[hg-engine](https://github.com/BluRosie/hg-engine) is a separate upstream HeartGold engine project. Sovereign Editor targets one qualified local development build; it does not claim authorship of hg-engine, its battle systems or its assets. The hg-engine/Sovereign Gold source snapshot, derived engine catalogs and game-derived repair images are not distributed here.

The source contains interoperability information such as resource paths, field layouts, numeric identifiers, hashes, offsets and short validation signatures. It reads game resources from locally supplied inputs. No ROM image, save, extracted graphics/model/audio resource, or encoded character asset package is distributed.

The C helper in `sovereign_editor/native/scene_collect.c` and its build script are editor tooling. The generated machine-code payload is excluded; the helper invokes qualified runtime addresses in a user-supplied baseline.

Pokémon game content and Nintendo DS names belong to their respective rights holders. This independent project is not affiliated with or endorsed by Nintendo, Game Freak or The Pokémon Company.

## Dependencies and external tools

Python dependencies are declared in `pyproject.toml`: ndspy, NumPy, Pillow and PySide6 Essentials. They are installed separately and retain their respective licenses and authorship. This source repository does not bundle their distributions. A future packaged app requires its own dependency-notice and distribution checks.

Aseprite is separately installed sprite-editing software. Sprite Forge is separate tooling used in local sprite development. Neither is represented as an original component of Sovereign Editor or bundled here.
