<!-- Goodfellow seed principles — curated from easelyte's cross-repo design knowledge.
     Version: 0.1.0. Do not edit; plugin-owned (see README). -->

# Universal design principles (core)

Stack-agnostic design rules. They apply to any codebase regardless of language or
framework. Web/JS/SQL-specific rules live in `principles-web.md` (opt-in).

Only facts a model cannot assume are kept: house conventions, platform limits and
incident-derived specifics. Retired and merged ids stay as one-line stubs
(`<!-- cat: retired -->`) so citations resolve; they are left out of the index.


### P-001. UI Hiding Is Never Authorization
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-002. Canonical Source of Truth
> If you need a sync script to keep two things in agreement, you have one source too many.
<!-- cat: data-integrity -->

Every datum has exactly ONE authoritative location; everything else is derived. When a normalized structure replaces a denormalized field, drop the old field in the same release or document which is canonical. When staged configuration becomes canonical, move its definitions, retire the staging artifact and update verification readers in the same change.

**Rules:**
- Divergent calculations of the same value are data-integrity bugs: one computation, one implementation.
- Code that consumes shared config or canonical data reads the authoritative store at read time, or has explicit cache invalidation tied to the source's write events. A local mirror (checkout, cache, replicated config) is acceptable only if refreshed before the read or the data is immutable.
- Before adding a queue, state machine, watchdog, retry loop or cache, grep for the primitive that already does the job and extend it. A review that keeps saying "route through X" means the design chose the wrong abstraction. If the existing primitive truly cannot be extended, say why in the PR.
- A non-blocking CI check that has been red on main for more than a few days is noise: fix it or remove it.
- A parser fix must invalidate every downstream quarantine category the defect can produce, including categories not named as parse failures.

**Anti-patterns:** a denormalized text field AND a join table holding the same relationship; a sibling repo's config read from a possibly stale local checkout; a second cache with different invalidation semantics next to the canonical one.

### P-003. Fail Visible
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-007. Verify Third-Party APIs Against Source
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-008. Guard Boundary Inputs
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-011. Privilege Escalation Is Self-Documenting
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-012. Side Effects Belong in Their Own Phase
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-014. Plans Are Executable Specs
> An agent following a plan copies the first code block it finds. Every block and every citation must be final and verified.
<!-- cat: correctness -->

**Rules:**
- Every code block in a plan is the final, correct version. Delete superseded drafts; do not leave them labeled "OLD:".
- Every `file:line`, symbol name, branch condition and schema constant cited in a spec or plan is grep-confirmed against HEAD in the same write session. A citation older than about 3 revisions without a re-grep is a violation. Before handoff, sweep the doc for `file:line` patterns and verify each resolves to the claimed symbol.
- If a plan references a third-party API, verify it exists (package exports or source) before including it.
- Spec body, Implementation and Acceptance are one contract: when a body edit lands, update the matching Implementation step and Acceptance criterion in the same edit. Review passes only when all three agree under independent grep.

### P-015. Data Egress Needs Explicit Permission
> Default-deny for outbound data. Explicit allowlist per sink.
<!-- cat: security -->

Data leaving the system (chat platform, database, external API) needs an allowlist of approved fields per destination, sanitized for that sink; each channel has its own escaping rules.

**Anti-patterns:** free-text fields posted to an external service unsanitized; sensitive fields reaching a destination with no field-level policy; direct API calls that bypass the routed comms layer, so future gates miss them.

### P-016. Deletion Is Productive Work
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-017. Ratchet Every Migration, and Ship New Gates Soft With an Exit Criterion
> Without a ratchet the old pattern creeps back within weeks. A warn-only gate with no ratchet date is dead code.
<!-- cat: data-integrity -->

