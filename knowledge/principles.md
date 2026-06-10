<!-- Goodfellow seed principles — curated from easelyte's cross-repo design knowledge.
     Version: 0.1.0. Do not edit; plugin-owned (see README). -->

# Universal design principles (core)

Stack-agnostic design rules. They apply to any codebase regardless of language or
framework. Web/JS/SQL-specific rules live in `principles-web.md` (opt-in).

### P-001. UI Hiding Is Never Authorization
> Hiding a control doesn't prevent someone from invoking the underlying operation directly.

If an action should be restricted, enforce it on the server / trusted side — in a
gate, guard, or the handler itself. Client-side hiding (removing menu items,
disabling buttons) is UX, not security.

**Anti-pattern:** a navigation link is hidden for unprivileged users, but the
endpoint still serves the data when called directly.
**Fix:** every restricted operation has a server-side authorization check; section-level
gates aren't sufficient when sub-routes have different permission levels.

### P-002. Canonical Source of Truth
> If you need a sync script to keep two things in agreement, you have one source too many.

Every piece of data has exactly ONE authoritative location; everything else is derived.
When adding a normalized structure to replace a denormalized field, either drop the old
field in the same release or document which is canonical.

**Anti-pattern:** a denormalized text field AND a join table holding the same relationship;
two pages computing the same number with different algorithms.
**Fix:** one computation, one implementation. Divergent calculations of the same value
are data-integrity bugs.

### P-003. Fail Visible
> Silent failures are debugging dead ends.

When something goes wrong, the system must produce evidence. Auth failures that redirect
with no error code, triggers that swallow exceptions, state changes that skip the activity
log — all create invisible state loss.

**Rules:**
- Failure paths carry an explicit error signal (code, parameter, log line), not a bare redirect.
- Event-time triggers have exception handling — a failing trigger must not silently break the flow.
- Every mutation that changes entity state writes a log entry in the same operation.
- Errors are classified: transient (retry) vs permanent (flag for human review).

**Anti-patterns:**
- `except Exception: pass`
- A callback that silently redirects to a default page on failure
- A state change that doesn't write to its activity timeline

### P-007. Verify Third-Party APIs Against Source
> Don't trust AI-generated API signatures for third-party libraries.

LLMs hallucinate API signatures, and package APIs change between major versions.
"It looks right" is not verification.

