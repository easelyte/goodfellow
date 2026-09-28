#!/usr/bin/env python3
"""CHANGELOG lint and release-notes extraction for the tag-triggered release workflow.

CHANGELOG.md is the single source of truth for release notes. It follows Keep a Changelog
(https://keepachangelog.com/en/1.1.0/): an ``## [Unreleased]`` section on top, then one
``## [X.Y.Z] - YYYY-MM-DD`` section per release, newest first, each grouped under
``### Added`` / ``Changed`` / ``Deprecated`` / ``Removed`` / ``Fixed`` / ``Security``.

Commands:

    release_notes.py lint [--root DIR]
        Validate CHANGELOG.md structure. Run in CI on every push.

    release_notes.py notes --tag vX.Y.Z [--root DIR] [--out FILE]
        Check that the tag, pyproject.toml, .claude-plugin/plugin.json and a dated
        CHANGELOG section all name the same version, then write that section to FILE
        (stdout when omitted). Prints ``version=`` and ``prerelease=`` lines for the
        workflow on stdout when --out is given. Exits 1 on any disagreement, so a
        release is never published with notes for the wrong version.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CHANGE_TYPES = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")

_SEMVER = r"(?P<core>\d+\.\d+\.\d+)(?:-(?P<pre>[0-9A-Za-z.-]+))?"
_TAG_RE = re.compile(rf"^v{_SEMVER}$")
_H2_RE = re.compile(r"^## (?P<rest>.*)$")
_VERSION_H2_RE = re.compile(
    rf"^\[(?P<version>{_SEMVER})\] - (?P<date>\d{{4}}-\d{{2}}-\d{{2}})$"
)
_LINK_DEF_RE = re.compile(r"^\[[^\]]+\]:\s+\S+")


class ReleaseError(Exception):
    """A release precondition is not met."""


def _sections(text: str) -> list[tuple[str, list[str]]]:
    """Split into (h2 heading text, body lines) pairs, in file order."""
    out: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        m = _H2_RE.match(line)
        if m:
            out.append((m.group("rest").strip(), []))
        elif out:
            out[-1][1].append(line)
    return out


def _strip_body(lines: list[str]) -> str:
    body = [ln for ln in lines if not _LINK_DEF_RE.match(ln)]
    return "\n".join(body).strip()


def extract(text: str, version: str) -> str:
    """Return the body of the ``## [version] - date`` section, without link definitions."""
    for heading, body in _sections(text):
        m = _VERSION_H2_RE.match(heading)
        if m and m.group("version") == version:
            notes = _strip_body(body)
            if not notes:
                raise ReleaseError(f"CHANGELOG.md section [{version}] is empty")
            return notes
    raise ReleaseError(f"CHANGELOG.md has no [{version}] section with a date")


def _sort_key(version: str) -> tuple:
    m = re.fullmatch(_SEMVER, version)
    assert m is not None
    core = tuple(int(p) for p in m.group("core").split("."))
    pre = m.group("pre")
    if pre is None:
        return core + (1, ())
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return core + (0, parts)


def lint(text: str) -> list[str]:
    """Return a list of structural problems; empty means the file is valid."""
    errors: list[str] = []
    if not text.startswith("# Changelog"):
        errors.append("first line must be '# Changelog'")
    sections = _sections(text)
    if not sections or sections[0][0] != "[Unreleased]":
        errors.append("the first section must be '## [Unreleased]'")
    seen: list[str] = []
    for heading, body in sections:
        if heading == "[Unreleased]":
            if seen:
                errors.append("'## [Unreleased]' must come before every version")
        else:
            m = _VERSION_H2_RE.match(heading)
            if not m:
                errors.append(
                    f"bad heading '## {heading}': expected '## [X.Y.Z] - YYYY-MM-DD'"
                )
                continue
            version = m.group("version")
            if version in seen:
                errors.append(f"duplicate version [{version}]")
            elif seen and _sort_key(version) >= _sort_key(seen[-1]):
                errors.append(
                    f"[{version}] is out of order: versions must be listed newest first"
                )
            seen.append(version)
        for line in body:
            if line.startswith("### "):
                kind = line[4:].strip()
                if kind not in CHANGE_TYPES:
                    errors.append(
                        f"unknown change type '### {kind}' under '## {heading}' "
                        f"(use one of: {', '.join(CHANGE_TYPES)})"
                    )
    return errors


def _pyproject_version(path: Path) -> str:
    in_project = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project = stripped == "[project]"
            continue
        if in_project:
            m = re.match(r'version\s*=\s*"([^"]+)"', stripped)
            if m:
                return m.group(1)
    raise ReleaseError("no [project] version in pyproject.toml")


def release_notes(root: Path, tag: str) -> tuple[str, str, bool]:
    """Validate a release tag against the repo and return (version, notes, prerelease)."""
    m = _TAG_RE.match(tag)
    if not m:
        raise ReleaseError(f"tag '{tag}' is not vMAJOR.MINOR.PATCH[-prerelease]")
    version = tag[1:]
    py_version = _pyproject_version(root / "pyproject.toml")
    if py_version != version:
        raise ReleaseError(f"tag {tag} but pyproject.toml version is {py_version}")
    plugin = json.loads(
        (root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    if plugin.get("version") != version:
        raise ReleaseError(
            f"tag {tag} but plugin.json version is {plugin.get('version')}"
        )
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    problems = lint(text)
    if problems:
        raise ReleaseError("CHANGELOG.md is invalid: " + "; ".join(problems))
    return version, extract(text, version), m.group("pre") is not None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_lint = sub.add_parser("lint", help="validate CHANGELOG.md")
    p_lint.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    p_notes = sub.add_parser("notes", help="check a tag and print its release notes")
    p_notes.add_argument("--tag", required=True)
    p_notes.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    p_notes.add_argument("--out")
    args = parser.parse_args(argv)
    root = Path(args.root)

    if args.cmd == "lint":
        problems = lint((root / "CHANGELOG.md").read_text(encoding="utf-8"))
        for p in problems:
            print(f"CHANGELOG.md: {p}", file=sys.stderr)
        if not problems:
            print("CHANGELOG.md: ok")
        return 1 if problems else 0

    try:
        version, notes, prerelease = release_notes(root, args.tag)
    except ReleaseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.out:
        Path(args.out).write_text(notes + "\n", encoding="utf-8")
        print(f"version={version}")
        print(f"prerelease={'true' if prerelease else 'false'}")
    else:
        print(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
