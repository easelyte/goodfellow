<!-- Goodfellow seed principles — curated from easelyte's cross-repo design knowledge.
     Version: 0.1.0. Do not edit; plugin-owned (see README). -->

# Universal design principles (web supplement)

Stack-specific rules for JS/TypeScript, React, Next.js, Postgres, Supabase, and RLS
codebases. Read only when web context is opted in (see README:
`GOODFELLOW_PRINCIPLES_WEB`, or auto-detected by a `package.json` at the project root).
Stack-agnostic rules live in `principles.md` (always read).

### P-004. RLS Is Row-Level Only
> Postgres RLS cannot restrict column access — only row access.

Understand what RLS can and cannot do before relying on it as your access layer.

**Rules:**
- `FOR UPDATE` needs both `USING` (which rows) AND `WITH CHECK` (what the row must look like after update). Without `WITH CHECK`, users can update rows to escape their access scope.
- `FOR ALL` is a superset of `FOR SELECT` — don't add redundant SELECT policies with the same USING clause.
- Every new column that affects access (visibility flags, type discriminators) must be reflected in policies. Adding a column without updating RLS is a data leak.
- If specific columns are sensitive (budget, rates), you need a VIEW, RPC, or separate table. Document the gap even if you accept it.
- NULL values are never equal in SQL UNIQUE constraints — multiple NULLs are allowed.

**Anti-patterns:**
- `FOR UPDATE USING (...)` with no `WITH CHECK` — a user can change a foreign key to escape scope
- Adding an access-affecting column without updating the policy that should check it
- Paired `FOR SELECT` + `FOR ALL` policies with identical conditions (the SELECT is dead weight)

### P-005. Don't Mirror External State in React
> If the DOM element has its own state machine, don't duplicate it in React.

Browser APIs (video elements, intersection observers, audio, canvas) maintain their own
state. Copying that into React via `useState` creates stale closures, race conditions, and
event-ordering bugs.

**Rules:**
- Read external state directly from refs at the moment you need it — don't cache it in React state.
- If a callback touches a DOM element, use a ref, not a `useCallback` with state deps.
- An effect that reads a ref should include any condition that controls when that ref becomes non-null in its dependency array.
- When a panel/dialog shows different items but stays mounted, add a `key` prop tied to the item ID to force remount.

**Anti-patterns:**
- `useState` mirroring a DOM element's loaded flag, then checking it in a hover handler — stale closure
- `useCallback([videoLoaded])` capturing React state that the DOM element also owns
- A dialog initialized from a prop via `useState` — opening item B after item A keeps A's state because React doesn't reinitialize on prop change

**Event-listener lifecycle:**
- Don't attach a listener inside a callback built from stale closure deps — the handler sees old state, and events firing between effect-run and attach are missed. Check `readyState` synchronously before attaching so you don't wait for an event that already fired.
- Track pending listeners in a ref and clean them up on unmount or element change — otherwise a replaced element keeps its old listener.
- An effect that reads a ref includes in its deps every condition gating when the ref becomes non-null (mount flags, viewport visibility, lazy-load triggers).

### P-006. Dates Are Timezone-Dependent
> `toISOString()` is always UTC. "Today" is not.

Date comparisons in user-facing applications must use the business timezone. Mixing
timestamp math with date-only strings produces off-by-one errors near timezone boundaries.

**Rules:**
- For date-only comparisons against `date` columns, format in the business timezone explicitly (e.g. an `Intl.DateTimeFormat` with an explicit `timeZone`).
- Never mix timestamp math with date-only strings — parse both sides in the same timezone.
- In server components, capture the current time once at the top and derive all dates from it.

**Anti-patterns:**
- `new Date().toISOString().split("T")[0]` for "today" — uses UTC, items move between buckets on the wrong day
- Subtracting a UTC-parsed date-only string from a local timestamp — the two sides are in different zones

### P-009. Next.js Route Mechanics
> Route groups are URL-invisible. `revalidatePath` doesn't cascade.

Know the framework's actual behavior, not the intuitive assumption.