**Rules:**
- When a migration completes, add an automatic check (pre-push hook, CI, nightly lint) that blocks the old pattern. 90% migrated is 0% without the ratchet. Pin dependency versions exactly; `>=` in a manifest means no reproducibility.
- The ratchet covers EVERY layer where the old pattern can re-enter: instruction strings, docs, reviewer prompts, docstrings, comments, test descriptions and log strings, not just code constants. Plans that rename code artifacts need an explicit code-trace round.
- Ship the forbid-rule and its exemption list together. A gate that cannot pass the very source it mandates is a shipping blocker. Anchor exemptions to parsed syntax context and exact spans (not path, marker or substring matches), and include fixtures for known bypass shapes (multiline or aliased forms), not just a repository scan. Validation wrappers forward scoped deletion acknowledgments with an explicit target and reason and reject blanket exemptions.
- A ratchet that scans the filesystem scopes to tracked files (`git ls-files`), not a raw tree walk, or it goes red on a live checkout by reading ignored runtime state and nested worktrees while passing in a fresh clone. Prefer AST-parsed path components over raw regex.
- New validators and restrictive guards start warn-only or audit-only (log and allow), with a written target (date, version or metric) for flipping to enforce. The ratchet is part of the same ticket. Design the boundary around the workflow people actually follow; a guard flipped off shortly after ship is a wrong-boundary signal, not operator error.

### P-017a. Validation Gates Exempt Their Own Sanctioned Instances
<!-- cat: retired -->

Merged into P-017 (2026-11).

### P-017b. Filesystem-Scanning Ratchets Scope to Tracked Files
<!-- cat: retired -->

Merged into P-017 (2026-11).

### P-018. Cognitive Budget
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-019. Check-Act Ordering and Irreversibility Boundaries
> A failed check must prevent the side effect. Front-load every abort-capable check before the first irreversible operation.
<!-- cat: correctness -->

**Rules:**
- Validate revocation, expiry and existence before inserting into any attempts, audit or rate-limit table; a write before the decision records phantom attempts. Charge item-level failures only when the evidence store was available to evaluate the item; record infrastructure outages as run-level errors.
- Classify each step by reversibility (resource creation, process spawn, notification, payment capture). Make the first irreversible step as late as possible with all checks ahead of it. Cleanup-after-optimistic-create paths are rarely tested.
- Atomic means the check, the act and any rollback share the SAME guard scope. A lock over three files does not protect a fourth. Rollback must be no wider than the forward guard: path-scoped operations over a hard repo reset, a scoped delete over a table truncate, invalidating the keys you wrote over flushing the cache.
- A pre-act gate that predicts a destructive primitive's success must key on the exact scope that primitive uses, not a "more canonical" base.
- When inserting a mutation into a multi-stage pipeline that restores state from a canonical store, trace every downstream writer that re-reads the store: a later stage can silently revert the mutation. Keep an explicit re-read hazard list.
- Commit imported content and its dedup ledger in one crash-atomic transaction, and lock the whole read-merge-write sequence. Publish an artifact set with a commit marker written last; individually atomic replacements do not make the set atomic.
- UI state init order: factory defaults, then user overrides, then render.

### P-019a. Irreversibility Boundaries
<!-- cat: retired -->

Merged into P-019 (2026-11).

### P-019b. Gate-Prediction Scope Matches the Predicted Primitive
<!-- cat: retired -->

Merged into P-019 (2026-11).

### P-019c. Trace Downstream Re-Read Hazards in Pipeline Mutations
<!-- cat: retired -->

Merged into P-019 (2026-11).

### P-020. Column Allowlists on Mass-Assignment
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-021. Platform Limits Drive Architecture, Not Workarounds
> When a platform imposes a hard limit, design around it architecturally; do not tunnel through.
<!-- cat: integration -->

Hosting tiers, upload caps, body-size limits and storage quotas each have a canonical architectural response.

**Rules:**
- Large uploads: three steps (server issues a signed or session URL, client uploads direct to the destination, client confirms). Never proxy large payloads through your own function.
- Document the constraint in the plan that introduces the feature so refactors do not regress to the naive approach.
- Recurring writers against cap-bound external tiers need a volume estimate in review: `tick_rate x per_tick_payload x retention`. Above about 5% of the cap per retention period, redesign before merge.

**Anti-patterns:** bumping a config flag to "bypass" a platform limit; a serverless function as an upload proxy to blob storage; a frequent cron writer on an audit table with no sustained-volume math.

