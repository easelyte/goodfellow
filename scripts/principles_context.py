#!/usr/bin/env python3
"""Resolve which seeded principle files the chain skills should read.

Skills invoke this via the CLI (not import), matching the loop_store.py pattern.
Capture the output and propagate a non-zero exit BEFORE iterating — a bare
`for f in $(failing-cmd)` runs zero iterations and exits 0, silently swallowing
an invalid GOODFELLOW_PRINCIPLES_WEB instead of hard-erroring:

    principle_files=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/principles_context.py" --project-root .) || { echo "$principle_files" >&2; exit 1; }
    for f in $principle_files; do
        cat "${CLAUDE_PLUGIN_ROOT}/knowledge/$f"
    done

Core (`principles.md`) is always read. The web supplement (`principles-web.md`)
is read only when web context is opted in: GOODFELLOW_PRINCIPLES_WEB=1, or a
package.json at the project root. An invalid GOODFELLOW_PRINCIPLES_WEB value
hard-errors (fail loud), so a misconfigured skill run fails visibly.
"""

import argparse
import os
import pathlib
import sys


class ConfigError(ValueError):
    pass


def resolve_principle_files(plugin_root, project_root):
    """Return the ordered list of principle filenames the chain skills should read."""
    files = ["principles.md"]
    web = os.environ.get("GOODFELLOW_PRINCIPLES_WEB")
    if web is None or web == "":
        web_on = (pathlib.Path(project_root) / "package.json").exists()
    elif web == "1":
        web_on = True
    else:
        raise ConfigError(
            f"GOODFELLOW_PRINCIPLES_WEB must be unset or '1' (got: {web!r})"
        )
    if (
        web_on
        and (pathlib.Path(plugin_root) / "knowledge" / "principles-web.md").exists()
    ):
        files.append("principles-web.md")
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Resolve seeded principle files to read."
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Project root (for package.json web autodetect).",
    )
    parser.add_argument(
        "--plugin-root",
        default=None,
        help="Plugin root holding knowledge/. Defaults to $CLAUDE_PLUGIN_ROOT.",
    )
    args = parser.parse_args(argv)

    plugin_root = args.plugin_root or os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not plugin_root:
        # Fallback: this file lives at <plugin_root>/scripts/principles_context.py
        plugin_root = str(pathlib.Path(__file__).resolve().parents[1])

    try:
        files = resolve_principle_files(
            plugin_root=plugin_root, project_root=args.project_root
        )
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        return 1
    for f in files:
        print(f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