**Rules:**
- A route-group folder doesn't add a URL segment — `app/(dashboard)/page.tsx` serves at `/`, not `/dashboard`. Don't create a `page.tsx` in both root and a route group.
- `revalidatePath("/x")` only revalidates `/x`. If a mutation affects `/x` AND `/x/[id]`, revalidate both explicitly.
- `ssr: false` dynamic imports render nothing on the server. If content matters for initial load, provide a server-renderable alternative.
- Never nest interactive elements (`<button>` inside `<button>`); use a `div` with a button role for the outer element.

### P-010. Postgres Migration Safety
> Incremental migrations are for existing databases. Fresh installs need a single schema file.

Migration chains accumulate assumptions. An enum default set in an early migration can't be
auto-cast when a later migration changes the enum type.

**Rules:**
- Enum changes require: drop dependent policies → convert column to text → migrate data → recreate enum → restore policies. All in one transaction.
- Name FK constraints explicitly — PostgREST uses constraint names (not column names) for embed disambiguation.
- `CONSTRAINT name` goes before the constraint definition in Postgres, not after.
- For fresh installs, generate a single `fresh-install.sql` representing final state; keep incremental migrations for history.
- UNIQUE constraints: think through the real uniqueness rule ("one per partner" usually means "one per partner per rater").

### P-013. Global CSS Has Non-Obvious Platform Interactions
> `scroll-behavior: smooth` on the root causes jitter on mobile momentum scroll.

Global CSS properties interact with native browser behaviors in platform-specific ways.
Desktop testing doesn't catch mobile regressions.

**Rules:**
- Prefer targeted `scrollIntoView({ behavior: 'smooth' })` over global `scroll-behavior: smooth`.
- Set base typography explicitly at all breakpoints — mobile-first defaults don't cascade as expected.
- Throttle scroll handlers that trigger DOM queries with `requestAnimationFrame`.
- External content (markdown, exports) leaks typographic conventions (em dashes, smart quotes) that don't survive rendering — lint or strip on import.

### P-029. Visibility-Gated Columns Enforced at the Data Layer
> If forgetting a `WHERE` clause is a data leak, the `WHERE` doesn't belong at the call site.

Generalizes soft-delete scopes (`deleted_at IS NULL`) to any boolean gate: `published`,
`approved`, `archived`, `active`, `client_visible`. Four call sites all missing the same
filter on the same table is an architecture problem, not a discipline problem.

**Enforcement hierarchy** (use the strongest available):
1. RLS policy — `USING (gate = true)` at the database. Strongest: even direct DB access is gated.
2. SQL view — queries against the view can't miss the filter. Strong.
3. ORM global filter (default scope / query filter / middleware). Strong within the ORM, bypassed by raw SQL.
4. Helper function wrapping the filter. Medium: someone eventually queries directly.
5. Call-site filter — the antipattern. Once is fine; four times is a leak.

**Anti-patterns:**
- Discovering the filter was missing via a user report
- A schema review approving a new boolean gate column without an RLS policy or view for it
- Soft-deleting into the same table when archiving to a separate table would be simpler

### P-031. Controlled Inputs Need Escape Hatches for HTML Behavior
> React's controlled-component model does not match HTML input semantics. Bridge the gap explicitly.

Extends P-005. HTML form controls maintain their own value semantics; `useState` forces a
typed value. The two disagree in specific, common cases — and the fix is more care at the
boundary, not more state.

**Rules:**
- `<input type="number">` with numeric state strips leading zeros and breaks partial entry. Store as a STRING in state; parse to number at submit/blur boundaries.
- Defaults that depend on props go through `useState(() => compute())` lazy init, not `useMemo` — lazy init runs exactly once; `useMemo` can recompute or return stale on first paint.
- Panels/dialogs that stay mounted across items need a `key` prop tied to the item ID.
- Effects reading refs must include every condition controlling when the ref becomes non-null.

**Anti-patterns:**
- `useState(0)` for a number input — typing "05" is impossible
- `useMemo(() => default, [])` for "value on first render" — the wrong tool
- A dialog showing item A's state after opening with item B's data