### P-022. Soft-Gate, Then Ratchet (With an Exit Criterion)
<!-- cat: retired -->

Merged into P-017 (2026-11).

### P-023. Explicit State Machines for Async UI
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-024. Time-Indexed Calculations Recompute Per Period
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-025. Entities Own Their Own Assumptions
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-026. Fixtures and Schema Tests Carry the Real Producer Shape
> Every stable output shape has a schema test that fails on drift, and every fixture carries what the real producer emits, schema metadata included.
<!-- cat: testing -->

**Rules:**
- Public output shapes (API responses, pipeline snapshots, file formats) consumed by 2+ modules get a contract test asserting required fields, types, defaults and load errors. The expected shape lives next to the producer. Skip purely internal shapes that change weekly.
- Fixtures for state-mutating code include the same schema metadata production enforces (`schema_version`, type discriminators); regenerate them with the schema. Old-shape fixtures let a fix emit schema-violating fields that pass tests and crash the production writer.
- Capture a real payload from the actual producer, including its generic defaults (for example `application/octet-stream` for every non-raster file), so fallback logic keyed on empty or missing values is exercised. Hundreds of green tests over idealized payloads ship a broken boundary.
- Pin shared processing boundaries, such as a messaging adapter, with golden outputs, including exact correspondence between model input and citation sources after truncation.

### P-027. E2E Tests Run Against Production Builds
> A dev server that compiles routes on first request makes the test race the compiler and lose.
<!-- cat: testing -->

Dev-server flakes show up as aborted requests, inconsistent first paint and skeleton-vs-hydrated mismatches in visual snapshots.

**Rules:**
- CI E2E runs against a production build with precompiled routes. Authoring against dev locally is fine. Toolchains that do not lazy-compile do not need this rule.
- Pixel-diff thresholds are empirical: 0% goes red on every run; a small tolerance still catches real regressions.
- Wait for known-lazy components to exist in the DOM before snapshotting.

### P-028. Test Fixtures Are Deterministic and Idempotent
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-030. Audit Logs Are Append-Only, With Tamper-Evidence Tiers
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-032. Idempotency for Mutations
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-033. Parse, Don't Validate
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-034. Observability Before Automation
> Every autonomous action logs its decision and dry-runs before it acts. Log state changes, not evaluations.
<!-- cat: reliability -->

**Rules:**
- Each autonomous action emits a structured event: `{action_id, ts, considered, decision, reason, would_act, did_act, result}`. "Skipped" with no reason is a dead log. The same `action_id` twice means retry, not two occurrences.
- New automations ship in dry-run mode (`would_act=true, did_act=false`) and are reviewed against expectations before writes are enabled.
- Activate monitoring before the first scheduled attempt, independently of successful publication, so initial credential and setup failures are observable.
- Per-row evaluators (reconcilers, syncers, watchers) log only state transitions. Routine guard-rule skips are noise and fill the table at rows x ticks. Error and contention skips ARE signal; log them. Test: would an operator search for this row during an incident?

### P-035. Code-Coordinate Citations Grep-Confirmed at Write Time
<!-- cat: retired -->

Merged into P-014 (2026-11).

### P-036. Review Loops Have a Complexity Ceiling, and Each Surface Converges Independently
> Past round 4 of a review loop, fix passes introduce regressions. Convergence is by defect class, not reviewer agreement.
<!-- cat: review-process -->

**Rules:**
- Skill-governed loops cap at 6 rounds; ad-hoc model-run loops cap at 3.
- If round N+1 findings carry the same defect-class tag as round N, halt and promote residuals to follow-ups. When findings drop from safety-critical (data loss, races, privilege escalation, broken contracts) to polish (style, wording), call convergence regardless of round count.
- Spec, plan and ship reviews are independent surfaces with separate defect classes: spec review catches architecture and schema, plan review catches env var names, file locations and library specifics, ship review catches integration drift and sink leaks. Budget rounds for each separately; a converged spec does not predict a converged plan.
- The terminal state is the operator's judgment of remaining severity, not chain agreement.

### P-037. Cross-Model Diversity for Code-Symbol Review
> Same-model self-review fills in plausible-but-wrong names from the same prior. A second model family has different blind spots.
<!-- cat: review-process -->

