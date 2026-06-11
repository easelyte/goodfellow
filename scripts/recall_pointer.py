#!/usr/bin/env python3
"""SessionStart recall pointer (best-effort, M5/CM2).

Emits a single pointer line when GOODFELLOW_MEMORY=rich AND .goodfellow/MEMORY.md
exists, so a fresh session knows to read the index before design/review. Flat-mode
users get nothing.

Best-effort only: bug #11509 (SessionStart hooks don't fire for local/git-clone
installs, closed "not planned") means this may never fire on the primary install
method — the on-demand in-chain read (T-2.7) is the load-bearing recall path. The
guard never crashes the hook: a bad config resolves to no pointer, not an error.

NOTE the param is `gf_root` (the .goodfellow ROOT). MEMORY.md lives at
gf_root/MEMORY.md, NOT gf_root/memory/ (MA-2)."""

import sys
import pathlib


def pointer_or_none(gf_root):
    try:
        from memory_config import resolve_mode

        mode = resolve_mode()
    except Exception:
        return None  # bad config / import error -> stay silent (don't crash hook)
    if mode != "rich":
        return None
    index = pathlib.Path(gf_root) / "MEMORY.md"
    if not index.exists():
        return None
    try:
        n = sum(1 for line in index.read_text().splitlines() if line.startswith("- "))
    except OSError:
        return None
    return (
        f"Goodfellow memory: {n} entries — read .goodfellow/MEMORY.md before "
        f"design/review."
    )


def main(argv=None):
    import argparse
    import os

    parser = argparse.ArgumentParser(
        description="Goodfellow SessionStart recall pointer"
    )
    parser.add_argument(
        "--root",
        default=os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", "."), ".goodfellow"),
        help="the .goodfellow ROOT directory",
    )
    args = parser.parse_args(argv)
    line = pointer_or_none(args.root)
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
