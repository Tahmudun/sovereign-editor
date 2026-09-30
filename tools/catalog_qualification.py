"""Engine-code evidence for expanded abilities and hold effects (DATA-03). Not native acceptance.

The pinned ROM's overlays 129/131 are byte-identical to the sibling engine build
(tools/trainer_party_qualification.py), so the engine source that produced them is the
implementation reference for entries the stock Gen 4 code does not know:

* abilities 1..123 are implemented by the stock battle code; expanded abilities (124+)
  are qualified when the engine's C/asm code references their constant (count and files
  recorded), otherwise refused as "no engine implementation reference";
* hold effects used by base items (1..536) are stock; others are qualified the same way.

Writes sovereign_editor/assets/engine-catalog.json (with the hashes of every scanned file
set). Usage: catalog_qualification.py
"""
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT.parent / 'sovereign-gold'
OUT = ROOT / 'sovereign_editor/assets/engine-catalog.json'
STOCK_ABILITIES = 123


def constants(path, prefix):
    table = {}
    for line in path.read_text().splitlines():
        m = re.match(rf'#define ({prefix}\w+)\s+\(?(\d+)', line)
        if m:
            table[m.group(1)] = int(m.group(2))
    return table


def references(names):
    files = sorted(p for p in (ENGINE / 'src').rglob('*.c')) + sorted(p for p in (ENGINE / 'asm').rglob('*.s'))
    digest = hashlib.sha256()
    counts, where = defaultdict(int), defaultdict(set)
    patterns = {n: re.compile(r'\b' + n + r'\b') for n in names}
    for path in files:
        text = path.read_text(errors='ignore')
        digest.update(str(path.relative_to(ENGINE)).encode() + b'\0' + text.encode('utf-8', 'replace'))
        for n, pat in patterns.items():
            hits = len(pat.findall(text))
            if hits:
                counts[n] += hits
                where[n].add(str(path.relative_to(ENGINE)))
    return counts, where, digest.hexdigest(), len(files)


def main():
    sys.path.insert(0, str(ROOT))
    from sovereign_editor.core import Project
    from sovereign_editor.formats import resource
    import ndspy.narc
    from sovereign_editor.formats import file_span
    project = Project(ROOT / 'projects/assets-gameplay-v1')
    abilities = constants(ENGINE / 'include/constants/ability.h', 'ABILITY_')
    holds = constants(ENGINE / 'include/constants/hold_item_effects.h', 'HOLD_EFFECT_')
    counts, where, source_sha, scanned = references(list(abilities) + list(holds))
    items = ndspy.narc.NARC(bytes(file_span(project.blob, 'a/0/1/7')[1])).files
    stock_holds = {items[i][2] for i in range(1, 537) if len(items[i]) == 36}
    rows_a = {}
    for name, value in sorted(abilities.items(), key=lambda kv: kv[1]):
        if value == 0:
            continue
        stock = value <= STOCK_ABILITIES
        rows_a[value] = {'constant': name, 'stock': stock, 'references': counts[name],
                         'files': sorted(where[name])[:6],
                         'qualified': stock or counts[name] > 0,
                         'reason': 'stock Gen 4 battle code' if stock else
                         (f'engine code references it ({counts[name]}x)' if counts[name] else
                          'no engine implementation reference')}
    rows_h = {}
    for name, value in sorted(holds.items(), key=lambda kv: kv[1]):
        if value == 0:
            continue
        stock = value in stock_holds
        rows_h[value] = {'constant': name, 'stock': stock, 'references': counts[name],
                         'qualified': stock or counts[name] > 0,
                         'reason': 'used by a stock item' if stock else
                         (f'engine code references it ({counts[name]}x)' if counts[name] else
                          'no engine implementation reference')}
    report = {'source': 'sibling sovereign-gold engine build (overlays 129/131 byte-identical to the pinned ROM)',
              'scanned_files': scanned, 'source_sha256': source_sha,
              'ability_header_sha256': hashlib.sha256((ENGINE / 'include/constants/ability.h').read_bytes()).hexdigest(),
              'hold_header_sha256': hashlib.sha256((ENGINE / 'include/constants/hold_item_effects.h').read_bytes()).hexdigest(),
              'note': 'Source-reference evidence, not a per-entry battle test.',
              'abilities': rows_a, 'hold_effects': rows_h}
    OUT.write_text(json.dumps(report, separators=(',', ':')))
    print(json.dumps({'abilities': len(rows_a), 'abilities_qualified': sum(r['qualified'] for r in rows_a.values()),
                      'hold_effects': len(rows_h), 'holds_qualified': sum(r['qualified'] for r in rows_h.values()),
                      'scanned_files': scanned}))


if __name__ == '__main__':
    main()
