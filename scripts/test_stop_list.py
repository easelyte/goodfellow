"""Tests for the autopilot stop list (stop_list.py, enforced by the PreToolUse hook).

The stop list is where autopilot must stop and ask: pushes or PRs to repos the user
does not own, default-branch pushes and PR-opens on public repos, releases and package
publishes, migrations, and plain force-pushes. It is a deny/allow gate, so every rule
is pinned on both sides, and the fail-closed branches (unparseable destination, failed
visibility lookup) are asserted explicitly.

Git state is real (temporary repositories with configured remotes; nothing touches the
network). The GitHub visibility lookup goes through a fake `gh` executable
(GOODFELLOW_GH) that answers from a JSON table and logs every call, so the tests can
also assert *when* the network would be used.
"""

import json
import os
import subprocess
import sys
import time

import pytest

import stop_list
from guard_engine import decision_for_input

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "guard_engine.py")

FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys
table = json.load(open(os.environ["FAKE_GH_TABLE"]))
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
# gh repo view OWNER/REPO --json visibility,defaultBranchRef
repo = sys.argv[3]
answer = table.get(repo)
if answer is None or answer == "fail":
    sys.stderr.write("HTTP 404\n")
    sys.exit(1)
print(json.dumps({"visibility": answer[0], "defaultBranchRef": {"name": answer[1]}}))
"""


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A repo whose origin is acme/app on GitHub, plus a fake gh."""
    repo = tmp_path / "app"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("x\n")
    git(repo, "add", "f.txt")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "origin", "git@github.com:acme/app.git")
    git(repo, "switch", "-q", "-c", "feature")

    gh = tmp_path / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    table = tmp_path / "gh-table.json"
    table.write_text(json.dumps({"acme/app": ["PUBLIC", "main"]}))
    log = tmp_path / "gh.log"
    log.write_text("")
    for k in ("GOODFELLOW_AUTOPILOT", "CLAUDE_HOOK_BYPASS", "GOODFELLOW_GUARDS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GOODFELLOW_GH", str(gh))
    monkeypatch.setenv("FAKE_GH_TABLE", str(table))
    monkeypatch.setenv("FAKE_GH_LOG", str(log))

    class Env:
        pass

    e = Env()
    e.repo, e.table, e.log, e.tmp = repo, table, log, tmp_path

    def set_repos(mapping):
        table.write_text(json.dumps(mapping))
        cache = repo / ".goodfellow" / "cache" / "repo-visibility.json"
        if cache.exists():
            cache.unlink()

    def calls():
        return [c for c in log.read_text().splitlines() if c]

    def decide(command, config=None):
        if config is not None:
            d = repo / ".goodfellow"
            d.mkdir(exist_ok=True)
            (d / "guards.json").write_text(json.dumps(config))
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(repo),
        }
        return decision_for_input(payload, str(repo))

    e.set_repos, e.calls, e.decide = set_repos, calls, decide
    return e


def denied(reason, *fragments):
    assert reason is not None, "expected a stop-list deny, got allow"
    for f in fragments:
        assert f in reason, f"{f!r} not in deny reason: {reason}"
    return True


# --------------------------------------------------------------------------- #
# Destinations the user does not own
# --------------------------------------------------------------------------- #


def test_push_to_a_remote_outside_the_owner_list_is_stopped(env):
    git(env.repo, "remote", "add", "upstream", "https://github.com/other/app.git")
    denied(
        env.decide("git push upstream feature"), "other/app", "not in your owner list"
    )


def test_push_to_own_origin_feature_branch_is_allowed(env):
    git(
        env.repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"
    )
    assert env.decide("git push -u origin feature") is None


def test_feature_push_lookups_are_cached(env):
    env.decide("git push origin feature")
    env.decide("git push origin feature")
    assert len(env.calls()) <= 1


def test_unknown_default_branch_is_looked_up_not_assumed(env):
    """No origin/HEAD locally and the real default is trunk: a push to trunk is a
    default-branch push even though trunk is not main or master."""
    env.set_repos({"acme/app": ["PUBLIC", "trunk"]})
    denied(env.decide("git push origin feature:trunk"), "trunk", "public")


