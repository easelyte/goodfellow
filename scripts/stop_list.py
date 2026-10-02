"""The stop list: the few actions goodfellow asks you about before it takes them.

goodfellow runs on autopilot. The stop list never blocks: for each action below the
PreToolUse hook (`guard_engine.py` calls `evaluate`) returns
`permissionDecision: "ask"` with a one-line reason, so Claude Code shows its normal
confirmation and your yes goes through. These are the actions that are hard to
take back:

  stop-force-push      `git push --force` / `-f` / `+refspec`. `--force-with-lease`
                       is fine.
  stop-default-branch  a push that writes the default branch: `protected_branches`
                       in .goodfellow/guards.json (default main, master) or the
                       remote's HEAD as git knows it locally. `--all`, `--mirror`
                       and wildcard refspecs count.
  stop-release         a tag push (`--tags`, a tag refspec, `--follow-tags` with an
                       annotated tag to send), `gh release create|upload|edit|delete`,
                       and `gh api` writes to a releases endpoint.
  stop-publish         package publishes (`npm publish`, `twine upload`,
                       `cargo publish`, `docker push`, ...). `--dry-run` is fine.
                       Replace the list with `stop_list.publish_commands`.

Everything else, including opening or merging a pull request, is reviewable or
reversible and asks nothing. Nothing here touches the network.

`GOODFELLOW_STOP_LIST=0` turns the list off. One entry can be turned off with its id
in `disable_builtins` in .goodfellow/guards.json.

Standard library only.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import List, NamedTuple, Optional, Sequence, Set, Tuple

STOP_IDS = (
    "stop-force-push",
    "stop-default-branch",
    "stop-release",
    "stop-publish",
)
# Ids and stop_list keys from 0.4.0, accepted in old configs and ignored.
RETIRED_IDS = ("stop-foreign-remote", "stop-public-repo", "stop-migration")
RETIRED_KEYS = ("owners", "migration_commands")


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


def _git_out(cwd: str, *args: str, cfg: Sequence[str] = ()) -> Optional[str]:
    """Run a read-only git query in `cwd`. `cfg` carries the `-c name=value`
    overrides of the command being judged, so git resolves remotes and push
    settings exactly as that command would."""
    pre: List[str] = []
    for pair in cfg:
        pre += ["-c", pair]
    try:
        proc = subprocess.run(
            ["git", *pre, "-C", cwd, *args], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _current_branch(cwd: str, cfg: Sequence[str] = ()) -> Optional[str]:
    return _git_out(cwd, "symbolic-ref", "--quiet", "--short", "HEAD", cfg=cfg) or None


class GitGlobals(NamedTuple):
    workdir: str
    cfg: List[str]  # `-c name=value` pairs, in order
    opaque: bool  # an option whose effect cannot be resolved here (--config-env)


def _git_globals(tokens: List[str], start: int, cwd: str) -> GitGlobals:
    """git's own options between `git` and the subcommand: `-C <dir>` (cumulative,
    relative), `-c name=value`, and `--config-env` (its value lives in an
    environment variable the hook cannot see)."""
    from guard_engine import _strip_wrappers

    i = len(tokens) - len(_strip_wrappers(tokens)) + 1  # first token after `git`
    wd, cfg, opaque = cwd, [], False
    while i < start:
        t = tokens[i]
        nxt = tokens[i + 1] if i + 1 < start else ""
        if t == "-C":
            wd = os.path.join(wd, os.path.expanduser(nxt))
            i += 2
            continue
        if t.startswith("-C"):
            wd = os.path.join(wd, os.path.expanduser(t[2:]))
        elif t == "-c":
            cfg.append(nxt)
            i += 2
            continue
        elif t.startswith("-c") and not t.startswith("--"):
            cfg.append(t[2:])
        elif t == "--config-env" or t.startswith("--config-env="):
            opaque = True
        i += 1
    return GitGlobals(wd, cfg, opaque)


class PushArgs(NamedTuple):
    repo: Optional[str]
    refspecs: List[str]
    force: bool
    lease: bool
    tags: bool
    all_branches: bool
    delete: bool
    follow_tags: Optional[bool] = None  # --follow-tags / --no-follow-tags / unset


def parse_push(args: Sequence[str]) -> PushArgs:
    from guard_engine import _PUSH_VALUE_OPTS

    repo: Optional[str] = None
    positionals: List[str] = []
    force = lease = tags = all_branches = delete = False
    follow_tags: Optional[bool] = None
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
        if a == "--tags":
            tags = True
            continue
        if a == "--follow-tags":
            follow_tags = True
            continue
        if a == "--no-follow-tags":
            follow_tags = False
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
    return PushArgs(
        repo, refspecs, force, lease, tags, all_branches, delete, follow_tags
    )


def _push_remote(wd: str, cfg: Sequence[str] = ()) -> str:
    branch = _current_branch(wd, cfg=cfg)
    if branch:
        for key in (
            f"branch.{branch}.pushRemote",
            "remote.pushDefault",
            f"branch.{branch}.remote",
        ):
            value = _git_out(wd, "config", "--get", key, cfg=cfg)
            if value:
                return value
    else:
        value = _git_out(wd, "config", "--get", "remote.pushDefault", cfg=cfg)
        if value:
            return value
    return "origin"


def _is_url_like(token: str) -> bool:
    return (
        "://" in token
        or token.startswith(("/", "./", "../", "~"))
        or bool(_SCP_URL.match(token))
    )


def _push_targets(
    p: PushArgs, wd: str, remote_name: Optional[str], cfg: Sequence[str]
) -> Tuple[Set[str], bool, bool]:
    """(branches written, writes tags, may write any branch) for a push. With no
    refspec on the command line git uses remote.<name>.push, else push.default."""
    from guard_engine import _normalize_ref

    current = _current_branch(wd, cfg=cfg)
    targets: Set[str] = set()
    tags, all_branches = p.tags, p.all_branches
    follow = p.follow_tags
    if follow is None:
        follow = (
            _git_out(wd, "config", "--bool", "--get", "push.followTags", cfg=cfg)
            == "true"
        )
    refspecs = list(p.refspecs)
    if not refspecs and not (p.all_branches or p.tags):
        configured = (
            _git_out(wd, "config", "--get-all", f"remote.{remote_name}.push", cfg=cfg)
            if remote_name
            else None
        )
        if configured:
            refspecs = configured.split()
        else:
            mode = _git_out(wd, "config", "--get", "push.default", cfg=cfg) or "simple"
            if mode == "matching":
                all_branches = True
            elif mode in ("upstream", "tracking") and current:
                merge = _git_out(
                    wd, "config", "--get", f"branch.{current}.merge", cfg=cfg
                )
                refspecs = [f"HEAD:{merge}" if merge else "HEAD"]
            elif mode != "nothing":
                refspecs = ["HEAD"]
    sources: List[str] = []
    for ref in refspecs:
        if "*" in ref:
            all_branches = True  # a wildcard refspec can write any branch
            continue
        src_dst = ref.lstrip("+")
        if src_dst == ":":
            all_branches = True  # the matching refspec updates every matching branch
            continue
        src = src_dst.split(":", 1)[0] if ":" in src_dst else src_dst
        dst = src_dst.split(":")[-1] if ":" in src_dst else src_dst
        if src:
            sources.append("HEAD" if src == "@" else src)
        if dst == "":
            dst = src  # `src:` pushes to the same name
        if dst in ("HEAD", "@"):
            if current:
                targets.add(current)
            else:
                all_branches = True  # detached HEAD: the target is unknowable here
            continue
        if dst.startswith("refs/tags/") or (
            not dst.startswith("refs/")
            and _git_out(
                wd, "show-ref", "--verify", "--quiet", f"refs/tags/{dst}", cfg=cfg
            )
            is not None
        ):
            tags = True
            continue
        targets.add(_normalize_ref(dst))
    if follow and not tags:
        tags = _follow_tags_can_write(wd, cfg, sources, all_branches)
    return targets, tags, all_branches


def _follow_tags_can_write(
    wd: str, cfg: Sequence[str], sources: Sequence[str], all_branches: bool
) -> bool:
    """With --follow-tags, git also sends annotated tags reachable from the refs
    being pushed. True when such a tag exists, or when a source cannot be resolved
    (fail closed)."""
    if all_branches or not sources:
        kinds = _git_out(
            wd, "for-each-ref", "--format=%(objecttype)", "refs/tags", cfg=cfg
        )
        return "tag" in (kinds or "").split()
    for src in sources:
        sha = _git_out(
            wd, "rev-parse", "--verify", "--quiet", f"{src}^{{commit}}", cfg=cfg
        )
        if not sha:
            return True
        kinds = _git_out(
            wd,
            "for-each-ref",
            f"--merged={sha}",
            "--format=%(objecttype)",
            "refs/tags",
            cfg=cfg,
        )
        if "tag" in (kinds or "").split():
            return True
    return False


_GH_API_VALUE_OPTS = {
    "-X": "method",
    "--method": "method",
    "-f": "field",
    "--raw-field": "field",
    "-F": "field",
    "--field": "field",
    "--input": "field",
    "-H": "other",
    "--header": "other",
    "-q": "other",
    "--jq": "other",
    "-t": "other",
    "--template": "other",
    "--hostname": "other",
    "-p": "other",
    "--preview": "other",
    "--cache": "other",
}


def _parse_gh_api(args: Sequence[str]) -> Tuple[str, bool, str, str]:
    """(method, has_body_fields, endpoint, all_field_text) for `gh api` arguments.
    Option values are consumed with their option, so a header like
    `Accept: application/vnd.github+json` is never mistaken for the endpoint."""
    method = ""
    writes = False
    positionals: List[str] = []
    raw: List[str] = []
    skip_kind: Optional[str] = None
    for a in args:
        if skip_kind is not None:
            if skip_kind == "method":
                method = a.upper()
            elif skip_kind == "field":
                raw.append(a)
            skip_kind = None
            continue
        if a in _GH_API_VALUE_OPTS:
            kind = _GH_API_VALUE_OPTS[a]
            writes = writes or kind == "field"
            skip_kind = kind
            continue
        if a.startswith("--") and "=" in a:
            name, value = a.split("=", 1)
            kind = _GH_API_VALUE_OPTS.get(name)
            if kind == "method":
                method = value.upper()
            elif kind == "field":
                writes = True
                raw.append(value)
            continue
        if len(a) > 2 and a[:2] in _GH_API_VALUE_OPTS and not a.startswith("--"):
            kind = _GH_API_VALUE_OPTS[a[:2]]
            if kind == "method":
                method = a[2:].upper()
            elif kind == "field":
                writes = True
                raw.append(a[2:])
            continue
        if a.startswith("-"):
            continue  # a boolean flag (--paginate, -i, --silent, ...)
        positionals.append(a)
    return method, writes, (positionals[0] if positionals else ""), " ".join(raw)


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


def _stop(stop_id: str, what: str) -> str:
    """The one-line reason shown with Claude Code's confirmation."""
    return f"goodfellow ({stop_id}): {what} Confirm to go ahead."


