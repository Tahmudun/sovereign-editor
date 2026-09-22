#!/usr/bin/env python3
"""Compile the accepted Tiana sources without changing art or game source.

Uses task-local native builds of the existing game converters. The resulting
portable package is input to Project's character import, never a direct ROM edit.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--game', type=Path, required=True)
    parser.add_argument('--converters', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    game = args.game.resolve()
    converters = args.converters.resolve()
    sources = {}

    def source(path, dest):
        raw = path.read_bytes()
        sources[str(path.relative_to(game))] = hashlib.sha256(raw).hexdigest()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        return dest

    def run(command):
        result = subprocess.run([str(v) for v in command], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)

    def encode(path):
        return base64.b64encode(path.read_bytes()).decode('ascii')

    ow = output / 'overworld'
    ow.mkdir()
    for name in ('tiana.png', 'tiana.json', 'tiana-shopw1.pal'):
        source(game / 'data/graphics/overworlds/custom' / name, ow / name)
    run([converters / 'btx/btx', ow / 'tiana.png', ow / 'tiana.btx0'])
    package = {'schema': 'sovereign-character-package-v1', 'name': 'Tiana',
               'gender': 'female', 'overworld': encode(ow / 'tiana.btx0'),
               'overworld_preview': encode(ow / 'tiana.png'),
               'provenance': 'Accepted 0024 overworld, front polish4, HeartGold back Astra2.'}
    home = game / 'data/characters/tiana/trainers'
    for kind, folder, stem in (
            ('front', home / 'front', 'tiana-front'),
            ('back', home / 'back-throw-hg-20260916', 'tiana-back')):
        work = output / kind
        work.mkdir()
        for path in (folder / 'source').glob(stem + '*'):
            source(path, work / path.name)
        gfx = converters / 'nitrogfx/nitrogfx'
        commands = [
            [work / (stem + '.png'), work / '00.NCGR', '-clobbersize', '-version101', '-bitdepth', '4', '-vram', '-mappingtype', '64'],
            [work / (stem + '.png'), work / '01.NCLR', '-ir', '-bitdepth', '4'],
            [work / (stem + '_cell.json'), work / '02.NCER'],
            [work / (stem + '_anim.json'), work / '03.NANR'],
            [work / (stem + '_enc.png'), work / '04.NCGR', '-bitdepth', '4', '-scanned', '-mwidth', '20'],
        ]
        for command in commands:
            run([gfx, *command])
        package[kind] = [encode(work / name) for name in ('00.NCGR', '01.NCLR', '02.NCER', '03.NANR', '04.NCGR')]
        package[kind + '_preview'] = encode(work / (stem + '_enc.png'))
    package['source_sha256'] = sources
    (output / 'tiana.character.json').write_text(json.dumps(package, indent=2) + '\n')
    for relative, expected in sources.items():
        assert hashlib.sha256((game / relative).read_bytes()).hexdigest() == expected
    print(json.dumps({'package': str(output / 'tiana.character.json'),
                      'source_files_unchanged': len(sources),
                      'runtime_assignment': 'pending'}))


if __name__ == '__main__':
    main()
