"""Game-camera software review of custom props. Evidence only, NOT native acceptance.

Renders a map context's terrain and placed models from the pinned field camera
(ov01 camera table entry 0: elevation 48.66 deg, 1 px per world unit at the target;
the 16 deg perspective is approximated orthographically) as 256x192 DS frames, with
an optional player stand-in drawn as a camera-facing quad moved 8 units toward the
camera (the sprite depth bias of ov01_021E6220). Lighting, fog, sprites, weather and
animation are not emulated.

    prop_review.py PROJECT OUTDIR
"""
import json
import math
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sovereign_editor.prop_camera import W, H, PITCH, scene_prims, stand_in, frame  # noqa: E402

def review(project, out, header=67, cell=(17, 12)):
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    context, description, prims = scene_prims(project, header, list(cell))
    ox, oz = context['origin']
    from sovereign_editor import props
    placed = props.instances(project.composed())
    report = {'camera': {'elevation_degrees': round(math.degrees(PITCH), 3), 'pixels_per_unit': 1, 'projection': 'orthographic approximation'},
              'frames': []}
    for key, inst in sorted(placed.items()):
        if inst['context']['cell'] != list(cell):
            continue
        tile = (math.floor(inst['x']) - ox, math.floor(inst['z']) - oz)
        views = {'centre': (tile, None), 'edge_left': ((tile[0] + 6, tile[1]), None), 'edge_top': ((tile[0], tile[1] + 4), None),
                 'north_behind': (tile, (tile[0], tile[1] - 1)), 'north2_behind': (tile, (tile[0], tile[1] - 2)),
                 'south_front': (tile, (tile[0], tile[1] + 1)), 'east_side': (tile, (tile[0] + 2, tile[1])),
                 'west_side': (tile, (tile[0] - 2, tile[1]))}
        for name, (focus, player) in views.items():
            extra = [stand_in(player, inst['height'])] if player else []
            img = frame(prims + extra, focus, inst['height'])
            path = out / f'{key}-{name}.png'
            img.resize((W * 3, H * 3), Image.NEAREST).save(path)
            report['frames'].append({'instance': key, 'view': name, 'focus_tile': [focus[0] + ox, focus[1] + oz],
                                     'stand_in_tile': [player[0] + ox, player[1] + oz] if player else None, 'image': path.name})
    (out / 'review.json').write_text(json.dumps(report, indent=1) + '\n')
    return report


if __name__ == '__main__':
    from sovereign_editor.core import Project
    r = review(Project(sys.argv[1]), sys.argv[2])
    print(json.dumps({'frames': len(r['frames'])}))
