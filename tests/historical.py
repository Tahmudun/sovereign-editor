"""Historical export ROMs that the approved 2026-09-28 storage cleanup removed.

evidence/storage-audit-2026-09-28/deletion-receipt.json lists the 29 generated game.nds files
the user authorized deleting (projects, saves, baselines and sources were kept). Tests that
compared a fresh export against one of them skip that comparison when the file is gone; they
must not recreate it.
"""
from pathlib import Path

import pytest

REMOVED = 'historical export removed by the approved 2026-09-28 storage cleanup'


def exported(path):
    path = Path(path)
    if not path.exists():
        pytest.skip(f'{REMOVED}: {path}')
    return path.read_bytes()
