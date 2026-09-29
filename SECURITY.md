# Security Policy

## Supported versions

Goodfellow is pre-1.0. Security fixes land on `main` and ship in the next release; older releases do
not receive backports. Marketplace installs track `main`, so `/plugin marketplace update goodfellow`
picks up a fix as soon as it is merged.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report it privately through GitHub: open the
[Security tab](https://github.com/easelyte/goodfellow/security) of this repository and choose
**Report a vulnerability**. The report stays private between you and the maintainers until a fix is
published.

If you cannot use GitHub's private reporting, email **fantin@easelyte.ai** with `goodfellow security`
in the subject.

A useful report includes the Goodfellow version or commit, your Claude Code version and platform,
what an attacker gains, and the shortest steps that reproduce it. A proof of concept helps but is not
required.

## What to expect

Goodfellow is maintained by a small team. We aim to acknowledge a report within five working days,
tell you whether we consider it a vulnerability and why, and keep you informed while a fix is
prepared. When the fix ships, the CHANGELOG and release notes describe the issue under **Security**,
and we credit you by the name or handle you prefer, unless you ask us not to.

## Threat model in brief

Goodfellow is a Claude Code plugin. It runs with the same permissions as your Claude Code session,
executes your project's test suite and analyzers, and sends diffs and file contents to the model
providers you have configured (Anthropic, and OpenAI when the Codex CLI is installed). Research
queries go to Tavily only when `GOODFELLOW_TAVILY_KEY` is set. It has no server and collects no
telemetry.

Running Goodfellow on a repository you do not trust means running that repository's tests and, when
enabled, its analyzers. That is by design and not a vulnerability. Executing analyzers (`eslint`,
`tsc`, `mypy`) stay off unless you set `GOODFELLOW_TRUST_ANALYZERS=1` for this reason.

## Areas worth a closer look

These parts handle untrusted input or enforce a safety boundary, so findings there are especially
valuable:

- **Tool-layer guards** (`scripts/guard_engine.py`): a command that should be blocked but is not, or
  a way to disable a guard without the documented bypass variables.
- **Review bridge** (`scripts/codex-bridge.sh`, `scripts/review_*.py`): repository content is placed
  into reviewer prompts. Prompt injection that makes a review report "clean" for a defective diff, or
  shell injection through file names, branch names or arguments.
- **Mutation and red checks** (`scripts/mutation_check.py`, `scripts/red_check.py`,
  `scripts/proc_group.py`): a mutant or test run that escapes its temporary copy, writes to the real
  working tree, or signals processes outside its own group.
- **Memory backend** (`scripts/memory_index.py`): path traversal through fact names, or a crash or
  race that loses or corrupts `.goodfellow/` data.
- **Public PR scrub** (`scripts/public_pr_scrub.py`): internal references that pass the scrub.

Vulnerabilities in Claude Code, the Codex CLI or the model providers themselves should be reported
to those projects.
