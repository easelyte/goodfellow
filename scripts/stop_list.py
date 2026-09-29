"""The autopilot stop list: the few actions autopilot never takes on its own.

goodfellow runs on autopilot by default: no per-step approvals, no gating questions.
It stops only where a mistake is public, irreversible or not yours to make. Those
stops live here, in the PreToolUse hook (`guard_engine.py` calls `evaluate`), because
a rule in a prompt is advice and a hook is enforcement.

What a command line reveals, and so what this module enforces:

  stop-foreign-remote  `git push` or `gh pr create` whose destination is a repository
                       outside your owner list (`stop_list.owners` in
                       .goodfellow/guards.json; default: the owner of this project's
                       `origin`). A destination that cannot be resolved from the
                       command and local git config is stopped too (fail closed).
  stop-public-repo     On a repository you own that is PUBLIC: a push to its default
                       branch, a tag push, or `gh pr create`. Visibility is looked up
                       live with `gh repo view` and cached for 10 minutes; if the
                       lookup fails, these actions are stopped (fail closed). Routine
                       pushes to other branches never trigger a lookup.
  stop-release         `gh release create|upload|edit|delete|delete-asset`, and
                       `gh api` writes to a releases endpoint.
  stop-publish         Package publishes (`npm publish`, `twine upload`,
                       `cargo publish`, `docker push`, ...). `--dry-run` is allowed.
  stop-migration       Deploy-style migration commands (`prisma migrate deploy`,
                       `alembic upgrade`, `manage.py migrate`, `rails db:migrate`,
                       ...). Replace the list with `stop_list.migration_commands`.
  stop-force-push      `git push --force` / `-f` / `+refspec` to any branch.
                       `--force-with-lease` to a non-protected branch is allowed.

What a command line does not reveal (sending messages, spending money, product calls
such as naming, pricing or public positioning) stays a written rule in the skills.

The list is on whenever autopilot is on, which is the default. `GOODFELLOW_AUTOPILOT=0`
turns autopilot off and with it this list (you then approve each step yourself).
`GOODFELLOW_GUARDS=0` turns off all built-in guards. Any single stop can be turned off
with its id in `disable_builtins` in .goodfellow/guards.json.

Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

CACHE_TTL_S = 600
GH_TIMEOUT_S = 10

STOP_IDS = (
    "stop-foreign-remote",
    "stop-public-repo",
    "stop-release",
    "stop-publish",
    "stop-migration",
    "stop-force-push",
)

DEFAULT_MIGRATION_COMMANDS = (
    "prisma migrate deploy",
    "prisma db push",
    "supabase db push",
    "alembic upgrade",
    "alembic downgrade",
    "manage.py migrate",
    "rails db:migrate",
    "rake db:migrate",
    "rails db:rollback",
    "flyway migrate",
    "liquibase update",
    "knex migrate:latest",
    "sequelize db:migrate",
    "sequelize-cli db:migrate",
    "typeorm migration:run",
    "drizzle-kit migrate",
    "drizzle-kit push",
    "dbmate up",
    "dbmate migrate",
    "atlas migrate apply",
    "diesel migration run",
    "sqlx migrate run",
    "artisan migrate",
)

DEFAULT_PUBLISH_COMMANDS = (
    "npm publish",
    "pnpm publish",
    "yarn publish",
    "yarn npm publish",
    "bun publish",
    "twine upload",
    "poetry publish",
    "uv publish",
    "flit publish",
    "hatch publish",
    "cargo publish",
    "gem push",
    "vsce publish",
    "ovsx publish",
    "docker push",
)

_RELEASE_WRITES = {"create", "upload", "edit", "delete", "delete-asset"}

# Prefixes that run another program: stripped before matching a command list.
_RUNNERS_1 = {"npx", "bunx", "pnpx", "pnpm", "yarn", "bun", "node", "php", "ruby"}
_RUNNERS_2 = {
    ("pnpm", "exec"),
    ("pnpm", "dlx"),
    ("npm", "exec"),
    ("yarn", "dlx"),
    ("bundle", "exec"),
    ("poetry", "run"),
    ("uv", "run"),
    ("pipenv", "run"),
    ("pdm", "run"),
    ("hatch", "run"),
}
_PYTHON = re.compile(r"^python(\d+(\.\d+)?)?$")

_SCP_URL = re.compile(r"^(?:[^@/\s]+@)?(?P<host>[^:/\s]+):(?!//)(?P<path>[^\s]+)$")
_URL = re.compile(
    r"^(?:ssh|https?|git|git\+ssh|ssh\+git)://(?:[^@/\s]+@)?(?P<host>[^/:\s]+)"
    r"(?::\d+)?/(?P<path>[^\s]+)$"
)


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------- #
# Repository destinations
# --------------------------------------------------------------------------- #


class Dest(NamedTuple):
    local: bool
    host: str = ""
    owner: str = ""
    name: str = ""  # owner/repo (or group/sub/repo)


def parse_remote(url: str) -> Optional[Dest]:
    """Classify a git remote URL. None when it cannot be understood."""
    url = url.strip()
    if not url or any(ch in url for ch in "$`"):
        return None
    if url.startswith(("file://", "/", "./", "../", "~")) or os.path.isdir(url):
        return Dest(local=True)
    m = _URL.match(url) or (
        None if re.match(r"^[A-Za-z]:[\\/]", url) else _SCP_URL.match(url)
    )
    if not m:
        return None
    path = m.group("path").strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None
    return Dest(
        False, m.group("host").lower(), parts[0].lower(), "/".join(parts).lower()
    )


def parse_repo_spec(spec: str) -> Optional[Dest]:
    """`gh -R` forms: OWNER/REPO, HOST/OWNER/REPO, or a URL."""
    if "://" in spec or "@" in spec:
        return parse_remote(spec)
    if any(ch in spec for ch in "$`"):
        return None
    parts = [p for p in spec.strip("/").split("/") if p]
    if len(parts) == 2:
        return Dest(False, "github.com", parts[0].lower(), "/".join(parts).lower())
    if len(parts) == 3:
        return Dest(
            False, parts[0].lower(), parts[1].lower(), "/".join(parts[1:]).lower()
        )
    return None


def _git_out(cwd: str, *args: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _remote_url(cwd: str, remote: str) -> Optional[str]:
    return _git_out(cwd, "remote", "get-url", "--push", remote)


def _current_branch(cwd: str) -> Optional[str]:
    return _git_out(cwd, "symbolic-ref", "--quiet", "--short", "HEAD") or None


def _default_owners(project_dir: str) -> Set[str]:
    url = _remote_url(project_dir, "origin")
    dest = parse_remote(url) if url else None
    return {dest.owner} if dest and not dest.local else set()


def _owners(config: dict, project_dir: str) -> Set[str]:
    configured = (config.get("stop_list") or {}).get("owners")
    if isinstance(configured, list):
        return {str(o).lower() for o in configured}
    return _default_owners(project_dir)


# --------------------------------------------------------------------------- #
# Live visibility lookup (cached, fail closed)
# --------------------------------------------------------------------------- #


def _cache_path(project_dir: str) -> str:
    return os.path.join(project_dir, ".goodfellow", "cache", "repo-visibility.json")


def lookup(dest: Dest, project_dir: str) -> Optional[Tuple[str, str]]:
    """(visibility, default_branch) for a hosted repo, or None if it cannot be
    determined. Answers are cached for CACHE_TTL_S; failures are never cached."""
    key = f"{dest.host}/{dest.name}"
    path = _cache_path(project_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cache = json.load(fh)
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    entry = cache.get(key)
    if (
        isinstance(entry, dict)
        and isinstance(entry.get("at"), (int, float))
        and 0 <= _now() - entry["at"] < CACHE_TTL_S
    ):
        return entry.get("visibility", ""), entry.get("default_branch", "")

    gh = os.environ.get("GOODFELLOW_GH") or "gh"
    target = dest.name if dest.host == "github.com" else f"{dest.host}/{dest.name}"
    try:
        proc = subprocess.run(
            [gh, "repo", "view", target, "--json", "visibility,defaultBranchRef"],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_S,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        visibility = str(data["visibility"]).lower()
        default = str((data.get("defaultBranchRef") or {}).get("name") or "")
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return None
    if visibility not in {"public", "private", "internal"}:
        return None
    cache[key] = {"visibility": visibility, "default_branch": default, "at": _now()}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, path)
    except OSError:
        pass  # caching is an optimisation; the answer still stands
    return visibility, default


# --------------------------------------------------------------------------- #
# Deny messages
# --------------------------------------------------------------------------- #


def _stop(stop_id: str, what: str) -> str:
    return (
        f"goodfellow stop list ({stop_id}): {what} Autopilot stops here: ask the "
        "user before doing this. They can run it themselves, or turn this stop off "
        f'with "{stop_id}" in disable_builtins in .goodfellow/guards.json.'
    )


# --------------------------------------------------------------------------- #
# git push
# --------------------------------------------------------------------------- #


def _git_workdir(tokens: List[str], start: int, cwd: str) -> str:
    """Apply `git -C <dir>` options (cumulative, relative) found before the subcommand."""
    wd = cwd
    i = 1
    while i < start:
        t = tokens[i]
        if t == "-C" and i + 1 < start:
            wd = os.path.join(wd, os.path.expanduser(tokens[i + 1]))
            i += 2
            continue
        if t.startswith("-C") and len(t) > 2:
            wd = os.path.join(wd, os.path.expanduser(t[2:]))
        i += 1
    return wd


class PushArgs(NamedTuple):
    repo: Optional[str]
    refspecs: List[str]
    force: bool
    lease: bool
    tags: bool
    all_branches: bool
    delete: bool


def parse_push(args: Sequence[str]) -> PushArgs:
    from guard_engine import _PUSH_VALUE_OPTS

    repo: Optional[str] = None
    positionals: List[str] = []
    force = lease = tags = all_branches = delete = False
    skip = False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a == "--repo":
            repo = args[i + 1] if i + 1 < len(args) else None
            skip = True
            continue
        if a.startswith("--repo="):
            repo = a.split("=", 1)[1]
            continue
        if a in _PUSH_VALUE_OPTS:
            skip = True
            continue
        if a.startswith("--") and a.split("=", 1)[0] in _PUSH_VALUE_OPTS:
            continue
        if a in ("--force",):
            force = True
            continue
        if a.startswith("--force-with-lease") or a.startswith("--force-if-includes"):
            lease = True
            continue
        if a in ("--tags", "--follow-tags"):
            tags = True
            continue
        if a in ("--all", "--mirror", "--branches"):
            all_branches = True
            continue
        if a in ("--delete",):
            delete = True
            continue
        if a.startswith("-o") and a != "-o":
            continue
        if a.startswith("-") and not a.startswith("--") and len(a) > 1:
            flags = a[1:]
            force = force or "f" in flags
            delete = delete or "d" in flags
            continue
        if a.startswith("-"):
            continue
        positionals.append(a)
    refspecs = positionals
    if repo is None and positionals:
        repo, refspecs = positionals[0], positionals[1:]
    force = force or any(r.startswith("+") for r in refspecs)
    return PushArgs(repo, refspecs, force, lease, tags, all_branches, delete)


def _push_remote(wd: str) -> str:
    branch = _current_branch(wd)
    if branch:
        for key in (
            f"branch.{branch}.pushRemote",
            "remote.pushDefault",
            f"branch.{branch}.remote",
        ):
            value = _git_out(wd, "config", "--get", key)
            if value:
                return value
    else:
        value = _git_out(wd, "config", "--get", "remote.pushDefault")
        if value:
            return value
    return "origin"


def _is_url_like(token: str) -> bool:
    return (
        "://" in token
        or token.startswith(("/", "./", "../", "~"))
        or bool(_SCP_URL.match(token))
    )


def check_push(
    tokens: List[str],
    start: int,
    cwd: str,
    config: dict,
    project_dir: str,
    protected: Sequence[str],
    disabled: Sequence[str],
) -> Optional[str]:
    from guard_engine import _normalize_ref

    wd = _git_workdir(tokens, start, cwd)
    p = parse_push(tokens[start + 1 :])

    if p.force:
        if "stop-force-push" not in disabled:
            return _stop(
                "stop-force-push",
                "a force-push rewrites the remote branch and can destroy commits. "
                "Use --force-with-lease on a feature branch if you must.",
            )

    # Where does it go?
    remote_name: Optional[str] = None
    if p.repo is None:
        remote_name = _push_remote(wd)
        url = _remote_url(wd, remote_name)
    elif any(ch in p.repo for ch in "$`"):
        url = None
    elif _is_url_like(p.repo):
        url = p.repo
    else:
        remote_name = p.repo
        url = _remote_url(wd, remote_name)
    dest = parse_remote(url) if url else None
    if dest is None:
        if "stop-foreign-remote" in disabled:
            return None
        return _stop(
            "stop-foreign-remote",
            f"cannot tell where this push goes ({p.repo or remote_name!r} does not "
            "resolve to a repository URL), so it is stopped rather than guessed.",
        )
    if dest.local:
        return None

    owners = _owners(config, project_dir)
    if dest.owner not in owners and "stop-foreign-remote" not in disabled:
        listed = ", ".join(sorted(owners)) or "empty"
        return _stop(
            "stop-foreign-remote",
            f"this pushes to {dest.name}, whose owner is not in your owner list "
            f"({listed}). Set stop_list.owners in .goodfellow/guards.json to add it.",
        )
    if "stop-public-repo" in disabled:
        return None

    # Which branches (or tags) does it write?
    current = _current_branch(wd)
    targets: Set[str] = set()
    tags = p.tags
    for ref in p.refspecs or ([] if (p.all_branches or p.tags) else ["HEAD"]):
        src_dst = ref.lstrip("+")
        dst = src_dst.split(":")[-1] if ":" in src_dst else src_dst
        if dst in ("HEAD", "@") or dst == "":
            if ":" in src_dst and dst == "":
                continue
            if current:
                targets.add(current)
            continue
        if dst.startswith("refs/tags/") or (
            not dst.startswith("refs/")
            and _git_out(wd, "show-ref", "--verify", "--quiet", f"refs/tags/{dst}")
            is not None
        ):
            tags = True
            continue
        targets.add(_normalize_ref(dst))

    candidates = set(protected)
    if remote_name:
        head = _git_out(
            wd, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote_name}/HEAD"
        )
        if head and "/" in head:
            candidates.add(head.split("/", 1)[1])
    if not (tags or p.all_branches or targets & candidates):
        return None

    info = lookup(dest, project_dir)
    if info is None:
        return _stop(
            "stop-public-repo",
            f"could not check whether {dest.name} is public (the `gh repo view` "
            "lookup failed), and this push writes a tag or a likely default branch.",
        )
    visibility, default = info
    if visibility != "public":
        return None
    if tags:
        return _stop(
            "stop-public-repo",
            f"this pushes a tag to the public repository {dest.name}; a tag push "
            "often publishes a release.",
        )
    if p.all_branches or (default and default in targets):
        return _stop(
            "stop-public-repo",
            f"this pushes to the default branch ({default}) of the public repository "
            f"{dest.name}. Open a pull request from a feature branch instead.",
        )
    return None


# --------------------------------------------------------------------------- #
# gh
# --------------------------------------------------------------------------- #


def _leading_env(tokens: List[str]) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for t in tokens:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", t)
        if m:
            env[m.group(1)] = m.group(2)
        elif t in ("env", "command", "sudo", "nice", "nohup", "time", "exec"):
            continue
        else:
            break
    return env


def _opt_value(args: Sequence[str], short: str, long: str) -> Optional[str]:
    for i, a in enumerate(args):
        if a in (short, long):
            return args[i + 1] if i + 1 < len(args) else ""
        if a.startswith(long + "="):
            return a.split("=", 1)[1]
        if (
            short
            and a.startswith(short)
            and len(a) > len(short)
            and not a.startswith("--")
        ):
            return a[len(short) :]
    return None


def _gh_base_repo(cwd: str) -> Optional[str]:
    """The repository `gh pr create` targets when no -R is given: a remote marked
    `gh-resolved`, else `upstream`, `github`, `origin`, else the only remote."""
    remotes = (_git_out(cwd, "remote") or "").split()
    if not remotes:
        return None
    for r in remotes:
        mark = _git_out(cwd, "config", "--get", f"remote.{r}.gh-resolved")
        if mark == "base":
            return _remote_url(cwd, r)
        if mark and "/" in mark:
            return mark
    for r in ("upstream", "github", "origin"):
        if r in remotes:
            return _remote_url(cwd, r)
    return _remote_url(cwd, remotes[0]) if len(remotes) == 1 else None


def check_pr_create(
    dest: Optional[Dest], config: dict, project_dir: str, disabled: Sequence[str]
) -> Optional[str]:
    if dest is None:
        if "stop-foreign-remote" in disabled:
            return None
        return _stop(
            "stop-foreign-remote",
            "cannot tell which repository this pull request targets, so it is "
            "stopped rather than guessed. Pass -R OWNER/REPO.",
        )
    if dest.local:
        return None
    owners = _owners(config, project_dir)
    if dest.owner not in owners:
        if "stop-foreign-remote" in disabled:
            return None
        listed = ", ".join(sorted(owners)) or "empty"
        return _stop(
            "stop-foreign-remote",
            f"this opens a pull request on {dest.name}, whose owner is not in your "
            f"owner list ({listed}).",
        )
    if "stop-public-repo" in disabled:
        return None
    info = lookup(dest, project_dir)
    if info is None:
        return _stop(
            "stop-public-repo",
            f"could not check whether {dest.name} is public (the `gh repo view` "
            "lookup failed), so opening a pull request there is stopped.",
        )
    if info[0] == "public":
        return _stop(
            "stop-public-repo",
            f"this opens a pull request on the public repository {dest.name}, "
            "which the world can see.",
        )
    return None


def check_gh(
    tokens: List[str], cwd: str, config: dict, project_dir: str, disabled: Sequence[str]
) -> Optional[str]:
    from guard_engine import _strip_wrappers

    env = _leading_env(tokens)
    toks = _strip_wrappers(tokens)
    args = toks[1:]
    if len(args) < 1:
        return None
    group = args[0]
    sub = args[1] if len(args) > 1 else ""

    if group == "release" and sub in _RELEASE_WRITES:
        if "stop-release" not in disabled:
            return _stop(
                "stop-release", f"`gh release {sub}` publishes or changes a release."
            )
        return None

    if group == "api":
        method = (_opt_value(args, "-X", "--method") or "").upper()
        writes = any(
            a in ("-f", "-F", "--field", "--raw-field", "--input")
            or a.startswith(("--field=", "--raw-field=", "--input="))
            for a in args
        )
        if not method:
            method = "POST" if writes else "GET"
        endpoint = next((a for a in args[1:] if not a.startswith("-") and "/" in a), "")
        if method != "GET" and re.search(r"(^|/)releases(/|$)", endpoint):
            if "stop-release" not in disabled:
                return _stop("stop-release", "this writes to a releases API endpoint.")
            return None
        m = re.match(r"^/?repos/([^/]+)/([^/]+)/pulls/?$", endpoint)
        if method == "POST" and m:
            dest = Dest(
                False,
                "github.com",
                m.group(1).lower(),
                f"{m.group(1)}/{m.group(2)}".lower(),
            )
            return check_pr_create(dest, config, project_dir, disabled)
        return None

    if group == "pr" and sub in ("create", "new"):
        spec = (
            _opt_value(args, "-R", "--repo")
            or env.get("GH_REPO")
            or os.environ.get("GH_REPO")
        )
        if spec:
            dest = parse_repo_spec(spec)
        else:
            url = _gh_base_repo(cwd)
            dest = (parse_remote(url) or parse_repo_spec(url)) if url else None
        return check_pr_create(dest, config, project_dir, disabled)
    return None


# --------------------------------------------------------------------------- #
# Command lists (publish, migration)
# --------------------------------------------------------------------------- #


def _token_is(token: str, word: str) -> bool:
    if token == word:
        return True
    if "/" in word:
        return False
    return os.path.basename(token) == word


def _matches_at(tokens: List[str], i: int, pattern: List[str]) -> bool:
    if i + len(pattern) > len(tokens):
        return False
    return all(_token_is(tokens[i + k], w) for k, w in enumerate(pattern))


def matches_command(tokens: List[str], pattern: str) -> bool:
    """Does a command segment run `pattern` (e.g. "prisma migrate deploy"), directly
    or through a runner (`npx`, `bundle exec`, `python -m`, `uv run`, ...)?"""
    words = pattern.split()
    if not words:
        return False
    i = 0
    while i < len(tokens):
        if _matches_at(tokens, i, words):
            return True
        t = os.path.basename(tokens[i])
        if i + 1 < len(tokens) and (t, tokens[i + 1]) in _RUNNERS_2:
            i += 2
        elif t in _RUNNERS_1 or _PYTHON.match(t):
            i += 1
        elif tokens[i].startswith("-") and i > 0:
            i += 1  # a runner's own flag (`npx -y`, `python -m`, `uv run --frozen`)
        else:
            return False
    return False


def _command_list(config: dict, key: str, default: Sequence[str]) -> List[str]:
    value = (config.get("stop_list") or {}).get(key)
    if isinstance(value, list):
        return [str(v) for v in value]
    return list(default)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def evaluate(
    segments: Sequence[List[str]],
    cwd: str,
    config: dict,
    project_dir: str,
    disabled: Sequence[str] = (),
) -> Optional[str]:
    """Return a deny reason for the first segment that hits the stop list, or None."""
    from guard_engine import (
        DEFAULT_PROTECTED_BRANCHES,
        _basename_is,
        _git_arg_start,
        _strip_wrappers,
    )

    protected = config.get("protected_branches", list(DEFAULT_PROTECTED_BRANCHES))
    migrations = _command_list(config, "migration_commands", DEFAULT_MIGRATION_COMMANDS)
    publishes = _command_list(config, "publish_commands", DEFAULT_PUBLISH_COMMANDS)
    for tokens in segments:
        start = _git_arg_start(tokens)
        if start is not None and start < len(tokens) and tokens[start] == "push":
            reason = check_push(
                tokens, start, cwd, config, project_dir, protected, disabled
            )
            if reason:
                return reason
            continue
        stripped = _strip_wrappers(tokens)
        if not stripped:
            continue
        if _basename_is(stripped[0], {"gh", "gh.exe"}):
            reason = check_gh(tokens, cwd, config, project_dir, disabled)
            if reason:
                return reason
            continue
        if "stop-publish" not in disabled and "--dry-run" not in stripped:
            for pat in publishes:
                if matches_command(stripped, pat):
                    return _stop("stop-publish", f"`{pat}` publishes a package.")
        if "stop-migration" not in disabled:
            for pat in migrations:
                if matches_command(stripped, pat):
                    return _stop(
                        "stop-migration",
                        f"`{pat}` looks like a migration against a real database. "
                        "Rehearse it on a copy first (tier T3).",
                    )
    return None
