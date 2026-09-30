"""Tests for tier.py: the risk-tier resolver behind `brainstorm` and `ship`.

The resolver is a gate: it decides whether an operator's `--tier` (or `ship --quick`)
is honoured or refused. Every case drives the real CLI against a real temporary git
repository, so the diff collection, the path lists and the exit-code contract are all
exercised end to end. Exit codes: 0 resolved, 3 refused (below a hard floor),
2 could not decide (never a silent T0).
"""

import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TIER = os.path.join(HERE, "tier.py")


def git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / "README.md").write_text("hi\n")
    git(r, "add", "README.md")
    git(r, "commit", "-q", "-m", "init")
    git(r, "switch", "-q", "-c", "feature")
    return r


def commit(repo, path, text="x\n"):
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", f"touch {path}")


def high_stakes(repo, *globs):
    d = repo / ".goodfellow"
    d.mkdir(exist_ok=True)
    (d / "high_stakes_paths.txt").write_text("\n".join(globs) + "\n")


def run(repo, *args, env=None):
    full = {
        k: v
        for k, v in os.environ.items()
        if k not in ("GOODFELLOW_HIGH_STAKES_PATHS", "GOODFELLOW_LIVE_STATE_PATHS")
    }
    if env:
        full.update(env)
    proc = subprocess.run(
        [sys.executable, TIER, "resolve", "--repo", str(repo), "--json", *args],
        capture_output=True,
        text=True,
        env=full,
    )
    data = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc.returncode, data, proc.stderr


# --------------------------------------------------------------------------- #
# Resolution without floors
# --------------------------------------------------------------------------- #


def test_proposed_tier_is_used_when_nothing_raises_it(repo):
    commit(repo, "src/export.py")
    rc, data, _ = run(repo, "--base", "main", "--proposed", "T1")
    assert rc == 0
    assert data["tier"] == "T1"
    assert data["floor"] == "T0"
    assert data["source"] == "proposed"


def test_operator_may_lower_the_model_proposal_down_to_the_floor(repo):
    commit(repo, "src/export.py")
    rc, data, _ = run(repo, "--base", "main", "--proposed", "T2", "--tier", "T0")
    assert rc == 0
    assert data["tier"] == "T0"
    assert data["source"] == "override"


def test_quick_is_an_alias_for_t0(repo):
    commit(repo, "src/export.py")
    rc, data, _ = run(repo, "--base", "main", "--proposed", "T1", "--quick")
    assert rc == 0
    assert data["tier"] == "T0"
    assert data["source"] == "override"


def test_previous_tier_is_a_ratchet_for_the_model(repo):
    """A tier chosen earlier in the run (brainstorm) is never lowered by the model."""
    commit(repo, "src/export.py")
    rc, data, _ = run(repo, "--base", "main", "--proposed", "T1", "--previous", "T2")
    assert rc == 0
    assert data["tier"] == "T2"
    assert data["source"] == "previous"


# --------------------------------------------------------------------------- #
# Hard floors
# --------------------------------------------------------------------------- #


def test_high_stakes_path_raises_the_floor_to_t1(repo):
    high_stakes(repo, "src/auth/**")
    commit(repo, "src/auth/session.py")
    rc, data, _ = run(repo, "--base", "main", "--proposed", "T0")
    assert rc == 0
    assert data["floor"] == "T1"
    assert data["tier"] == "T1"
    assert data["source"] == "floor"
    assert any("src/auth/session.py" in r for r in data["floor_reasons"])


def test_tier_below_the_high_stakes_floor_is_refused(repo):
    high_stakes(repo, "src/auth/**")
    commit(repo, "src/auth/session.py")
    rc, data, err = run(repo, "--base", "main", "--proposed", "T1", "--tier", "T0")
    assert rc == 3
    assert data["refused"] is True
    assert data["floor"] == "T1"
    assert "src/auth/session.py" in err


def test_quick_is_refused_on_a_high_stakes_diff(repo):
    high_stakes(repo, "src/auth/**")
    commit(repo, "src/auth/session.py")
    rc, data, _ = run(repo, "--base", "main", "--proposed", "T0", "--quick")
    assert rc == 3
    assert data["refused"] is True


def test_override_exactly_at_the_floor_is_honoured(repo):
    """Boundary: floor T1, --tier T1 is allowed (T0 is the refused side)."""
    high_stakes(repo, "src/auth/**")
    commit(repo, "src/auth/session.py")
    rc, data, _ = run(repo, "--base", "main", "--proposed", "T2", "--tier", "T1")
    assert rc == 0
    assert data["tier"] == "T1"


def test_default_live_state_path_sets_a_t3_floor(repo):
    commit(repo, "db/migrations/0042_add_index.sql")
    rc, data, _ = run(repo, "--base", "main", "--proposed", "T1")
    assert rc == 0
    assert data["floor"] == "T3"
    assert data["tier"] == "T3"


def test_tier_below_a_live_state_floor_is_refused(repo):
    commit(repo, "deploy/worker.service")
    rc, data, err = run(repo, "--base", "main", "--proposed", "T3", "--tier", "T2")
    assert rc == 3
    assert data["floor"] == "T3"
    assert "deploy/worker.service" in err


