## 5. File follow-up loops

Every deferred finding from the convergence exit needs a durable destination — no
substantive tier may be silently dropped. Route by the three-tier severity taxonomy the
reviewers emit (`## Blockers` / `## Major` / `## Minor`; ranks blocker > major > minor,
per `SEVERITY_RANKS` in `convergence_detector.py`):

- **Safety-critical / blocker (rank 3)** → file to `.goodfellow/loops.json` via loop store. Blocker findings ALSO halt the PR per §3 — the loop is filed in addition to the halt, never as a substitute for fixing it.
- **Major (rank 2)** → file to `.goodfellow/loops.json` via loop store, the same destination as blocker. A major finding is substantive follow-up work, not a polish gotcha, so it belongs in the loop store. It does NOT block the PR (only blocker/safety-critical HALTs per §3), but it MUST be durably filed as a loop — never dropped, never downgraded to a knowledge gotcha.
- **Polish-tier / minor (rank 1)** → append to the knowledge file as gotchas instead of filing loops.

Summary: **blocker + major → loop store; minor → knowledge gotchas.** No severity tier is
left without a home. Loop priority comes from finding severity; round 4+ findings default
to p4 unless safety-critical.

Canonical routing map (severity → durable destination — the machine-readable source of
truth; keep the prose above and the README in sync with it, and never desync a tier to a
different destination):

```text
blocker -> loop_store        # substantive follow-up; ALSO halts the PR per §3
major   -> loop_store        # substantive follow-up; non-blocking, but MUST be filed — never a gotcha
minor   -> knowledge_gotchas # polish-tier
```

Pass `--lens <lens>` when the finding came from the judged Codex review: read the finding's `lens` cell from the review artifact's `## Judge audit` table (one of `auth-trust`, `data-integrity`, `failure-handling`, `concurrency`, `input-edge`, `compat-migration`, `observability`, `contract-scope`, or `other`). This threads the interpretation frame onto the loop so `triage` and `lens_tuning.py` can attribute outcomes per-lens. Omit `--lens` for hand-filed loops or an unjudged (fallback) review — a missing lens is fine (bucketed `unattributed`).

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/loop_store.py" --root . add "<title>" --priority <p> --source "ship-review-r<N>" --description "<text>" [--lens <lens>]
```

Soft cap check: if >15 active loops, warn "loop backlog growing — consider /goodfellow:triage".

