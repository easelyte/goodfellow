## Step 1 — Internal-ref scrub gate (blocking)

Internal provenance (internal ticket/PR numbers, internal rule-id citations,
internal host names or absolute paths, internal product/customer names) is
world-visible in a public repo and meaningless-to-misleading in a repo that isn't
yours. A **partial** scrub is worse than none — scrub ALL of it or none.

The gate reuses goodfellow's own egress matcher against **your own** denylist —
this ships with no built-in inventory of any particular project's internal names.
Define your denylist once (first that exists wins):

1. `--denylist <path>`
2. `$GOODFELLOW_INTERNAL_DENYLIST` (a file path)
3. `<project-root>/.goodfellow/internal_denylist.txt`

One phrase per line; `#` comments; blanks ignored. List your internal-only tokens.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/public_pr_scrub.py" --base "$BASE"
# exit 0 = clean · exit 1 = hits (BLOCK) · exit 2 = fail-closed: --require-denylist with
# none set, or the diff could not be computed (bad base, no git, or no --base and no
# upstream/*, origin/* or local main/master to default to; without --base it scans
# against every one of those that exists)
```

If it reports hits: remove **every** internal ref from the added lines, re-run
until clean. Do NOT open the PR with any hit unresolved. (No denylist configured →
the gate passes with a note; add `--require-denylist` to hard-fail instead.)