def check_push(
    tokens: List[str],
    start: int,
    cwd: str,
    protected: Sequence[str],
    disabled: Sequence[str],
) -> Optional[str]:
    g = _git_globals(tokens, start, cwd)
    wd, cfg = g.workdir, g.cfg
    p = parse_push(tokens[start + 1 :])

    if p.force and "stop-force-push" not in disabled:
        return _stop(
            "stop-force-push",
            "a force-push rewrites the remote branch and can drop commits.",
        )
    if p.repo is None:
        remote_name: Optional[str] = _push_remote(wd, cfg=cfg)
    elif _is_url_like(p.repo):
        remote_name = None
    else:
        remote_name = p.repo
    targets, tags, all_branches = _push_targets(p, wd, remote_name, cfg)
    if tags and "stop-release" not in disabled:
        return _stop(
            "stop-release", "this pushes a tag, which often publishes a release."
        )
    if "stop-default-branch" in disabled:
        return None
    defaults = set(protected)
    if remote_name:
        head = _git_out(
            wd,
            "symbolic-ref",
            "--quiet",
            "--short",
            f"refs/remotes/{remote_name}/HEAD",
            cfg=cfg,
        )
        if head and "/" in head:
            defaults.add(head.split("/", 1)[1])
    hit = sorted(targets & defaults)
    if all_branches or hit:
        branch = hit[0] if hit else "every branch"
        return _stop("stop-default-branch", f"this pushes to {branch} directly.")
    return None


