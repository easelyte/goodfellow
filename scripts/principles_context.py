#!/usr/bin/env python3
"""Resolve, index, and disclose the seeded principle files for the chain skills.

Skills invoke this via the CLI (not import), matching the loop_store.py pattern.

## Progressive disclosure

The seeded corpus (`knowledge/principles.md` ~1,059 lines + opt-in
`principles-web.md`) is far too large to inject on every chain run: full-dump is
~17k estimated tokens / ~300 standing directives, i.e. ~6x the ~2,500-3,000-token
accuracy-erosion ceiling and past the 150-250-instruction adherence cliff
(`docs/instruction-density-budget.md`). So the chain no longer injects the full
bodies. It mirrors the Agent Skills loading model the plugin already uses for
skills (name + description always; full SKILL.md only when the skill fires):

  --index            Always-injected. Emits each principle's P-NNN id + title +
                     one-line rule (the blockquote). ~2.6k tokens for the whole
                     corpus. This is the menu.
  --show P-003 P-020 On demand. Emits the FULL body of specific principles once
                     the model (having scanned the index) decides they are
                     relevant to the current task/diff. Requesting a parent
                     (P-017) includes its sub-principles (P-017a, P-017b).
  --emit             Legacy full dump (every body). Retained for callers that
                     genuinely want everything; the chain skills no longer use it.

Vital-few principles (safety / data-loss / irreversibility) are placed at the
edges of the index — first (primacy) and recapped last (recency) — because the
middle of a long context is where adherence dies (`docs/instruction-density-budget.md` §C).

## Resolution contract (unchanged)

Core (`principles.md`) is always read. The web supplement (`principles-web.md`)
is read only when web context is opted in: GOODFELLOW_PRINCIPLES_WEB=1, or a
package.json at the project root. An invalid GOODFELLOW_PRINCIPLES_WEB value
hard-errors (fail loud), so a misconfigured skill run fails visibly.

    principle_files=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --project-root .) || { echo "$principle_files" >&2; exit 1; }
"""

import argparse
import os
import pathlib
import re
import sys


class ConfigError(ValueError):
    pass


# Highest-consequence, expensive-to-reverse principles. Placed at the index edges
# (primacy + recency) so they survive the mid-context adherence dip. IDs absent
# from the resolved set (e.g. a web-only id when web is off) are skipped silently.
VITAL_FEW = [
    "P-001",  # UI Hiding Is Never Authorization (authz)
    "P-002",  # Canonical Source of Truth (data integrity)
    "P-003",  # Fail Visible (silent-failure / debuggability)
    "P-015",  # Data Egress Needs Explicit Permission (security)
    "P-019",  # Check-Act Ordering (+ P-019a Irreversibility Boundaries)
    "P-032",  # Idempotency for Mutations (data safety)
    "P-069",  # Never Squash-Merge Across a Diverged Base (data loss)
    "P-079",  # Reaching a Limit Is Not Success (correctness under limits)
]


def resolve_principle_files(plugin_root, project_root):
    """Return the ordered list of principle filenames the chain skills should read.

    GOODFELLOW_PRINCIPLES_WEB contract:
      - unset or empty  -> autodetect web context via a project-root package.json
      - exactly "1"      -> FORCE web on (operator opted in explicitly)
      - any other value  -> hard error (fail loud)
    `forced` vs `autodetect` differ on a missing web file: a forced opt-in whose
    `principles-web.md` is absent is packaging/install drift and hard-errors
    (CM-R5-1); autodetect is best-effort and silently falls back to core-only.
    """
    # Core is mandatory — validate it exists rather than just asserting it in prose
    # (R6: the skill cat-loop can't be the only guard; fail loud at resolution).
    core = pathlib.Path(plugin_root) / "knowledge" / "principles.md"
    if not core.exists():
        raise ConfigError(f"core seed {core} is missing (packaging/install drift)")
    files = ["principles.md"]
    web = os.environ.get("GOODFELLOW_PRINCIPLES_WEB")
    forced = False
    if web is None or web == "":
        web_on = (pathlib.Path(project_root) / "package.json").exists()
    elif web == "1":
        web_on = True
        forced = True
    else:
        raise ConfigError(
            f"GOODFELLOW_PRINCIPLES_WEB must be unset, empty, or '1' (got: {web!r})"
        )
    web_file = pathlib.Path(plugin_root) / "knowledge" / "principles-web.md"
    if web_on:
        if web_file.exists():
            files.append("principles-web.md")
        elif forced:
            # explicit opt-in but the supplement isn't shipped -> visible failure,
            # not a silent core-only run (CM-R5-1)
            raise ConfigError(
                f"GOODFELLOW_PRINCIPLES_WEB=1 but {web_file} is missing "
                "(packaging/install drift)"
            )
        # autodetect + missing file -> best-effort core-only (no error)
    return files