**Rules:**
- Before using a library API in a plan or implementation, check the package's actual exports, docs, or source.
- "Source" includes runtime behavior. For any third-party API/CLI flag/protocol claim that drives load-bearing architecture (a whole feature collapses if it's wrong), run a minimal empirical POC BEFORE committing the architecture.
- Use a library's built-in read-only/disabled mechanism, never CSS or display hacks.

**Anti-pattern:** building architecture around a documented flag behavior without ever
running the flag once; a POC refutes it N rounds later as rework.

### P-008. Guard Boundary Inputs
> Trust internal code. Validate at system boundaries.

Data entering the system (user input, API responses, callbacks, message/queue payloads,
file contents) needs validation. Data flowing between internal modules does not.

**Rules:**
- Guard collection queries against empty inputs before issuing them.
- Use a null-preserving coalesce (e.g. nullish coalescing) rather than a truthy OR when the value could legitimately be `0`, `""`, or `false`.
- Use the "zero-or-one row" query variant when a lookup might return nothing, rather than the "exactly one" variant that throws on no rows.
- A multi-step handshake (e.g. an auth exchange) uses the same client/transport on both legs.

**Anti-patterns:**
- Passing an empty list to a query helper that doesn't handle the empty case
- `value || null` silently converting `0` to `null`
- A "fetch exactly one" call against a table that might be empty

### P-011. Privilege Escalation Is Self-Documenting
> Every elevated-privilege function needs a comment explaining why.

Functions that run with elevated privileges bypass the normal access layer. Future
editors need the "why" inline, not buried in history.

**Rules:**
- Split clients/credentials by trust tier: a normal/anon client, a session-bound server client, and a privileged admin client that is a named dangerous tool.
- The privileged client explicitly disables session persistence/auto-refresh — it is not a fallback.
- Comment every elevated-privilege function at its definition site.

### P-012. Side Effects Belong in Their Own Phase
> If a function both reads a file and writes a file, it is doing too much.

Structure logic in three phases: (1) gather inputs, (2) compute decisions as pure logic,
(3) execute side effects. Event handlers should do one thing; side effects with failure
modes belong in their own isolated path with their own error handling.

**Anti-patterns:**
- A handler that sets state AND runs an unrelated side computation that can throw
- Sending a notification mid-computation before knowing if the whole operation succeeds
- A function that returns a value AND writes to disk

### P-014. Plans Are Executable Specs
> Never leave broken code in a plan document.

An agent following a plan will use the first code block it finds. Draft/broken versions
create copy-paste traps.

**Rules:**
- Every code block in a plan is the final, correct version.
- Delete superseded versions — don't leave them labeled "OLD:".
- If a plan references a third-party API, verify the API exists before including it.

### P-015. Data Egress Needs Explicit Permission
> Default-deny for outbound data. Explicit allowlist per sink.

Data leaving the system (to a chat platform, a database, an external API) needs an
allowlist of approved fields per destination, sanitized for that specific sink — each
output channel has its own escaping rules.

**Anti-patterns:**
- Free-text fields posted to an external service without sanitization
- Sensitive fields flowing to a destination without a field-level policy
- Direct API calls bypassing the routed comms layer, making future gates easy to miss

### P-016. Deletion Is Productive Work
> Every line of code that exists is a line that can break, confuse, or mislead.

Dead code, compatibility shims, unused config entries, and deprecated scripts are
liabilities, not neutral. Track lines removed as a positive metric.

**Anti-patterns:**
- `# REMOVED: old_function() — see commit abc123` (just delete it; version control has history)
- Keeping a 700-line script around because "we might need it later"
- Redundant access policies that duplicate coverage already provided elsewhere

### P-017. Ratchet Every Migration
> Without a ratchet, the old pattern creeps back within weeks.

When a migration completes (format change, API deprecation, pattern cleanup), add a
check that prevents the old pattern from returning.

**Rules:**
- Complete the migration before celebrating — 90% is 0% without the ratchet.
- The check runs automatically (pre-push hook, CI, nightly lint).
- Pin dependency versions exactly; `>=` in manifests means no reproducibility.
- The ratchet covers EVERY layer where the old pattern can re-enter — instruction strings, docs, conventions, reviewer prompts — not just code constants. Plans that move/rename code artifacts need an explicit code-trace round, because standard reviews check plan-text only and miss stale references in docstrings, comments, test descriptions, and log strings.

### P-018. Cognitive Budget
> If a reader cannot hold the entire file in working memory, they cannot reason about its behavior.

No script should exceed ~400 lines. If it does, it has multiple concerns and should be
split. N is small — linear scans beat complex data structures for lists under 50 elements.

**Anti-patterns:**
- A 2400-line script with mixed concerns
- Importing a priority queue for 15 items
- A utility module with 40 one-use helper functions

### P-019. Check-Act Ordering
> A failed check must prevent the side effect. Record the attempt only after you've decided the attempt was legitimate.

Any function that does {validate, mutate, side-effect} must order them so a failing check
short-circuits the rest. Writing to an audit/rate-limit table before deciding whether the
request is valid pollutes the audit trail and corrupts counters with phantom attempts.

**Related:** CWE-367 (TOCTOU), "Look Before You Leap."

**Rules:**
- Validate revocation, expiry, and existence before inserting into any attempts/log table.
- If the check-and-act pair can race concurrent mutations, make it atomic (transaction, row lock, or a single guarded statement).
- Atomic means the check, the act, and any rollback all run under the SAME guard scope. A lock that protects three files does not protect a fourth; a transaction wrapping one row does not protect another. Scope mismatch makes the safety check vacuous.
- For UI state init: apply canonical/factory defaults first, THEN user overrides, THEN render — reversing this makes the UI lie about its mode.

**Anti-patterns:**
- Inserting into an attempts table before checking the credential is still valid
- Decrementing a quota before validating the request was well-formed
- Acquiring a lock on a narrow scope, then running a repo-wide destructive rollback on failure (the rollback escapes the lock's scope)

#### P-019a. Irreversibility Boundaries
A stronger form: when a sequence mixes reversible and irreversible operations (resource
creation, process spawn, notification send, payment capture), identify the irreversibility
boundary and front-load ALL validation before ANY irreversible side effect. Classify every
operation by reversibility, then make the first irreversible operation as late as possible
with every abort-capable check ahead of it (the Saga "pivot transaction" idea).

**Anti-patterns:**
- Interleaving validation with side effects because the code "reads naturally" top-to-bottom
- Creating a resource optimistically and cleaning up on failure (cleanup paths are rarely tested)
- Leaving one check after the commit point "because it almost never fails"

#### P-019b. Gate-Prediction Scope Matches the Predicted Primitive
When a pre-act gate decides whether to perform a later destructive operation by predicting
that operation will succeed, the gate must key on the EXACT scope the destructive primitive
uses — not a "more canonical" base. A gate keyed on a broader scope can pass while the
predicted act still fails, producing exactly the partial state the gate was meant to prevent.

**Anti-patterns:**
- Substituting a "more correct"/"more canonical" base into a gate that predicts a scope-specific primitive
- Gating a remove on a different reference than the delete that follows it

#### P-019c. Trace Downstream Re-Read Hazards in Pipeline Mutations
When inserting a mutation step into a multi-stage pipeline that preserves prior state from
a canonical store, trace EVERY downstream writer that re-reads the store and ask: "does it
re-read and undo my mutation?" A mutation correct at its insertion point can be silently
reverted by a later stage that rebuilds from the store. Carry an explicit "re-read hazard"
list enumerating each downstream write that may overwrite.

**Anti-patterns:**
- Designing a pipeline mutation by reading only the insertion function, not the downstream writers
- Assuming a write "sticks" when a later stage rebuilds state from the canonical store

### P-020. Column Allowlists on Mass-Assignment
> Any operation that accepts a user-supplied field name needs an allowlist.

This is OWASP API6:2019 (Mass Assignment). Whenever a write accepts a dynamic field name
or spreads a request body into an update, an attacker can set fields you didn't intend.

**Applies to:** any handler with a dynamic `field` parameter, any "merge request body into
record" pattern, any ORM update fed raw input.

**Rules:**
- Define an explicit allowed-field set + a runtime check that rejects anything outside it.
- Never spread a request body directly into an update/insert — pick fields explicitly.
- When new fields are added, audit every mass-update path for exposure before merging.
- The allowlist lives next to the schema, not scattered across call sites.

**Anti-patterns:**
- An update keyed by a `field` name that comes straight from the client
- A blocklist (`filter out keys starting with "_"`) — blocklists always miss fields

### P-021. Platform Limits Drive Architecture, Not Workarounds
> When a platform imposes a hard limit, design around it architecturally — don't tunnel through.

Hosting tiers, upload caps, body-size limits, and storage quotas each have a canonical
architectural response. Discovering the limit and patching around it locally creates tech
debt the next developer inherits.

**Rules:**
- Large uploads: a three-step flow (server issues a signed/session URL → client uploads direct to the destination → client confirms). Never proxy large payloads through your own function.
- Document the constraint in the plan that introduces the feature, so future refactors don't regress to the naive approach.
- Prefer long-lived connections (resumable uploads, WebSockets, SSE) over polling when the platform supports them.
- Recurring writers against cap-bound external tiers require a daily-volume estimate at PR review: `tick_rate × per_tick_payload × retention`. Above ~5% of cap per retention period must be redesigned before merge.

**Anti-patterns:**
- Bumping a config flag to "bypass" a platform limit (it usually isn't bypassable; you're papering over)
- Using a serverless function as an upload proxy to a blob store
- Adding a frequent cron writer to an audit table without computing its sustained volume against the storage cap

### P-022. Soft-Gate, Then Ratchet (With an Exit Criterion)
> New validation ships warning-only; enforcement comes after producers catch up. A warn-only gate without a ratchet date is dead code.

This is Parallel Change / expand-contract applied to validators. Shipping a required schema
change on day 1 blocks every producer that hasn't updated; shipping it warn-only unblocks rollout.

**Rules:**
- New validators default to warn-only during rollout.
- Every soft-gate has a written target — a date, a version, or a metric — for flipping to required.
- The ratchet is part of the ticket, not "later" — an un-ratcheted soft-gate silently stops catching anything.
- Permanent severity tiers (security posture) are fine, but they're not a substitute for a ratchet.

**Anti-patterns:**
- A warn-only validator live for six months with no exit plan
- Flipping to required without checking all producers emit the field
- Fanning out severity tiers to avoid the hard decision of what counts as a failure

### P-023. Explicit State Machines for Async UI
> Boolean flags collapse under real async flows. Use explicit states with timeout-backed transitions.

The rule kicks in when state represents a multi-step async lifecycle (save, delete-with-confirm,
submit-with-retry) — not a binary toggle.

**Rules:**
- Use a discriminated-union enum (`idle | loading | success | error`), not parallel booleans.
- Success/error states auto-transition back to idle after a timeout.
- Split callbacks when redirects and in-place updates need different timing.
- Disable inputs during transient states so double-clicks can't fire the mutation twice.
- Confirm-destructive patterns: `idle | confirming | deleting` with cancel and auto-cancel timeout.
- 2-3 booleans is fine; the rule triggers at 4+ related flags or any async-with-error flow.

**Anti-patterns:**
- Parallel `isLoading` / `hasError` booleans — what does `isLoading && hasError` mean?
- Mutation UIs that "snap back" after save because the button state races the refetch
- Deleting with a single click and no confirm state

### P-024. Time-Indexed Calculations Recompute Per Period
> Values that depend on compounding or decaying state must be computed inside the period loop, not snapshotted at t=0.

Anything of the form `state[t+1] = f(state[t])` — amortization, accrual, interest on a
declining balance, cumulative reservoirs — must iterate from current state. Snapshotting
period 1 and reusing it produces silently wrong numbers that look plausible on the total line.

**Rules:**
- Track the mutating variable as a running value inside the loop, not a constant outside it.
- Derive each period's output from current state, not initial conditions.
- If all inputs are genuinely constant across the horizon, closed-form formulas are more accurate (they avoid cumulative floating-point drift) — the principle only applies when inputs change over time.
- When adding a time-varying input, audit every downstream calc for snapshot-from-t0 assumptions.

**Anti-patterns:**
- Computing one period's interest once, then reusing it for the full schedule
- "Year 1 cash flow × 30" for a 30-year projection with growth and vacancy factors

### P-025. Entities Own Their Own Assumptions
> When a system goes mono-to-multi, every hardcoded value migrates from "reasonable default" to "broken assumption."

Extends P-002. When a codebase grows from one entity (tenant, project, workspace) to many,
constants baked into formulas — ratios, counts, labels, storage keys — become bombs. Each
entity object must own its own assumptions, and persistence keys must be scoped per entity.

**Rules:**
- Domain objects carry their own parameters — never a hardcoded ratio or label baked into a shared formula.
- "Defaults with explicit override" is legitimate — global defaults are fine, but each entity must be able to override them, and overrides live in the entity's data, not in conditional branches.
- Local-storage / cache / cookie keys prefix with the entity ID, not a global key.
- Version persistence schemas so future changes can coexist.
- Mono-to-multi migrations audit every hardcoded string and constant before shipping the second entity.

**Anti-patterns:**
- A constant baked into a formula that now needs to support a second case
- An unnamespaced persistence key silently shared across entities
- A label that's correct for one entity and wrong for the rest

### P-026. Schema Tests for Persisted Output Shapes
> Every stable output — API response, pipeline snapshot, file format — has a schema test that fails on drift.

Related to consumer-driven contracts, but broader: applies to internal JSON files, pipeline
outputs, and any shape consumed by more than one module. Without drift detection, contracts
silently diverge and downstream breaks look mysterious.

**Rules:**
- Every public output shape has a contract test asserting required fields, types, defaults, and load errors.
- Tests run in CI; contract drift blocks the PR.
- Scope to STABLE shapes consumed by 2+ modules — purely internal, week-by-week shapes don't earn the maintenance cost.
- The expected shape lives next to the producer — producers own their contracts.

**Anti-patterns:**
- "We'll know if it breaks because something will log an error" — they don't, and you won't
- A snapshot test with no schema — catches byte-level change but not type drift
- Contract tests only on the producer side, with no consumer-side verification

### P-027. E2E Tests Run Against Production Builds
> A dev server that compiles routes on first request makes your test race the compiler and lose.

Dev-server flakes manifest as aborted requests, inconsistent first-paint timings, and
skeleton-vs-hydrated mismatches in visual snapshots.

**Rules:**
- CI E2E runs against a production build, with precompiled routes — not the dev server.
- Local test-authoring against dev is fine; CI isn't.
- Pixel-diff thresholds are empirical, not idealistic: 0% goes red on every run; a small tolerance still catches real regressions.
- Wait for known-lazy components to exist in the DOM before snapshotting.
- Toolchains that don't lazy-compile don't need this rule — the requirement is about bundlers with compile-on-first-request.

**Anti-patterns:**
- CI running the E2E suite against the dev server
- Visual regression thresholds of 0% ("pixel-perfect")
- Snapshotting immediately after navigation without waiting for async-rendered UI

### P-028. Test Fixtures Are Deterministic and Idempotent
> Fixed IDs. Trusted seeds. Running twice is a no-op.

Authed surfaces can't be snapshotted without consistent data. Random IDs break references
across scripts; non-idempotent seeds double-insert on re-run.

**Rules:**
- Fixture data uses fixed, human-readable IDs or deterministic hashes of stable inputs.
- Seed scripts run with trusted/elevated access to bypass row-level gates — fixtures are trusted input.
- Seeds check existence before inserting (upsert / on-conflict-do-nothing) — running twice produces the same final state.
- Seeds are versioned alongside schema; a migration that breaks a fixture gets a matching seed update in the same PR.

**Anti-patterns:**
- A random UUID in a fixture — IDs change every run, references break
- Seeds that fail on the second run with duplicate-key errors
- Seeding with an unprivileged client, then wondering why access rules block the insert

### P-030. Audit Logs Are Append-Only, With Tamper-Evidence Tiers
> If the log can be edited, it isn't proof.

Proof-of-action records (attestation, rotation, approval) only ever append. A missing log
means "never happened" (negative proof). Staleness is computed from timestamps, not maintained by hand.

**Tamper-evidence tiers** (pick based on what the log protects):
1. Append-only by convention — adequate when the threat is "forgot to log," not "adversary with write access."
2. Hash-chained entries — each entry includes a hash of `(prev_hash + body)`; detects in-place edits.
3. WORM storage — physically prevents overwrite; required for regulated data.
4. Ledger database — cryptographic proof over the whole table; use when compliance demands it.

**Rules:**
- The "no file" state is meaningful: a missing attestation is red/never, not yellow/unknown.
- Staleness is computed (`now - last_entry.ts`), not stored.
- Audit logs have a written retention policy — immutable-forever is expensive and rarely necessary.
- Never expose update/delete against an audit table to application code.

**Anti-patterns:**
- An update statement against an audit table — if the code can do this, the log is not proof
- A "last attested at" column on the entity being attested — collapses the history
- A log file that occasionally gets rewritten "to clean up old entries"

### P-032. Idempotency for Mutations
> Every mutation that can be retried needs an idempotency key. Webhooks retry. Queues retry. Agents retry.

Canonical pattern: an `Idempotency-Key` header. Applies to webhook handlers, queue consumers,
agent retries, signup flows — any mutation where the caller might not learn the first attempt's outcome.

**Rules:**
- Public mutation endpoints accept an idempotency key; the server stores `(key → response)` for a retention window and returns the cached response on replay.
- Webhook handlers dedupe on the provider's event ID — store processed IDs, reject duplicates.
- Queue consumers make the work itself idempotent: "set state to X" not "increment by 1"; check-then-skip before acting.
- Agent actions with side effects check for prior execution by a stable signal (content hash, correlation ID) before re-running.
- The retention window for idempotency keys is written down and enforced.

**Anti-patterns:**
- A handler that crashes midway and gets retried, double-charging or double-sending
- Retry logic that re-runs from the top without checking what already completed
- Hashing the full request body as the key (tiny changes break retry equivalence)

### P-033. Parse, Don't Validate
> At the boundary, transform untrusted input into a strongly-typed value. The rest of the system trusts it.

Alexis King's essay. Validation checks input is OK then passes on the same untyped shape;
parsing produces a new, stronger type that CANNOT represent invalid states.

**Rules:**
- Every system boundary (handler, CLI entry, webhook, consumer, file reader) parses into a typed, validated value.
- The parsed type is distinct from the raw input type; pass the parsed value downstream, not the raw one.
- Parsers fail loudly on unexpected fields by default; silently dropping unknown fields hides drift.
- Parse once at the boundary. Re-validating three layers deep means the boundary is in the wrong place.
- Pair with P-020: the parser's output type is the allowlist.

**Anti-patterns:**
- A handler that calls string methods on a field assuming it's a string, when the caller sent an array
- Validating at the boundary but passing the original raw input downstream
- Scattered `if typeof x === ...` checks through business logic

### P-034. Observability Before Automation
> You cannot automate what you cannot observe. Every autonomous action logs its decision — and dry-runs before it acts.

Every agent, cron, webhook handler, or scheduled job must emit enough structured evidence
that an operator can answer afterward: what did it consider, what did it decide, why, and
did it actually do the thing?

**Rules:**
- Every autonomous action emits a structured event before and after: `{action_id, ts, considered, decision, reason, would_act, did_act, result}`.
- New automations ship in dry-run mode first (`would_act=true, did_act=false`), reviewed against expectations before enabling writes (P-022 applied to actions).
- Decision traces include WHY: which rule fired, which threshold crossed, which input was absent. "Skipped" without a reason is a dead log.
- Idempotency keys (P-032) are part of the event — the same `action_id` twice means "retry," not "happened twice."
- Rate-limit and dedup policies are observable.

**Anti-patterns:**
- An agent that only logs when it acts — silent ticks are invisible
- Dry-run mode gated behind a flag nobody remembers to flip
- A scheduled job whose last run can only be confirmed by grepping many log files

### P-035. Code-Coordinate Citations Grep-Confirmed at Write Time
> Every `file:line`, symbol name, branch condition, or schema constant cited in a spec or plan must be grep-confirmed against HEAD at write time. Mental-model citations decay across revisions.

A plan that cites a symbol at a line is making a claim the next implementer will
mechanically copy-paste. If the symbol moved or was renamed, the implementer ships a P-014
violation no review round catches without independently grepping the file.

**Rules:**
- Every `file:line`, branch condition, symbol-with-line, and schema constant cited in a spec/plan is grep-confirmed in the same write session as the citation.
- Pre-handoff lint: regex-sweep the doc for file:line patterns and known names; verify each resolves to the claimed symbol.
- A code-coordinate citation older than ~3 revisions without a re-grep is a P-014 violation by default.

**Anti-patterns:**
- "I remember the function being around line 10K" without grepping
- Refactoring a symbol without sweeping spec/plan citations
- A reviewer flags a wrong line; the next revision cites a different wrong line without grep-verification

This refines P-014 by spelling out the executable-references contract.

### P-036. Adversarial Review Has a Complexity Ceiling
> Past round 4 of any review loop, each fix-mode pass introduces regressions. Convergence is by defect class, not by reviewer agreement.

Multi-round adversarial review loops plateau between rounds 3 and 5. Past round 5: (a) new
findings are sub-cases of earlier ones at finer granularity, and (b) fix-mode introduces
regressions because each pass operates on a findings file without full repo context.

**Rules:**
- Skill-governed loops cap at 6 rounds; model-invented loops cap at 3.
- Convergence-by-class: if round N+1 findings carry the same defect-class tag as round N, halt and promote residuals to follow-ups.
- Severity-tier convergence: when findings drop from safety-critical (data loss, races, privilege escalation, broken contracts) to polish-tier (style, wording), call convergence regardless of round count.
- The terminal state is operator judgment of remaining severity, not chain agreement.

**Anti-patterns:**
- Running review until the reviewer says "LGTM" — it never converges; the reviewer always has another angle
- Treating each round's findings as independent signal without checking class-overlap
- Continuing fix-mode past round 5 without re-reading whether the KIND of finding changed

### P-037. Cross-Model Diversity for Code-Symbol Review
> Same-model self-review fills in plausible-but-wrong names from training data. Cross-model review has different blind spots.

Specs and plans referencing concrete code symbols need cross-model adversarial review at
least once. Same-model self-review accepts plausible-but-wrong names because it fills the gap
from the same prior; a second model has independent blind spots.

**Rules:**
- Any spec/plan touching concrete code symbols runs at least one cross-model review pass.
- Single-model multi-round review is fine for architectural decisions but NOT for code-coordinate accuracy.
- The reviewer triangle (a strong parent + a second reviewer model + a non-same-family reviewer) is the canonical pattern: distinct post-training lineages catch different defect classes.

**Anti-patterns:**
- Spec author writes spec, asks the same model to review — code-symbol drift survives
- Review with only same-model-family reviewers — family blind spots not caught
- Treating "the reviewer found nothing" as proof of correctness when only one model-family reviewed

### P-038. Spec Body + Implementation + Acceptance Are a Single Contract
> A revision to one section is a revision to all three. Body-first revision routinely leaves Implementation and Acceptance lagging by N rounds.

Multi-section specs form a single contract. The revision pattern is body-first; Implementation
and Acceptance lag, surfacing later as "inconsistency between body and acceptance" findings.

**Rules:**
- Every body edit's checklist includes "find the matching Implementation step and Acceptance criterion, update them in the same edit."
- A spec passes review only when body / Implementation / Acceptance are mutually consistent under independent grep.
- Reviewer prompts explicitly check for body↔implementation↔acceptance drift, not just body correctness.

**Anti-patterns:**
- Editing the body to switch approaches, leaving Implementation steps describing the old one
- Acceptance criteria written before the spec converges and never re-read
- "I'll fix Acceptance after the architecture stabilizes" — it doesn't get fixed

### P-040. Liveness Signal Independent of Monitored Subsystem
> If the heartbeat shares the same scheduler/event-loop/runtime that can be blocked by the failure mode you're detecting, the heartbeat is also blocked when it matters most.

A liveness signal must be produced by a mechanism at a DIFFERENT layer of the stack than the
subsystem it monitors.

**Rules:**
- Event-loop liveness → an OS-thread watchdog, not another async task.
- Thread-group liveness → a separate process, not another thread in the same group.
- Process liveness → an external probe, not in-process self-report.
- Same-layer liveness signals can detect cadence misalignment but not a wedged loop.

**Anti-patterns:**
- A heartbeat task scheduled on the very event loop it monitors
- Same-process self-pinging when the failure mode is a process-wide deadlock
- A heartbeat that depends on the database when the failure mode is DB-connection exhaustion

### P-042. Schema-Aware Test Fixtures
> Fixtures that drop schema metadata silently exempt tests from production validation paths.

Test fixtures for state-mutating code must include the same schema metadata
(`schema_version`, type discriminators) that production validation enforces. Fixtures using
old-shape objects let a fix introduce schema-violating fields that pass unit tests but crash
production writers.

**Rules:**
- Every fixture for state-mutating code includes the same shape gates production enforces.
- Lint or assert this invariant — flag any fixture that omits the schema metadata its production counterpart enforces.
- When the schema changes, regenerate fixtures alongside production code.

**Anti-patterns:**
- A fixture missing `schema_version` when production writes it through a validator
- Mock data untouched since the schema migration
- Helpers that build "minimal valid" objects without the schema-required fields

### P-044. Tolerate-Failure Modifiers Are Blanket Switches
> If your design needs to tolerate some failures but not others, the modifier is the wrong layer. Encode it in exit-code semantics.

Failure-tolerance modifiers (`|| true`, bare `except:`, predicate-less retry, a unit's
ignore-failure prefix, continue-on-error) are blanket switches, not selective filters.
Designs that try to use them selectively are unimplementable at that layer.

**Rules:**
- Encode the success/failure distinction in EXIT-CODE semantics — collapse acceptable cases into success, reserve non-zero for genuinely-fatal cases.
- Let modifier-free default behavior do the work.
- Bare `except:` and `|| true` are code smells unless paired with an immediate re-raise on classified cases.
- Retry decorators need a predicate; predicate-less retry hides real bugs.

**Anti-patterns:**
- `restart || true` — masks restart failures
- `try: ...; except: pass` — masks every exception class indiscriminately
- An ignore-failure prefix to tolerate one error code — it tolerates all of them

### P-045. The Workaround Tool Is the Regression Vector
> Any safety invariant guarded by N lines of design is regressed by an M-line tool that goes around it. The workaround's "just for cleanup" framing is the same framing every future bypass will use.

When you've enforced an invariant via a design pattern — operator-only resolver, access
policy, type system, schema constraint, permission gate — do not write tools that bypass it.
The tool's existence undermines the invariant for every future caller.

**Rules:**
- When extending a system with an enforced invariant, the extension lives behind the same gate; if the gate's UX cost is the problem, fix the gate, not the extension.
- "One-shot cleanup" / "migration" / "just this once" tools are the canonical regression vector — their justification is the same as every future bypass that will cite them as precedent.
- If a tool genuinely must bypass the invariant (rare), run it with elevated, logged, single-invocation authorization — not committed as a callable.

**Anti-patterns:**
- Three rounds of review defending an invariant, then a cleanup script that does the operation directly
- "We need a way to fix wrongly-resolved records" → a bespoke script that mutates the table directly
- An access policy plus a privileged helper that selectively does the operation — the helper is the policy hole

### P-046. Persist-Then-Acknowledge for Shared Message Buses
> For a per-conversation worker on a single-stream message bus, the bus's offset advance cannot be driven by per-conversation completion. Persist incoming messages durably with fsync, advance the offset eagerly, replay from the store on restart.

Documented in mainstream messaging systems: persist every operation before acknowledging;
treat committed offsets as the durable acknowledgment. The trap is custom workers that gate
the offset on per-conversation work — one slow conversation backs up the whole bus, or
partial offset advance loses messages on restart.

**Rules:**
- The bus offset and the per-conversation work are decoupled: the offset tracks "what the bus delivered," the per-conversation state tracks "what we've finished."
- Durably persist incoming messages on receipt, advance the offset on persist-success, drive work off the store.
- On restart, replay from the store, not by re-fetching from the bus.
- A reviewer reading a multi-conversation-bus spec must trace cross-topic concurrency through the bus's actual offset semantics, not assume per-conversation isolation.

**Anti-patterns:**
- "Advance offset when this conversation is done" — couples bus throughput to single-conversation latency
- "Acknowledge on idle" — drops messages from conversations still mid-flight at restart
- Treating bus-level delivery state as per-conversation state

### P-047. Spec, Plan, and Ship Convergences Are Independent Surfaces
> A converged spec does not predict a converged plan. A converged plan does not predict a converged ship review. Each surface has its own defect classes and review budget.

Multi-stage review is not a funnel. Spec-review catches architectural/schema issues;
plan-review catches implementation-detail defects (env var names, file locations, library
specifics, fixture timing) the spec can't reach; ship-review catches integration drift, sink
leaks, async/sync mismatches the plan couldn't see.

**Rules:**
- Budget review rounds for spec, plan, and ship independently; all three expending their cap is the normal case for non-trivial features.
- Don't gate spec merge on "the plan will probably be fine" — the plan surface is its own domain.
- Don't gate plan merge on "the implementation will probably be fine" — the ship surface is its own domain.
- Treat each surface's convergence as independent terminal evidence.

This complements P-036 (per-loop cap) and P-038 (within-spec consistency).

**Anti-patterns:**
- "We did 6 rounds of spec-review, the plan should be quick" — the first plan-review round surfaces a stack of implementation findings
- Skipping plan-review on the assumption spec convergence is enough
- Skipping ship-review's final round because plan-review converged

### P-048. Authoritative Store Beats Local Mirror
> Code reading from a local mirror of an authoritative store will silently regress as the mirror drifts. "It's already on disk" is not justification.

When a system has both an authoritative store (origin, API, source repo) and a local mirror
(checkout, cache, replicated config), consuming code must read from the authoritative store
unless cache-invalidation semantics are explicit.

**Rules:**
- Code consuming shared config or canonical data reads from the authoritative store at read time, OR has explicit cache-invalidation tied to the source's write events.
- Local-mirror reads are acceptable only when the mirror is explicitly refreshed before the read, OR the data is immutable.
- Non-blocking CI checks need a periodic green-vs-red audit, or they atrophy into noise everyone routes around.
- A check failing on main for more than a few days has regressed to noise — fix it or remove it.

**Anti-patterns:**
- Reading a sibling repo's config from the local working tree on a host where it might be stale
- Non-blocking lint red on main for weeks with nobody acting
- Cache reads with no invalidation contract

### P-049. Deny-List Growth Is an Upstream Failure
> When a deny-list, ignore-list, or filter-list keeps growing, the failure is upstream of the list. Fix what generates entries, not the list.

A deny-list is a record of past leaks, not a defense against future ones. The growth rate is
the signal: when new entries land monthly and the list approaches three-digit length, nothing
prevents the antipattern at source.

**Rules:**
- Before adding an entry, ask whether the upstream generator can be prevented from producing the bad shape.
- If the same upstream produces N entries over N months, that upstream is the bug — file the root-cause fix instead of the next entry.
- Deny-lists have an expiration / audit cadence — old entries get re-justified or removed.
- New entries cite the prevention attempt: "tried to prevent at source, source path is X, prevention requires Y, deferred for reason Z."

**Anti-patterns:**
- Adding ignore patterns instead of finding the code that creates the bad artifacts
- Per-PR additions to an exemption list with no comment on why source can't be fixed
- Allow/deny lists scattered across config, code, and DB rows with no central audit

### P-050. Exact-Shape Resolution for User-Supplied Identifiers
> When resolving a user-supplied identifier to a filesystem artifact, require an exact canonical shape and refuse on zero-or-multiple matches. Never use unanchored substring globs in dispatch paths.

Substring globs silently dispatch the wrong artifact when prefixes overlap (`foo` matches
`foo-bar`). The bug is invisible until two artifacts share a prefix, at which point behavior
depends on filesystem iteration order.

**Rules:**
- Identifier → artifact resolution requires the canonical filename shape (`<id>.md`, `<id>.json`, `<prefix>/<id>.md`).
- On zero matches: explicit error with what was tried.
- On multiple matches: explicit error listing all matches; never pick one.
- The same resolver serves both the runtime and any operator-facing CLI; don't duplicate the substring-glob in two places.

**Anti-patterns:**
- A `*<slug>*` glob — silently picks the first or last match
- Two resolvers (one in code, one in shell) with subtly different matching semantics
- "It's only ambiguous in edge cases" — those edges are the production failures

### P-051. Rollback Mutation Surface Matches Protection Surface
> Rollback mutations must be no wider than the guard that protected the forward mutation. If a lock covers three files, the rollback touches three files — not the whole repo.

When a forward path uses a narrow guard and the rollback uses a wide primitive (a hard repo
reset, a table truncate, a full cache flush), the rollback escapes the guard and clobbers
state the guard never protected — adjacent in-flight work gets wiped.

**Rules:**
- For every rollback primitive, name the exact set of objects it mutates and confirm that set is a subset of what the forward guard covered.
- Prefer ref-only and path-scoped operations over a hard repo reset when the lock covers specific paths.
- SQL rollback: prefer a scoped delete over a table truncate when the transaction scope was narrower than the table.
- Cache rollback: invalidate the specific keys you wrote, not the whole store.

**Anti-patterns:**
- Pairing a per-file lock with a repo-wide hard reset
- "It's faster to wipe and replay" — fast until it wipes the concurrent writer's work
- Documenting forward-path scope carefully but treating rollback as a single "undo" primitive

### P-052. Verify Remote Non-Landing Before Local Compensation
> For ambiguous remote operations (push timeout, deploy timeout, RPC error after send), the operation may have succeeded with the acknowledgement lost. Verify remote state before compensating locally.

Network operations have three outcomes, not two: succeeded, failed, and "you don't know."
Timeouts and connection resets land in the third bucket. If a push times out and the local
rollback discards the commit, the push may have actually landed — discarding work that exists
on the remote and creating phantom-commit divergence.

**Rules:**
- Before discarding local state after an ambiguous remote operation, fetch authoritative remote state and verify the operation did not land.
- For a push timeout: fetch the branch, then check whether the local HEAD is already an ancestor of the remote tip before any reset.
- For external API errors: retry with the same idempotency key and check the response, or query the remote for the operation by ID, before compensating.
- Define the verification protocol in the same change that defines the rollback.

**Anti-patterns:**
- `try { push } catch { hard reset HEAD~1 }` with no fetch between
- Treating any non-success response as "failed" without distinguishing timeout from a definite client error
- Assuming idempotency keys guarantee no-op replay when the upstream may have committed without persisting the key

### P-053. Retention Declared at Write-One
> Every append-only table declares a retention policy at the PR that introduces the first writer. "No retention" is unbounded growth by default.

Audit logs, event tables, and metrics tables grow at `writers × write_rate × payload × time`.
Without an explicit retention policy up front, that product runs forever and the bill is paid
later by whoever maintains the system. Retention can be lenient — but it must be explicit and
committed alongside the writer.

**Rules:**
- The PR that introduces an append-only writer also commits the retention policy: a time-based sweep, size-based eviction, or external archival.
- The policy is committed as code or migration, not documented as intent.
- Choose retention against the writer's sustained volume and the storage tier — same math as P-021's recurring-writer rule.
- "Compliance requires X years" is a real policy; "we might want old rows someday" is not.

**Anti-patterns:**
- Adding a timestamp column and leaving retention "for later"
- Retention declared in a comment or a chat thread — it must be code
- A logging table growing for 18 months whose first retention conversation is in the incident channel

### P-054. Log State Changes, Not Evaluations
> When a per-row evaluator chooses no-op based on guard rules, do not log the evaluation. Log only when state actually changed.

Per-row evaluators (reconcilers, syncers, watchers) run on a tick over a large set and
short-circuit most rows via guard rules. If every evaluation writes an audit row, the table
fills with N rows per tick × T ticks, mostly "considered, did nothing." The signal lives in
state changes; the noise is the evaluations.

**Rules:**
- Per-row evaluators emit audit rows on state transitions only (create / update / delete / decision-change).
- Routine skip reasons (guard rules, filters, intentional no-ops) are NOT logged — evaluator noise.
- Exception: error and contention skips ARE diagnostic signal; log those even though they're "no-op" — they answer "why didn't this work?" later.
- When in doubt: would the operator search for this row during an incident? If no, don't write it.

**Anti-patterns:**
- A skip-reason log firing on every non-matching row every tick
- Confusing "comprehensive observability" with "log every code path"
- Adding more disk to absorb evaluator-noise growth instead of suppressing the writes at source

### P-055. Compensate for Absent Signals
> The operation didn't fail — the confirmation never arrived. Standard error handling covers failures; this covers silence.

When the system depends on an event that should arrive but might not (callback not fired, hook
not triggered, acknowledgment lost, process exited without notification), add a bounded
watchdog timer that forces a state transition after a deadline — the application-code form of
a hardware watchdog.

Distinct from error classification because there is no error to classify — the operation
completed on the other side; you never hear about it.

**Rules:**
- The watchdog has a finite ceiling (no unbounded waits).
- The forced transition is safe — idempotent and conservative (prefer clearing state over taking action).
- Log when the watchdog fires — it indicates a reliability gap in the upstream signal path.
- The watchdog and its forced transition are part of the design, not an afterthought.
- Pair with exponential backoff when the retry has side effects; fixed-interval when it doesn't.

This generalizes the specific case of background-task liveness.

**Anti-patterns:**
- Assuming a callback will always eventually fire
- An infinite wait on a signal with no timeout
- Treating "no response" as equivalent to "rejected" — they have different recovery paths

### P-056. Stateful Identity Is the Full Tuple
> A session is not an ID. It's (ID + workdir + isolation context + capabilities + extras). Resume by ID alone and the session silently diverges.

A stateful resource's identity is the tuple of ALL its defining properties, not just its
primary key. When persisting, restoring, cloning, or resuming, every dimension must be
propagated — or the restored resource appears to work but operates in the wrong context. The
failure mode is silent divergence, not a crash.

Extends P-025 from the mono-to-multi case to the general lifecycle case.

**Rules:**
- When adding a new dimension to a stateful resource, grep for every code path that persists, restores, clones, or resumes it — each must propagate the new dimension.
- Persistence schemas include ALL identity dimensions, not just the primary key.
- Resume operations validate the full tuple is restorable before proceeding (fail loudly over partial restoration).
- New dimensions silently don't propagate through paths the author didn't think of — the registration-blind-spot pattern.

**Anti-patterns:**
- Resuming by ID alone, losing workdir/context/extras
- Adding a field to a config object but not updating the serialization paths
- Cloning a resource and assuming it inherits context from the environment rather than the original

### P-057. Behavioral Safety Parity Across Parallel Implementations
> When a destructive operation has more than one implementation, a safety fix to one must reach all of them — or collapse them to one. The forgotten copy is where the accident happens.

When the same destructive operation is implemented more than once — e.g. a canonical code
entrypoint and a shell/markdown reimplementation — a safety fix to one path silently leaves
the others unguarded. Mirror the full guard set across every implementation in one pass, or
collapse the duplicates to one.

This is distinct from byte-equality of command/skill twins: that is textual sameness; this is
BEHAVIORAL safety parity across paths that need not be byte-identical but must enforce the same guards.

**Rules:**
- Inventory every implementation of a destructive op before fixing one; apply the full guard set to all in the same change.
- Prefer collapsing duplicate destructive paths to a single canonical entrypoint.
- A safety-fix PR touching one implementation must state where the others are and that they were updated or delegated.

**Anti-patterns:**
- Patching the code path's guard and leaving the shell/markdown reimplementation behind
- Treating command/skill byte-equality as proof of behavioral parity for a destructive op

### P-058. Propagate Test-Hermetic Fixes to the Shared Production Call-Site
> "CI will fail without X" means the test path needs X. Ask whether the production path shares the same dependency on X — it usually does.

When a correctness fix is motivated by "the test/CI will fail without X" — passing an explicit
config kwarg, making an acceptance check pass over a global config — the same dependency on X
usually exists on the production call-site the test stands in for. A fix applied only in the
test-hermetic context leaves the production path with the original latent defect.

**Rules:**
- When a fix is justified by test/CI failure, locate the production call-site the test mirrors and verify it carries the fix too.
- Treat "made the test pass" and "fixed the behavior in production" as two separate, both-required deliverables.

**Anti-patterns:**
- Passing an explicit dependency in the test to make it pass while the production caller still relies on the implicit one
- Closing a finding when only the hermetic test surface is fixed
