## Step 5 — Cross-fork `gh pr create` mechanics

When the base repo is a fork's upstream (or otherwise not your origin), pin all
three of `--repo`, `--base`, `--head` — `gh` silently defaults `--base` to the
wrong repo otherwise:

```bash
gh pr create \
  --repo <UPSTREAM_OWNER>/<repo> \
  --base <upstream-default-branch> \
  --head <your-fork-owner>:<branch>
```

Base off the upstream default branch and expect a squash merge. Verify your push
access to the target before pushing; never force-push a branch you do not own, and
never push to a shared default branch.

For a same-owner public repo (your origin is the public repo), a normal same-repo
PR is fine — `--base <default-branch> --head <branch>` — the scrub gate (Step 1)
is the load-bearing part there.

## Step 6 — Open + record

Open with the pinned flags. In the body: what changed, why, verification results
(suite/lint/build), any cross-PR sync point, and where cargo was parked. Peer
tone. Then stop — a maintainer reviews before merge.

## Optional: wire into `ship`

`ship` targets your own repo by default and does not force this gate. When the
ship target is public / not-solely-owned, run Step 1 as a pre-open gate first:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/public_pr_scrub.py" --base "$BASE" || {
  echo "internal-ref scrub failed — do not open the public PR" >&2; exit 1; }
```