def test_explicit_url_push_to_a_nonstandard_default_branch_is_stopped(env):
    env.set_repos({"acme/app": ["PUBLIC", "trunk"]})
    denied(
        env.decide("git push https://github.com/acme/app.git feature:refs/heads/trunk"),
        "trunk",
    )


def test_unknown_default_and_failed_lookup_still_allow_a_feature_push(env):
    env.set_repos({"acme/app": "fail"})
    assert env.decide("git push origin feature") is None


def test_configured_push_refspec_is_the_real_target(env):
    """`git push` with no refspec uses remote.<name>.push when it is set."""
    git(env.repo, "config", "remote.origin.push", "refs/heads/feature:refs/heads/main")
    denied(env.decide("git push origin"), "public")


def test_push_default_upstream_targets_the_tracked_branch(env):
    git(env.repo, "config", "push.default", "upstream")
    git(env.repo, "config", "branch.feature.remote", "origin")
    git(env.repo, "config", "branch.feature.merge", "refs/heads/main")
    denied(env.decide("git push"), "public")


def test_owner_list_from_config_replaces_the_origin_default(env):
    git(env.repo, "remote", "add", "upstream", "https://github.com/other/app.git")
    cfg = {"stop_list": {"owners": ["acme", "other"]}}
    assert env.decide("git push upstream feature", cfg) is None


def test_same_owner_name_on_another_host_is_foreign(env):
    denied(
        env.decide("git push https://evil.example/acme/app.git feature"), "evil.example"
    )


def test_owner_list_entries_can_name_a_host(env):
    cfg = {"stop_list": {"owners": ["acme", "gitlab.com/acme"]}}
    assert env.decide("git push https://gitlab.com/acme/app.git feature", cfg) is None
    denied(
        env.decide("git push https://gitlab.com/other/app.git feature", cfg),
        "gitlab.com/other",
    )


def test_owner_match_is_case_insensitive(env):
    git(env.repo, "remote", "add", "mirror", "https://github.com/ACME/app-mirror.git")
    assert env.decide("git push mirror feature") is None


def test_explicit_url_destination_is_checked(env):
    denied(env.decide("git push https://github.com/other/app.git feature"), "other/app")


def test_repo_option_destination_is_checked(env):
    denied(
        env.decide("git push --repo=git@github.com:other/app.git feature"), "other/app"
    )


def test_unknown_remote_name_fails_closed(env):
    denied(env.decide("git push nosuchremote feature"), "cannot tell where")


def test_variable_destination_fails_closed(env):
    denied(env.decide("git push $REMOTE feature"), "cannot tell where")


def test_local_path_remote_is_not_a_hosted_repo(env):
    bare = env.tmp / "bare.git"
    git(env.tmp, "init", "-q", "--bare", str(bare))
    assert env.decide(f"git push {bare} feature") is None


def test_nested_shell_and_wrappers_are_seen(env):
    git(env.repo, "remote", "add", "upstream", "https://github.com/other/app.git")
    denied(env.decide("bash -c 'env FOO=1 git push upstream feature'"), "other/app")


def test_git_dash_c_resolves_remotes_in_that_directory(env):
    other = env.tmp / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    git(other, "remote", "add", "origin", "https://github.com/stranger/lib.git")
    # owners default to *this* project's origin (acme), so stranger/lib is foreign
    denied(env.decide(f"git -C {other} push origin main"), "stranger/lib")


def test_no_origin_and_no_owner_list_stops_hosted_pushes(env):
    git(env.repo, "remote", "rename", "origin", "gh")
    denied(env.decide("git push gh feature"), "owner list")


# --------------------------------------------------------------------------- #
# Public repos: default branch, tags, PR-open (live visibility, fail closed)
# --------------------------------------------------------------------------- #


def test_push_to_default_branch_of_a_public_repo_is_stopped(env):
    denied(env.decide("git push origin main"), "acme/app", "public")


