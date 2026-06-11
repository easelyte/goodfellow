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
# CB1: fact names are filename components — reject anything that could escape memory/
# (path separators, dot segments, absolute paths). Same charset as migration slugs.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

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
    domains_dir = pathlib.Path(memory_dir) / "domains"
    # CB2: purge stale registries first — a domain whose facts were all deleted/retagged
    # must NOT leave a registry file behind (rich reads auto-pull domain bodies, so a
    # stale registry would re-surface an invalidated learning). Rebuild the dir each regen.
    if domains_dir.exists():
        for stale in domains_dir.glob("*.md"):
            stale.unlink()
    if not by_domain:
        return
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
        if not NAME_RE.match(name or ""):
            raise ValueError(f"name must match {NAME_RE.pattern} (got: {name!r})")
        if type not in TYPES:
            raise ValueError(f"type must be one of {TYPES} (got: {type!r})")
        if status not in STATUS:
            raise ValueError(f"status must be one of {STATUS} (got: {status!r})")
        if domain is not None and domain != "" and not DOMAIN_RE.match(domain):
            raise ValueError(f"domain must match {DOMAIN_RE.pattern} (got: {domain!r})")
        # Frontmatter values are single-line: a newline (or a bare `---`) in a value
        # would produce a malformed file that regenerate() silently skips while
        # write-fact still exits 0 -> the learning vanishes. Reject at write time.
        for _k, _v in (("description", description), ("opened", opened)):
            if "\n" in str(_v) or str(_v).strip() == "---":
                raise ValueError(
                    f"{_k} must be a single line without '---' (got: {_v!r})"
                )
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
        if not NAME_RE.match(name or ""):
            raise ValueError(f"name must match {NAME_RE.pattern} (got: {name!r})")
        with memory_lock(self.gf_root):
            path = self.memory_dir / f"{name}.md"
            text = path.read_text()
            # count=1: only the FIRST (frontmatter) status line — never a `status: pending`
            # line that happens to appear in the body (which would corrupt content).
            new = re.sub(
                r"(?m)^status:\s*pending\s*$", "status: confirmed", text, count=1
            )
            _atomic_write(path, new)
            self._regenerate_locked()

    def delete_fact(self, name):
        """Locked delete + regenerate (CB2). Invalidation must NOT be a raw `rm`
        from skill markdown — that bypasses memory_lock and races concurrent writers
        (a just-written fact could be removed and a regenerate publish without it)."""
        if not NAME_RE.match(name or ""):
            raise ValueError(f"name must match {NAME_RE.pattern} (got: {name!r})")
        with memory_lock(self.gf_root):
            path = self.memory_dir / f"{name}.md"
            if path.exists():
                path.unlink()
            self._regenerate_locked()

    def regenerate(self):
        with memory_lock(self.gf_root):
            return self._regenerate_locked()

    def _maybe_auto_migrate_locked(self):
        """Auto-migrate flat knowledge.md -> rich facts on the first rich write
        (B3). Trigger: knowledge.md non-empty AND (memory/ empty OR .migrating
        present). Runs INSIDE the caller's held lock (_lock_held=True) — must NOT
        re-enter memory_lock (flock re-acquire deadlocks)."""
        if not self.knowledge_path.exists():
            return
        if not self.knowledge_path.read_text().strip():
            return
        has_facts = any(
            not p.name.startswith(".") for p in self.memory_dir.glob("*.md")
        )
        if (not has_facts) or self.migrating_path.exists():
            migrate(self.gf_root, _lock_held=True)

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
        """Ordered read contract (CB-R5-3 + CM-P2R1: absent-index must NOT fall back
        to knowledge.md when rich facts exist or .dirty is set — that would hide
        already-written facts after a first-write index failure):
        1. .migrating present -> read knowledge.md, do NOT regenerate.
        2. index absent AND no facts AND not .dirty -> genuinely-empty rich store ->
           read knowledge.md.
        3. else if index present and fresh -> read the index.
        4. else (.dirty, stale, OR absent-with-facts) -> regenerate under lock
           (re-check after acquire)."""
        if self.migrating_path.exists():
            return self._read_knowledge_fallback()
        has_facts = any(
            not p.name.startswith(".") for p in self.memory_dir.glob("*.md")
        )
        if (
            not self.index_path.exists()
            and not has_facts
            and not self.dirty_path.exists()
        ):
            return self._read_knowledge_fallback()
        if self.index_path.exists() and not self._is_stale():
            return self.index_path.read_text()
        with memory_lock(self.gf_root):
            # re-check after acquiring (a concurrent writer may have published/fixed it)
            if self.migrating_path.exists():
                return self._read_knowledge_fallback()
            if self.index_path.exists() and not self._is_stale():
                return self.index_path.read_text()
            return self._regenerate_locked()

    def _read_knowledge_fallback(self):
        if self.knowledge_path.exists():
            return self.knowledge_path.read_text()
        return ""


# --------------------------------------------------------------------------- #
# Migration (flat knowledge.md -> rich per-fact files) — crash-resumable,
# idempotent, auto on first rich write (T-2.6).
# --------------------------------------------------------------------------- #
import datetime as _dt
import hashlib

_SECTION_TYPE = {
    "principles": "principle",
    "patterns": "pattern",
    "gotchas": "gotcha",
}
_ENTRY_RE = re.compile(r"^-\s+(.*)$")
_PENDING_RE = re.compile(r"^\[pending\]\s*(.*)$")
_ISO_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}):?\s*(.*)$")


