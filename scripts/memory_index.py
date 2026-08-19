#!/usr/bin/env python3
"""Goodfellow rich memory backend — per-fact files + regenerated index.

Canonical layout (gf_root = the `.goodfellow/` ROOT directory):
- facts:       gf_root/memory/*.md          (one fact per file, frontmatter + body)
- index:       gf_root/MEMORY.md            (top-level, regenerated; never hand-edited)
- registries:  gf_root/memory/domains/<domain>.md
- sentinels:   gf_root/memory/.dirty, gf_root/memory/.migrating
- journal:     gf_root/memory/.journal.jsonl (append-only mutation log; rollback)
- lock:        gf_root/memory/.lock         (flock, per-fd, NON-reentrant)

Each public mutation (write/promote/delete) uses a TWO-PHASE write-ahead log. Phase 1
journals the reversible intent (pre-image + post-image hash) with ``committed: false``
BEFORE touching the fact, under the held lock. Phase 2 flips that entry to
``committed: true`` (in place — no new entry) AFTER the fact + regenerated index have
durably landed. A crash anywhere in between leaves a *dangling intent* (a forward entry
still ``committed: false`` with no matching rollback); `recover()` (also run on
lock-acquiring reads/mutations/rollbacks) resolves it deterministically:
  - fact matches the recorded post-image -> the mutation DID land -> roll FORWARD (mark
    committed);
  - fact is still at the pre-image (write never landed / promote-or-delete never
    applied) -> roll BACK (the fact is already at pre-image, so record a rollback
    marker; no fact op needed);
  - fact is in a third state (a genuine non-journaled edit after the crash) -> leave it
    ``committed: false`` so an explicit `rollback()` surfaces the CAS conflict
    (fail-visible), never silently clobbering newer content.
`rollback()` restores a fact to its pre-image (compare-and-swap on the post-image) and
regenerates. Facts may also carry an optional `evidence:` provenance pointer.

Backward compatibility: a pre-existing journal predates two-phase, so its forward
entries have NO ``committed`` field. Such entries are treated as **committed** (they
already completed before this code shipped) — only an explicit ``committed: false`` is a
dangling intent. The on-disk schema change is purely additive (one optional field), so
existing `.goodfellow` journals load and behave unchanged.

Byte budget (retention): the journal is bounded by entry count AND total bytes, keeping
the NEWEST entries (a suffix). Suffix-keep guarantees the record of the mutation in
flight — always the newest entry — is never dropped, and a rollback marker (always newer
than the forward entry it resolves) is never retained while its forward entry is dropped
in the direction that would resurrect the forward entry as un-resolved. DOCUMENTED
EXCEPTION: a single newest entry whose pre-image alone exceeds the byte budget is still
kept — durability of the in-flight record wins over the bound (facts are soft-capped far
below the 1 MiB default, so this corner is not reachable in normal operation).

Writes are atomic (same-dir temp + fsync + os.replace), serialized by a single
`memory_lock(gf_root)` acquired ONCE at the top of each mutation. flock is per-fd
and re-acquiring from the same process DEADLOCKS, so inner helpers receive
`_lock_held=True` and never re-enter the lock context.

Diagnostics go to STDERR; data (read-index) goes to STDOUT — so a skill reading
the index never mixes WARN lines into memory content (V7).
"""

import hashlib
import json
import os
import re
import sys
import pathlib
from datetime import datetime, timezone

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
# Any line separator str.splitlines() recognizes — a frontmatter value must contain
# NONE of these, else _parse_frontmatter (which uses splitlines) would split it into an
# extra line and inject/overwrite a field (P8). A bare `\n` check is not enough.
_LINE_SEP_RE = re.compile(r"[\n\r\v\f\x1c\x1d\x1e\x85  ]")