def _read_file(p):
    try:
        return p.read_text()
    except OSError as e:
        raise ConfigError(f"cannot read principle file {p}: {e}")


def emit_principles(plugin_root, project_root):
    """Resolve AND read the principle files, returning their concatenated content.

    LEGACY full dump — every principle body. The chain skills no longer call this
    (they use --index + --show); retained for any caller that genuinely wants the
    whole corpus. All error handling lives here (one place, fully testable): bad
    config, missing core, or an unreadable file all raise ConfigError."""
    files = resolve_principle_files(plugin_root=plugin_root, project_root=project_root)
    kn = pathlib.Path(plugin_root) / "knowledge"
    parts = [_read_file(kn / f) for f in files]
    return "\n".join(parts)


# --- progressive disclosure primitives ---------------------------------------

# `### P-020. Title` (top-level) or `#### P-017a. Title` (sub-principle).
_HDR = re.compile(r"^(#{2,4})\s+(P-\d+[a-z]?)\.\s+(.+?)\s*$")
# A heading that LOOKS like a principle (starts with ## .. #### then `P-`) —
# used to catch drift from the canonical grammar so a malformed principle can't
# silently vanish from the index (and from the density ratchet, which counts
# only parsed entries).
_HDR_LOOSE = re.compile(r"^#{2,4}\s+P-")
# Inline routing marker: `<!-- cat: testing -->`. Lowercase kebab category name.
_CAT = re.compile(r"<!--\s*cat:\s*([a-z0-9][a-z0-9-]*)\s*-->")


def parse_principles(text, source=""):
    """Parse a principles markdown doc into ordered entries.

    Each entry: {id, level, title, oneliner, body, source}. `oneliner` is the
    leading blockquote (the compressed rule) if present, else "". `body` is the
    full markdown block from the header through everything up to the next header
    at the same-or-shallower level, so a parent's body includes its
    sub-principles (P-017 -> P-017a, P-017b)."""
    lines = text.split("\n")
    heads = []
    for i, line in enumerate(lines):
        m = _HDR.match(line)
        if m:
            heads.append((i, len(m.group(1)), m.group(2), m.group(3).strip()))
        elif _HDR_LOOSE.match(line):
            # looks like a principle but drifts from `P-NNN. Title` — a silent
            # drop here would remove it from injection AND evade the ratchet.
            raise ConfigError(
                f"malformed principle header in {source or '<principles>'}: "
                f"{line.strip()!r} (expected '#### P-NNN. Title')"
            )
    entries = []
    for k, (i, level, pid, title) in enumerate(heads):
        j = i + 1
        while j < len(lines) and lines[j].strip() == "":
            j += 1
        oneliner = ""
        if j < len(lines) and lines[j].lstrip().startswith(">"):
            oneliner = lines[j].lstrip().lstrip(">").strip()
        end = len(lines)
        own_end = (
            None  # first DEEPER (sub-principle) header — bounds this entry's own text
        )
        for i2, level2, _, _ in heads[k + 1 :]:
            if level2 <= level:
                end = i2
                break
            if own_end is None:
                own_end = i2
        if own_end is None:
            own_end = end
        body = "\n".join(lines[i:end]).rstrip()
        # Routing category (tiered index): an inline `<!-- cat: <name> -->` marker
        # assigns the principle to a category; untagged -> "general". Search only
        # the entry's OWN text (header through its first sub-principle), NOT the
        # full body: a parent's body deliberately includes its sub-principles, so
        # searching all of it would let a tagged child's marker leak onto an
        # untagged parent (loop-#723-style taxonomy collision). Mirrors this box's
        # per-memory `domain:` frontmatter — each entry declares its own routing.
        cat_m = _CAT.search("\n".join(lines[i:own_end]))
        cat = cat_m.group(1) if cat_m else "general"
        entries.append(
            {
                "id": pid,
                "level": level,
                "title": title,
                "oneliner": oneliner,
                "cat": cat,
                "body": body,
                "source": source,
            }
        )
    return entries


