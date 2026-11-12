"""SPEC §3 rule 2: live_unlock.py is never edited after Phase 1 created it.

The expected hash is the file as committed in Phase 1 (43f2b245c). Only the
human owner may change the file and this hash, together.
"""

import hashlib
from pathlib import Path

import lumisafe

PHASE1_SHA256 = "cec311a4e0886ff31467e13d93cb0d957a954b4f748f8d423e471129fbddd6b3"


def test_live_unlock_is_unchanged_since_phase_1():
    data = (Path(lumisafe.__file__).parent / "live_unlock.py").read_bytes()
    assert hashlib.sha256(data).hexdigest() == PHASE1_SHA256