def _content_hash(text):
    """Stable hash of a fact file's content, for rollback compare-and-swap."""
    return hashlib.sha256(text.encode()).hexdigest()


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
        self.journal_path = self.memory_dir / ".journal.jsonl"
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

    def _prepare_fact_file(
        self,
        *,
        name,
        description,
        type,
        status,
        opened,
        domain=None,
        evidence=None,
        body="",
    ):
        """Validate + assemble a fact WITHOUT writing it. Returns (final_name, text).
        The caller journals the intent (write-ahead) before publishing the file, so
        this is pure compute — no filesystem mutation."""
        if not NAME_RE.match(name or ""):
            raise ValueError(f"name must match {NAME_RE.pattern} (got: {name!r})")
        if type not in TYPES:
            raise ValueError(f"type must be one of {TYPES} (got: {type!r})")
        if status not in STATUS:
            raise ValueError(f"status must be one of {STATUS} (got: {status!r})")
        if domain is not None and domain != "" and not DOMAIN_RE.match(domain):
            raise ValueError(f"domain must match {DOMAIN_RE.pattern} (got: {domain!r})")
        # Frontmatter values are single-line: ANY line separator (_parse_frontmatter uses
        # splitlines, which also splits on \r, \v, \f, NEL, LS, PS...) or a bare `---`
        # would produce a malformed / injected file that regenerate() silently skips
        # while write-fact still exits 0 -> the learning vanishes. Reject at write time.
        _single_line_checks = [("description", description), ("opened", opened)]
        if evidence is not None and evidence != "":
            _single_line_checks.append(("evidence", evidence))
        for _k, _v in _single_line_checks:
            if _LINE_SEP_RE.search(str(_v)) or str(_v).strip() == "---":
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
        if evidence:
            # Provenance pointer (PR/commit/finding-id/url) — optional, single-line.
            fm["evidence"] = evidence
        if domain:
            fm["domain"] = domain
        # CB1 (R3): never silently overwrite an existing fact. On the first rich write
        # auto-migrate runs first; if the triggering --name collides with a just-migrated
        # slug, overwriting would drop the migrated legacy learning (silent loss). Also
        # guards same-name reuse within a session. Allocate a deterministic suffix like
        # migration does; skills discover actual on-disk names (they iterate the dir), so
        # the suffix is safe.
        final = name
        if (self.memory_dir / f"{final}.md").exists():
            i = 2
            while (self.memory_dir / f"{name}-{i}.md").exists():
                i += 1
            final = f"{name}-{i}"
            fm["name"] = final
        fm_text = "\n".join(f"{k}: {v}" for k, v in fm.items())
        text = f"---\n{fm_text}\n---\n{body}\n"
        return final, text

    # -- public mutations (each acquires the lock ONCE) --------------------- #
    def write_fact(
        self,
        *,
        name,
        description,
        type,
        status,
        opened,
        domain=None,
        evidence=None,
        body="",
    ):
        self._preflight()  # P-019: validate abort-capable config BEFORE mutating,
        # else a bad env var throws only at regenerate() — after the fact is on disk —
        # and a retry would suffix a duplicate (CB R4).
        with memory_lock(self.gf_root):
            self._recover_locked()  # drain a dangling intent from a prior crash first
            self._maybe_auto_migrate_locked()
            final, text = self._prepare_fact_file(
                name=name,
                description=description,
                type=type,
                status=status,
                opened=opened,
                domain=domain,
                evidence=evidence,
                body=body,
            )
            # Two-phase WAL. Phase 1: journal the reversible INTENT (pre-image None for a
            # new file, post-image hash for compare-and-swap) with committed:false BEFORE
            # the fact lands. If journaling fails nothing is written; if the write then
            # fails, the dangling intent is resolved by recovery (rolled back — the fact
            # never landed) instead of wedging on a CAS conflict.
            self._mark_dirty_locked()
            intent = self._journal_locked(
                "write", final, None, post_hash=_content_hash(text), committed=False
            )
            _atomic_write(self.memory_dir / f"{final}.md", text)
            self._regenerate_locked()
            # Phase 2: fact + index durably landed -> mark the intent committed.
            self._mark_committed_locked(intent["seq"])

    def promote(self, name):
        """Flip status: pending -> confirmed for a per-fact file."""
        if not NAME_RE.match(name or ""):
            raise ValueError(f"name must match {NAME_RE.pattern} (got: {name!r})")
        self._preflight()  # P-019 preflight (see write_fact)
        with memory_lock(self.gf_root):
            self._recover_locked()  # drain a dangling intent from a prior crash first
            path = self.memory_dir / f"{name}.md"
            text = path.read_text()
            # P54: only a real transition belongs in the journal. Promoting an already-
            # confirmed fact would rewrite an identical file and journal a no-op that a
            # later default rollback would "succeed" on while masking the real mutation.
            fm = _parse_frontmatter(path)
            if not fm or fm.get("status") != "pending":
                raise ValueError(
                    f"cannot promote {name!r}: status is not pending "
                    f"(got: {fm.get('status') if fm else None!r})"
                )
            # count=1: only the FIRST (frontmatter) status line — never a `status: pending`
            # line that happens to appear in the body (which would corrupt content).
            new = re.sub(
                r"(?m)^status:\s*pending\s*$", "status: confirmed", text, count=1
            )
            # Two-phase WAL (see write_fact). Phase 1: journal the intent (pre-image =
            # pending text, post-image hash) with committed:false BEFORE the rewrite.
            self._mark_dirty_locked()
            intent = self._journal_locked(
                "promote", name, text, post_hash=_content_hash(new), committed=False
            )
            _atomic_write(path, new)
            self._regenerate_locked()
            self._mark_committed_locked(intent["seq"])  # Phase 2

    def delete_fact(self, name):
        """Locked delete + regenerate (CB2). Invalidation must NOT be a raw `rm`
        from skill markdown — that bypasses memory_lock and races concurrent writers
        (a just-written fact could be removed and a regenerate publish without it)."""
        if not NAME_RE.match(name or ""):
            raise ValueError(f"name must match {NAME_RE.pattern} (got: {name!r})")
        self._preflight()  # P-019 preflight (see write_fact)
        with memory_lock(self.gf_root):
            self._recover_locked()  # drain a dangling intent from a prior crash first
            path = self.memory_dir / f"{name}.md"
            if not path.exists():
                # surface a typo'd name rather than a silent no-op (parity with promote)
                raise FileNotFoundError(f"no such fact: {path}")
            pre = path.read_text()  # capture pre-image before removing, for rollback
            # Two-phase WAL (see write_fact). Phase 1: durably journal the pre-image
            # (post-image None — the fact is absent after delete) with committed:false
            # BEFORE the destructive unlink. If journaling fails the fact is NOT unlinked,
            # so the learning is never lost.
            self._mark_dirty_locked()
            intent = self._journal_locked(
                "delete", name, pre, post_hash=None, committed=False
            )
            path.unlink()
            self._regenerate_locked()
            self._mark_committed_locked(intent["seq"])  # Phase 2

    def regenerate(self):
        with memory_lock(self.gf_root):
            return self._regenerate_locked()

    # -- rollback journal (each mutation records a reversible pre-image) ----- #
    _FORWARD_OPS = ("write", "promote", "delete")

    def _journal_retention(self):
        """Rollback window: the journal keeps at most this many entries (declared at
        write-one, P53). unset/empty -> 100; positive int -> that; else ConfigError.
        Bounds storage + the per-mutation reparse to O(window)."""
        raw = os.environ.get("GOODFELLOW_JOURNAL_RETENTION")
        if raw is None or raw == "":
            return 100
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        raise ConfigError(
            f"GOODFELLOW_JOURNAL_RETENTION must be a positive integer (got: {raw!r})"
        )

    def _preflight(self):
        """P-019: validate every abort-capable config BEFORE mutating, so a bad env
        var throws before a fact is on disk (a mid-mutation throw + retry would
        otherwise suffix a duplicate)."""
        warn_kb()
        self._journal_retention()
        self._journal_byte_budget()

    def _mark_dirty_locked(self):
        """Write-ahead: mark the index dirty BEFORE changing a fact, so a mid-mutation
        failure (fact changed, but journal or regenerate did not complete) is recovered
        on the next read — closing the delete-then-journal-fails staleness gap.
        _regenerate_locked clears it only after the index is republished."""
        try:
            self.dirty_path.write_text("")
        except OSError:
            pass

    def _safe_fact_path(self, name):
        """Resolve a fact path from a name that came off the journal (an input
        boundary). Enforce NAME_RE + containment so a crafted entry like
        ``../../README`` cannot unlink/overwrite outside memory/ (P33)."""
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ValueError(f"invalid fact name in journal: {name!r}")
        path = self.memory_dir / f"{name}.md"
        if path.resolve().parent != self.memory_dir.resolve():
            raise ValueError(f"journal name escapes memory dir: {name!r}")
        return path

    class JournalCorruption(ValueError):
        """An interior journal line failed to parse — history is incomplete, so
        rollback's reverted/conflict computation cannot be trusted (P3 fail visible)."""

    @staticmethod
    def _valid_entry_shape(obj):
        """A journal line must parse to an object with an int seq and str op — else
        downstream code (which does obj.get(...)) would raise on e.g. a bare `[]`. Bool
        is excluded (bool is an int subclass) so `true` is not read as a sequence."""
        return (
            isinstance(obj, dict)
            and isinstance(obj.get("seq"), int)
            and not isinstance(obj.get("seq"), bool)
            and isinstance(obj.get("op"), str)
        )

    def read_journal(self):
        """Return the mutation journal as a list of entries. ONLY a demonstrably
        incomplete FINAL line (a torn last append) is tolerated; an unparseable OR
        wrong-shaped interior line means lost/forged history and raises
        JournalCorruption rather than silently dropping — or crashing on — an entry a
        later rollback relies on (P3 / P33)."""
        if not self.journal_path.exists():
            return []
        raw = [ln for ln in self.journal_path.read_text().splitlines() if ln.strip()]
        entries = []
        for idx, line in enumerate(raw):
            is_final = idx == len(raw) - 1
            try:
                obj = json.loads(line.strip())
            except json.JSONDecodeError:
                if is_final:
                    break  # torn final append — tolerated
                raise self.JournalCorruption(
                    f"corrupt interior journal line {idx + 1} in {self.journal_path}"
                )
            if not self._valid_entry_shape(obj):
                if is_final:
                    break  # garbage final line (e.g. torn write that parsed) — tolerated
                raise self.JournalCorruption(
                    f"malformed interior journal entry at line {idx + 1} in "
                    f"{self.journal_path}"
                )
            entries.append(obj)
        return entries

    def _journal_locked(
        self, op, name, pre_image, post_hash=None, target_seq=None, committed=None
    ):
        """Record one journal entry, capped to the retention window by BOTH entry count
        and total bytes (a single huge pre-image can't bloat the log unbounded — P53).
        Caller MUST hold memory_lock. Seqs stay monotonic across truncation because we
        keep the newest (highest-seq) entries, so max(seq)+1 is preserved. Always keeps
        at least the entry just recorded.

        ``committed`` is the two-phase WAL flag: a forward-mutation INTENT is recorded
        with ``committed=False`` and flipped to ``True`` by `_mark_committed_locked`
        after the fact lands. Non-forward records (rollback markers) pass ``None`` and
        carry no flag; a legacy entry with no flag is treated as committed on read."""
        entries = self.read_journal()
        seq = max((e.get("seq", 0) for e in entries), default=0) + 1
        entry = {
            "seq": seq,
            "op": op,
            "name": name,
            "pre_image": pre_image,
            "post_hash": post_hash,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if target_seq is not None:
            entry["target_seq"] = target_seq
        if committed is not None:
            entry["committed"] = committed
        entries.append(entry)
        entries, serialized = self._truncate_to_budget(entries)
        _atomic_write(self.journal_path, serialized)
        return entry

    def _truncate_to_budget(self, entries):
        """Bound the journal by entry count AND total bytes, keeping the NEWEST entries
        (a suffix); returns ``(kept_entries, serialized_text)``. Suffix-keep guarantees:
        (1) the record of the mutation currently in flight — always the newest entry — is
        never dropped, so recovery can always resolve it; (2) a rollback marker (always
        NEWER than the forward entry it resolves) is never kept while its forward entry is
        dropped in the direction that would resurrect the forward entry as un-resolved —
        keeping any entry keeps everything newer, including its marker. There is no
        intent/commit *pair* to split: the commit is an in-place flag flip on the same
        entry (`_mark_committed_locked`), not a separate record. Seqs stay monotonic
        (max(seq) preserved). DOCUMENTED EXCEPTION: if the single newest entry alone
        exceeds the byte budget (a pathologically large pre_image) it is STILL kept —
        losing it would forfeit recovery of the in-flight mutation."""
        cap = self._journal_retention()
        if len(entries) > cap:
            entries = entries[-cap:]  # keep newest; older mutations exit the window
        byte_budget = self._journal_byte_budget()

        def _serialize(items):
            return "".join(json.dumps(e) + "\n" for e in items)

        while len(entries) > 1 and len(_serialize(entries).encode()) > byte_budget:
            entries = entries[
                1:
            ]  # drop OLDEST; never below the newest in-flight record
        return entries, _serialize(entries)

    def _mark_committed_locked(self, seq):
        """Two-phase WAL Phase 2: after the fact + regenerated index have durably landed,
        flip the intent entry ``seq`` to ``committed: true``. Rewrites the journal in
        place (no NEW entry, so seq/entry-count contracts are unchanged). Idempotent —
        re-flipping an already-committed entry is a no-op. A crash before this flip leaves
        the entry ``committed: false`` (a dangling intent) which recovery resolves."""
        entries = self.read_journal()
        for e in entries:
            if e.get("seq") == seq:
                if e.get("committed") is True:
                    return  # already committed — nothing to do
                e["committed"] = True
                break
        else:
            return  # intent aged out (cannot happen for a just-recorded newest entry)
        _, serialized = self._truncate_to_budget(entries)
        _atomic_write(self.journal_path, serialized)

    def _journal_byte_budget(self):
        """Max journal size in bytes (GOODFELLOW_JOURNAL_MAX_BYTES, default 1 MiB).
        Bounds a pathologically large pre-image from growing the log without limit."""
        raw = os.environ.get("GOODFELLOW_JOURNAL_MAX_BYTES")
        if raw is None or raw == "":
            return 1024 * 1024
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        raise ConfigError(
            f"GOODFELLOW_JOURNAL_MAX_BYTES must be a positive integer (got: {raw!r})"
        )

    def _recover_locked(self):
        """Resolve dangling two-phase intents (crash between the intent journal and the
        commit flip). Caller MUST hold memory_lock. Returns True if anything changed.

        A dangling intent is a forward entry with ``committed is False`` and no matching
        rollback marker. For each (oldest first):
          - fact matches the recorded post-image  -> roll FORWARD (mark committed);
          - fact still at the pre-image            -> roll BACK (record a rollback
            marker; the fact is already at pre-image so no fact op is needed);
          - fact in a third state (a genuine non-journaled edit after the crash) -> leave
            it committed:false so an explicit rollback() surfaces the CAS conflict.
        Legacy entries (no ``committed`` field) predate two-phase and are treated as
        committed — never dangling."""
        entries = self.read_journal()
        reverted = {e.get("target_seq") for e in entries if e.get("op") == "rollback"}
        danglers = [
            e
            for e in entries
            if e.get("op") in self._FORWARD_OPS
            and e.get("committed") is False
            and e.get("seq") not in reverted
        ]
        changed = False
        for intent in danglers:
            name = intent.get("name")
            try:
                path = self._safe_fact_path(name)  # NAME_RE + containment (P33)
            except ValueError:
                continue  # crafted/invalid name — cannot act safely; leave it
            pre = intent.get("pre_image")
            if pre is not None and not isinstance(pre, str):
                continue  # malformed pre-image — leave for manual reconcile
            post_hash = intent.get("post_hash")
            actual = _content_hash(path.read_text()) if path.exists() else None
            if actual == post_hash:
                # the mutation landed in its produced state -> roll FORWARD
                self._mark_committed_locked(intent["seq"])
                changed = True
            elif (pre is None and actual is None) or (
                pre is not None and actual == _content_hash(pre)
            ):
                # the fact is still at the pre-image -> the mutation never landed -> roll
                # BACK. The fact already holds the pre-image, so no fact op is needed;
                # record a rollback marker so the abandoned intent is auditable and not
                # re-detected (its seq joins the reverted set).
                self._mark_dirty_locked()
                current = path.read_text() if path.exists() else None
                self._journal_locked(
                    "rollback",
                    name,
                    current,
                    post_hash=_content_hash(pre) if pre is not None else None,
                    target_seq=intent["seq"],
                )
                changed = True
            else:
                # third state: a genuine non-journaled edit after the crash. Cannot auto-
                # resolve without clobbering; leave committed:false so an explicit
                # rollback() fails loud (P3/P19) rather than silently overwriting.
                continue
        return changed

    def recover(self):
        """Resolve any dangling two-phase intents and republish the index if anything
        changed. Safe to call anytime; a no-op on a clean journal. Also run implicitly on
        lock-acquiring reads (when stale/dirty), mutations, and rollbacks."""
        self._preflight()
        with memory_lock(self.gf_root):
            if self._recover_locked():
                self._regenerate_locked()

    def rollback(self, seq=None):
        """Undo a journaled mutation: restore the affected fact to its pre-image
        (delete it if it was newly created), regenerate the index, and journal the
        rollback so it is itself auditable. ``seq=None`` targets the most recent
        forward mutation not already rolled back.

        Only the LATEST unreverted mutation of a given fact may be rolled back — a
        newer unreverted change to the same name would otherwise be clobbered (P19).
        Raises ValueError if there is no eligible entry, the seq is already reverted,
        or a later same-name mutation must be rolled back first."""
        self._preflight()
        with memory_lock(self.gf_root):
            # Resolve any dangling intent first, so an in-flight incomplete mutation is
            # rolled forward/back cleanly instead of surfacing as a spurious CAS conflict.
            self._recover_locked()
            entries = self.read_journal()
            reverted = {
                e.get("target_seq") for e in entries if e.get("op") == "rollback"
            }
            target = None
            if seq is None:
                for e in reversed(entries):
                    if (
                        e.get("op") in self._FORWARD_OPS
                        and e.get("seq") not in reverted
                    ):
                        target = e
                        break
                if target is None:
                    raise ValueError("no journaled mutation to roll back")
            else:
                for e in entries:
                    if e.get("seq") == seq and e.get("op") in self._FORWARD_OPS:
                        target = e
                        break
                if target is None:
                    raise ValueError(f"no forward mutation at journal seq {seq}")
                if target["seq"] in reverted:
                    raise ValueError(f"journal seq {seq} already rolled back")

            # P19 check-act: refuse to clobber a newer unreverted change to this fact.
            later = [
                e
                for e in entries
                if e.get("op") in self._FORWARD_OPS
                and e.get("name") == target.get("name")
                and e.get("seq", 0) > target["seq"]
                and e.get("seq") not in reverted
            ]
            if later:
                raise ValueError(
                    f"cannot roll back seq {target['seq']} for {target.get('name')!r}: "
                    f"a later mutation (seq {min(x['seq'] for x in later)}) must be "
                    "rolled back first"
                )

            path = self._safe_fact_path(target.get("name"))  # NAME_RE + containment
            pre = target.get("pre_image")
            if pre is not None and not isinstance(pre, str):
                raise ValueError(f"invalid pre_image in journal seq {target['seq']}")
            current = path.read_text() if path.exists() else None

            # P19 compare-and-swap: the fact must still hold the state this mutation
            # PRODUCED (its post-image). If a non-journaled edit (or an incomplete
            # mutation) changed it, refuse rather than silently clobber newer content.
            expected = target.get("post_hash")
            actual = _content_hash(current) if current is not None else None
            if expected != actual:
                raise ValueError(
                    f"rollback conflict for {target.get('name')!r}: the fact changed "
                    "since the recorded mutation (non-journaled edit?); reconcile manually"
                )

            self._mark_dirty_locked()  # write-ahead before touching the fact
            if pre is None:
                if path.exists():
                    path.unlink()
            else:
                _atomic_write(path, pre)
            # Journal the rollback with the state it overwrote + the post-image it left,
            # so it too is reversible and CAS-checkable.
            self._journal_locked(
                "rollback",
                target["name"],
                current,
                post_hash=_content_hash(pre) if pre is not None else None,
                target_seq=target["seq"],
            )
            self._regenerate_locked()
            return target["seq"]

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
            # We only reach here because the index is absent/dirty/stale — which is also
            # exactly the state a crash mid-mutation leaves. Resolve any dangling intent
            # before regenerating so the recovered index reflects the true fact state.
            self._recover_locked()
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
    warn_kb()  # P-019 preflight: abort on bad config BEFORE writing any fact (CB R4).
    # (Safe under _lock_held=True: it's a pure env read, no lock involved.)
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
    wf.add_argument(
        "--evidence",
        default=None,
        help="optional provenance pointer (PR/commit/finding-id/url), single line",
    )
    wf.add_argument("--body", default="")

    sub.add_parser("read-index")

    pr = sub.add_parser("promote")
    pr.add_argument("--name", required=True)

    df = sub.add_parser("delete-fact")
    df.add_argument("--name", required=True)

    sub.add_parser("regenerate")

    sub.add_parser("migrate")

    rb = sub.add_parser(
        "rollback", help="undo the last (or a given) journaled mutation"
    )
    rb.add_argument(
        "--seq", type=int, default=None, help="journal seq to undo (default: last)"
    )

    sub.add_parser("journal", help="print the mutation journal (JSONL) to stdout")

    sub.add_parser(
        "recover", help="resolve any dangling two-phase intents (crash recovery)"
    )
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
                evidence=args.evidence,
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
        elif args.cmd == "rollback":
            seq = store.rollback(args.seq)
            print(f"rolled back journal seq {seq}", file=sys.stderr, flush=True)
        elif args.cmd == "journal":
            for entry in store.read_journal():
                sys.stdout.write(json.dumps(entry) + "\n")
        elif args.cmd == "recover":
            store.recover()
            print("recovery complete", file=sys.stderr, flush=True)
    except (ConfigError, SchemaError, ValueError, FileNotFoundError) as e:
        print(str(e), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