def test_push_to_default_branch_of_a_private_repo_is_allowed(env):
    env.set_repos({"acme/app": ["PRIVATE", "main"]})
    assert env.decide("git push origin main") is None


def test_refspec_to_the_default_branch_is_stopped(env):
    denied(env.decide("git push origin feature:refs/heads/main"), "public")


def test_bare_push_on_the_default_branch_is_stopped(env):
    git(env.repo, "switch", "-q", "main")
    denied(env.decide("git push"), "public")


def test_head_refspec_on_the_default_branch_is_stopped(env):
    git(env.repo, "switch", "-q", "main")
    denied(env.decide("git push origin HEAD"), "public")


def test_failed_visibility_lookup_fails_closed_for_a_default_branch_push(env):
    env.set_repos({"acme/app": "fail"})
    denied(env.decide("git push origin main"), "could not check")


def test_failed_lookup_never_blocks_a_routine_branch_push(env):
    env.set_repos({"acme/app": "fail"})
    assert env.decide("git push origin feature") is None


def test_push_to_master_is_allowed_when_the_real_default_is_main(env):
    assert env.decide("git push origin master") is None  # looked up: default is main
    assert len(env.calls()) == 1


def test_tag_push_to_a_public_repo_is_stopped(env):
    denied(env.decide("git push origin --tags"), "tag")
    git(env.repo, "tag", "v1.0.0")
    denied(env.decide("git push origin v1.0.0"), "tag")


def test_visibility_is_cached_for_ten_minutes(env, monkeypatch):
    env.decide("git push origin main")
    env.decide("git push origin main")
    assert len(env.calls()) == 1
    real = time.time()
    monkeypatch.setattr(stop_list, "_now", lambda: real + stop_list.CACHE_TTL_S + 1)
    env.decide("git push origin main")
    assert len(env.calls()) == 2


def test_cache_entry_just_inside_the_ttl_is_reused(env, monkeypatch):
    env.decide("git push origin main")
    real = time.time()
    monkeypatch.setattr(stop_list, "_now", lambda: real + stop_list.CACHE_TTL_S - 5)
    env.decide("git push origin main")
    assert len(env.calls()) == 1


def test_pr_create_on_a_public_own_repo_is_stopped(env):
    denied(env.decide("gh pr create --fill"), "acme/app", "public")


def test_pr_create_on_a_private_own_repo_is_allowed(env):
    env.set_repos({"acme/app": ["PRIVATE", "main"]})
    assert env.decide("gh pr create --fill --base main") is None


def test_pr_create_to_a_foreign_repo_is_stopped(env):
    denied(env.decide("gh pr create -R other/app --fill"), "other/app")


def test_pr_create_head_flag_is_not_the_destination(env):
    env.set_repos({"acme/app": ["PRIVATE", "main"]})
    assert env.decide("gh pr create --head other:feature --fill") is None


def test_pr_create_lookup_failure_fails_closed(env):
    env.set_repos({"acme/app": "fail"})
    denied(env.decide("gh pr create --fill"), "could not check")


def test_pr_create_prefers_the_upstream_remote(env):
    git(env.repo, "remote", "add", "upstream", "https://github.com/other/app.git")
    denied(env.decide("gh pr create --fill"), "other/app")


def test_gh_repo_env_names_the_destination(env):
    denied(env.decide("GH_REPO=other/app gh pr create --fill"), "other/app")


# --------------------------------------------------------------------------- #
# Releases, publishes, migrations, force-push
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cmd",
    [
        "gh release create v1.0.0 --notes x",
        "gh release upload v1.0.0 dist.tgz",
        "gh api -X POST repos/acme/app/releases -f tag_name=v1",
    ],
)
def test_releases_are_stopped(env, cmd):
    denied(env.decide(cmd), "release")