**Rules:**
- A spec or plan that cites concrete code symbols gets at least one cross-model review pass. Single-model multi-round review is fine for architectural decisions, not for code-coordinate accuracy.
- Reviewers from distinct post-training lineages catch different defect classes; "the reviewer found nothing" proves little when only one family reviewed.

### P-038. Spec Body + Implementation + Acceptance Are a Single Contract
<!-- cat: retired -->

Merged into P-014 (2026-11).

### P-040. Liveness Signal Independent of Monitored Subsystem
> A heartbeat that shares the runtime it monitors is blocked exactly when it matters.
<!-- cat: reliability -->

**Rules:**
- Event-loop liveness needs an OS-thread watchdog, not another async task. Thread-group liveness needs a separate process. Process liveness needs an external probe, not self-report. A same-layer signal detects cadence drift, not a wedged loop.
- A heartbeat must not depend on the resource whose exhaustion you are detecting (a DB-backed heartbeat for DB-connection exhaustion).
- For an expected event that may never arrive (callback not fired, acknowledgment lost, process exited silently), add a bounded watchdog that forces a safe, idempotent transition after a deadline (prefer clearing state over acting) and logs when it fires. Treat "no response" and "rejected" as different recovery paths. Use exponential backoff when the retry has side effects, fixed interval when it does not.

### P-042. Schema-Aware Test Fixtures
<!-- cat: retired -->

Merged into P-026 (2026-11).

### P-044. Tolerate-Failure Modifiers Are Blanket Switches
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-045. The Workaround Tool Is the Regression Vector
<!-- cat: retired -->

Merged into P-057 (2026-11).

### P-046. Persist-Then-Acknowledge for Shared Message Buses
> On a single-stream bus, the offset advance cannot be driven by per-conversation completion. Persist durably with fsync, advance the offset eagerly, replay from the store on restart.
<!-- cat: reliability -->

Gating the offset on per-conversation work backs up the whole bus behind one slow conversation, and a partial advance loses messages on restart.

**Rules:**
- The offset tracks what the bus delivered; per-conversation state tracks what is finished. Persist on receipt, advance on persist-success, drive work from the store, replay from the store (not the bus) after restart.
- Trace cross-topic concurrency through the bus's real offset semantics; do not assume per-conversation isolation.
- Match durable apply and advancement to a stream cursor's full scope, including every account it consumes.
- Treat notification-throttle advancement as acknowledgment: persist a durable notification first, and schedule renewal of an expiring recovery link before it expires.

### P-047. Spec, Plan, and Ship Convergences Are Independent Surfaces
<!-- cat: retired -->

Merged into P-036 (2026-11).

### P-048. Authoritative Store Beats Local Mirror
<!-- cat: retired -->

Merged into P-002 (2026-11).

### P-049. Deny-List Growth Is an Upstream Failure
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-050. Exact-Shape Resolution for User-Supplied Identifiers
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-051. Rollback Mutation Surface Matches Protection Surface
<!-- cat: retired -->

Merged into P-019 (2026-11).

### P-052. Verify Remote Non-Landing Before Local Compensation
> A timeout is "you don't know", not "failed". The remote operation may have landed with the acknowledgement lost.
<!-- cat: correctness -->

**Rules:**
- After an ambiguous remote operation (push timeout, deploy timeout, RPC error after send), fetch authoritative remote state before discarding local work.
- Push timeout: fetch the branch and check whether local HEAD is already an ancestor of the remote tip before any reset. `try { push } catch { hard reset HEAD~1 }` with no fetch creates phantom-commit divergence.
- External API error: retry with the same idempotency key, or query the remote by operation ID, before compensating. Upstream may have committed without persisting the key.
- Define the verification protocol in the same change that defines the rollback.

### P-053. Retention Declared at Write-One
> Every append-only table declares retention in the PR that introduces its first writer.
<!-- cat: data-integrity -->

Growth is `writers x write_rate x payload x time`; "no retention" means unbounded.

