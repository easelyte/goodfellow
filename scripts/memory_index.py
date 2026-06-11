#!/usr/bin/env python3
"""Goodfellow rich memory backend — per-fact files + regenerated index.

Canonical layout (gf_root = the `.goodfellow/` ROOT directory):
- facts:       gf_root/memory/*.md          (one fact per file, frontmatter + body)
- index:       gf_root/MEMORY.md            (top-level, regenerated; never hand-edited)
- registries:  gf_root/memory/domains/<domain>.md
- sentinels:   gf_root/memory/.dirty, gf_root/memory/.migrating
- lock:        gf_root/memory/.lock         (flock, per-fd, NON-reentrant)

Writes are atomic (same-dir temp + fsync + os.replace), serialized by a single
`memory_lock(gf_root)` acquired ONCE at the top of each mutation. flock is per-fd
and re-acquiring from the same process DEADLOCKS, so inner helpers receive
`_lock_held=True` and never re-enter the lock context.

Diagnostics go to STDERR; data (read-index) goes to STDOUT — so a skill reading
the index never mixes WARN lines into memory content (V7).
"""

import os
import re
import sys
import pathlib

try:
    import fcntl

    _HAS_FLOCK = True
except ImportError:
    _HAS_FLOCK = False

REQUIRED = ("name", "description", "type", "status", "opened")
TYPES = ("principle", "pattern", "gotcha")
STATUS = ("pending", "confirmed")
DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

_TYPE_HEADINGS = [
    ("principle", "## Principles"),
    ("pattern", "## Patterns"),
    ("gotcha", "## Gotchas"),
]


class SchemaError(ValueError): ...


class ConfigError(ValueError): ...


def warn_kb():
    """Validated GOODFELLOW_MEMORY_WARN_KB (CM-R4-1: lives HERE in memory_index,
    not memory_config — regenerate() needs it, and putting it here removes the
    forward dependency that made the T-2.2-before-T-2.4 body order a trap).
    unset/empty -> 16; positive int -> that; else ConfigError. memory_config
    re-exports this for symmetry (memory_config -> memory_index, no cycle)."""
    raw = os.environ.get("GOODFELLOW_MEMORY_WARN_KB")
    if raw is None or raw == "":
        return 16
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    raise ConfigError(
        f"GOODFELLOW_MEMORY_WARN_KB must be a positive integer (got: {raw!r})"
    )


# --------------------------------------------------------------------------- #
# Atomic write — same-dir temp + fsync + os.replace (V5 / MN-R6-1)
# --------------------------------------------------------------------------- #
def _atomic_write(path, text):
    """All-or-nothing publish. Temp file created in the SAME directory as the
    target so os.replace stays intra-filesystem and truly atomic; a /tmp temp
    could cross a mount boundary and degrade to copy+delete."""
    import tempfile

    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Frontmatter parse + schema validation
# --------------------------------------------------------------------------- #
def _parse_frontmatter(path):
    text = pathlib.Path(path).read_text()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm


def _body(path):
    text = pathlib.Path(path).read_text()
    m = re.match(r"^---\n.*?\n---\n(.*)$", text, re.S)
    return m.group(1) if m else text


def validate_fact(path):
    fm = _parse_frontmatter(path)
    if not fm or any(k not in fm for k in REQUIRED):
        return False
    if fm["type"] not in TYPES or fm["status"] not in STATUS:
        return False
    if "domain" in fm and not DOMAIN_RE.match(fm["domain"]):
        return False
    return True


# --------------------------------------------------------------------------- #
# Index regeneration (returns text; caller writes it — MN-R3-1)
# --------------------------------------------------------------------------- #
def _index_line(fm):
    base = f"- {fm['_name']} — {fm.get('description', '')}"
    if fm.get("status") == "pending":
        return f"- (pending) {fm['_name']} — {fm.get('description', '')}"
    return base


def _render(facts, memory_dir):
    """Build MEMORY.md text grouped by taxonomy; pending under its own heading.
    Also (re)writes domain registries under memory_dir/domains/."""
    confirmed = [f for f in facts if f.get("status") == "confirmed"]
    pending = [f for f in facts if f.get("status") == "pending"]

    lines = ["# Goodfellow memory index", ""]
    for type_key, heading in _TYPE_HEADINGS:
        group = sorted(
            (f for f in confirmed if f.get("type") == type_key),
            key=lambda f: f["_name"],
        )
        if not group:
            continue
        lines.append(heading)
        for f in group:
            lines.append(_index_line(f))
        lines.append("")

    if pending:
        lines.append("## Pending (unconfirmed)")
        for f in sorted(pending, key=lambda f: f["_name"]):
            lines.append(_index_line(f))
        lines.append("")

    _write_domain_registries(facts, memory_dir)

    return "\n".join(lines).rstrip() + "\n"