@pytest.mark.parametrize(
    "cmd",
    [
        'gh api -X POST -H "Accept: application/vnd.github+json" repos/acme/app/releases -f tag_name=v1',
        "gh api --method POST --header X-Custom:a/b repos/acme/app/releases -f tag_name=v1",
        "gh api -H Accept:x/y -f tag_name=v1 repos/acme/app/releases",
    ],
)
def test_option_values_are_not_mistaken_for_the_api_endpoint(env, cmd):
    denied(env.decide(cmd), "release")


def test_api_pr_create_behind_a_header_is_checked(env):
    denied(
        env.decide(
            'gh api -X POST -H "Accept: application/vnd.github+json" repos/other/app/pulls -f title=x'
        ),
        "other/app",
    )


def test_reading_releases_is_allowed(env):
    assert env.decide("gh release list") is None
    assert env.decide("gh release view v1.0.0") is None


@pytest.mark.parametrize(
    "cmd",
    [
        "npm publish",
        "pnpm publish --access public",
        "twine upload dist/*",
        "cargo publish",
    ],
)
def test_package_publishes_are_stopped(env, cmd):
    denied(env.decide(cmd), "publish")


def test_npm_publish_dry_run_is_allowed(env):
    assert env.decide("npm publish --dry-run") is None


@pytest.mark.parametrize(
    "cmd",
    [
        "npx prisma migrate deploy",
        "python manage.py migrate",
        "./manage.py migrate app 0003",
        "alembic upgrade head",
        "bundle exec rails db:migrate",
        "supabase db push",
    ],
)
def test_migration_commands_are_stopped(env, cmd):
    denied(env.decide(cmd), "migration")


def test_non_deploy_migration_subcommands_are_allowed(env):
    assert env.decide("npx prisma migrate dev --create-only") is None
    assert env.decide("alembic revision -m add_index") is None


def test_migration_list_can_be_replaced(env):
    assert (
        env.decide("alembic upgrade head", {"stop_list": {"migration_commands": []}})
        is None
    )
    denied(
        env.decide(
            "make migrate-prod",
            {"stop_list": {"migration_commands": ["make migrate-prod"]}},
        ),
        "migration",
    )


def test_plain_force_push_is_stopped(env):
    denied(env.decide("git push --force origin feature"), "force")
    denied(env.decide("git push origin +feature"), "force")


def test_force_with_lease_to_a_feature_branch_is_allowed(env):
    assert env.decide("git push --force-with-lease origin feature") is None


# --------------------------------------------------------------------------- #
# Switches
# --------------------------------------------------------------------------- #


def test_autopilot_off_disables_the_stop_list(env, monkeypatch):
    monkeypatch.setenv("GOODFELLOW_AUTOPILOT", "0")
    assert env.decide("gh release create v1") is None


def test_dry_run_autopilot_keeps_the_stop_list(env, monkeypatch):
    monkeypatch.setenv("GOODFELLOW_AUTOPILOT", "dry-run")
    denied(env.decide("gh release create v1"), "release")


def test_a_single_stop_can_be_disabled(env):
    cfg = {"disable_builtins": ["stop-release"]}
    assert env.decide("gh release create v1", cfg) is None
    denied(env.decide("npm publish", cfg), "publish")


def test_writing_the_command_as_text_does_not_trip_it(env):
    assert env.decide('git commit -m "never run gh release create"') is None


def test_invalid_stop_list_config_keeps_the_stop_list_on(env):
    """A malformed guards.json skips user rules but must not disarm the stop list."""
    d = env.repo / ".goodfellow"
    d.mkdir(exist_ok=True)
    (d / "guards.json").write_text("{not json")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "gh release create v1"},
        "cwd": str(env.repo),
    }
    denied(decision_for_input(payload, str(env.repo)), "release")


def test_wire_contract_through_the_real_hook(env):
    proc = subprocess.run(
        [sys.executable, ENGINE, "--project-dir", str(env.repo)],
        input=json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "gh release create v1"},
                "cwd": str(env.repo),
            }
        ),
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    assert proc.returncode == 0
    assert proc.stdout.strip(), "expected a deny object on stdout"
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "stop list" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_command_lists_match_only_in_command_position(env):
    """`echo alembic upgrade head` mentions a migration; it does not run one."""
    assert env.decide("echo alembic upgrade head") is None
    assert env.decide("grep -rn npm publish docs") is None