def check_gh(tokens: List[str], disabled: Sequence[str]) -> Optional[str]:
    from guard_engine import _strip_wrappers

    if "stop-release" in disabled:
        return None
    args = _strip_wrappers(tokens)[1:]
    k = 0
    while k < len(args) and args[k].startswith("-"):  # gh -R x/y release ...
        k += 2 if args[k] in ("-R", "--repo") else 1
    args = args[k:]
    if not args:
        return None
    group, sub = args[0], (args[1] if len(args) > 1 else "")
    if group == "release" and sub in _RELEASE_WRITES:
        return _stop(
            "stop-release", f"`gh release {sub}` publishes or changes a release."
        )
    if group == "api":
        method, writes, endpoint, _raw = _parse_gh_api(args[1:])
        endpoint = endpoint.split("?", 1)[0].split("#", 1)[0]
        method = method or ("POST" if writes else "GET")
        if method != "GET" and re.search(r"(^|/)releases(/|$)", endpoint):
            return _stop("stop-release", "this writes to a releases API endpoint.")
    return None


def evaluate(
    segments: Sequence[List[str]],
    cwd: str,
    config: dict,
    project_dir: str,
    disabled: Sequence[str] = (),
) -> Optional[str]:
    """The ask reason for the first segment on the stop list, or None."""
    from guard_engine import (
        DEFAULT_PROTECTED_BRANCHES,
        _basename_is,
        _git_arg_start,
        _strip_wrappers,
    )

    protected = config.get("protected_branches", list(DEFAULT_PROTECTED_BRANCHES))
    publishes = _command_list(config, "publish_commands", DEFAULT_PUBLISH_COMMANDS)
    for tokens in segments:
        start = _git_arg_start(tokens)
        if start is not None and start < len(tokens) and tokens[start] == "push":
            reason = check_push(tokens, start, cwd, protected, disabled)
            if reason:
                return reason
            continue
        stripped = _strip_wrappers(tokens)
        if not stripped:
            continue
        if _basename_is(stripped[0], {"gh", "gh.exe"}):
            reason = check_gh(tokens, disabled)
            if reason:
                return reason
            continue
        if "stop-publish" not in disabled and "--dry-run" not in stripped:
            for pat in publishes:
                if matches_command(stripped, pat):
                    return _stop("stop-publish", f"`{pat}` publishes a package.")
    return None
