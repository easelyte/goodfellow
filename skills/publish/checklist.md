## Step 2 — Ship only the mergeable unit

The PR should contain the **product change (src + test)**, nothing else. Park
cargo — design mocks, local plan/spec/evidence docs, deploy logs, fork-process
paper trail — on a separate branch (nothing lost; say so in the PR). The
maintainer should not have to itemize your cargo for you.

## Step 3 — Description must match shipped behavior

The PR description is a durable artifact. Every claim must be true of the code as
pushed. If you described a behavior you did not implement, build it or reword
before opening.

## Step 4 — Self-verify at the maintainer's bar

They will re-verify in a fresh clone; beat them to it. Run the full suite and
confirm the diff contains only what you claim:

```bash
# run the project's test/lint/build, then:
git diff "$BASE"...HEAD --stat
```

Surface any cross-PR dependency (a field another in-flight PR defines) and its
graceful-degradation path in the body — don't make them discover it.

