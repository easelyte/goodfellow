---
name: grill
description: Opt-in relentless one-question-at-a-time interview for underspecified or high-stakes design intent. Fires ONLY on explicit invocation — /goodfellow:grill, "grill me on X", "interview me about X", "grill me". Does NOT claim the generic "design X" / "brainstorm X" triggers (those stay with brainstorm). Scouts facts, interviews to a ledger-empty finish, writes a spec, auto-dispatches spec-review.
---

<!-- CONTRACT-SYNC: grill contract_version=1 sha=2ca1bded7beb9124bf91a61c785a712d9cbcff2357ce534026e20c6d153ca680 -->

The operator wants to be **grilled**: a relentless, one-question-at-a-time interview that
drives a fuzzy or high-stakes idea to a resolved design before any spec is written. This is the
deliberate opposite of `brainstorm`'s low-friction 3-question cap.

**When this fires vs. brainstorm.** `brainstorm` is the default for clear intent (fast, ≤3
questions, biases ambitious scope). `grill` is the opt-in escalation for fuzzy/underspecified
intent — invoked explicitly, never auto-selected. If the operator gave an unqualified "design X"
prompt, that is a `brainstorm`, not a grill.

**Typical range: ≈5-15 questions**, but there is **no hard cap** — grill self-terminates when the
open-decision ledger is empty (see §2). The operator can say **"enough / write it"** at any point to
jump straight to the spec; that escape hatch is surfaced in every question.