**Rules:**
- Commit the policy as code or a migration (time-based sweep, size-based eviction or external archival), not as a comment or chat note.
- Size it against the writer's sustained volume and storage tier (same math as P-021's recurring-writer rule). "Compliance requires X years" is a policy; "we might want old rows someday" is not.

### P-054. Log State Changes, Not Evaluations
<!-- cat: retired -->

Merged into P-034 (2026-11).

### P-055. Compensate for Absent Signals
<!-- cat: retired -->

Merged into P-040 (2026-11).

### P-056. Stateful Identity Is the Full Tuple
> A session is not an ID. It is ID + workdir + isolation context + capabilities + extras. Resume by ID alone and it silently diverges.
<!-- cat: data-integrity -->

The failure is silent divergence, not a crash.

**Rules:**
- When adding a dimension to a stateful resource, grep every path that persists, restores, clones or resumes it; each must propagate the new dimension. Persistence schemas hold all identity dimensions.
- Resume validates the full tuple is restorable and fails loudly over partial restoration.
- When adding a second entity to a one-entity system, audit every hardcoded constant and persistence key; scope cache and local-storage keys per entity.

### P-057. Behavioral Safety Parity Across Parallel Implementations
> When a destructive operation has more than one implementation, a safety fix to one must reach all of them, or collapse them to one.
<!-- cat: reliability -->

Typical case: a canonical code entrypoint plus a shell or markdown reimplementation. This is behavioral parity, not byte-equality of command and skill twins.

**Rules:**
- Inventory every implementation before fixing one; apply the full guard set to all in the same change, and state in the PR where the others are. Prefer collapsing to one entrypoint.
- Inventory every authorization-file reader when hardening integrity checks; inspect the named object and its parent without following symlinks to borrow target metadata.
- An invariant enforced by a design (resolver, access policy, permission gate) is regressed by any tool that goes around it, and "just this once" cleanup tools are the usual vector. Extend behind the same gate; if a bypass is unavoidable, run it with elevated, logged, single-invocation authorization, never as a committed callable.

### P-058. Propagate Test-Hermetic Fixes to the Shared Production Call-Site
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-059. Extend the Canonical Primitive, Don't Reinvent It
<!-- cat: retired -->

Merged into P-002 (2026-11).

### P-061. Bound Untrusted-Input Work by Its Actual Cost Curve
> A single character cap cannot bound work whose cost is structural or whose paths differ by orders of magnitude.
<!-- cat: security -->

**Rules:**
- Render guards for untrusted content need a structural budget (line, block or AST-node count) as well as a byte cap; fall back to raw text above it. A short list-marker string repeated thousands of times becomes thousands of DOM nodes.
- When two work paths have very different costs (explicit-language vs auto-detected syntax highlighting, cached vs computed), give each its own cap or drop the expensive path.
- Gate only the expensive work (decode, parse, highlight, render), never a legitimate capability's output path. A reviewer suggestion to "bound earlier" can move the bound onto a capability gate; check what else that seam controls.
- Run cheap eligibility checks before expensive artifact validation, and stop at the newest valid eligible artifact so damaged retained history does not inflate routine work.

### P-063. Never Truncate Aggregation Sources
> A LIMIT on a query whose rows feed a total, count or financial summary silently truncates data downstream code treats as complete.
<!-- cat: data-integrity -->

Default row caps on API layers hit the same way (for example a `.limit(1000)` or a platform max-rows setting): every row shown looks right while the total is wrong.

**Rules:**
- Never limit a query whose results feed an aggregation. If the source must be bounded for performance, aggregate in the database (`SELECT sum(...)`, `count(*)`).
- Paginated views over an aggregated set carry a separately queried total; a footer count equal to the page size is the failure signature.
- Audit every consumer before adding a limit to a shared query.

### P-064. Key Guards on the Protected Artifact, Not a Proxy
<!-- cat: retired -->

Merged into P-091 (2026-11).

### P-065. Distinguish Measured-Zero from No-Data in Fallbacks
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-066. Auto-Delete Only Provably-Untouched Rows
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-067. Isolate Untrusted Agents by Capability, Not Sandboxing
> A permission allowlist on a shell-capable agent is UX, not a boundary. Anything the model can place in a tool argument is untrusted for auth, audit and egress.
<!-- cat: security -->