def test_live_state_flag_sets_a_t3_floor_without_a_path(repo):
    commit(repo, "src/mailer.py")
    rc, data, _ = run(
        repo,
        "--base",
        "main",
        "--proposed",
        "T1",
        "--live-state",
        "sends email to customers",
    )
    assert rc == 0
    assert data["tier"] == "T3"
    assert any("sends email to customers" in r for r in data["floor_reasons"])


def test_project_live_state_list_extends_and_can_drop_a_default(repo):
    d = repo / ".goodfellow"
    d.mkdir()
    (d / "live_state_paths.txt").write_text("ops/**\n!**/migrations/**\n")
    commit(repo, "db/migrations/0001.sql")
    rc, data, _ = run(repo, "--base", "main", "--proposed", "T0")
    assert rc == 0 and data["floor"] == "T0"  # default dropped
    commit(repo, "ops/rotate.sh")
    rc, data, _ = run(repo, "--base", "main", "--proposed", "T0")
    assert rc == 0 and data["floor"] == "T3"  # project glob applies


def test_uncommitted_and_untracked_files_count(repo):
    high_stakes(repo, "billing/**")
    (repo / "billing").mkdir()
    (repo / "billing" / "charge.py").write_text("x\n")  # untracked
    rc, data, _ = run(repo, "--base", "main", "--proposed", "T0")
    assert rc == 0 and data["floor"] == "T1"


def test_a_deleted_live_state_file_still_counts(repo):
    commit(repo, "db/migrations/0001.sql")
    git(repo, "switch", "-q", "main")
    git(repo, "merge", "-q", "feature")
    git(repo, "switch", "-q", "-c", "cleanup")
    git(repo, "rm", "-q", "db/migrations/0001.sql")
    git(repo, "commit", "-q", "-m", "drop")
    rc, data, _ = run(repo, "--base", "main", "--proposed", "T0")
    assert rc == 0 and data["floor"] == "T3"


def test_explicit_paths_work_without_a_base(repo):
    """brainstorm runs before any diff exists: it passes the paths it expects to touch."""
    high_stakes(repo, "src/auth/**")
    rc, data, _ = run(repo, "--paths", "src/auth/login.py", "--proposed", "T0")
    assert rc == 0 and data["tier"] == "T1"


# --------------------------------------------------------------------------- #
# Fail closed: no decision is ever a silent T0
# --------------------------------------------------------------------------- #


def test_bad_base_is_an_error_not_t0(repo):
    rc, data, err = run(repo, "--base", "no-such-ref", "--proposed", "T0")
    assert rc == 2
    assert data is None
    assert "no-such-ref" in err


def test_configured_high_stakes_list_that_does_not_exist_is_an_error(repo, tmp_path):
    commit(repo, "src/x.py")
    rc, _, err = run(
        repo,
        "--base",
        "main",
        "--proposed",
        "T0",
        env={"GOODFELLOW_HIGH_STAKES_PATHS": str(tmp_path / "missing.txt")},
    )
    assert rc == 2
    assert "missing.txt" in err


def test_no_proposal_and_no_override_is_an_error(repo):
    rc, _, _ = run(repo, "--base", "main")
    assert rc == 2


@pytest.mark.parametrize("bad", ["T4", "t1", "1", ""])
def test_invalid_tier_values_are_rejected(repo, bad):
    rc, _, _ = run(repo, "--base", "main", "--proposed", bad)
    assert rc == 2


def test_quick_conflicting_with_another_tier_is_an_error(repo):
    rc, _, _ = run(
        repo, "--base", "main", "--proposed", "T0", "--quick", "--tier", "T2"
    )
    assert rc == 2


# --------------------------------------------------------------------------- #
# Human output
# --------------------------------------------------------------------------- #


def test_text_output_announces_tier_and_floor(repo):
    high_stakes(repo, "src/auth/**")
    commit(repo, "src/auth/session.py")
    proc = subprocess.run(
        [
            sys.executable,
            TIER,
            "resolve",
            "--repo",
            str(repo),
            "--base",
            "main",
            "--proposed",
            "T0",
            "--reason",
            "token refresh fails after expiry",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    lines = proc.stdout.strip().splitlines()
    assert lines[0] == "Tier T1 (feature): token refresh fails after expiry"
    assert lines[1].startswith("Floor T1: src/auth/session.py")


# --------------------------------------------------------------------------- #
# Wiring: the entry skills call the resolver and honour its exit codes
# --------------------------------------------------------------------------- #

SKILLS = os.path.join(os.path.dirname(HERE), "skills")


@pytest.mark.parametrize("skill", ["brainstorm", "ship"])
def test_entry_skills_run_the_resolver_and_handle_refusal(skill):
    with open(os.path.join(SKILLS, skill, "SKILL.md"), encoding="utf-8") as fh:
        flat = " ".join(fh.read().split())
    assert 'scripts/tier.py" resolve' in flat
    assert "**Exit 3:**" in flat and "Never honour it silently" in flat
    assert "**Exit 2:**" in flat and "T0" in flat


def test_ship_quick_is_documented_as_the_t0_alias():
    with open(os.path.join(SKILLS, "ship", "SKILL.md"), encoding="utf-8") as fh:
        text = fh.read()
    assert "`--quick` means `--tier T0`" in text
    assert "Quick mode" not in text  # the old single-round mode is gone
