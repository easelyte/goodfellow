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


def test_add_loop_rejects_invalid_priority():
    with tempfile.TemporaryDirectory() as d:
        for bad in ("p5", "P1", "high", "", "1"):
            try:
                loop_store.add_loop("Bad pri", priority=bad, project_root=d)
                assert False, f"Should have rejected priority {bad!r}"
            except ValueError as e:
                assert "priority" in str(e).lower()


def test_add_loop_accepts_valid_priorities():
    with tempfile.TemporaryDirectory() as d:
        for good in loop_store.VALID_PRIORITIES:
            lid = loop_store.add_loop("Good pri", priority=good, project_root=d)
            assert loop_store.get_loop(lid, project_root=d)["priority"] == good


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


def test_add_loop_mints_uuid():
    with tempfile.TemporaryDirectory() as d:
        lid = loop_store.add_loop("Has uuid", project_root=d)
        loop = loop_store.get_loop(lid, project_root=d)
        u = loop["uuid"]
        # uuid4 hex is 32 lowercase hex chars.
        assert isinstance(u, str) and len(u) == 32
        int(u, 16)  # parses as hex or raises


def test_uuids_unique_within_store():
    with tempfile.TemporaryDirectory() as d:
        for _ in range(10):
            loop_store.add_loop("dup title", project_root=d)
        uuids = {l["uuid"] for l in loop_store.list_loops(project_root=d)}
        assert len(uuids) == 10


def test_reset_store_ids_dont_alias():
    """Aliasing repro: after a store reset the sequential id restarts at 1, but
    the durable uuid must differ so a reference to the first loop is never
    silently satisfied by the second incarnation."""
    with tempfile.TemporaryDirectory() as d:
        lid1 = loop_store.add_loop("First incarnation", project_root=d)
        uuid1 = loop_store.get_loop(lid1, project_root=d)["uuid"]

        # Reset the store: delete loops.json so next_id restarts at 1.
        loop_store._loops_path(d).unlink()

        lid2 = loop_store.add_loop("Second incarnation", project_root=d)
        uuid2 = loop_store.get_loop(lid2, project_root=d)["uuid"]

        # Sequential ids collide across the reset...
        assert lid1 == lid2 == 1
        # ...but the durable identities must not.
        assert uuid1 != uuid2


def test_old_loops_json_without_uuid_still_loads():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / ".goodfellow" / "loops.json"
        path.parent.mkdir(parents=True)
        # Legacy store shape: no "uuid" on the loop.
        legacy = {
            "loops": [
                {
                    "id": 1,
                    "title": "Legacy loop",
                    "status": "open",
                    "priority": "p3",
                    "source": None,
                    "opened": "2020-01-01",
                    "description": "",
                    "tags": [],
                    "owner": "operator",
                    "next_action": "",
                    "triage_count": 0,
                    "last_triaged": None,
                }
            ],
            "next_id": 2,
        }
        path.write_text(json.dumps(legacy))
        loops = loop_store.list_loops(project_root=d)
        assert len(loops) == 1
        assert loops[0]["title"] == "Legacy loop"
        assert loop_store.get_loop(1, project_root=d)["title"] == "Legacy loop"
        # A new loop added alongside legacy ones still gets a durable uuid.
        lid = loop_store.add_loop("New alongside legacy", project_root=d)
        assert "uuid" in loop_store.get_loop(lid, project_root=d)


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
