# Contributing to Goodfellow

Thanks for your interest. Bug reports, fixes, new seed principles and documentation improvements are
all welcome. For anything larger than a fix, please open an issue first so we can agree on the shape
before you spend time on it.

## Ways to contribute

- **Report a bug** with the [bug report form](https://github.com/easelyte/goodfellow/issues/new?template=bug_report.yml).
  Include the Goodfellow version (from `/plugin`) or commit, your Claude Code version, and whether
  the Codex CLI is installed.
- **Suggest a feature** with the [feature request form](https://github.com/easelyte/goodfellow/issues/new?template=feature_request.yml).
- **Report a security problem** privately, as described in [SECURITY.md](SECURITY.md). Please do not
  open a public issue for it.
- **Send a pull request** for an open issue, a bug you found, or a documentation fix.

## Development setup

You need git, Python 3.10 or newer, and Claude Code. The Codex CLI is optional; without it the
review skills fall back to a single Claude reviewer.

```bash
git clone https://github.com/easelyte/goodfellow.git
cd goodfellow
python -m pip install pytest
cd scripts && python -m pytest -q
```

To try your checkout in Claude Code without installing it, load it for one session:

```bash
claude --plugin-dir /path/to/goodfellow
```

## Repository layout

| Path | What lives there |
|---|---|
| `skills/<name>/SKILL.md` | The skills. Each is a Markdown prompt with YAML frontmatter. |
| `scripts/` | Python (standard library only) and shell helpers the skills call, plus their tests (`test_*.py`). |
| `hooks/hooks.json` | Claude Code hooks: the `PreToolUse` guard engine and the `SessionStart` recall pointer. |
| `knowledge/` | Seeded principles (`principles.md`, `principles-web.md`). Read-only for users. |
| `configs/` | Example configs and vendored analyzer rules. |
| `docs/` | Design notes and README assets. |
| `.claude-plugin/` | Plugin and marketplace manifests. |

## Pull request checklist

- **Tests.** Behaviour changes to `scripts/` come with tests that fail before the change and pass
  after it, on an assertion rather than an import or type error. Goodfellow checks this for itself:
  `python scripts/red_check.py --base origin/main` replays your new tests against `main`.
- **CI passes.** CI runs the test suite, `bash -n` and `shellcheck` on shell scripts, a plugin
  structure check, a version consistency check, a CHANGELOG lint, and the principle density ratchet.
- **CHANGELOG.** Add a line under `## [Unreleased]` for anything users will notice, in the right
  group (`Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`), ending with your PR link.
  See [RELEASING.md](RELEASING.md).
- **No new dependencies** in `scripts/` without discussion. The plugin runs on a stock Python.
- **Keep it project-neutral.** No references to a specific company, host, internal ticket or private
  repository in code, comments, docs or principles. CI greps for some of these.
- **Stage specific files.** The guard engine blocks `git add -A` in Claude Code sessions for a reason.

## Skills

Skills are prompts, so a change to one is a behaviour change. When you edit a `SKILL.md`:

- Keep the frontmatter `description` accurate. Claude Code uses it to decide when to load the skill.
- Say what the skill does in autopilot and dry-run mode if the change touches either.
- If the skill writes files, say where, and keep dry-run free of project writes.
- Update the README skills table and CHANGELOG when the invocation or behaviour changes.

## Seed principles

New principles go in `knowledge/principles.md` (or `principles-web.md` for JS/React/Next.js/Postgres
rules). Each needs the next free `P-NNN` id, a one-line rule, a body that explains the failure it
prevents, and an `<!-- cat: NAME -->` category marker. The density ratchet
(`scripts/measure_principle_density.py`) caps the always-loaded index, so a new principle should earn
its place. See [docs/instruction-density-budget.md](docs/instruction-density-budget.md).

## Commit messages

No strict convention. A short imperative subject (`fix(ship): route major findings to the loop
store`) and a body explaining why are enough. Pull requests are squash-merged.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
