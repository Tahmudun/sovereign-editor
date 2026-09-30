"""Independent readback of the gameplay demonstration in an exported ROM (evidence only).

Reads the exported ROM with the generic format readers and checks, against the project's
composed state: the Snivy item evolution row; the Route 29 grass words (form-packed); the
redefined ordinary trainer's party words; the demo NPC records; and runs the ROM's own
compiled tutor and relearner scripts through the bounded service interpreter of
tests/test_assets_gameplay.py for success, cancel, decline, no-funds and ineligible paths.
It also confirms the demo switch flags are clear in the delivered save. Not native acceptance.

    gameplay_demo_readback.py PROJECT ROM SAVE [OUT.json]
"""
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(ROOT / 'tools')]
from sovereign_editor import dialogue_format as text, gameplay, species as sp, species_forms as sf, story_authoring, world  # noqa: E402
from sovereign_editor.formats import digest, events, resource  # noqa: E402
from test_assets_gameplay import Script  # noqa: E402

DEMO = {'demo_guide': (591, 393, 338), 'demo_tutor': (594, 390, 330), 'demo_relearner': (599, 390, 331), 'demo_kiko': (602, 393, 319)}


def scripts(raw):
    offs, at = [], 0
    while struct.unpack_from('<H', raw, at)[0] != 0xFD13:
        offs.append(at + 4 + struct.unpack_from('<i', raw, at)[0]); at += 4
    return offs


def run(project, rom, save):
    checks = []

    def check(label, ok, detail=None):
        checks.append({'check': label, 'pass': bool(ok), **({'detail': detail} if detail is not None else {})})
    state = project.composed()
    # Evolution row of Snivy (personal index = species for base forms).
    evo = sp.decode_evolutions_v3(resource(rom, 'a/0/3/4', sf.personal_index(project, 545, 0))[1])
    check('Snivy evolution slot 1 = Leaf Stone (item 85) -> Servine', any(
        e['method'] == 'item' and e['param'] == 85 and e['target'] == 546 and e['form'] == 0 for e in evo), evo[:3])
    h = project.header(33)
    wild = gameplay.decode_wild(resource(rom, gameplay.WILD, h['wild_pokemon'])[1])
    want = {2: 263 | 1 << 11, 3: 263 | 1 << 11, 4: 556}
    check('Route 29 grass slots 2,3 = Galarian Zigzagoon, 4 = Lillipup, all times',
          all(wild['grass'][t][s] == w for t in gameplay.TIMES for s, w in want.items()),
          {t: wild['grass'][t][:6] for t in gameplay.TIMES})
    trainer = story_authoring.catalog(state, 'trainer')['practice_17']
    tid = trainer['trainer_id']
    header = resource(rom, gameplay.TRAINERS, tid)[1]; party = resource(rom, gameplay.PARTIES, tid)[1]
    rows = gameplay.decode_team(header, party)
    check(f'trainer {tid} (Kiko) party words carry forms',
          [(r['species'], r['form'], r['level']) for r in rows] == [(27, 1, 12), (263, 1, 12), (881, 0, 13)],
          [(r['species'], r['form'], r['level']) for r in rows])
    seq = story_authoring.catalog(state, 'sequence')
    ctx = seq['demo_tutor']
    npcs = {(n['x'], n['z']): n for n in events(resource(rom, world.EVENT_ARCHIVE, ctx['event_member'])[1])['npcs']}
    raw = resource(rom, text.SCRIPT_ARCHIVE, ctx['script_member'])[1]
    offs = scripts(raw)
    code = {}
    for key, (x, z, sprite) in DEMO.items():
        n = npcs.get((x, z))
        check(f'{key}: NPC record at ({x},{z}) sprite {sprite}', n is not None and n['sprite'] == sprite and n['flag'] == 0,
              n and {k: n[k] for k in ('sprite', 'script', 'flag')})
        if n:
            code[key] = raw[offs[n['script'] - 1]:]
    vulpix = {'species': 37, 'form': 1, 'moves': [39, 52]}
    cases = {'tutor success (Alolan Vulpix, 3104)': ('demo_tutor', [vulpix], 3104, [0, 0, 0], [('charge', 500)], True),
             'tutor cancel learn screen': ('demo_tutor', [vulpix], 3104, [0, 0, 255], [], True),
             'tutor cancel party': ('demo_tutor', [vulpix], 3104, [0, 255], [], False),
             'tutor declined': ('demo_tutor', [vulpix], 3104, [1], [], False),
             'tutor no funds': ('demo_tutor', [vulpix], 499, [0], [], False),
             'tutor Kantonian Vulpix ineligible': ('demo_tutor', [{'species': 37, 'form': 0, 'moves': [39]}], 3104, [0], [], False),
             'tutor already knows Icy Wind': ('demo_tutor', [dict(vulpix, moves=[196])], 3104, [0], [], False),
             'relearner no funds (3104 < 5000)': ('demo_relearner', [dict(vulpix, relearnable=2)], 3104, [0], [], False),
             'relearner success after grant': ('demo_relearner', [dict(vulpix, relearnable=2)], 6104, [0, 0, 0], [('charge', 5000)], True),
             'relearner cancel': ('demo_relearner', [dict(vulpix, relearnable=2)], 6104, [0, 0, 255], [], True),
             'relearner nothing to remember': ('demo_relearner', [dict(vulpix, relearnable=0)], 6104, [0], [], False)}
    for label, (key, party_, money, choices, charged, learn) in cases.items():
        try:
            s = Script(code[key], party_, money, {}, choices).run()
            got = [e for e in s.log if isinstance(e, tuple) and e[0] == 'charge']
            ui = any(isinstance(e, tuple) and e[0] == 'learn_ui' for e in s.log)
            check(f'ROM script: {label}', got == charged and ui == learn and s.money == money - sum(a for _, a in charged),
                  {'charged': got, 'learn_ui': ui})
        except (AssertionError, KeyError, IndexError) as exc:
            check(f'ROM script: {label}', False, str(exc))
    flags = [v['flag'] for k, v in story_authoring.catalog(state, 'state').items() if k.startswith('demo_')]
    clear = True
    for base in (0, 0x40000):
        if struct.unpack_from('<I', save, base + 65440 - 16 + 8)[0] != 0x20060623:
            continue
        at = base + 4052 + 2 * 0x170  # vars 0x4000..0x416F precede the flags (storage_qualification)
        clear &= all(not (save[at + f // 8] >> (f % 8) & 1) for f in flags)
    check('demo switch flags are clear in the delivered save', clear, flags)
    return {'scope': 'independent format readback + bounded script interpretation; not native acceptance',
            'rom_sha256': digest(rom), 'save_sha256': digest(save), 'checks': checks,
            'passed': sum(c['pass'] for c in checks), 'failed': sum(not c['pass'] for c in checks)}


if __name__ == '__main__':
    from sovereign_editor.core import Project
    report = run(Project(sys.argv[1]), Path(sys.argv[2]).read_bytes(), Path(sys.argv[3]).read_bytes())
    if len(sys.argv) > 4:
        Path(sys.argv[4]).write_text(json.dumps(report, indent=1) + '\n')
    print(json.dumps({'passed': report['passed'], 'failed': report['failed']}))
    for c in report['checks']:
        if not c['pass']:
            print('FAIL', c)
