"""Replay snapshots recorded from real processes (see record_fixtures.py) through classify()."""
import glob
import json
import os

import pytest

import whystuck

FILES = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "fixtures", "recorded", "*.json")))


def test_recorded_fixtures_exist():
    assert len(FILES) >= 8


@pytest.mark.parametrize("path", FILES, ids=[os.path.basename(p)[:-5] for p in FILES])
def test_recorded_snapshot(path):
    with open(path) as f:
        rec = json.load(f)
    v = whystuck.classify(rec["snapshot"])
    assert v.id == rec["expect"]
    assert rec["snapshot"]["processes"], "recorded from a real process tree"
