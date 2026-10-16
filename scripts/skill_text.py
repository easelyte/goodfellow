"""Read skill cores and their local details for source-wiring tests."""

from pathlib import Path


def read_skill(path):
    core = Path(path)
    return "\n".join(
        p.read_text()
        for p in [core, *sorted(p for p in core.parent.glob("*.md") if p != core)]
    )
