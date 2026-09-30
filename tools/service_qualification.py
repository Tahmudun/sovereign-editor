"""SERVICE-01/02 and field-authority qualification of the pinned ROM (read-only evidence).

Records: the special-mart table/literal and the 30 stock lists (all in overlay 131 in this
build), the stock clerk/heal script shapes, the spawn table, the badge each native field
action checks (field_moves.qualify_field_authority) and a CPU run of the ROM's own
special-mart lookup for every stock index (tools/mart_qualification.py).

Usage: service_qualification.py PROJECT OUT.json
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools')]
from sovereign_editor.core import Project  # noqa: E402
from sovereign_editor import field_services as fs, field_moves as fm, script_disasm as sd  # noqa: E402
from sovereign_editor import dialogue_format as fmt, character_runtime as cr  # noqa: E402
from sovereign_editor.formats import digest  # noqa: E402
import mart_qualification as mq  # noqa: E402


def main(project_root, out):
    p = Project(project_root)
    blob = p.blob
    checks = []

    def check(name, ok, detail=None):
        checks.append({'check': name, 'pass': bool(ok), **({'detail': detail} if detail is not None else {})})

    fs.qualify_runtime(blob)
    stock = fs.stock_marts(blob)
    field = cr.overlay(blob, 131)
    in131 = all(field['address'] <= m['address'] < field['address'] + len(field['data']) for m in stock)
    check('special-mart literal 0x02048190 -> table 0x0210FA3C', True)
    check('30 stock lists terminated, all in overlay 131', len(stock) == 30 and in131,
          [len(m['items']) for m in stock])
    cpu = mq.run(mq.memory_from_rom(blob), range(30))
    agree = all(r['items'] == [i for i in stock[r['index']]['items'] if r['pokeball_flag'] or i != fs.POKE_BALL]
                for r in cpu)
    check('ROM ScrCmd_SpecialMartBuy + Mart_Init count/copy reproduce every stock list '
          '(Poké Ball dropped while flag 0x9A is clear)', agree)
    raw3 = p.resource(fmt.SCRIPT_ARCHIVE, 3)[1]
    st = sd.entries(raw3)
    heal = sd.listing(raw3, st[69])
    check('std 2069 rest heal: fade, 436, fanfare 1183, heal_party, 150, fade',
          [line.split(': ')[1].split('(')[0] for line in heal] ==
          ['fade_screen', 'wait_fade', 'scrcmd_436', 'play_fanfare', 'wait_fanfare', 'heal_party', 'scrcmd_150',
           'fade_screen', 'wait_fade', 'endstd'], heal)
    mart = sd.listing(raw3, st[52])
    check('std 2052 buy/sell/quit menu calls special_mart_buy(0x8004)', any('special_mart_buy(32772)' in l for l in mart))
    spawns = fs.spawns(blob)
    check('spawn table rows 30; Cherrygrove (2) is a blackout spawn at T21PC0101',
          len(spawns) == 30 and spawns[1]['blackout'] and spawns[1]['map'] == 'T21PC0101')
    authority = fm.qualify_field_authority(blob)
    check('native field-move badge checks and Surf water prompt', True, authority)
    result = {'schema': 'sovereign-service-qualification-v1', 'baseline_sha256': digest(blob),
              'project_revision': p.doc['revision'], 'checks': checks,
              'passed': sum(c['pass'] for c in checks), 'failed': sum(not c['pass'] for c in checks),
              'stock_marts': [{'index': m['index'], 'address': m['address'], 'items': m['items']} for m in stock],
              'spawns': spawns, 'field_authority': authority,
              'native_acceptance': 'not established (software/CPU evidence only)'}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(result, indent=1))
    print(json.dumps({'passed': result['passed'], 'failed': result['failed']}))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