Attribution: the interview philosophy ("interview relentlessly, one question at a time, look up
facts rather than ask, reserve questions for human judgment") adapts Matt Pocock's `grilling` skill.
A one-line credit satisfies its MIT-style permissive terms — no further notice text required in this
file.

<!-- CONTRACT-SYNC-BLOCK-START -->
## Contract invariant

The block between the `CONTRACT-SYNC-BLOCK` delimiters is the portable contract. Any copy of `grill`
in another repo must carry this same block (same `contract_version`, same `sha`). The `sha` in the
marker at the top of this file is SHA-256 over the bytes **strictly between** the two delimiter
comment lines (exclusive of the delimiter lines themselves and of the marker line), UTF-8 encoded,
LF-normalized, emitted as full lowercase hex.

- **Routing.** `grill` fires only on explicit signal: `/goodfellow:grill`, or a grill-exclusive
  phrase — "grill me on X", "interview me about X", "grill me". It does NOT claim the generic
  "design X" / "brainstorm X" triggers; those route to `brainstorm`. There is no implicit
  model-decides-it's-fuzzy escalation.
- **Frontmatter schema.** The written spec carries: `interview_rounds` (count of question→answer
  exchanges; the scout pass is round 0 and is NOT counted); `unresolved_questions` (a list;
  populated on both the early-cut and autopilot paths — autopilot holds the best-effort identifiable
  set, not a fictional "every question"); `confidence` (`high` = interview fully resolved AND the
  approach has precedent; `medium` = fully resolved but novel; `low` = operator cut early with
  material questions open, OR autopilot) plus `confidence_basis: grill`.
- **Autopilot `=1` degradation.** Under `GOODFELLOW_AUTOPILOT=1`, grill emits zero interactive
  questions: it answers from scout + context, writes the spec with `confidence: low`, best-effort
  `unresolved_questions`, and `next_action: halt-after-spec-review`, then dispatches spec-review.
- **`next_action` halt.** An autopilot spec sets `next_action: halt-after-spec-review` so spec-review
  stops before cascading into plan — an unreviewed low-confidence design never auto-advances.
- **Termination rule.** Grill self-terminates when the open-decision ledger is empty: every
  foundational and dependent decision resolved AND the last answer generated no new decision branch.
  No hard question cap.
- **Escape hatch.** Every interview question carries a prominent "enough / write it" exit; the
  operator can invoke it at any turn to jump to the spec, populating `unresolved_questions` from the
  still-open ledger.
<!-- CONTRACT-SYNC-BLOCK-END -->

## 0. Initialize the run log

```bash
RUN_LOG=$(bash "${CLAUDE_PLUGIN_ROOT}/scripts/run_log.sh")
```

This resolves a concrete `.goodfellow/runs/<timestamp>-<pid>.jsonl` path (the pid suffix keeps two
runs in the same UTC second distinct) and ensures the runs dir exists. Use `$RUN_LOG` for every
`would_act` append under dry-run (§4) — never a literal `<timestamp>.jsonl` placeholder. Under
`dry-run` this step does NOT touch `.gitignore` (observe-without-mutating).

## 1. Scout phase (facts before questions)

Grilling's rule is **"look up facts, don't ask."** Open with a bounded fact-gathering pass so the
interview spends questions only on genuine human judgment.

- **Bounded by scope, not a timer:** a single focused pass, **≤8 tool-calls total**. Read the code,
  existing specs, and knowledge file that bear on the idea; never open-ended.
- **Liveness:** the scout runs as a bounded **foreground** pass. This plugin **cannot** portably
  hard-kill a hung subagent, so it makes **no wall-clock watchdog / hard-kill promise** — the bound
  is the scope-limited prompt plus the operator's ability to interrupt. A truly hung subagent is a
  rare harness-level failure grill does not claim to guard against.
- **Degradation (branch by mode):** an empty result or an error does **not** abort grill. It is
  additive.
  - **Interactive** (autopilot unset) → proceed to the interview in §2 and gather what the scout
    missed through questions.
  - **Autopilot** (`=1` or `dry-run`) → proceed to write-from-context (§4), **never** to an
    interview — there is no operator to answer.

## 2. The interview loop

Interactive only (autopilot never reaches this section — see §4).

- **One question at a time.** Ask, then **wait** for the answer. Never batch two questions into one
  message.
- **Every question ships a recommended default** (answerable with a one-word confirm) **and** the
  prominent **"enough / write it"** escape hatch. Example shape:

  > **Q4.** Should the importer dedupe on email or on external id?
  > *Recommended: external id (stable across email changes).*
  > *(Say "enough / write it" anytime to stop here and write the spec.)*

- **Dependency order.** Resolve foundational decisions (data model, source of truth, system
  boundaries) before downstream detail. A later question may only exist because of an earlier answer.
- **Look up, don't ask.** Anything answerable from the scout or from tools is looked up silently, not
  turned into a question.
- **Understanding ledger + running count.** Each turn, surface a compact ledger of resolved vs. open
  decisions and the running question count, e.g. `Ledger: 3 resolved · 2 open · Q5 asked`. This is
  both the convergence signal and — for metered-billing users — a live cost signal.
- **Termination is the ledger invariant, not a vibe.** Self-terminate when the open-decision ledger
  is empty: every foundational and dependent decision resolved **and** the last answer generated no
  new decision branch. "No new branch from the last answer" is the inspectable convergence test.
  There is no numeric cap. The operator's "enough / write it" is the always-available manual exit; on
  it, jump immediately to §3 and populate `unresolved_questions` from whatever remains open.
- On termination (ledger-empty or operator cut): **announce the outcome, then write the spec (§3).**

## 3. Spec write + auto-dispatch tail

### 3a. Build the complete spec, then publish atomically

Path convention: `docs/specs/<slug>-design.md` (kebab-case slug from the topic).

Write the **entire** spec — body **and** the full frontmatter, including the pending-review recovery
keys below — into a same-directory temp file first, flush it, then publish with an **atomic
no-clobber** link. Do **not** reserve an empty file and fill it later (non-atomic), and do **not**
check-then-write (TOCTOU — see P19).

```bash
slug="<kebab-slug>"
dir="docs/specs"
mkdir -p "$dir"

# 1. Author the COMPLETE spec (frontmatter + body) into a temp file in the SAME dir.
tmp="$(mktemp "$dir/.${slug}-design.XXXXXX.tmp")"
#    ...write the full document to "$tmp" here...

# 2. Flush to disk before publishing.
python3 -c "import os,sys; fd=os.open(sys.argv[1], os.O_RDONLY); os.fsync(fd); os.close(fd)" "$tmp"

# 3. Publish with atomic exclusive-create. ln() fails atomically if the target already
#    exists, so two concurrent grills with the same date+slug cannot both win the path.
n=1
target="$dir/${slug}-design.md"
until ln "$tmp" "$target" 2>/dev/null; do
  n=$((n+1))
  target="$dir/${slug}-design-${n}.md"   # -2, -3, ... each also exclusive-create
done
rm -f "$tmp"   # content persists via the hardlink at "$target"
echo "$target"
```

On a collision, the loop retries `<slug>-design-2.md`, `<slug>-design-3.md`, … — never a blind
overwrite. (Interactively you may instead ask the operator to confirm/rename; the loop is the
autopilot-safe default.)

### 3b. Frontmatter schema

```yaml
---
title: "<one-line design title>"
status: draft
date: <YYYY-MM-DD>
confidence: <high|medium|low>       # high = resolved + precedent; medium = resolved + novel;
                                     # low  = operator cut early with open questions, OR autopilot
confidence_basis: grill
interview_rounds: <int>             # question->answer exchanges; the scout pass is round 0, NOT counted
unresolved_questions: []            # populated on early-cut AND autopilot (best-effort identifiable set)
# --- pending-review recovery (written UP FRONT, cleared only on confirmed successful review) ---
review_status: pending
failed_reviewers: []
resume: "/goodfellow:spec-review docs/specs/<slug>-design.md"
---
```

`interview_rounds` counts only question→answer exchanges. `unresolved_questions` is empty for a
clean ledger-empty finish, non-empty on an early cut or on the autopilot best-effort path.

### 3c. Pending-review recovery (durable across a crash)

The recovery keys (`review_status: pending`, `failed_reviewers: []`,
`resume: /goodfellow:spec-review <path>`) are written **at the initial atomic write** — so a hang or
kill *between* the spec write and the review dispatch still leaves durable, machine-readable pending
state (not "written only after a failure is noticed"). The `resume` command is repo-namespaced to
this plugin (`/goodfellow:spec-review`).

- **On a reviewer failure / timeout:** atomically **append** that reviewer's id to
  `failed_reviewers`. Update the frontmatter by rewriting the file to a same-dir temp and renaming
  over the original (`mv` is an atomic same-filesystem replace) — never an in-place partial edit.
- **Clear** `review_status` / `failed_reviewers` / `resume` **only on a confirmed successful
  review** (the terminal transition — performed by the resumed spec-review). A spec is never left
  permanently `pending` after a review that later succeeded, so later sessions don't re-dispatch on
  stale state.

### 3d. Auto-dispatch spec-review (same turn, no gate)

After publishing the spec, in the **same turn**:

1. Emit a one-line summary (final path, what the spec commits to, `interview_rounds`, `confidence`).
2. Dispatch `/goodfellow:spec-review <target>`.

No gate, no "Want me to proceed?" — matching `brainstorm`'s auto-dispatch tail. spec-review reviews
the spec **by file content** (`codex-bridge.sh --kind spec --file <target>`), so the freshly-written,
still-untracked spec is actually seen by the reviewer instead of showing up as an empty git diff.

## 4. Autopilot

`GOODFELLOW_AUTOPILOT` is three-state. **No interactive question is ever emitted while autopilot is
active** — §1's degradation routes autopilot to write-from-context, never to §2.

- **`GOODFELLOW_AUTOPILOT=1` (full-auto):** answer every decision from scout + context. Write the
  spec (§3) with `confidence: low`, a best-effort `unresolved_questions` list, and
  `next_action: halt-after-spec-review`; then dispatch `/goodfellow:spec-review`. The halt key makes
  spec-review stop before plan, so a low-confidence auto-spec never cascades unreviewed.
- **`GOODFELLOW_AUTOPILOT=dry-run` (observe-only):** do **NOT** write the spec and do **NOT** dispatch
  spec-review. Instead log the mutations you *would* perform to `$RUN_LOG` (from §0), one JSONL event
  each, e.g.:

  ```bash
  printf '%s\n' '{"event":"would_write_spec","would_act":true,"path":"docs/specs/<slug>-design.md","confidence":"low"}' >> "$RUN_LOG"
  printf '%s\n' '{"event":"would_dispatch","would_act":true,"skill":"spec-review"}' >> "$RUN_LOG"
  ```

  The dry-run contract is observe-without-mutating: the only thing touched is the `.goodfellow/runs/`
  audit log itself.
- **`GOODFELLOW_AUTOPILOT` unset:** not autopilot — the interactive flow (§1 → §2 → §3) applies.
  (Listed only to enumerate the env var's three values.)

## Cross-repo parity note

This file carries a `CONTRACT-SYNC` marker so a copy in another repo can be checked for contract
parity (matching `contract_version` AND `sha` over the delimited invariant block). **Automated
cross-repo parity enforcement is deferred to a later deliverable (D2)** — today the marker is a
documented soft-gate, present so the checker can land without re-touching this file.
