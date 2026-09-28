"""Tests for the CHANGELOG lint and release-notes extraction used by the release workflow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import release_notes as rn

GOOD = """# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

### Added

- Something new.

## [0.3.0] - 2026-10-01

### Added

- Feature A ([#10](https://github.com/o/r/pull/10)).

### Fixed

- Bug B.

## [0.2.0] - 2026-06-11

### Added

- Feature C.

[Unreleased]: https://github.com/o/r/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/o/r/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/o/r/releases/tag/v0.2.0
"""


def _project(
    tmp_path: Path, changelog: str, py_version: str, plugin_version: str
) -> Path:
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "x"\nversion = "{py_version}"\n', encoding="utf-8"
    )
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "x", "version": plugin_version}), encoding="utf-8"
    )
    return tmp_path


# --- extract -------------------------------------------------------------------------


def test_extract_returns_only_the_requested_section():
    notes = rn.extract(GOOD, "0.3.0")
    assert notes == (
        "### Added\n\n"
        "- Feature A ([#10](https://github.com/o/r/pull/10)).\n\n"
        "### Fixed\n\n"
        "- Bug B."
    )


def test_extract_last_section_drops_link_reference_definitions():
    notes = rn.extract(GOOD, "0.2.0")
    assert notes == "### Added\n\n- Feature C."


def test_extract_missing_version_raises():
    with pytest.raises(rn.ReleaseError, match=r"no \[0\.9\.0\] section"):
        rn.extract(GOOD, "0.9.0")


def test_extract_does_not_match_a_version_prefix():
    # [0.3.0] must not be found by asking for 0.3 or 0.3.0-rc.1.
    with pytest.raises(rn.ReleaseError):
        rn.extract(GOOD, "0.3")
    with pytest.raises(rn.ReleaseError):
        rn.extract(GOOD, "0.3.0-rc.1")


def test_extract_empty_section_raises():
    text = GOOD.replace("### Added\n\n- Feature C.\n\n", "")
    with pytest.raises(rn.ReleaseError, match="empty"):
        rn.extract(text, "0.2.0")


# --- lint ----------------------------------------------------------------------------


def test_lint_accepts_keep_a_changelog_file():
    assert rn.lint(GOOD) == []


def test_lint_requires_unreleased_section():
    text = GOOD.replace("## [Unreleased]\n\n### Added\n\n- Something new.\n\n", "")
    assert any("Unreleased" in e for e in rn.lint(text))


def test_lint_rejects_undated_version_heading():
    text = GOOD.replace("## [0.3.0] - 2026-10-01", "## [0.3.0]")
    errors = rn.lint(text)
    assert any("0.3.0" in e and "YYYY-MM-DD" in e for e in errors)


def test_lint_rejects_unknown_change_type():
    text = GOOD.replace("### Fixed", "### Improvements")
    assert any("Improvements" in e for e in rn.lint(text))


def test_lint_rejects_versions_out_of_order():
    text = GOOD.replace("## [0.2.0] - 2026-06-11", "## [0.4.0] - 2026-06-11")
    assert any("order" in e for e in rn.lint(text))


def test_lint_rejects_duplicate_versions():
    text = GOOD.replace("## [0.2.0] - 2026-06-11", "## [0.3.0] - 2026-06-11")
    assert any("duplicate" in e for e in rn.lint(text))


def test_lint_orders_prerelease_below_its_release():
    text = GOOD.replace("## [0.2.0] - 2026-06-11", "## [0.3.0-rc.1] - 2026-09-30")
    assert rn.lint(text) == []
    reversed_text = GOOD.replace(
        "## [0.3.0] - 2026-10-01", "## [0.3.0-rc.1] - 2026-10-01"
    ).replace("## [0.2.0] - 2026-06-11", "## [0.3.0] - 2026-06-11")
    assert any("order" in e for e in rn.lint(reversed_text))


# --- notes (the workflow entry point) --------------------------------------------------


def test_notes_writes_section_when_tag_and_manifests_agree(tmp_path):
    root = _project(tmp_path, GOOD, "0.3.0", "0.3.0")
    out = tmp_path / "notes.md"
    rc = rn.main(["notes", "--tag", "v0.3.0", "--root", str(root), "--out", str(out)])
    assert rc == 0
    assert out.read_text(encoding="utf-8").startswith("### Added\n\n- Feature A")


def test_notes_refuses_tag_that_disagrees_with_manifests(tmp_path, capsys):
    root = _project(tmp_path, GOOD, "0.2.0", "0.2.0")
    rc = rn.main(
        ["notes", "--tag", "v0.3.0", "--root", str(root), "--out", str(tmp_path / "n")]
    )
    assert rc == 1
    assert "pyproject.toml" in capsys.readouterr().err
    assert not (tmp_path / "n").exists()


def test_notes_refuses_when_plugin_json_alone_disagrees(tmp_path, capsys):
    root = _project(tmp_path, GOOD, "0.3.0", "0.2.0")
    rc = rn.main(
        ["notes", "--tag", "v0.3.0", "--root", str(root), "--out", str(tmp_path / "n")]
    )
    assert rc == 1
    assert "plugin.json" in capsys.readouterr().err


def test_notes_refuses_malformed_tag(tmp_path, capsys):
    root = _project(tmp_path, GOOD, "0.3.0", "0.3.0")
    rc = rn.main(
        ["notes", "--tag", "0.3.0", "--root", str(root), "--out", str(tmp_path / "n")]
    )
    assert rc == 1
    assert "vMAJOR.MINOR.PATCH" in capsys.readouterr().err


def test_notes_prerelease_flag_output(tmp_path, capsys):
    text = GOOD.replace("## [0.3.0] - 2026-10-01", "## [0.3.0-rc.1] - 2026-10-01")
    root = _project(tmp_path, text, "0.3.0-rc.1", "0.3.0-rc.1")
    rc = rn.main(
        [
            "notes",
            "--tag",
            "v0.3.0-rc.1",
            "--root",
            str(root),
            "--out",
            str(tmp_path / "n"),
        ]
    )
    assert rc == 0
    assert "prerelease=true" in capsys.readouterr().out


def test_lint_command_fails_on_bad_changelog(tmp_path):
    root = _project(
        tmp_path, GOOD.replace("## [Unreleased]", "## Unreleased"), "0.3.0", "0.3.0"
    )
    assert rn.main(["lint", "--root", str(root)]) == 1


def test_repo_changelog_passes_lint():
    root = Path(__file__).resolve().parent.parent
    assert rn.main(["lint", "--root", str(root)]) == 0
