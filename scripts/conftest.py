"""Test-session setup.

red_check and mutation_check refuse to run tests without their sandbox
(bubblewrap). On a machine where it cannot run, the tests of everything else
those gates do run unisolated instead (GOODFELLOW_SANDBOX=off), and the sandbox
tests themselves skip. CI sets GOODFELLOW_REQUIRE_SANDBOX_TESTS=1, which turns
a missing sandbox into a failure rather than a skip.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sandbox  # noqa: E402


def pytest_configure(config):
    if "GOODFELLOW_SANDBOX" in os.environ:
        return
    try:
        sandbox.create([])
    except sandbox.SandboxError as exc:
        if os.environ.get("GOODFELLOW_REQUIRE_SANDBOX_TESTS") == "1":
            raise
        os.environ["GOODFELLOW_SANDBOX"] = "off"
        print(
            f"\nconftest: {exc}\nconftest: gate tests run with GOODFELLOW_SANDBOX=off",
            file=sys.stderr,
        )