def load_entries(plugin_root, project_root):
    """Resolve the opted-in files and parse them into a single ordered entry list."""
    files = resolve_principle_files(plugin_root=plugin_root, project_root=project_root)
    kn = pathlib.Path(plugin_root) / "knowledge"
    entries = []
    for f in files:
        entries.extend(parse_principles(_read_file(kn / f), source=f))
    seen = {}
    for e in entries:
        if e["id"] in seen:
            raise ConfigError(
                f"duplicate principle id {e['id']} ({seen[e['id']]} and {e['source']})"
            )
        seen[e["id"]] = e["source"]
    return entries


def _index_line(e):
    prefix = "  - " if e["level"] >= 4 else "- "
    if e["oneliner"]:
        return f"{prefix}{e['id']}. {e['title']} — {e['oneliner']}"
    return f"{prefix}{e['id']}. {e['title']}"


# One-line descriptions for the category routing table (tier 1). A category with
# no label here still renders (its raw name); labels just make the table readable.
CATEGORY_LABELS = {
    "security": "authorization, egress, secrets, untrusted input",
    "data-integrity": "canonical source, idempotency, migrations, identifiers",
    "correctness": "check-act ordering, ground truth, behavior under limits",
    "testing": "fixtures, run-it gates, gate verification, baselines",
    "review-process": "adversarial review, cross-model diversity, guard design",
    "reliability": "fail-visible, persistence, concurrency, boundaries",
    "integration": "seams, extending vs reinventing dependencies",
    "agent-runtime": "model-driven runtimes, harness vs prose, disclosure",
    "ui": "UI/UX surface, design tokens",
    "general": "uncategorized",
}


def _categories(entries):
    """Ordered {category: [entry, ...]} over all entries, category order by first
    appearance so the table is stable and file-driven."""
    order = []
    groups = {}
    for e in entries:
        c = e.get("cat", "general")
        if c not in groups:
            groups[c] = []
            order.append(c)
        groups[c].append(e)
    return [(c, groups[c]) for c in order]


def index_entry_count(entries):
    """Tier-1 row count: vital-few present + distinct categories. This — NOT the
    total corpus size — is what the always-loaded budget caps, because the tiered
    index only ever renders these rows regardless of how large the corpus grows."""
    by_id = {e["id"]: e for e in entries}
    vital = sum(1 for i in VITAL_FEW if i in by_id)
    cats = len({e.get("cat", "general") for e in entries})
    return vital + cats


def build_index(entries):
    """Render the always-injected TIER-1 index (progressive disclosure).

    Two always-loaded parts, both bounded so the corpus can grow without inflating
    what loads every run (mirrors this box's `MEMORY.md`: vital-few + a routing
    table, everything else on demand):
      - VITAL_FEW full one-liners (primacy at the top, recap at the bottom).
      - A CATEGORY ROUTING TABLE — one row per category with its member ids (no
        per-principle one-liners). The model scans it, then expands the relevant
        category with `--category <name>` (tier 2) and reads full bodies with
        `--show P-NNN` (tier 3)."""
    by_id = {e["id"]: e for e in entries}
    vital = [by_id[i] for i in VITAL_FEW if i in by_id]

    out = [
        "# Design principles — index (progressive disclosure, tiered)",
        "#",
        "# Always-loaded = the vital-few one-liners + a category routing table (ids",
        "# only). Full one-liners and bodies load ON DEMAND:",
        "#   --category testing      # tier 2: one-liners for a whole category",
        "#   --show P-003 P-020      # tier 3: full bodies of specific principles",
        "# Scan the table, expand the categories relevant to this task/diff, then apply.",
        "# Cite violations by P-NNN.",
        "",
    ]
    if vital:
        out.append("## Most load-bearing (safety / data-loss / irreversibility)")
        out.extend(_index_line(e) for e in vital)
        out.append("")
    out.append("## Categories (expand with --category <name>)")
    for cat, members in _categories(entries):
        label = CATEGORY_LABELS.get(cat, "")
        ids = ", ".join(e["id"] for e in members)
        desc = f" — {label}" if label else ""
        out.append(f"- {cat} ({len(members)}){desc}: {ids}")
    if vital:
        out.append("")
        out.append("Most load-bearing, one line: " + ", ".join(e["id"] for e in vital))
    return "\n".join(out) + "\n"


