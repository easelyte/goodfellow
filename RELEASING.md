# Releasing Goodfellow

Goodfellow follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html) and keeps its history
in [CHANGELOG.md](CHANGELOG.md) using [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
A GitHub Release is published automatically when a `vX.Y.Z` tag is pushed; its notes are the
matching CHANGELOG section, copied verbatim.

## Choosing the version

Goodfellow is pre-1.0. Until 1.0.0:

- **Minor (`0.X.0`)** for new features, and for any breaking change to something users rely on:
  skill names and arguments, `GOODFELLOW_*` variables, the formats of files under `.goodfellow/`,
  hook behaviour, or a default that changes what a run does.
- **Patch (`0.x.Y`)** for fixes and documentation that change nothing users depend on.
- **Pre-release (`0.X.0-rc.1`)** when a release needs wider testing first. Pre-releases are
  published as GitHub pre-releases and are not marked latest.

1.0.0 is cut once those user-facing contracts are stable enough to promise compatibility.

## Where the version lives

| File | Field |
|---|---|
| `.claude-plugin/plugin.json` | `version` (what Claude Code shows users) |
| `pyproject.toml` | `[project] version` |
| `CHANGELOG.md` | `## [X.Y.Z] - YYYY-MM-DD` heading |
| git | tag `vX.Y.Z` |

CI fails if `plugin.json` and `pyproject.toml` disagree (`scripts/check_version_consistency.py`) or
if the CHANGELOG is malformed (`scripts/release_notes.py lint`). The release workflow refuses to
publish if the tag disagrees with any of them.

## Day to day

Every pull request that changes user-visible behaviour adds a line under `## [Unreleased]` in the
right group: `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed` or `Security`. One line per change,
written for users, ending with the PR link. Breaking changes go under `Changed` or `Removed` and say
what users need to do.

## Cutting a release

1. **Open a release PR** from an up-to-date `main`:

   ```bash
   git switch main && git pull
   git switch -c release/vX.Y.Z
   ```

2. **Bump the version** in `.claude-plugin/plugin.json` and `pyproject.toml`.

3. **Move the changelog entries.** In `CHANGELOG.md`, rename `## [Unreleased]` to
   `## [X.Y.Z] - YYYY-MM-DD` (today's date, UTC), add a fresh empty `## [Unreleased]` above it, and
   update the link references at the bottom:

   ```markdown
   [Unreleased]: https://github.com/easelyte/goodfellow/compare/vX.Y.Z...HEAD
   [X.Y.Z]: https://github.com/easelyte/goodfellow/compare/vPREVIOUS...vX.Y.Z
   ```

   For the first tagged release, point `[X.Y.Z]` at
   `https://github.com/easelyte/goodfellow/releases/tag/vX.Y.Z` instead.

4. **Check it locally:**

   ```bash
   python scripts/check_version_consistency.py
   python scripts/release_notes.py lint
   python scripts/release_notes.py notes --tag vX.Y.Z   # prints the release notes
   cd scripts && python -m pytest -q
   ```

5. **Merge the release PR** once CI is green.

6. **Tag the merge commit and push the tag:**

   ```bash
   git switch main && git pull
   git tag -a vX.Y.Z -m "Goodfellow X.Y.Z"
   git push origin vX.Y.Z
   ```

7. **Watch the Release workflow** (`gh run watch` or the Actions tab). It checks that the tag is on
   `main`, that the tag, `plugin.json`, `pyproject.toml` and CHANGELOG agree, runs the tests, and
   publishes the release with the CHANGELOG section as its notes.

8. **Verify** the release page and that a fresh install reports the new version:

   ```text
   /plugin marketplace update goodfellow
   /plugin
   ```

## Fixing a release

- **Wrong notes:** fix `CHANGELOG.md` on `main`, then re-run the workflow for the existing tag:
  `gh workflow run release.yml -f tag=vX.Y.Z`. A manual run checks versions against the tag but
  takes the notes from `main`'s CHANGELOG, and updates the release in place without changing which
  release is marked latest.
- **Broken release:** do not move or delete a published tag. Fix forward with a patch release
  (for example `v0.3.1` after `v0.3.0`), and mark the broken release as such in its notes if users need to avoid it.
- **Workflow failed before publishing:** fix the cause on `main`. If the tag itself is wrong (for
  example it points at a commit with the old version), delete it with
  `git push --delete origin vX.Y.Z && git tag -d vX.Y.Z`, then tag the correct commit. This is safe
  only while no release has been published for that tag.

## Why a small custom workflow and not release-please

release-please derives the changelog from Conventional Commit messages and keeps a standing release
PR open. Goodfellow's history does not use Conventional Commits consistently, and its changelog is
written by hand for readers, not generated from commit subjects. The custom workflow is about sixty
lines plus a tested standard-library script, keeps the CHANGELOG as the only source of release
notes, and fails closed when the version files disagree. It has no configuration to maintain and no
third-party action beyond `actions/checkout` and `actions/setup-python`.