def test_graphql_create_pull_request_fails_closed(env):
    """The target is an opaque repository id, so the stop cannot check ownership."""
    denied(
        env.decide(
            "gh api graphql -f query='mutation { createPullRequest(input: {}) { clientMutationId } }'"
        ),
        "cannot tell which repository",
    )


def test_graphql_queries_are_allowed(env):
    assert env.decide("gh api graphql -f query='{ viewer { login } }'") is None


# --------------------------------------------------------------------------- #
# Review round 2: git global options, API query strings, stale remote HEAD
# --------------------------------------------------------------------------- #


def test_git_config_option_before_push_is_seen(env):
    denied(
        env.decide(
            "git -c push.default=current push https://github.com/other/app.git feature"
        ),
        "other/app",
    )


def test_git_boolean_global_option_before_push_is_seen(env):
    git(env.repo, "remote", "add", "upstream", "https://github.com/other/app.git")
    denied(env.decide("git --no-pager push upstream feature"), "other/app")


def test_git_config_override_of_the_remote_url_is_honoured(env):
    denied(
        env.decide(
            "git -c remote.origin.url=https://github.com/other/app.git push origin feature"
        ),
        "other/app",
    )


def test_git_config_env_option_fails_closed(env):
    denied(
        env.decide("git --config-env=remote.origin.url=URL push origin feature"),
        "cannot tell",
    )


@pytest.mark.parametrize(
    "cmd",
    [
        "gh api -X POST repos/acme/app/releases?draft=true -f tag_name=v1",
        "gh api -X POST 'repos/acme/app/releases/?x=1' -f tag_name=v1",
    ],
)
def test_api_query_string_does_not_hide_a_release_write(env, cmd):
    denied(env.decide(cmd), "release")


def test_api_query_string_does_not_hide_a_pull_request(env):
    denied(
        env.decide("gh api -X POST repos/other/app/pulls?draft=true -f title=x"),
        "other/app",
    )


def test_api_owner_placeholders_resolve_to_this_repo(env):
    denied(
        env.decide("gh api -X POST 'repos/{owner}/{repo}/pulls' -f title=x"),
        "acme/app",
        "public",
    )


def test_stale_local_remote_head_does_not_hide_the_real_default(env):
    git(
        env.repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"
    )
    env.set_repos({"acme/app": ["PUBLIC", "trunk"]})
    denied(env.decide("git push origin feature:trunk"), "trunk")


# --------------------------------------------------------------------------- #
# Review round 3: gh global options, matching refspec, follow-tags
# --------------------------------------------------------------------------- #


def test_gh_repo_option_before_the_command_group_is_seen(env):
    denied(env.decide("gh -R other/app pr create --fill"), "other/app")
    denied(env.decide("gh --repo=other/app release create v1"), "release")


def test_matching_refspec_can_write_the_default_branch(env):
    denied(env.decide("git push origin :"), "public")


def test_follow_tags_config_with_an_annotated_tag_is_a_tag_push(env):
    git(env.repo, "tag", "-a", "v9.9.9", "-m", "release")
    git(env.repo, "config", "push.followTags", "true")
    denied(env.decide("git push origin feature"), "tag")


def test_follow_tags_with_no_annotated_tag_is_a_routine_push(env):
    """No tag can travel, so a failed lookup must not block the feature push."""
    env.set_repos({"acme/app": "fail"})
    assert env.decide("git push --follow-tags origin feature") is None


def test_no_follow_tags_overrides_the_config(env):
    git(env.repo, "tag", "-a", "v9.9.9", "-m", "release")
    git(env.repo, "config", "push.followTags", "true")
    env.set_repos({"acme/app": ["PRIVATE", "main"]})
    assert env.decide("git push --no-follow-tags origin feature") is None