def _write_domain_registries(facts, memory_dir):
    by_domain = {}
    for f in facts:
        dom = f.get("domain")
        if dom and DOMAIN_RE.match(dom):
            by_domain.setdefault(dom, []).append(f)
    if not by_domain:
        return
    domains_dir = pathlib.Path(memory_dir) / "domains"
    domains_dir.mkdir(parents=True, exist_ok=True)
    for dom, group in by_domain.items():
        lines = [f"# Domain: {dom}", ""]
        for f in sorted(group, key=lambda f: f["_name"]):
            lines.append(_index_line(f))
        _atomic_write(domains_dir / f"{dom}.md", "\n".join(lines).rstrip() + "\n")


def regenerate(memory_dir):
    """Return the MEMORY.md text for facts in memory_dir.

    Malformed files are SKIPPED with a stderr warning, never crash (P26 / CB-R6-2).
    V7: ALL diagnostics go to stderr; the return value is the consumable index API.
    No forward import of memory_config (CM-R4-1)."""
    memory_dir = pathlib.Path(memory_dir)
    facts = []
    for p in sorted(memory_dir.glob("*.md")):
        if p.name.startswith("."):  # .dirty / .migrating / .lock sentinels
            continue
        if not validate_fact(p):
            print(
                f"WARN memory_index: skipping malformed fact {p.name}",
                file=sys.stderr,
                flush=True,
            )
            continue
        facts.append(_parse_frontmatter(p) | {"_name": p.stem})
    text = _render(facts, memory_dir)
    if len(text.encode()) > warn_kb() * 1024:
        print(
            f"WARN memory_index: index {len(text) // 1024}KB exceeds warn "
            f"threshold; run /goodfellow:triage",
            file=sys.stderr,
            flush=True,
        )
    return text


# --------------------------------------------------------------------------- #
# Cross-process lock — flock (per-fd, NON-reentrant). Acquire ONCE per mutation.
# --------------------------------------------------------------------------- #
import contextlib


@contextlib.contextmanager
def memory_lock(gf_root):
    """Exclusive cross-process lock on gf_root/memory/.lock.

    flock is per-open-file-description and NON-reentrant: a same-process
    re-acquire DEADLOCKS (it does NOT silently no-op — that is lockf). So this
    is acquired EXACTLY ONCE at the top of each mutation; inner helpers take
    `_lock_held=True` and never re-enter. On Windows (no fcntl) locking is
    skipped — the existing single-session caveat applies (matches loop_store)."""
    mem = pathlib.Path(gf_root) / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    if not _HAS_FLOCK:
        yield
        return
    lock_path = mem / ".lock"
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