The durable boundary is capability: the agent process holds no scoped credentials (they live in separate-OS-user tool processes it cannot read), reaches the outside only through a trusted broker plus egress fence, and any actor identity or confirmation is stamped out-of-band by the broker.

**Rules:**
- No env-var credentials in the agent process; do not describe a prompt or allowlist as containment. Trusting `confirmed: true` or `actor: "operator"` written into a tool call is the canonical failure.
- Assign evaluation-case provenance through trusted construction; inline origin claims cannot satisfy an approval requirement for a minimum amount of real data.
- Untrusted text never rides inline in a command string an agent runs: text interpolated into a quoted argument (`--claims '<json>'`) can close the quote and execute. Write it to a file and pass the path, or feed stdin. In slash-command or skill templates, early shell directives run before any in-skill validation.
- A production entry strips inherited test overrides (by namespace, so new ones are covered) and interpreter startup variables before launching its core or another interpreter; inherited settings can change authorization before payload validation. Keep intentional overrides on separate test entries.

### P-068. Design Boundaries Against the Observed Workflow, Ship Them Soft
<!-- cat: retired -->

Merged into P-017 (2026-11).

### P-069. Never Squash-Merge Across a Diverged Base
> A branch built on base A, squash-merged into diverged base B, silently drops code: B's version of concurrently edited files wins with no conflict.
<!-- cat: data-integrity -->

The PR shows "merged" while only some files (often just docs) landed.

**Rules:**
- A change that must land on two diverged bases is applied to each explicitly (cherry-pick onto each).
- Rebase onto the true target base before merging, and after any merge across possibly-diverged bases grep the result for the intended code. "Only the docs landed" is the signature; re-check the code files.

### P-079. Reaching a Limit Is Not Success
> A cap, budget or timeout says nothing about whether the task was accomplished. Report the limit as a limit, never as completion.
<!-- cat: correctness -->

An agent or loop that stops on a round cap, token or time budget, retry ceiling or truncated tool result has learned only that it ran out of allowance. Conflating the two yields false "complete", "converged" or "passed" claims that hide unresolved findings and skipped verification.

**Rules:**
- Carry "resolved" vs "limit reached" as an explicit flag (for example `resolved | limit_reached`), not just a prose reason.
- A limit-halt summary names what is still open: deferred findings, unrun verification, incomplete tasks. It never claims success it did not verify, and "verification passed" is stated only when verification ran and passed.
- A limit reached with unresolved safety-critical work halts and surfaces it; it is not downgraded to "complete with deferred items".

**Anti-patterns:** "Converged at round 6" when round 6 was the hard cap and blocking findings remain; a truncated, rate-limited or timed-out tool result read as evidence the task succeeded; a `converged=True` that a caller cannot tell from a cap-halt.

### P-080. Test Fixtures Carry the Producer's Real Output Shape
<!-- cat: retired -->

Merged into P-026 (2026-11).

### P-081. Derive Asserted Facts From Ground Truth, Not Model-Supplied Variables
> A fact a deterministic output asserts must come from what the harness can read, never from a variable the model was told to set.
<!-- cat: correctness -->

A model that omits a defaulted flag on an exceptional path makes the output print the success value: it fails open.

**Rules:**
- Derive status, counts and verdicts from a store, artifact or the filesystem, not from a model-emitted value.
- Never default a correctness-bearing flag to its success value; an absent flag reads as not passed.

### P-082. Sanitize Third-Party Responses Before Persisting or Logging
> Providers embed live credentials in response fields; strip them at the ingest boundary.
<!-- cat: security -->

Access tokens in paging URLs (`paging.next`), signed URLs, refresh tokens and auth echoes in ordinary-looking fields all land in tables, caches and debug logs if stored verbatim.

**Rules:**
- Redact known secret-bearing fields, or allowlist only the fields you need, before data reaches a table, cache or log. Treat URLs in responses as token-bearing.
- Redact inbound message text at the first persistence boundary and check coverage with representative real samples; egress-only redaction leaves secrets at rest.

