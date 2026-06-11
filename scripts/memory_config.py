#!/usr/bin/env python3
"""Goodfellow memory backend config switch (fail-loud).

GOODFELLOW_MEMORY: unset/empty -> flat (default); exact `flat`/`rich` -> that
mode; anything else -> ConfigError (no silent coerce — this var gates migration,
write format, and read format, so a typo must fail visibly).

warn_kb / ConfigError are re-exported from memory_index (CM-R4-1: they live
there because regenerate() needs warn_kb; dependency direction is
memory_config -> memory_index, never the reverse, so no import cycle)."""

import os
import sys

from memory_index import warn_kb, ConfigError  # re-export (no cycle)

__all__ = ["resolve_mode", "warn_kb", "ConfigError"]

MODES = ("flat", "rich")


def resolve_mode():
    raw = os.environ.get("GOODFELLOW_MEMORY")
    if raw is None or raw == "":
        return "flat"
    if raw in MODES:
        return raw
    raise ConfigError(f"GOODFELLOW_MEMORY must be 'flat' or 'rich' (got: {raw!r})")


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Goodfellow memory config")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("resolve-mode")
    sub.add_parser("warn-kb")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "resolve-mode":
            print(resolve_mode())
        elif args.cmd == "warn-kb":
            print(warn_kb())
    except ConfigError as e:
        print(str(e), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