def _slugify(text):
    # ASCII-only so the slug always satisfies NAME_RE (M1: `\w` passed Unicode through,
    # producing slugs that promote()/NAME_RE later rejected). Drop non-[a-z0-9] after
    # casefold; collapse whitespace/underscores to hyphens.
    s = re.sub(r"[^a-z0-9\s-]", "", text.casefold())
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s or "fact"


def _source_id(section, date, body, ordinal):
    """Stable identity for crash-resume idempotency. Ordinal makes two
    identical-body source entries distinct (CB-R5-2)."""
    raw = f"{section}\x00{date}\x00{body}\x00{ordinal}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _parse_knowledge_entries(text):
    """Yield dicts {section, type, status, date, body} in deterministic source
    order. Section header sets type; entries before any header default to the
    'principle' bucket (irregular -> reported)."""
    entries = []
    current_section = None  # None => irregular (no header yet)
    for line in text.splitlines():
        stripped = line.strip()
        hm = re.match(r"^#{1,6}\s+(.*)$", stripped)
        if hm:
            current_section = hm.group(1).strip().casefold()
            continue
        em = _ENTRY_RE.match(stripped)
        if not em:
            continue
        rest = em.group(1).strip()
        status = "confirmed"
        pm = _PENDING_RE.match(rest)
        if pm:
            status = "pending"
            rest = pm.group(1).strip()
        date = None
        dm = _ISO_RE.match(rest)
        if dm:
            date = dm.group(1)
            body = dm.group(2).strip()
        else:
            body = rest
        irregular = current_section not in _SECTION_TYPE
        ftype = _SECTION_TYPE.get(current_section, "principle")
        entries.append(
            {
                "section": current_section or "(none)",
                "type": ftype,
                "status": status,
                "date": date,
                "body": body,
                "irregular": irregular,
            }
        )
    return entries


def _existing_source_ids(memory_dir):
    ids = set()
    for p in pathlib.Path(memory_dir).glob("*.md"):
        if p.name.startswith("."):
            continue
        fm = _parse_frontmatter(p)
        if fm and "source_id" in fm:
            ids.add(fm["source_id"])
    return ids


def _migrate_core(gf_root):
    """The actual migration body — caller holds the lock. Idempotent + resumable
    via source_id."""
    gf_root = pathlib.Path(gf_root)
    memory_dir = gf_root / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    knowledge = gf_root / "knowledge.md"
    migrating = memory_dir / ".migrating"
    report_path = gf_root / "migration-report.md"

    text = knowledge.read_text() if knowledge.exists() else ""
    migrating.write_text("")  # sentinel BEFORE first fact write

    run_date = _dt.date.today().isoformat()
    entries = _parse_knowledge_entries(text)
    existing = _existing_source_ids(memory_dir)
    used_slugs = {p.stem for p in memory_dir.glob("*.md") if not p.name.startswith(".")}

    report_irregular = []
    report_slugs = []
    written = 0
    for ordinal, e in enumerate(entries):
        sid = _source_id(e["section"], e["date"] or "", e["body"], ordinal)
        if sid in existing:
            continue  # resume: already written
        opened = e["date"] or run_date
        slug = _slugify(e["body"])
        base = slug
        n = 1
        while slug in used_slugs:
            n += 1
            slug = f"{base}-{n}"
        if slug != base:
            report_slugs.append(f"- `{slug}` (collision on `{base}`)")
        used_slugs.add(slug)
        fm_lines = [
            f"name: {slug}",
            f"description: {e['body'][:200]}",
            f"type: {e['type']}",
            f"status: {e['status']}",
            f"opened: {opened}",
            f"source_id: {sid}",
        ]
        fact_text = "---\n" + "\n".join(fm_lines) + "\n---\n" + e["body"] + "\n"
        _atomic_write(memory_dir / f"{slug}.md", fact_text)
        existing.add(sid)
        written += 1
        if e["irregular"]:
            report_irregular.append(
                f"- `{slug}`: {e['body'][:120]} (defaulted to principle)"
            )

    # regenerate index (in-process; caller holds the lock)
    idx_text = regenerate(memory_dir)
    _atomic_write(gf_root / "MEMORY.md", idx_text)

    # write report OUTSIDE the fact glob
    report = [
        "# Migration report",
        "",
        f"Migrated {written} entries from knowledge.md.",
        "",
    ]
    if report_irregular:
        report += [
            "## Irregular entries (retag candidates — defaulted to type: principle)",
            "",
        ]
        report += report_irregular + [""]
    if report_slugs:
        report += ["## Slug collisions (deterministic suffix applied)", ""]
        report += report_slugs + [""]
    _atomic_write(report_path, "\n".join(report).rstrip() + "\n")
    print(
        f"migrate: wrote {written} facts; report at {report_path}",
        file=sys.stderr,
        flush=True,
    )

    migrating.unlink(missing_ok=True)  # clear sentinel ONLY after full success


def migrate(gf_root, *, _lock_held=False):
    """Convert flat knowledge.md -> rich per-fact files, then regenerate.

    Two call sites (PM2/B1):
    - Standalone CLI: `_lock_held=False` -> acquires memory_lock itself.
    - Auto-migrate from write_fact() (already inside memory_lock):
      `_lock_held=True` -> does NOT re-enter the lock (flock is per-fd; re-acquire
      deadlocks).
    knowledge.md is left in place (non-destructive backup)."""
    if _lock_held:
        _migrate_core(gf_root)
    else:
        with memory_lock(gf_root):
            _migrate_core(gf_root)


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

    df = sub.add_parser("delete-fact")
    df.add_argument("--name", required=True)

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
        elif args.cmd == "delete-fact":
            store.delete_fact(args.name)
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
