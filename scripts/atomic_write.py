#!/usr/bin/env python3
"""Atomic file writes — crash-safe JSON and text persistence.

Writes to a temporary file in the same directory, fsyncs, then renames to
the target path. On POSIX, rename() on the same filesystem is atomic: the
file is either fully old or fully new, never half-written.

Usage:
    from atomic_write import (
        write_bytes_atomic,
        write_json_atomic,
        write_text_atomic,
    )

    write_json_atomic(Path("state.json"), {"status": "ok"})
    write_text_atomic(Path("report.md"), content)
    write_bytes_atomic(Path("archive.json.gz"), compressed_bytes)
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(
    path: Path,
    data: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
    **json_kwargs: Any,
) -> None:
    """Atomically write JSON data to a file.

    Writes to a temp file in the same directory, fsyncs to disk,
    then renames over the target. If the process is killed mid-write,
    the original file remains intact.

    Extra keyword arguments (e.g. sort_keys=True) are forwarded to
    json.dumps().
    """
    content = (
        json.dumps(data, indent=indent, ensure_ascii=ensure_ascii, **json_kwargs) + "\n"
    )
    _write_atomic(path, content)


def write_text_atomic(path: Path, content: str) -> None:
    """Atomically write text content to a file."""
    _write_atomic(path, content)


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Atomically write binary content to a file."""
    _write_atomic_bytes(path, data)


def _write_atomic(path: Path, content: str) -> None:
    """Core atomic write: tempfile → fsync → rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temp file in the same directory (same filesystem = atomic rename)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        # os.replace is atomic on POSIX and Windows; os.rename fails on Windows
        # when the target exists.
        os.replace(tmp_path, str(path))
    except BaseException:
        # Clean up temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_atomic_bytes(path: Path, data: bytes) -> None:
    """Core atomic write for bytes: tempfile → fsync → rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
