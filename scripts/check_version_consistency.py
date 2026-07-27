#!/usr/bin/env python3
"""Assert pyproject.toml version == .claude-plugin/plugin.json version.

Prevents the packaging drift where the Python package metadata and the Claude
Code plugin manifest disagree on the release version. Run in CI; exits non-zero
with a diagnostic when the two sources disagree.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version(path: Path) -> str:
    # Read the [project] version without a TOML dependency (3.10 has no tomllib).
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
    raise SystemExit("ERROR: no [project] version found in pyproject.toml")


def main() -> int:
    py_version = _pyproject_version(ROOT / "pyproject.toml")
    plugin_version = json.loads(
        (ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )["version"]

    if py_version != plugin_version:
        print(
            "ERROR: version drift — "
            f"pyproject.toml={py_version} != plugin.json={plugin_version}",
            file=sys.stderr,
        )
        return 1

    print(f"Version consistent: {py_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
