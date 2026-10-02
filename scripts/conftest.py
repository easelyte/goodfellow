"""Test-session setup.

On a machine WITHOUT bubblewrap, the tests of everything else the gates do run
unisolated (GOODFELLOW_SANDBOX=off) and the sandbox tests skip. Anything else
that keeps the sandbox from working (a GOODFELLOW_BWRAP that names no file, a
bubblewrap that does not isolate) stops the session: it must never turn into
mutants running unisolated. Set GOODFELLOW_SANDBOX=off yourself to run the
suite unisolated knowingly. CI sets GOODFELLOW_REQUIRE_SANDBOX_TESTS=1, which
also turns a missing bubblewrap into a failure rather than a skip.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sandbox  # noqa: E402


def pytest_configure(config):
    if "GOODFELLOW_SANDBOX" in os.environ:
        return
    try:
        sandbox.create([])
    except sandbox.SandboxMissing as exc:
        if os.environ.get("GOODFELLOW_REQUIRE_SANDBOX_TESTS") == "1":
            raise pytest.UsageError(str(exc)) from exc
        os.environ["GOODFELLOW_SANDBOX"] = "off"
        print(
            f"\nconftest: {exc}\nconftest: gate tests run with GOODFELLOW_SANDBOX=off",
            file=sys.stderr,
        )
    except sandbox.SandboxError as exc:
        raise pytest.UsageError(
            f"{exc}\nSet GOODFELLOW_SANDBOX=off to run the suite unisolated knowingly."
        ) from exc