### P-083. Fix Recurring Failure Modes With a Mechanism at the Choke Point
<!-- cat: retired -->

Merged into P-086 (2026-11).

### P-084. Run-It Gates Catch Defects Static Review Cannot
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-085. Prefer a Decoupled Integration Seam Over Adapting a Dependency You Don't Own
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-086. Constraints That Must Bind Go in the Harness, Not in Prose
> A running session never reloads its instructions, and compaction can drop a constraint unnoticed. Recurring failures need a mechanism at the choke point.
<!-- cat: agent-runtime -->

**Rules:**
- A rule that must bind in-flight sessions lives where every tool call passes (a PreToolUse hook, a tool-layer veto), not only in a document. A doc binds future sessions only.
- Once a failure has recurred despite instructions, add a mechanism at the shared seam (a sentinel at the line every caller reads, a hook that vetoes the unsafe action) instead of a third "please remember" preamble.

### P-087. A Baseline Measured in a Different Environment Is Not a Baseline
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-089. Test a Gate on Both a Passing and a Failing Case, Through the Real Host
> If a pass case and a fail case return the same verdict, you are reading the wrong channel, not observing the gate.
<!-- cat: testing -->

**Rules:**
- Verify a gate with one case that should pass and one that should fail, and confirm the verdicts differ. A guard that signals via a structured decision and exits 0 either way looks identical when you check the exit code.
- Assert a protection functionally: trigger the guarded event and confirm it was blocked or recorded. Config shows a rule is listed, not that it works (a ruleset can sit loaded but inactive; a full disk suspends logging). Before declaring runtime readiness, verify the installed target, matcher coverage and exact command, plus a real denial probe through the active installation.
- Assert each independent protection's own signal as well as the final verdict; a denial proves only that at least one check fired. Keep signal assertions even when another rule currently rejects the same inputs.
- A guard is fail-closed only if its host also refuses after timeout or error. Exercise timeout and error paths through the real host runner, and revisit the execution budget when added validation makes the guard slower.
- When extending authorization, compare denial sets and keep all baseline denials, with a baseline usable without version history. Require adversarial and negative coverage inside every independently approved partition; aggregate coverage lets a partition pass vacuously.

### P-090. Assert a Protection Functionally, Not by Its Configuration
<!-- cat: retired -->

Merged into P-089 (2026-11).

### P-091. Key Guards on the Artifact and the Defect, and Invert Bypassable Guards to Fail-Closed
> A guard keys on the protected artifact itself, is stated from how the defect failed, and fails closed when bypass shapes are unbounded.
<!-- cat: review-process -->

**Rules:**
- A content-integrity guard hashes or reads the protected artifact directly, not a companion hash, pinned constant or sibling file; a proxy leaves unprotected siblings and can be rewritten. Direct keying usually deletes the proxy-parsing code. Confirm a "more canonical" key is still what the protected operation uses.
- Phrase a guard from what the defect did wrong ("fires ONLY on its schedule"), not from the fix just made, and test it against the original defective input. A guard written alongside its fix certifies the exact defect it was added to catch.
- If a reviewer keeps defeating a guard with new bypass shapes, invert it to "unprotected unless proven intact", and pair the inversion with tests that harmless cases stay unflagged, or it degrades into fail-always and gets ignored.

### P-092. Read the Code Path Before Attributing a Failure to a Tool
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-093. State a Guard From the Defect's Failure Mode, Not the Fix
<!-- cat: retired -->

Merged into P-091 (2026-11).

### P-094. A New Test Must Fail for the Right Reason Before It Passes
> A test that goes red only because the symbol is missing, or never goes red, proves nothing about the behaviour it names.
<!-- cat: testing -->

An ImportError, NameError or collection error on the first run says "the code is not there yet", which a test asserting the wrong answer would also say.

**Rules:**
- Run the new test before implementing and read the failure: it must be an assertion failure naming the expected behaviour. If the symbol does not exist yet, add a stub returning a wrong answer first.
- For high-stakes logic (allow/deny, gates, deletion and retention, money, verdicts), write the expected red into the plan and, where possible, check it mechanically by replaying the new tests against the base.
- Never edit an expected value to match the output without a stated reason the old one was wrong.

