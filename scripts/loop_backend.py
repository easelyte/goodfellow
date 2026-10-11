#!/usr/bin/env python3
"""Loop-store backend interface.

A backend stores goodfellow's follow-up loops. The interface is a small,
versioned protocol:

    add(title, priority, source, description, tags, owner, next_action, lens) -> int
    close(loop_id) -> bool
    list(status, min_age_days) -> list[dict]
    get(loop_id) -> dict | None
    update_triage(loop_id, triage_count, last_triaged) -> bool

``local`` (the default) is the ``.goodfellow/loops.json`` file in ``loop_store``.
With no remote backend configured nothing changes. A remote backend lives in its
own ``scripts/backend_<name>.py`` module and exposes three names:

    ENV_PREFIX                       every environment variable it reads starts with this
    is_configured(env) -> bool       True when any of its variables is set
    from_env(env, project_root)      validate the configuration and return a backend

A configured remote backend is never skipped: bad or partial configuration is an
error, not a quiet fall back to the local file. Setting two remote backends at
once is an error too.

Standard library only.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol, runtime_checkable

# Bump on any change to the method set or the shape of a loop dict.
BACKEND_PROTOCOL_VERSION = 1

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


class BackendError(RuntimeError):
    """A backend could not do what was asked. Always loud; never a silent fallback."""


class BackendConfigError(BackendError):
    """The backend's configuration is missing, partial or unsafe."""


@runtime_checkable
class LoopBackend(Protocol):
    protocol_version: int
    name: str

    def add(
        self,
        title: str,
        priority: str = "p3",
        source: Optional[str] = None,
        description: str = "",
        tags: Optional[List[str]] = None,
        owner: str = "operator",
        next_action: str = "",
        lens: Optional[str] = None,
    ) -> int: ...

    def close(self, loop_id: int) -> bool: ...

    def list(
        self, status: Optional[str] = None, min_age_days: Optional[int] = None
    ) -> List[Dict[str, Any]]: ...

    def get(self, loop_id: int) -> Optional[Dict[str, Any]]: ...

    def update_triage(
        self,
        loop_id: int,
        triage_count: Optional[int] = None,
        last_triaged: Optional[str] = None,
    ) -> bool: ...


class LocalBackend:
    """Today's ``.goodfellow/loops.json`` store, behind the protocol."""

    protocol_version = BACKEND_PROTOCOL_VERSION
    name = "local"

    def __init__(self, project_root: str = ".") -> None:
        self.project_root = project_root

    def add(self, title, priority="p3", source=None, description="", tags=None,
            owner="operator", next_action="", lens=None):
        import loop_store

        return loop_store.add_loop(
            title, priority=priority, source=source, description=description,
            tags=tags, owner=owner, next_action=next_action, lens=lens,
            project_root=self.project_root,
        )

    def close(self, loop_id):
        import loop_store

        return loop_store.close_loop(loop_id, project_root=self.project_root)

    def list(self, status=None, min_age_days=None):
        import loop_store

        return loop_store.list_loops(
            status=status, min_age_days=min_age_days, project_root=self.project_root
        )

    def get(self, loop_id):
        import loop_store

        return loop_store.get_loop(loop_id, project_root=self.project_root)

    def update_triage(self, loop_id, triage_count=None, last_triaged=None):
        import loop_store

        return loop_store.update_triage(
            loop_id, triage_count=triage_count, last_triaged=last_triaged,
            project_root=self.project_root,
        )


def _remote_modules() -> List[str]:
    """Names of the remote backend modules shipped next to this file."""
    return sorted(
        p.stem for p in HERE.glob("backend_*.py") if p.stem != Path(__file__).stem and not p.stem.startswith("test_")
    )


def get_backend(
    project_root: str = ".", env: Optional[Mapping[str, str]] = None
) -> LoopBackend:
    """The configured backend. ``local`` unless a remote backend's variables are set."""
    env = os.environ if env is None else env
    configured = []
    for mod_name in _remote_modules():
        mod = importlib.import_module(mod_name)
        if mod.is_configured(env):
            configured.append(mod)
    if len(configured) > 1:
        raise BackendConfigError(
            "more than one remote loop backend is configured: "
            + ", ".join(m.__name__ for m in configured)
        )
    if configured:
        return configured[0].from_env(env, project_root)
    return LocalBackend(project_root)


def count_open(backend: LoopBackend) -> int:
    return len(backend.list(status="open"))
