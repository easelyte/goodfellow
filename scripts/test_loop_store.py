"""Tests for loop_store.py."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import loop_store


def test_add_loop_returns_id():
    with tempfile.TemporaryDirectory() as d:
        lid = loop_store.add_loop("Test loop", project_root=d)
        assert lid == 1
        lid2 = loop_store.add_loop("Second loop", project_root=d)
        assert lid2 == 2


def test_close_loop():
    with tempfile.TemporaryDirectory() as d:
        lid = loop_store.add_loop("Closeable", project_root=d)
        assert loop_store.close_loop(lid, project_root=d)
        loop = loop_store.get_loop(lid, project_root=d)
        assert loop["status"] == "closed"


def test_close_nonexistent():
    with tempfile.TemporaryDirectory() as d:
        assert not loop_store.close_loop(999, project_root=d)


def test_list_by_status():
    with tempfile.TemporaryDirectory() as d:
        loop_store.add_loop("Open one", project_root=d)
        lid2 = loop_store.add_loop("To close", project_root=d)
        loop_store.close_loop(lid2, project_root=d)
        open_loops = loop_store.list_loops(status="open", project_root=d)
        assert len(open_loops) == 1
        assert open_loops[0]["title"] == "Open one"


def test_list_stale():
    with tempfile.TemporaryDirectory() as d:
        loop_store.add_loop("Old loop", project_root=d)
        path = loop_store._loops_path(d)
        store = json.loads(path.read_text())
        store["loops"][0]["opened"] = "2020-01-01"
        path.write_text(json.dumps(store))
        stale = loop_store.list_loops(status="open", min_age_days=30, project_root=d)
        assert len(stale) == 1


def test_auto_creates_missing_file():
    with tempfile.TemporaryDirectory() as d:
        loops = loop_store.list_loops(project_root=d)
        assert loops == []


def test_corrupt_json_raises():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / ".goodfellow" / "loops.json"
        path.parent.mkdir(parents=True)
        path.write_text("{corrupt")
        try:
            loop_store.list_loops(project_root=d)
            assert False, "Should have raised"
        except ValueError as e:
            assert "Corrupt" in str(e)


def test_concurrent_add_distinct_ids():
    with tempfile.TemporaryDirectory() as d:
        script = f"""
import sys
sys.path.insert(0, '{Path(__file__).parent}')
import loop_store
lid = loop_store.add_loop("Concurrent", project_root="{d}")
print(lid)
"""
        procs = [
            subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE)
            for _ in range(5)
        ]
        ids = set()
        for p in procs:
            out, _ = p.communicate()
            ids.add(int(out.strip()))
        assert len(ids) == 5, f"Expected 5 distinct IDs, got {ids}"
