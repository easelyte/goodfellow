"""Tests for the stop list (stop_list.py, run by the PreToolUse hook).

The stop list never blocks. It asks: the hook returns `permissionDecision: "ask"`,
Claude Code shows its normal confirmation, and the user's yes goes through. Four
rules stay, each pinned on both sides:

  stop-force-push      a plain force-push
  stop-default-branch  a push that writes the default branch
  stop-release         a tag push, or a GitHub release write
  stop-publish         a package publish

Opening or merging a pull request is reviewable and reversible, so it asks nothing.
Nothing here touches the network.
"""

import json
import os
import subprocess
import sys

import pytest

import stop_list
from guard_engine import decide

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "guard_engine.py")


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo on branch `feature` whose origin is a GitHub URL (never contacted)."""
    r = tmp_path / "app"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / "f.txt").write_text("x\n")
    git(r, "add", "f.txt")
    git(r, "commit", "-q", "-m", "init")
    git(r, "remote", "add", "origin", "git@github.com:acme/app.git")
    git(r, "switch", "-q", "-c", "feature")
    for k in ("CLAUDE_HOOK_BYPASS", "GOODFELLOW_STOP_LIST"):
        monkeypatch.delenv(k, raising=False)
    return r


def ask(repo, command, config=None):
    d = decide(
        {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)},
        str(repo),
        config=config,
    )
    if d is None:
        return None
    decision, reason = d
    assert decision == "ask", f"the stop list must ask, never {decision!r}"
    return reason


# --- force-push -----------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    ["git push --force origin feature", "git push -f", "git push origin +feature"],
)
def test_force_push_asks(repo, cmd):
    assert "stop-force-push" in ask(repo, cmd)


def test_force_with_lease_on_a_feature_branch_asks_nothing(repo):
    assert ask(repo, "git push --force-with-lease origin feature") is None


# --- default branch -------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "git push origin main",
        "git push origin feature:main",
        "git push origin HEAD:refs/heads/main",
        "git push --all origin",
    ],
)
def test_push_to_the_default_branch_asks(repo, cmd):
    assert "stop-default-branch" in ask(repo, cmd)


def test_bare_push_from_the_default_branch_asks(repo):
    git(repo, "switch", "-q", "main")
    assert "stop-default-branch" in ask(repo, "git push")


def test_push_to_a_feature_branch_asks_nothing(repo):
    assert ask(repo, "git push -u origin feature") is None
    assert ask(repo, "git push") is None


def test_remote_head_names_the_default_branch(repo):
    git(repo, "update-ref", "refs/remotes/origin/trunk", "HEAD")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    assert "stop-default-branch" in ask(repo, "git push origin feature:trunk")


# --- releases and tags ----------------------------------------------------------


def test_tag_push_asks(repo):
    git(repo, "tag", "v1.0.0")
    assert "stop-release" in ask(repo, "git push origin v1.0.0")
    assert "stop-release" in ask(repo, "git push --tags")


@pytest.mark.parametrize(
    "cmd",
    [
        "gh release create v1.0.0 --notes x",
        "gh release upload v1.0.0 dist.tgz",
        "gh api -X POST repos/acme/app/releases -f tag_name=v1",
    ],
)
def test_release_writes_ask(repo, cmd):
    assert "stop-release" in ask(repo, cmd)


def test_reading_releases_asks_nothing(repo):
    assert ask(repo, "gh release view v1.0.0") is None
    assert ask(repo, "gh api repos/acme/app/releases") is None


# --- publishing -----------------------------------------------------------------


@pytest.mark.parametrize("cmd", ["npm publish", "twine upload dist/*", "cargo publish"])
def test_package_publish_asks(repo, cmd):
    assert "stop-publish" in ask(repo, cmd)


def test_publish_dry_run_asks_nothing(repo):
    assert ask(repo, "npm publish --dry-run") is None


# --- what no longer asks --------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "gh pr create --fill",
        "gh pr create -R someone-else/upstream --fill",
        "gh pr merge 12 --squash",
        "git push git@github.com:someone-else/fork.git feature",
        "prisma migrate deploy",
        "git add -A",
    ],
)
def test_reviewable_or_reversible_actions_ask_nothing(repo, cmd):
    assert ask(repo, cmd) is None


def test_no_network_lookup_remains():
    assert not hasattr(stop_list, "lookup")
    assert set(stop_list.STOP_IDS) == {
        "stop-force-push",
        "stop-default-branch",
        "stop-release",
        "stop-publish",
    }


# --- switches -------------------------------------------------------------------


def test_disable_one_stop(repo):
    assert ask(repo, "npm publish", {"disable_builtins": ["stop-publish"]}) is None


def test_stop_list_off_switch(repo, monkeypatch):
    monkeypatch.setenv("GOODFELLOW_STOP_LIST", "0")
    assert ask(repo, "git push --force") is None


# --- the wire: ask, never deny --------------------------------------------------


def test_hook_emits_ask_with_a_one_line_reason(repo):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CLAUDE_HOOK_BYPASS", "GOODFELLOW_STOP_LIST", "CLAUDE_PROJECT_DIR")
    }
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git push --force origin feature"},
        "cwd": str(repo),
    }
    p = subprocess.run(
        [sys.executable, ENGINE, "--project-dir", str(repo)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    assert p.returncode == 0
    hs = json.loads(p.stdout)["hookSpecificOutput"]
    assert hs["permissionDecision"] == "ask"
    assert "\n" not in hs["permissionDecisionReason"]


def test_a_project_block_rule_still_denies_when_a_stop_would_ask(repo):
    """The project's own block rules win: a matching stop must not turn a
    forbidden action into a confirmation."""
    cfg = {"block": [{"id": "no-publish", "pattern": "npm publish", "reason": "never"}]}
    d = decide(
        {"tool_name": "Bash", "tool_input": {"command": "npm publish"}, "cwd": str(repo)},
        str(repo),
        config=cfg,
    )
    assert d is not None and d[0] == "deny" and "no-publish" in d[1]


def test_config_env_that_can_redirect_a_push_asks(repo):
    """`--config-env` takes a setting from an environment variable the hook cannot
    read, so where the push lands is unknown: ask rather than guess."""
    cmd = "git --config-env=remote.origin.push=PUSH_REFS push origin"
    assert "stop-default-branch" in ask(repo, cmd)


def test_the_example_config_keeps_every_default_publish_command(tmp_path):
    example = os.path.join(os.path.dirname(HERE), "configs", "guards.example.json")
    with open(example) as fh:
        cfg = json.load(fh)
    publishes = stop_list._command_list(
        cfg, "publish_commands", stop_list.DEFAULT_PUBLISH_COMMANDS
    )
    assert set(stop_list.DEFAULT_PUBLISH_COMMANDS) <= set(publishes)
