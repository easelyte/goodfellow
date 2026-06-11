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