### P-095. Prove a Test Can Fail: Break the Code on Purpose
> After green, break the line the test protects and confirm the test notices.
<!-- cat: testing -->

Coverage says a line ran, not that an assertion depends on it. On guard, gate and verdict code the dangerous break is the quiet one: an error branch returning "ok", `>=` becoming `>`, a `raise` becoming `pass`.

**Rules:**
- For each rule the change implements, name one deliberate break and confirm the new test goes red. Apply it to a saved copy or throwaway sandbox, never by discarding working-tree changes. Pin every fail-closed error branch and every changed boundary with a test on each side of the edge. A surviving mutant on a high-stakes line is a finding.
- Code that signals processes, deletes or writes real resources is tested (mutation runs included) only against fakes or an isolated namespace. Use a fake process table or pid namespace for signals and a throwaway tmp tree for file writes; a pid namespace does not protect files. A sandbox copy of the files does not contain a mutant that kills every process it can see. Classify signal targets by ownership evidence (owned, cwd-only, unknown) and halt on ambiguity. Never point a teardown at a path derived from the real home or data directory.
- Restore verification owns the temporary database server lifecycle and uses a private connection namespace so restored data cannot reach another session's database.

### P-096. Test Behaviour Through the Real Entry Point, Not the Source Text
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).

### P-097. Safety Nets Cover Partial Failure, Not Just Total
> A safety net scoped to total failure never fires for partial failure, and partial is the more common mode.
<!-- cat: reliability -->

**Rules:**
- Reconcile against the backup item by item; do not threshold on emptiness or a minimum count. A "restore if empty" guard misses the realistic loss of a handful of recent files.
- Test recovery with a partial loss, and drill restores against non-empty archives (an empty archive passes trivially).
- Compare expected and discovered inventories in both directions: reject unexpected items and missing registered items.
- Validate a historical artifact against the versioned inventory it was proven complete against at creation, stored alongside counts and integrity evidence. An evolving registry must not retroactively invalidate an old complete snapshot, and counts without the expected item set prove little.

### P-098. Exercise Destructive Code Only Against Fakes or Isolated Namespaces
<!-- cat: retired -->

Merged into P-095 (2026-11).

### P-099. Untrusted Text Never Rides Inline in an Agent-Run Command String
<!-- cat: retired -->

Merged into P-067 (2026-11).

### P-100. Gate Actions on Who Bears the Cost, Not on Reversibility
> Whether to act unasked depends on who bears the cost of a mistake and whether they can undo it, not on whether you can.
<!-- cat: agent-runtime -->

**Rules:**
- Act without asking when the work is authorized and a mistake costs only you or your own project.
- Ask, or stop, when the cost lands on another party (a person mid-session, other sessions on a shared channel, a third party), however cheap the undo is for you. A shared-service restart is not "safe" because it can be repeated.

### P-101. Sanitize Inherited Execution Controls at Production Entry
<!-- cat: retired -->

Merged into P-067 (2026-11).

### P-102. Test Independent Protections Independently
<!-- cat: retired -->

Merged into P-089 (2026-11).

### P-103. Fail-Closed Includes the Host's Failure Semantics
<!-- cat: retired -->

Merged into P-089 (2026-11).

### P-104. Render Durable Documents from Frozen Inputs
> Freeze content, configuration and issuance time, then persist the values used to render.
<!-- cat: data-integrity -->

The historical record must describe the document the recipient received.

**Rules:**
- Freeze document content, shared rendering configuration and issuance time before rendering; pass them explicitly through rendering and persistence.
- Persist exactly the values used in the delivered artifact; avoid later clock or live-config reads (a later issuance timestamp than the one shown, frozen rows rendered with live shared config).

### P-105. Preserve an Artifact's Completeness Contract
<!-- cat: retired -->

Merged into P-097 (2026-11).

### P-106. Reserve Time for the Entire Remaining Operation
<!-- cat: retired -->

Retired 2026-11 (general practice current models follow unprompted; A/B 2026-11-13).
