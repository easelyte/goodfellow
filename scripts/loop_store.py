#!/usr/bin/env python3
"""Goodfellow loop store — tracks follow-up work in .goodfellow/loops.json.

File lock covers the entire read-modify-write critical section.
Unix only (fcntl.flock). Windows raises ImportError — document in README.
"""

import fcntl
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

LOOPS_FILE = ".goodfellow/loops.json"


def _loops_path(project_root="."):
    return Path(project_root) / LOOPS_FILE


def _empty_store():
    return {"loops": [], "next_id": 1}


def _read_store(path):
    if not path.exists():
        return _empty_store()
    text = path.read_text()
    if not text.strip():
        return _empty_store()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Corrupt loops.json: {e}") from e


def _write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _locked(func):
    def wrapper(*args, project_root=".", **kwargs):
        path = _loops_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(".lock")
        lock_path.touch(exist_ok=True)
        with open(lock_path, "r") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                return func(*args, project_root=project_root, **kwargs)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

    return wrapper


@_locked
def add_loop(
    title,
    priority="p3",
    source=None,
    description="",
    tags=None,
    owner="operator",
    next_action="",
    project_root=".",
):
    path = _loops_path(project_root)
    store = _read_store(path)
    loop_id = store["next_id"]
    loop = {
        "id": loop_id,
        "title": title,
        "status": "open",
        "priority": priority,
        "source": source,
        "opened": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "description": description,
        "tags": tags or [],
        "owner": owner,
        "next_action": next_action,
        "triage_count": 0,
        "last_triaged": None,
    }
    store["loops"].append(loop)
    store["next_id"] = loop_id + 1
    _write_atomic(path, store)
    return loop_id


@_locked
def close_loop(loop_id, project_root="."):
    path = _loops_path(project_root)
    store = _read_store(path)
    for loop in store["loops"]:
        if loop["id"] == loop_id:
            loop["status"] = "closed"
            _write_atomic(path, store)
            return True
    return False


@_locked
def update_triage(loop_id, triage_count=None, last_triaged=None, project_root="."):
    path = _loops_path(project_root)
    store = _read_store(path)
    for loop in store["loops"]:
        if loop["id"] == loop_id:
            if triage_count is not None:
                loop["triage_count"] = triage_count
            if last_triaged is not None:
                loop["last_triaged"] = last_triaged
            _write_atomic(path, store)
            return True
    return False


def list_loops(status=None, min_age_days=None, project_root="."):
    path = _loops_path(project_root)
    store = _read_store(path)
    loops = store["loops"]
    if status:
        loops = [l for l in loops if l["status"] == status]
    if min_age_days is not None:
        today = datetime.now(timezone.utc).date()
        loops = [
            l
            for l in loops
            if (today - datetime.strptime(l["opened"], "%Y-%m-%d").date()).days
            >= min_age_days
        ]
    return loops


def get_loop(loop_id, project_root="."):
    path = _loops_path(project_root)
    store = _read_store(path)
    for loop in store["loops"]:
        if loop["id"] == loop_id:
            return loop
    return None


def count_open(project_root="."):
    return len(list_loops(status="open", project_root=project_root))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Goodfellow loop store CLI")
    parser.add_argument("--root", default=".", help="Project root")
    sub = parser.add_subparsers(dest="cmd")

    add_p = sub.add_parser("add")
    add_p.add_argument("title")
    add_p.add_argument("--priority", default="p3")
    add_p.add_argument("--source", default=None)
    add_p.add_argument("--description", default="")
    add_p.add_argument("--tags", default="")
    add_p.add_argument("--owner", default="operator")
    add_p.add_argument("--next-action", default="")

    close_p = sub.add_parser("close")
    close_p.add_argument("id", type=int)

    sub.add_parser("list")
    sub.add_parser("stale")
    sub.add_parser("count")

    args = parser.parse_args()
    if args.cmd == "add":
        tags = (
            [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
        )
        lid = add_loop(
            args.title,
            priority=args.priority,
            source=args.source,
            description=args.description,
            tags=tags,
            owner=args.owner,
            next_action=args.next_action,
            project_root=args.root,
        )
        print(f"Added loop #{lid}: {args.title}")
    elif args.cmd == "close":
        if close_loop(args.id, project_root=args.root):
            print(f"Closed loop #{args.id}")
        else:
            print(f"Loop #{args.id} not found", file=sys.stderr)
            sys.exit(1)
    elif args.cmd == "list":
        for l in list_loops(status="open", project_root=args.root):
            print(f"#{l['id']} [{l['priority']}] {l['title']}")
    elif args.cmd == "stale":
        for l in list_loops(status="open", min_age_days=30, project_root=args.root):
            print(f"#{l['id']} [{l['priority']}] {l['title']} (opened {l['opened']})")
    elif args.cmd == "count":
        print(count_open(project_root=args.root))
    else:
        parser.print_help()