def build_index_flat(entries):
    """FLAT index: id + title + one-liner for EVERY principle (vital-few first).

    For consumers that embed the principle list into a one-shot subprocess prompt
    and cannot do progressive disclosure — e.g. `codex-bridge.sh` builds a static
    reviewer prompt, so a child reviewer can't run `--category` to expand a routing
    row. Those consumers need every one-liner inline. This is the pre-tiered index
    shape; it is NOT what the interactive chain skills inject (they use the tiered
    `build_index`, which the density ratchet caps), and it is NOT ratcheted."""
    by_id = {e["id"]: e for e in entries}
    vital = [by_id[i] for i in VITAL_FEW if i in by_id]
    vital_ids = {e["id"] for e in vital}
    rest = [e for e in entries if e["id"] not in vital_ids]
    out = [
        "# Design principles — flat index (id + title + one-line rule, all principles)",
        "",
    ]
    if vital:
        out.append("## Most load-bearing (safety / data-loss / irreversibility)")
        out.extend(_index_line(e) for e in vital)
        out.append("")
    out.append("## All principles")
    out.extend(_index_line(e) for e in rest)
    return "\n".join(out) + "\n"


def show_category(entries, name):
    """Tier 2: emit the id + title + one-liner for every principle in a category.

    Unknown category produces a visible marker (fail-visible, P-003) rather than
    silent empty output."""
    members = [e for e in entries if e.get("cat", "general") == name]
    if not members:
        known = ", ".join(sorted({e.get("cat", "general") for e in entries}))
        return f"> category {name!r}: no principles (known categories: {known})\n"
    out = [f"# {name} — {CATEGORY_LABELS.get(name, name)}", ""]
    out.extend(_index_line(e) for e in members)
    return "\n".join(out) + "\n"


def show_principles(entries, ids):
    """Emit the full bodies of the requested principle ids, in requested order.

    Unknown ids produce a visible `> P-NNN: not found` marker rather than silent
    omission (fail-visible, P-003)."""
    by_id = {e["id"]: e for e in entries}
    blocks = []
    for pid in ids:
        e = by_id.get(pid)
        if e is None:
            blocks.append(f"> {pid}: not found in the resolved principle set")
        else:
            blocks.append(e["body"])
    return "\n\n".join(blocks) + "\n"


def _resolve_plugin_root(arg_root):
    plugin_root = arg_root or os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not plugin_root:
        # Fallback: this file lives at <plugin_root>/scripts/principles_context.py
        plugin_root = str(pathlib.Path(__file__).resolve().parents[1])
    return plugin_root


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Resolve / index / disclose the seeded principle files."
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Project root (for package.json web autodetect).",
    )
    parser.add_argument(
        "--plugin-root",
        default=None,
        help="Plugin root holding knowledge/. Defaults to $CLAUDE_PLUGIN_ROOT.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--index",
        action="store_true",
        help="Print the always-injected index (id + title + one-line rule).",
    )
    mode.add_argument(
        "--show",
        nargs="+",
        metavar="P-NNN",
        help="Print the FULL body of the given principle id(s), on demand.",
    )
    mode.add_argument(
        "--category",
        metavar="NAME",
        help="Tier 2: print id + title + one-liner for every principle in a "
        "category (expand a routing-table row from --index).",
    )
    mode.add_argument(
        "--index-flat",
        action="store_true",
        help="Print id + title + one-liner for EVERY principle (pre-tiered shape). "
        "For consumers that embed the list into a one-shot prompt and cannot "
        "expand categories on demand (e.g. the review bridge).",
    )
    mode.add_argument(
        "--emit",
        action="store_true",
        help="Legacy: print every principle body (full corpus). Chain skills use "
        "--index + --show instead.",
    )
    args = parser.parse_args(argv)

    plugin_root = _resolve_plugin_root(args.plugin_root)

    try:
        if args.emit:
            sys.stdout.write(
                emit_principles(plugin_root=plugin_root, project_root=args.project_root)
            )
        elif args.index:
            entries = load_entries(
                plugin_root=plugin_root, project_root=args.project_root
            )
            sys.stdout.write(build_index(entries))
        elif args.index_flat:
            entries = load_entries(
                plugin_root=plugin_root, project_root=args.project_root
            )
            sys.stdout.write(build_index_flat(entries))
        elif args.show:
            entries = load_entries(
                plugin_root=plugin_root, project_root=args.project_root
            )
            sys.stdout.write(show_principles(entries, args.show))
        elif args.category:
            entries = load_entries(
                plugin_root=plugin_root, project_root=args.project_root
            )
            sys.stdout.write(show_category(entries, args.category))
        else:
            for f in resolve_principle_files(
                plugin_root=plugin_root, project_root=args.project_root
            ):
                print(f)
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