# --------------------------------------------------------------------------- #
# MemoryStore — atomic + locked + transactional write API
# --------------------------------------------------------------------------- #
class MemoryStore:
    """Operates on the .goodfellow ROOT (gf_root). Facts live under
    gf_root/memory/; the index at gf_root/MEMORY.md (top-level)."""

    def __init__(self, gf_root):
        self.gf_root = pathlib.Path(gf_root)
        self.memory_dir = self.gf_root / "memory"
        self.index_path = self.gf_root / "MEMORY.md"
        self.dirty_path = self.memory_dir / ".dirty"
        self.migrating_path = self.memory_dir / ".migrating"
        self.knowledge_path = self.gf_root / "knowledge.md"

    # -- internal: regenerate + publish under a held lock ------------------- #
    def _regenerate_locked(self):
        """Caller MUST hold memory_lock. Per-fact files already on disk; build
        the index and publish atomically. On regen/publish failure leave a
        .dirty marker and re-raise."""
        try:
            text = regenerate(self.memory_dir)
            _atomic_write(self.index_path, text)
        except Exception:
            try:
                self.dirty_path.write_text("")
            except OSError:
                pass
            raise
        # success: clear any stale dirty marker
        if self.dirty_path.exists():
            try:
                self.dirty_path.unlink()
            except OSError:
                pass
        return text

    def _write_fact_file(
        self, *, name, description, type, status, opened, domain=None, body=""
    ):
        if type not in TYPES:
            raise ValueError(f"type must be one of {TYPES} (got: {type!r})")
        if status not in STATUS:
            raise ValueError(f"status must be one of {STATUS} (got: {status!r})")
        if domain is not None and domain != "" and not DOMAIN_RE.match(domain):
            raise ValueError(f"domain must match {DOMAIN_RE.pattern} (got: {domain!r})")
        fm = {
            "name": name,
            "description": description,
            "type": type,
            "status": status,
            "opened": opened,
        }
        if domain:
            fm["domain"] = domain
        fm_text = "\n".join(f"{k}: {v}" for k, v in fm.items())
        text = f"---\n{fm_text}\n---\n{body}\n"
        _atomic_write(self.memory_dir / f"{name}.md", text)

    # -- public mutations (each acquires the lock ONCE) --------------------- #
    def write_fact(
        self, *, name, description, type, status, opened, domain=None, body=""
    ):
        with memory_lock(self.gf_root):
            self._maybe_auto_migrate_locked()
            self._write_fact_file(
                name=name,
                description=description,
                type=type,
                status=status,
                opened=opened,
                domain=domain,
                body=body,
            )
            self._regenerate_locked()

    def promote(self, name):
        """Flip status: pending -> confirmed for a per-fact file."""
        with memory_lock(self.gf_root):
            path = self.memory_dir / f"{name}.md"
            text = path.read_text()
            new = re.sub(r"(?m)^status:\s*pending\s*$", "status: confirmed", text)
            _atomic_write(path, new)
            self._regenerate_locked()

    def regenerate(self):
        with memory_lock(self.gf_root):
            return self._regenerate_locked()

    def _maybe_auto_migrate_locked(self):
        """Placeholder hook filled in by T-2.6 (migrate). Defined here so the
        write path is migration-aware once T-2.6 lands; in T-2.3 it is a no-op."""
        return

    # -- read path (ordered fallback) --------------------------------------- #
    def _is_stale(self):
        if self.dirty_path.exists():
            return True
        if not self.index_path.exists():
            return False  # absence handled separately
        idx_mtime = self.index_path.stat().st_mtime
        for p in self.memory_dir.glob("*.md"):
            if p.name.startswith("."):
                continue
            if p.stat().st_mtime > idx_mtime:
                return True
        return False

    def read_index_recovering_stale(self):
        """Ordered read contract (CB-R5-3):
        1. .migrating present -> read knowledge.md, do NOT regenerate.
        2. else MEMORY.md absent -> read knowledge.md.
        3. else .dirty/stale -> regenerate under lock (re-check after acquire).
        4. else -> read the index."""
        if self.migrating_path.exists():
            return self._read_knowledge_fallback()
        if not self.index_path.exists():
            return self._read_knowledge_fallback()
        if self._is_stale():
            with memory_lock(self.gf_root):
                # re-check after acquiring (a concurrent writer may have fixed it)
                if self.migrating_path.exists():
                    return self._read_knowledge_fallback()
                if self._is_stale():
                    return self._regenerate_locked()
            return self.index_path.read_text()
        return self.index_path.read_text()

    def _read_knowledge_fallback(self):
        if self.knowledge_path.exists():
            return self.knowledge_path.read_text()
        return ""


# --------------------------------------------------------------------------- #
# CLI — skills invoke via CLI (loop_store.py pattern, PM1)
# --------------------------------------------------------------------------- #
def _build_parser():
    import argparse

    p = argparse.ArgumentParser(description="Goodfellow rich memory backend")
    p.add_argument("--root", required=True, help="the .goodfellow ROOT directory")
    sub = p.add_subparsers(dest="cmd", required=True)

    wf = sub.add_parser("write-fact")
    wf.add_argument("--name", required=True)
    wf.add_argument("--description", required=True)
    wf.add_argument("--type", required=True)
    wf.add_argument("--status", required=True)
    wf.add_argument("--opened", required=True)
    wf.add_argument("--domain", default=None)
    wf.add_argument("--body", default="")

    sub.add_parser("read-index")

    pr = sub.add_parser("promote")
    pr.add_argument("--name", required=True)

    sub.add_parser("regenerate")

    sub.add_parser("migrate")
    return p


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    store = MemoryStore(args.root)
    try:
        if args.cmd == "write-fact":
            store.write_fact(
                name=args.name,
                description=args.description,
                type=args.type,
                status=args.status,
                opened=args.opened,
                domain=args.domain,
                body=args.body,
            )
        elif args.cmd == "read-index":
            # data -> stdout (V7); diagnostics already went to stderr
            sys.stdout.write(store.read_index_recovering_stale())
        elif args.cmd == "promote":
            store.promote(args.name)
        elif args.cmd == "regenerate":
            store.regenerate()
        elif args.cmd == "migrate":
            migrate(args.root)
    except (ConfigError, SchemaError, ValueError) as e:
        print(str(e), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
