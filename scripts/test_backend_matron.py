"""Contract tests for the optional Matron loop backend (backend_matron.py).

The journal is a fake that answers with the response shapes of the journal's
documented item and milestone routes (see FIXTURES below). A live test against a
real journal is opt-in: set GOODFELLOW_MATRON_LIVE_TEST=1 plus the usual
GOODFELLOW_MATRON_* variables.
"""

import json
import os
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import pytest

import loop_backend
import loop_store
import backend_matron as mb
from guard_engine import decide

HERE = Path(__file__).resolve().parent
CONVO = "work-convo"
MISSION = "#7"
TOKEN = "tok-SECRET-0123456789abcdef"

# Recorded response shapes (documented journal routes), trimmed to the fields the
# backend reads. Timestamps are epoch milliseconds.
# Verbatim excerpt of the journal's own GET /help text for the routes this backend
# uses (the producer's real output; the capability probe parses it).
HELP_EXCERPT = """# Matron journal HTTP API

## Items (task & decision tracker)

- `GET /items?convo=&kind=&state=&awaiting=&label=&sort=rank|updated&since=&limit=&cursor=`
  \u2014 one ranked list per user; `{items, next_cursor}`, limit <= 500.
- `GET /items/:id` \u2014 `{item, comments}` (comments oldest first).
- `POST /items` `{kind, title, body?, labels?, links?, attachments?,
  actions?, awaiting?, convo_id, supersedes?, on_behalf_of?:'user', and at most one of
  position:'top'|'bottom' / after / before}` -> 201 `{item}`. Send
  `on_behalf_of:'user'` when the USER asked for the item, so it reads as
  theirs. Optional `Idempotency-Key` header (replay -> 200, no second marker).
- `PATCH /items/:id` `{title?, body?, labels?, links?, awaiting?, actions?,
  mission?: id|"#num"|null}` -> 200 `{item}`; `attachments` is 400
  (create-only in v1), moving `awaiting` on a closed item is 409.
- `POST /items/:id/close` `{resolution:'done'|'answered'|'decided'|'reversed'|'cancelled', comment?}`
  -> 200 `{item, comment}`; 409 if already closed. Closing clears `awaiting`.

## Missions & milestones

`POST /missions` and
`POST /milestones` take an optional `Idempotency-Key` (replay -> 200, no
second marker); join and close are naturally repeatable.
"""
HELP_BYTES = HELP_EXCERPT.encode()

FIXTURES = {
    "help_ok": (200, {"x-journal-api-version": "1"}, HELP_BYTES),
    "help_body_version": (200, {}, HELP_BYTES + b"\nJournal API version: 1\n"),
    "help_future": (200, {"x-journal-api-version": "2"}, HELP_BYTES),
    "help_body_future": (200, {}, HELP_BYTES + b"\nJournal API version: 2\n"),
    "help_silent": (200, {}, HELP_BYTES),  # announces no version: fine when the probe passes
    "close_conflict": (409, {}, b'{"error":"already_closed"}'),
    "not_found": (404, {}, b'{"error":"not_found"}'),
}


class FakeJournal:
    """In-memory journal. Records every request as (method, path, body, headers)."""

    def __init__(self, help_response="help_ok"):
        self.help = FIXTURES[help_response]
        self.items = []  # newest last
        self.requests = []
        self.idem = {}
        self.milestones = []
        self.milestone_status = 201
        self.fail_after_commit = False  # create commits, then the response is lost
        self.overwrite_patch = False  # a concurrent writer reverts every PATCH
        self.page_size = 500
        self._clock = 1_760_000_000_000  # 2025-10-09 UTC

    def transport(self, method, url, headers, body, timeout):
        parts = urlsplit(url)
        path = parts.path + (("?" + parts.query) if parts.query else "")
        doc = json.loads(body) if body else None
        self.requests.append((method, path, doc, dict(headers)))
        status, rheaders, payload = self._route(method, parts, doc, headers)
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode()
        return status, rheaders, payload

    def _item(self, ref):
        ref = unquote(ref)
        for i in self.items:
            if ref in (i["id"], "#%d" % i["num"]):
                return i
        return None

    def _route(self, method, parts, doc, headers):
        p = parts.path
        if (method, p) == ("GET", "/help"):
            return self.help
        if (method, p) == ("GET", "/items"):
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            rows = [
                i for i in self.items
                if ("label" not in q or q["label"] in i["labels"])
                and ("convo" not in q or q["convo"] == i["origin_convo_id"])
                and ("state" not in q or q["state"] == i["state"])
            ]
            start = int(q.get("cursor", 0))
            size = min(int(q.get("limit", self.page_size)), self.page_size)
            page = rows[start:start + size]
            nxt = str(start + size) if start + size < len(rows) else None
            return 200, {}, {"items": page, "next_cursor": nxt}
        if (method, p) == ("POST", "/items"):
            key = headers.get("Idempotency-Key")
            if key in self.idem:
                return 200, {}, {"item": self.idem[key]}
            self._clock += 1000
            item = {
                "id": "it_%03d" % (len(self.items) + 1), "num": len(self.items) + 1,
                "kind": doc["kind"], "state": "open", "awaiting": "agent",
                "title": doc["title"], "body": doc.get("body", ""), "labels": doc["labels"],
                "origin_convo_id": doc["convo_id"], "created_at": self._clock,
            }
            self.items.append(item)
            self.idem[key] = item
            if self.fail_after_commit:
                raise mb.MatronUnreachable("journal unreachable: lost")
            return 201, {}, {"item": item}
        if parts.path.startswith("/items/"):
            ref, _, sub = parts.path[len("/items/"):].partition("/")
            item = self._item(ref)
            if item is None:
                return FIXTURES["not_found"]
            if method == "GET" and not sub:
                return 200, {}, {"item": item, "comments": []}
            if method == "PATCH" and not sub:
                if self.overwrite_patch:
                    return 200, {}, {"item": item}
                item.update({k: v for k, v in doc.items() if k in ("title", "body", "labels")})
                return 200, {}, {"item": item}
            if method == "POST" and sub == "close":
                if item["state"] == "closed":
                    return FIXTURES["close_conflict"]
                item["state"], item["resolution"] = "closed", doc["resolution"]
                return 200, {}, {"item": item}
        if (method, p) == ("POST", "/milestones"):
            if self.milestone_status >= 400:
                return self.milestone_status, {}, b'{"error":"blocked","blocked_by":"not_linked"}'
            key = headers.get("Idempotency-Key")
            if key in self.idem:
                return 200, {}, {"milestone": {"id": "ms_1"}}
            self.idem[key] = True
            self.milestones.append(doc)
            return 201, {}, {"milestone": {"id": "ms_1"}}
        raise AssertionError(f"fake journal: unexpected {method} {parts.path}")

    def calls(self, method=None):
        return [(m, p, d) for m, p, d, _ in self.requests if method in (None, m)]


def make_config(tmp_path, mission=None, token=TOKEN, mode=0o600):
    tf = tmp_path / "tok"
    tf.write_text(token + "\n")
    tf.chmod(mode)
    env = {
        mb.ENV_URL: "https://journal.example.test",
        mb.ENV_TOKEN_FILE: str(tf),
        mb.ENV_CONVO: CONVO,
    }
    if mission:
        env[mb.ENV_MISSION] = mission
    return env


@pytest.fixture
def journal():
    return FakeJournal()


@pytest.fixture
def backend(tmp_path, journal):
    return mb.MatronBackend(mb.parse_config(make_config(tmp_path)), transport=journal.transport)


@pytest.fixture
def backend_m(tmp_path, journal):
    cfg = mb.parse_config(make_config(tmp_path, mission=MISSION))
    return mb.MatronBackend(cfg, transport=journal.transport)


# ------------------------------------------------------------- configuration
def test_unconfigured_is_the_local_backend(tmp_path):
    b = loop_backend.get_backend(str(tmp_path), env={"PATH": "/usr/bin"})
    assert isinstance(b, loop_backend.LocalBackend)
    assert b.add("x") == 1


def test_configured_returns_the_remote_backend(tmp_path):
    b = loop_backend.get_backend(str(tmp_path), env=make_config(tmp_path))
    assert isinstance(b, mb.MatronBackend)
    assert isinstance(b, loop_backend.LoopBackend)
    assert b.protocol_version == loop_backend.BACKEND_PROTOCOL_VERSION


def test_only_one_remote_module_is_discovered_and_it_is_not_the_dispatcher():
    assert loop_backend._remote_modules() == ["backend_matron"]


def test_two_configured_remote_backends_are_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(loop_backend, "_remote_modules", lambda: ["backend_matron", "backend_matron"])
    with pytest.raises(loop_backend.BackendConfigError, match="more than one"):
        loop_backend.get_backend(str(tmp_path), env=make_config(tmp_path))


@pytest.mark.parametrize("drop", [mb.ENV_URL, mb.ENV_TOKEN_FILE, mb.ENV_CONVO])
def test_partial_config_is_an_error_not_a_fallback(tmp_path, drop):
    env = make_config(tmp_path)
    del env[drop]
    with pytest.raises(loop_backend.BackendConfigError, match=drop):
        loop_backend.get_backend(str(tmp_path), env=env)


def test_empty_value_counts_as_missing(tmp_path):
    env = make_config(tmp_path)
    env[mb.ENV_CONVO] = "  "
    with pytest.raises(loop_backend.BackendConfigError):
        mb.parse_config(env)


def test_a_lone_prefixed_variable_is_an_error(tmp_path):
    with pytest.raises(loop_backend.BackendConfigError):
        loop_backend.get_backend(str(tmp_path), env={mb.ENV_URL: "https://j.example.test"})


def test_unknown_prefixed_variable_is_an_error(tmp_path):
    env = make_config(tmp_path)
    env[mb.ENV_PREFIX + "TOKEN_FIL"] = "typo"
    with pytest.raises(loop_backend.BackendConfigError, match="unknown"):
        mb.parse_config(env)


def test_token_value_in_env_is_refused_and_not_echoed(tmp_path):
    env = make_config(tmp_path)
    env[mb.ENV_PREFIX + "TOKEN"] = TOKEN
    with pytest.raises(loop_backend.BackendConfigError) as exc:
        mb.parse_config(env)
    assert TOKEN not in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://journal.example.test",  # plain http to a remote host
        "ftp://journal.example.test",
        "https://user:pw@journal.example.test",
        "https://journal.example.test/?x=1",
        "https://journal.example.test/#f",
        "https:///nohost",
        "journal.example.test",
        "https://journal.example.test:notaport",
        "https://journal.example.test/a/../b",
    ],
)
def test_bad_urls_are_refused(tmp_path, url):
    env = make_config(tmp_path)
    env[mb.ENV_URL] = url
    with pytest.raises(loop_backend.BackendConfigError):
        mb.parse_config(env)


@pytest.mark.parametrize("url", ["http://127.0.0.1:9810", "http://localhost:9810", "http://[::1]:9810", "https://j.example.test/base/"])
def test_good_urls_are_accepted(tmp_path, url):
    env = make_config(tmp_path)
    env[mb.ENV_URL] = url
    assert not mb.parse_config(env).base_url.endswith("/")


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o606, 0o777])
def test_token_file_open_to_group_or_other_is_refused(tmp_path, mode):
    with pytest.raises(loop_backend.BackendConfigError, match="other users or groups"):
        mb.parse_config(make_config(tmp_path, mode=mode))


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_private_token_file_is_accepted(tmp_path, mode):
    assert mb.parse_config(make_config(tmp_path, mode=mode)).convo == CONVO


@pytest.mark.parametrize("content", ["", "   \n", "two words", "line1\nline2", "café-token"])
def test_malformed_token_files_are_refused(tmp_path, content):
    with pytest.raises(loop_backend.BackendConfigError):
        mb.parse_config(make_config(tmp_path, token=content))


def test_missing_and_relative_token_paths_are_refused(tmp_path):
    env = make_config(tmp_path)
    env[mb.ENV_TOKEN_FILE] = str(tmp_path / "nope")
    with pytest.raises(loop_backend.BackendConfigError):
        mb.parse_config(env)
    env[mb.ENV_TOKEN_FILE] = "relative/tok"
    with pytest.raises(loop_backend.BackendConfigError, match="absolute"):
        mb.parse_config(env)


def test_directory_as_token_file_is_refused(tmp_path):
    env = make_config(tmp_path)
    env[mb.ENV_TOKEN_FILE] = str(tmp_path)
    with pytest.raises(loop_backend.BackendConfigError, match="regular file"):
        mb.parse_config(env)


def test_token_never_appears_in_repr_or_str(tmp_path):
    cfg = mb.parse_config(make_config(tmp_path, mission=MISSION))
    b = mb.MatronBackend(cfg)
    for obj in (cfg, b, cfg._token, [cfg]):
        assert TOKEN not in repr(obj) and TOKEN not in str(obj)


def test_bad_mission_and_convo_are_refused(tmp_path):
    for key, bad in ((mb.ENV_MISSION, "7; drop"), (mb.ENV_CONVO, "a b")):
        env = make_config(tmp_path, mission=MISSION)
        env[key] = bad
        with pytest.raises(loop_backend.BackendConfigError):
            mb.parse_config(env)


def test_cli_with_bad_config_exits_2_and_writes_no_local_store(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(mb.ENV_PREFIX)}
    env[mb.ENV_URL] = "https://journal.example.test"  # token file and convo missing
    r = subprocess.run(
        [sys.executable, str(HERE / "loop_store.py"), "--root", str(tmp_path), "add", "x"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 2
    assert "loop backend error" in r.stderr
    assert not (tmp_path / ".goodfellow").exists()


def test_cli_get_prints_the_loop_with_its_uuid(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(mb.ENV_PREFIX)}
    cli = [sys.executable, str(HERE / "loop_store.py"), "--root", str(tmp_path)]
    subprocess.run(cli + ["add", "x", "--lens", "L"], check=True, capture_output=True, env=env)
    r = subprocess.run(cli + ["get", "1"], capture_output=True, text=True, env=env)
    doc = json.loads(r.stdout)
    assert r.returncode == 0 and doc["lens"] == "L" and len(doc["uuid"]) == 32
    assert subprocess.run(cli + ["get", "9"], capture_output=True, env=env).returncode == 1


def test_cli_without_config_still_uses_the_local_file(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(mb.ENV_PREFIX)}
    r = subprocess.run(
        [sys.executable, str(HERE / "loop_store.py"), "--root", str(tmp_path), "add", "x"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0
    assert (tmp_path / ".goodfellow" / "loops.json").exists()


# ----------------------------------------------------------- API version gate
def test_supported_version_header_is_accepted(backend, journal):
    assert backend.list() == []
    assert journal.calls()[0][:2] == ("GET", "/help")


def test_version_line_in_body_is_accepted(tmp_path):
    j = FakeJournal("help_body_version")
    b = mb.MatronBackend(mb.parse_config(make_config(tmp_path)), transport=j.transport)
    assert b.list() == []


@pytest.mark.parametrize("fixture", ["help_future", "help_body_future"])
def test_an_announced_unsupported_version_is_refused_before_any_item_call(tmp_path, fixture):
    j = FakeJournal(fixture)
    b = mb.MatronBackend(mb.parse_config(make_config(tmp_path)), transport=j.transport)
    for op in (lambda: b.list(), lambda: b.add("t"), lambda: b.close(1), lambda: b.get(1),
               lambda: b.update_triage(1, triage_count=1)):
        with pytest.raises(mb.MatronApiVersionError, match="not supported"):
            op()
    assert {p for _, p, _ in j.calls()} == {"/help"}  # nothing else ever left


@pytest.mark.parametrize("announced", ["0", "1.1", "10", "v1", " ", "x" * 50])
def test_any_announced_version_but_a_supported_one_is_refused(tmp_path, announced):
    j = FakeJournal()
    j.help = (200, {"x-journal-api-version": announced}, HELP_BYTES)
    b = mb.MatronBackend(mb.parse_config(make_config(tmp_path)), transport=j.transport)
    with pytest.raises(mb.MatronApiVersionError):
        b.list()


def test_a_journal_that_announces_no_version_works_when_the_probe_passes(tmp_path):
    j = FakeJournal("help_silent")
    b = mb.MatronBackend(mb.parse_config(make_config(tmp_path)), transport=j.transport)
    num = b.add("t")
    assert b.get(num)["title"] == "t"


# ------------------------------------------------------------ capability probe
def _backend(tmp_path, journal):
    return mb.MatronBackend(mb.parse_config(make_config(tmp_path)), transport=journal.transport)


def test_probe_is_read_only_and_runs_once(tmp_path, journal):
    b = _backend(tmp_path, journal)
    b.list()
    b.list()
    probe = journal.calls()[:3]
    assert [(m, p.split("?")[0]) for m, p, _ in probe] == [("GET", "/help"), ("GET", "/items"), ("GET", "/items")]
    assert all(m == "GET" for m, _, _ in journal.calls()[:3])
    assert [p for _, p, _ in journal.calls()].count("/help") == 1


def test_probe_passes_with_and_without_existing_items(tmp_path, journal):
    b = _backend(tmp_path, journal)
    b.add("seed")
    fresh = _backend(tmp_path, journal)  # a second process: probes against a non-empty journal
    assert len(fresh.list()) == 1


def _without(marker):
    lines = [l for l in HELP_EXCERPT.splitlines(keepends=True)]
    return "".join(l for l in lines if marker not in l).encode()


@pytest.mark.parametrize(
    "capability, help_bytes",
    [
        ("list items with label and convo filters", _without("GET /items?convo=")),
        ("paged item lists", HELP_EXCERPT.replace("next_cursor", "cursor_next").encode()),
        ("read one item by #num", _without("GET /items/:id` \u2014")),
        ("create item with Idempotency-Key", HELP_EXCERPT.replace("Optional `Idempotency-Key` header", "Optional header").encode()),
        ("patch item body", HELP_EXCERPT.replace("{title?, body?, labels?", "{title?, labels?").encode()),
        ("close item with a resolution", HELP_EXCERPT.replace("'done'|", "").encode()),
        ("milestones with Idempotency-Key", HELP_EXCERPT.replace("`POST /milestones` take an optional `Idempotency-Key`", "`POST /milestones` take nothing").encode()),
        ("list items with label and convo filters", b""),
    ],
)
def test_each_undocumented_capability_is_refused_by_name(tmp_path, capability, help_bytes):
    j = FakeJournal()
    j.help = (200, {}, help_bytes)
    b = _backend(tmp_path, j)
    with pytest.raises(mb.MatronCapabilityError, match="required capability") as exc:
        b.add("t")
    assert capability in str(exc.value) and exc.value.capability.startswith(capability)
    assert [m for m, _, _ in j.calls()] == ["GET"] and {p for _, p, _ in j.calls()} == {"/help"}


GOOD = {"id": "it_1", "num": 1, "kind": "task", "state": "open", "awaiting": "agent", "title": "t",
        "body": "", "labels": ["goodfellow"], "origin_convo_id": CONVO, "created_at": 1_760_000_000_000}


def _probe_with(tmp_path, scoped, anyrows):
    j = FakeJournal()
    orig = j._route
    answers = iter([scoped, anyrows])

    def route(method, parts, doc, headers):
        if method == "GET" and parts.path == "/items":
            body = next(answers)
            return 200, {}, json.dumps(body).encode()
        return orig(method, parts, doc, headers)

    j._route = route
    return j, _backend(tmp_path, j)


ENV_OK = {"items": [], "next_cursor": None}


@pytest.mark.parametrize(
    "capability, scoped, anyrows",
    [
        ("items list envelope", [], ENV_OK),
        ("items list envelope", {"items": []}, ENV_OK),  # no next_cursor key
        ("items list envelope", {"items": "x", "next_cursor": None}, ENV_OK),
        ("items list envelope", ENV_OK, {"nope": 1}),
        ("limit parameter", {"items": [GOOD, GOOD], "next_cursor": None}, ENV_OK),
        ("label filter", {"items": [{**GOOD, "labels": ["p1"]}], "next_cursor": None}, ENV_OK),
        ("convo filter", {"items": [{**GOOD, "origin_convo_id": "other"}], "next_cursor": None}, ENV_OK),
        ("item field id", ENV_OK, {"items": [{**GOOD, "id": 5}], "next_cursor": None}),
        ("item field num", ENV_OK, {"items": [{**GOOD, "num": "1"}], "next_cursor": None}),
        ("item field num", ENV_OK, {"items": [{**GOOD, "num": 0}], "next_cursor": None}),
        ("item field state", ENV_OK, {"items": [{**GOOD, "state": "done"}], "next_cursor": None}),
        ("item field labels", ENV_OK, {"items": [{**GOOD, "labels": "p1"}], "next_cursor": None}),
        ("item field labels", ENV_OK, {"items": [{**GOOD, "labels": [1]}], "next_cursor": None}),
        ("item field awaiting", ENV_OK, {"items": [{k: v for k, v in GOOD.items() if k != "awaiting"}], "next_cursor": None}),
        ("item field created_at", ENV_OK, {"items": [{**GOOD, "created_at": 1_760_000_000}], "next_cursor": None}),  # seconds
        ("item field created_at", ENV_OK, {"items": [{**GOOD, "created_at": "2025-10-09T00:00:00Z"}], "next_cursor": None}),
        ("item field title", ENV_OK, {"items": [{**GOOD, "title": None}], "next_cursor": None}),
        ("item field body", ENV_OK, {"items": [{**GOOD, "body": 5}], "next_cursor": None}),
        ("item field origin_convo_id", ENV_OK, {"items": [{k: v for k, v in GOOD.items() if k != "origin_convo_id"}], "next_cursor": None}),
        ("item field created_at", {"items": [{**GOOD, "created_at": 5}], "next_cursor": None}, ENV_OK),
    ],
)
def test_each_probe_shape_failure_is_refused_by_name_and_nothing_is_written(tmp_path, capability, scoped, anyrows):
    j, b = _probe_with(tmp_path, scoped, anyrows)
    with pytest.raises(mb.MatronCapabilityError) as exc:
        b.add("t")
    assert capability in str(exc.value)
    assert all(m == "GET" for m, _, _ in j.calls())  # fail closed before any write


@pytest.mark.parametrize("bad", [None, {}, {"labels": None}, 5, "x", [], {"id": "it_1", "num": 1, "labels": None}])
def test_malformed_scoped_items_raise_a_capability_error_not_a_traceback(tmp_path, bad):
    j, b = _probe_with(tmp_path, {"items": [bad], "next_cursor": None}, ENV_OK)
    with pytest.raises(mb.MatronCapabilityError):
        b.add("t")


@pytest.mark.parametrize("mutate", [lambda u: u[:-1] + ("0" if u[-1] != "0" else "1"), lambda u: "0" * 32, lambda u: u + "0"])
def test_a_reconciliation_hit_with_another_uuid_is_never_accepted(backend, journal, mutate):
    backend.list()
    orig = journal._route

    done = []

    def commit_then_5xx(method, parts, doc, headers):
        if method == "GET" and parts.path == "/items" and "gf-uuid" in parts.query:
            # a lookup that ignores the requested uuid label and returns every goodfellow item
            q = {k: v[0] for k, v in parse_qs(parts.query).items() if k != "label"}
            q["label"] = "goodfellow"
            parts = parts._replace(query=urlencode(q))
        res = orig(method, parts, doc, headers)
        if method == "POST" and parts.path == "/items":
            if done:  # the retried (replayed) create: leave the lookalike label as is
                return 502, {}, b""
            done.append(1)
            labels = journal.items[-1]["labels"]
            journal.items[-1]["labels"] = [
                "gf-uuid:" + mutate(l[len("gf-uuid:"):]) if l.startswith("gf-uuid:") else l for l in labels
            ]
            return 502, {}, b""
        return res

    journal._route = commit_then_5xx
    with pytest.raises(mb.MatronHTTPError):
        backend.add("t")  # the lookalike is somebody else's loop: never adopted


def test_a_good_probe_with_a_real_looking_item_passes(tmp_path):
    j, b = _probe_with(tmp_path, {"items": [GOOD], "next_cursor": None}, {"items": [{**GOOD, "state": "closed"}], "next_cursor": "c"})
    assert b.add("t") >= 1


def test_version_is_checked_once_per_backend(backend, journal):
    backend.list()
    backend.list()
    assert [p for _, p, _ in journal.calls()].count("/help") == 1


# ------------------------------------------------------------------- mapping
def test_add_maps_goodfellow_fields_to_labels_and_work_meta(backend, journal):
    num = backend.add(
        "Fix the thing", priority="p2", source="ship-review-r1", description="Details here.",
        tags=["perf", "area/db"], owner="me", next_action="look at it", lens="correctness",
    )
    assert num == 1
    method, path, doc = [c for c in journal.calls("POST") if c[1] == "/items"][0]
    assert doc["kind"] == "task" and doc["convo_id"] == CONVO and doc["title"] == "Fix the thing"
    labels = doc["labels"]
    assert labels[:4] == ["goodfellow", "p2", "gf:perf", "gf:area/db"]
    assert labels[4].startswith("gf-uuid:") and len(labels) == 5
    assert all(len(l) <= 40 for l in labels)
    prose, meta = mb.decode_body(doc["body"])
    assert prose == "Details here."
    assert meta == {"v": 1, "owner": "me", "source": "ship-review-r1", "next_action": "look at it",
                    "lens": "correctness", "triage_count": 0, "last_triaged": None}
    assert doc["body"].endswith("\n```") and "\n\n```work-meta\n" in doc["body"]
    key = [h for m, p, d, h in journal.requests if m == "POST" and p == "/items"][0]["Idempotency-Key"]
    assert key == "gf-add-" + labels[4][len("gf-uuid:"):]


def test_every_request_carries_the_bearer_token_and_no_other_secret(backend, journal):
    backend.add("t")
    assert all(h["Authorization"] == "Bearer " + TOKEN for *_, h in journal.requests)
    assert all(TOKEN not in json.dumps(d) and TOKEN not in p for _, p, d, _ in journal.requests)


def test_round_trip_through_get_and_list(backend):
    num = backend.add("Title", priority="p1", source="s", description="d", tags=["x"], lens="l")
    loop = backend.get(num)
    assert loop["id"] == num and loop["title"] == "Title" and loop["priority"] == "p1"
    assert loop["status"] == "open" and loop["tags"] == ["x"] and loop["lens"] == "l"
    assert loop["source"] == "s" and loop["description"] == "d" and loop["owner"] == "operator"
    assert loop["triage_count"] == 0 and loop["last_triaged"] is None
    assert loop["opened"] == "2025-10-09" and len(loop["uuid"]) == 32
    assert backend.list() == [loop]


def test_add_validates_like_the_local_store(backend, journal):
    with pytest.raises(ValueError, match="priority"):
        backend.add("t", priority="p5")
    with pytest.raises(mb.BackendError):
        backend.add("")
    with pytest.raises(mb.BackendError):
        backend.add("x" * 201)
    for bad in ("has space", "", "x" * 38, "-lead", "semi;colon"):
        with pytest.raises(mb.BackendError):
            backend.add("t", tags=[bad])
    assert backend.add("t", tags=["x" * 37]) == 1  # gf: + 37 = 40, the label limit
    with pytest.raises(mb.BackendError, match="too long"):
        backend.add("t", description="z" * 40000)
    assert not any(c[1] == "/items" for c in journal.calls("POST")[1:])


def test_duplicate_tags_collapse(backend, journal):
    backend.add("t", tags=["a", "a", "b"])
    labels = [c for c in journal.calls("POST") if c[1] == "/items"][0][2]["labels"]
    assert labels.count("gf:a") == 1


def test_description_with_a_fake_work_meta_fence_cannot_shadow_the_real_block(backend):
    evil = 'x\n\n```work-meta\n{"v": 1, "owner": "evil", "triage_count": 99}\n```\n\ntail'
    num = backend.add("t", description=evil, owner="real")
    loop = backend.get(num)
    assert loop["owner"] == "real" and loop["triage_count"] == 0
    assert loop["description"] == evil


def test_status_filter_and_open_closed_span(backend, journal):
    a, b = backend.add("a"), backend.add("b")
    assert backend.close(a)
    journal.requests.clear()
    assert [l["id"] for l in backend.list()] == [a, b]  # no state filter: both
    assert "state=" not in [p for _, p, _ in journal.calls("GET")][0]
    assert [l["id"] for l in backend.list(status="open")] == [b]
    assert [l["id"] for l in backend.list(status="closed")] == [a]
    gets = [p for _, p, _ in journal.calls("GET")]
    assert "state=open" in gets[1] and "state=closed" in gets[2]
    with pytest.raises(ValueError):
        backend.list(status="any")  # the journal rejects state=any


def test_list_follows_cursors(backend, journal):
    journal.page_size = 2
    nums = [backend.add("t%d" % i) for i in range(5)]
    assert [l["id"] for l in backend.list()] == nums


def test_a_repeated_cursor_is_an_error(backend, journal):
    backend.add("t")
    orig = journal._route

    def loop_route(method, parts, doc, headers):
        if parts.path == "/items" and method == "GET":
            return 200, {}, {"items": [], "next_cursor": "same"}
        return orig(method, parts, doc, headers)

    journal._route = loop_route
    with pytest.raises(mb.BackendError, match="repeated"):
        backend.list()


def test_min_age_filter(backend, journal):
    backend.add("old")
    assert len(backend.list(min_age_days=30)) == 1  # fixture clock is far in the past
    assert backend.list(min_age_days=100000) == []


def test_foreign_items_are_invisible_and_untouchable(backend, journal):
    ours = backend.add("ours")
    journal.items.append({"id": "it_x", "num": 50, "state": "open", "title": "other tool",
                          "body": "", "labels": ["p1"], "origin_convo_id": CONVO, "created_at": 1})
    journal.items.append({"id": "it_y", "num": 51, "state": "open", "title": "other convo",
                          "body": "", "labels": ["goodfellow", "p1"], "origin_convo_id": "elsewhere",
                          "created_at": 1})
    assert [l["id"] for l in backend.list()] == [ours]
    for foreign in (50, 51):
        assert backend.get(foreign) is None
        assert backend.close(foreign) is False
        assert backend.update_triage(foreign, triage_count=1) is False
    assert not [c for c in journal.calls() if c[0] in ("PATCH",) or c[1].endswith("/close")]


def test_unknown_item_is_none_and_false(backend):
    assert backend.get(999) is None
    assert backend.close(999) is False


def test_invalid_ids_are_rejected_before_any_request(backend, journal):
    for bad in (0, -1, "1", True, None, 1.5):
        with pytest.raises(ValueError):
            backend.get(bad)
    assert journal.requests == []


def test_malformed_foreign_edit_degrades_reads_but_refuses_writes(backend, journal):
    num = backend.add("t")
    journal.items[0]["body"] = "someone rewrote this by hand"
    loop = backend.get(num)
    assert loop["description"] == "someone rewrote this by hand" and loop["triage_count"] == 0
    with pytest.raises(mb.MatronWriteError, match="refusing to guess"):
        backend.update_triage(num, triage_count=2)
    assert not journal.calls("PATCH")


def test_priority_label_edge_cases(backend, journal):
    num = backend.add("t", priority="p3")
    journal.items[0]["labels"] = ["goodfellow", "p4", "p2"]
    assert backend.get(num)["priority"] == "p2"
    journal.items[0]["labels"] = ["goodfellow"]
    assert backend.get(num)["priority"] == "p3"


# ---------------------------------------------------------------- close
def test_close_resolves_done_and_posts_no_milestone_without_a_mission(backend, journal):
    num = backend.add("t")
    assert backend.close(num) is True
    closes = [c for c in journal.calls("POST") if c[1].endswith("/close")]
    assert closes == [("POST", "/items/%23" + str(num) + "/close", {"resolution": "done"})]
    assert journal.milestones == []
    assert backend.get(num)["status"] == "closed"


def test_close_posts_a_progress_milestone_to_the_named_mission(backend_m, journal):
    num = backend_m.add("Ship the thing")
    assert backend_m.close(num)
    assert journal.milestones == [{
        "convo_id": CONVO, "kind": "progress", "title": "Closed: Ship the thing",
        "body": "Goodfellow closed follow-up #%d." % num, "mission": MISSION,
    }]


def test_close_is_idempotent_and_posts_one_milestone(backend_m, journal):
    num = backend_m.add("t")
    assert backend_m.close(num) and backend_m.close(num)
    assert len(journal.milestones) == 1


def test_close_survives_a_409_race_when_someone_else_closed_it(backend, journal):
    num = backend.add("t")
    orig = journal._route

    def race(method, parts, doc, headers):
        if method == "POST" and parts.path.endswith("/close"):
            journal.items[0]["state"] = "closed"  # closed by someone else a moment ago
            return FIXTURES["close_conflict"]
        return orig(method, parts, doc, headers)

    journal._route = race
    assert backend.close(num) is True


@pytest.mark.parametrize("status", [301, 302, 307, 204, 199])
def test_a_non_2xx_or_redirected_close_is_an_error_not_success(backend_m, journal, status):
    num = backend_m.add("t")
    orig = journal._route

    def redirect(method, parts, doc, headers):
        if method == "POST" and parts.path.endswith("/close"):
            return status, {"location": "http://elsewhere.invalid/"}, b""
        return orig(method, parts, doc, headers)

    journal._route = redirect
    with pytest.raises(mb.BackendError):
        backend_m.close(num)
    assert journal.milestones == []  # no false closure milestone
    assert backend_m.get(num)["status"] == "open"


def test_2xx_close_that_does_not_close_is_an_error(backend, journal):
    num = backend.add("t")
    orig = journal._route

    def liar(method, parts, doc, headers):
        if method == "POST" and parts.path.endswith("/close"):
            return 200, {}, {"item": journal.items[0]}
        return orig(method, parts, doc, headers)

    journal._route = liar
    with pytest.raises(mb.MatronWriteError, match="not closed"):
        backend.close(num)


def test_a_failed_milestone_is_posted_by_retrying_close(backend_m, journal):
    num = backend_m.add("t")
    journal.milestone_status = 409
    with pytest.raises(mb.MilestoneNotPosted, match="run close again"):
        backend_m.close(num)
    assert journal.milestones == []
    journal.milestone_status = 201
    assert backend_m.close(num) is True
    assert len(journal.milestones) == 1
    assert backend_m.close(num) is True  # and exactly once
    assert len(journal.milestones) == 1
    _, meta = mb.decode_body(journal.items[0]["body"])
    assert "milestone_pending" not in meta


def test_an_item_closed_by_hand_gets_no_goodfellow_milestone(backend_m, journal):
    num = backend_m.add("t")
    journal.items[0]["state"] = "closed"
    assert backend_m.close(num) is True
    assert journal.milestones == []


def test_a_milestone_failure_is_loud_but_the_item_stays_closed(backend_m, journal):
    num = backend_m.add("t")
    journal.milestone_status = 409
    with pytest.raises(mb.MilestoneNotPosted, match="is closed, but the milestone"):
        backend_m.close(num)
    assert backend_m.get(num)["status"] == "closed"


def test_no_request_can_ever_close_a_mission(backend_m, journal):
    num = backend_m.add("t")
    backend_m.close(num)
    assert not [p for _, p, _ in journal.calls() if "mission" in p]
    for method, path in (("POST", "/missions/7/close"), ("PATCH", "/missions/7"), ("DELETE", "/items/%231")):
        with pytest.raises(mb.BackendError, match="outside the allowed set"):
            backend_m._request(method, path, {})


# ---------------------------------------------------------- update_triage
def test_update_triage_writes_the_body_only_and_keeps_labels(backend, journal):
    num = backend.add("t", priority="p2", tags=["k"], description="prose", lens="L")
    labels_before = list(journal.items[0]["labels"])
    assert backend.update_triage(num, triage_count=3, last_triaged="2025-10-10")
    (m, p, doc), = journal.calls("PATCH")
    assert set(doc) == {"body"}  # PATCH replaces whole arrays: never send labels
    assert journal.items[0]["labels"] == labels_before
    loop = backend.get(num)
    assert loop["triage_count"] == 3 and loop["last_triaged"] == "2025-10-10"
    assert loop["lens"] == "L" and loop["description"] == "prose"


def test_update_triage_partial_and_noop(backend, journal):
    num = backend.add("t")
    assert backend.update_triage(num, triage_count=1)
    assert backend.get(num)["last_triaged"] is None
    n = len(journal.calls("PATCH"))
    assert backend.update_triage(num, triage_count=1)
    assert len(journal.calls("PATCH")) == n  # nothing to change, nothing sent


def test_update_triage_unconfirmed_write_raises_after_retries(backend, journal):
    num = backend.add("t")
    journal.overwrite_patch = True
    with pytest.raises(mb.MatronWriteError, match="not confirmed"):
        backend.update_triage(num, triage_count=5)
    assert len(journal.calls("PATCH")) == mb.WRITE_RETRIES


def test_an_edit_between_read_and_write_is_not_overwritten(backend, journal):
    num = backend.add("t", description="before")
    orig = journal._route
    state = {"edited": False}

    def edit_then_patch(method, parts, doc, headers):
        if method == "GET" and parts.path.startswith("/items/") and not state["edited"]:
            # the operator edits right after our first read of the item
            res = orig(method, parts, doc, headers)
            state["edited"] = True
            prose, meta = mb.decode_body(journal.items[0]["body"])
            journal.items[0]["body"] = mb.encode_body("edited by a person", meta)
            return res
        return orig(method, parts, doc, headers)

    journal._route = edit_then_patch
    assert backend.update_triage(num, triage_count=4)
    loop = backend.get(num)
    assert loop["description"] == "edited by a person" and loop["triage_count"] == 4


@pytest.mark.parametrize("kwargs", [{"triage_count": -1}, {"triage_count": True}, {"triage_count": "1"},
                                     {"last_triaged": "yesterday"}, {"last_triaged": 5}])
def test_update_triage_validates_inputs(backend, kwargs):
    num = backend.add("t")
    with pytest.raises(ValueError):
        backend.update_triage(num, **kwargs)


# --------------------------------------------------------- failure handling
def test_lost_create_response_is_resolved_by_uuid_lookup(backend, journal):
    journal.fail_after_commit = True
    num = backend.add("t")
    assert num == 1 and len(journal.items) == 1  # exactly one item, found by its label


def test_unreachable_before_commit_raises(tmp_path):
    def down(*a, **k):
        raise mb.MatronUnreachable("journal unreachable: down")

    b = mb.MatronBackend(mb.parse_config(make_config(tmp_path)), transport=down)
    with pytest.raises(mb.MatronUnreachable):
        b.add("t")


def test_auth_failure_is_a_distinct_error_without_the_token(tmp_path):
    def deny(method, url, headers, body, timeout):
        return 401, {}, ("bad token " + headers["Authorization"]).encode()

    b = mb.MatronBackend(mb.parse_config(make_config(tmp_path)), transport=deny)
    with pytest.raises(mb.MatronAuthError) as exc:
        b.list()
    assert TOKEN not in str(exc.value)


@pytest.mark.parametrize("field", ["error", "message", "detail"])
def test_server_text_never_reaches_an_error_message(backend, journal, field):
    num = backend.add("t")
    journal._route = lambda *a: (400, {}, json.dumps({field: "bad bearer " + TOKEN}).encode())
    with pytest.raises(mb.MatronHTTPError) as exc:
        backend.update_triage(num, triage_count=1)
    assert TOKEN not in str(exc.value) and "bearer" not in str(exc.value)


def test_a_short_machine_code_is_kept(backend, journal):
    journal._route = lambda *a: (409, {}, b'{"error":"not_linked"}')
    with pytest.raises(mb.MatronHTTPError, match="not_linked"):
        backend.add("t")


def test_non_json_error_bodies_are_ignored(backend, journal):
    journal._route = lambda *a: (502, {}, ("<html>" + TOKEN).encode())
    with pytest.raises(mb.BackendError) as exc:
        backend.add("t")
    assert TOKEN not in str(exc.value)


def test_5xx_is_retried_once_for_reads(tmp_path, journal):
    calls = {"n": 0}
    real = journal.transport

    def flaky(method, url, headers, body, timeout):
        calls["n"] += 1
        if calls["n"] == 2:  # first /items read
            return 503, {}, b"{}"
        return real(method, url, headers, body, timeout)

    b = mb.MatronBackend(mb.parse_config(make_config(tmp_path)), transport=flaky)
    assert b.list() == []


def test_malformed_json_from_the_journal_is_an_error(tmp_path):
    def junk(method, url, headers, body, timeout):
        if url.endswith("/help"):
            return FIXTURES["help_ok"]
        return 200, {}, b"<html>"

    b = mb.MatronBackend(mb.parse_config(make_config(tmp_path)), transport=junk)
    with pytest.raises(mb.BackendError, match="malformed"):
        b.list()


# --------------------------------------------------- egress / request pinning
def test_allowed_requests_are_pinned():
    assert mb.ALLOWED_REQUESTS == (
        ("GET", r"/help"),
        ("GET", r"/items"),
        ("POST", r"/items"),
        ("GET", r"/items/%23[0-9]{1,12}"),
        ("PATCH", r"/items/%23[0-9]{1,12}"),
        ("POST", r"/items/%23[0-9]{1,12}/close"),
        ("POST", r"/milestones"),
    )


def test_a_full_session_only_touches_pinned_paths(backend_m, journal):
    n = backend_m.add("t", tags=["a"])
    backend_m.list()
    backend_m.get(n)
    backend_m.update_triage(n, triage_count=1)
    backend_m.close(n)
    seen = {(m, p.split("?")[0]) for m, p, _ in journal.calls()}
    for method, path in seen:
        assert any(m == method and rx.match(path) for m, rx in mb._ALLOWED_RES), (method, path)


# ------------------------------------------------ transport (real sockets)
class _Hit(BaseHTTPRequestHandler):
    log = None
    redirect_to = None

    def do_GET(self):
        type(self).log.append(("GET", self.path, self.headers.get("Authorization")))
        if type(self).redirect_to:
            self.send_response(302)
            self.send_header("Location", type(self).redirect_to + self.path)
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("X-Journal-Api-Version", "1")
            self.end_headers()
            self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def _serve(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_redirects_are_never_followed_so_the_token_cannot_leak():
    target_log, origin_log = [], []
    target = type("T", (_Hit,), {"log": target_log, "redirect_to": None})
    origin = type("O", (_Hit,), {"log": origin_log, "redirect_to": None})
    t_srv = _serve(target)
    origin.redirect_to = f"http://127.0.0.1:{t_srv.server_port}"
    o_srv = _serve(origin)
    try:
        status, _, _ = mb.default_transport(
            "GET", f"http://127.0.0.1:{o_srv.server_port}/items",
            {"Authorization": "Bearer " + TOKEN}, None, 5.0,
        )
    finally:
        o_srv.shutdown()
        t_srv.shutdown()
    assert status == 302
    assert origin_log and not target_log  # the second host never saw a request


def test_default_transport_reports_unreachable():
    srv = _serve(type("X", (_Hit,), {"log": []}))
    port = srv.server_port
    srv.shutdown()
    srv.server_close()
    with pytest.raises(mb.MatronUnreachable):
        mb.default_transport("GET", f"http://127.0.0.1:{port}/help", {}, None, 2.0)


# ------------------------------------------------------------ stop list
def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def test_stop_list_is_unchanged_and_fail_closed_under_a_remote_backend(tmp_path, monkeypatch):
    repo = tmp_path / "app"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "f").write_text("x\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "i")
    _git(repo, "remote", "add", "origin", "git@git.example.test:team/app.git")
    _git(repo, "switch", "-q", "-c", "feature")
    for k in ("CLAUDE_HOOK_BYPASS", "GOODFELLOW_STOP_LIST"):
        monkeypatch.delenv(k, raising=False)

    def verdict(cmd):
        return decide({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(repo)}, str(repo))

    cmds = ["git push --force origin feature", "git push origin main", "npm publish", "git status"]
    before = [verdict(c) for c in cmds]
    for k, v in make_config(tmp_path, mission=MISSION).items():
        monkeypatch.setenv(k, v)
    after = [verdict(c) for c in cmds]
    assert after == before
    assert [v[0] if v else None for v in after] == ["ask", "ask", "ask", None]  # asks, never allows or denies


def test_stop_list_and_guard_engine_do_not_know_about_backends():
    for name in ("stop_list.py", "guard_engine.py"):
        text = (HERE / name).read_text()
        assert "loop_backend" not in text and "_backend" not in text.replace("sandbox_backend", "")


# ------------------------------------------------------------ shared contract
@pytest.fixture(params=["local", "remote"])
def any_backend(request, tmp_path, journal):
    if request.param == "local":
        return loop_backend.LocalBackend(str(tmp_path / "proj"))
    return mb.MatronBackend(mb.parse_config(make_config(tmp_path)), transport=journal.transport)


def test_both_backends_honour_the_same_contract(any_backend):
    b = any_backend
    first = b.add("one", priority="p2", source="src", description="d", tags=["t"], lens="L")
    second = b.add("two")
    assert first != second
    loop = b.get(first)
    for key in ("id", "uuid", "title", "status", "priority", "source", "opened", "description",
                "tags", "owner", "next_action", "lens", "triage_count", "last_triaged"):
        assert key in loop, key
    assert (loop["title"], loop["priority"], loop["tags"], loop["status"]) == ("one", "p2", ["t"], "open")
    assert b.update_triage(first, triage_count=2, last_triaged="2025-10-10") is True
    assert b.get(first)["triage_count"] == 2
    assert b.close(first) is True
    assert [l["id"] for l in b.list(status="open")] == [second]
    assert [l["id"] for l in b.list(status="closed")] == [first]
    assert b.get(10_000) is None
    assert b.close(10_000) is False
    assert b.update_triage(10_000, triage_count=1) is False


# -------------------------------------------------------------- live (opt-in)
@pytest.mark.skipif(os.environ.get(mb.ENV_LIVE_TEST) != "1", reason="live journal test is opt-in")
def test_live_round_trip():
    b = loop_backend.get_backend(".")
    assert isinstance(b, mb.MatronBackend)
    num = b.add("goodfellow live backend test", tags=["live-test"])
    try:
        assert b.get(num)["title"] == "goodfellow live backend test"
        assert b.update_triage(num, triage_count=1, last_triaged="2025-01-01")
    finally:
        b.close(num)


# ------------------------------------------------ review round 2 regressions
@pytest.mark.parametrize("scheme", ["HTTP", "Http", "http"])
def test_loopback_http_never_goes_through_a_proxy_whatever_the_scheme_case(tmp_path, monkeypatch, scheme):
    seen = []

    class Spy(urllib.request.ProxyHandler):
        def __init__(self, proxies=None):
            seen.append(proxies)
            super().__init__(proxies)

    monkeypatch.setattr(urllib.request, "ProxyHandler", Spy)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    env = make_config(tmp_path)
    env[mb.ENV_URL] = scheme + "://127.0.0.1:1"
    cfg = mb.parse_config(env)
    assert cfg.base_url.startswith("http://")
    with pytest.raises(mb.MatronUnreachable):
        mb.default_transport("GET", cfg.base_url + "/help", {}, None, 1.0)
    assert seen == [{}]  # proxies explicitly disabled


@pytest.mark.parametrize("echo", [TOKEN, "tok_secret_token"])
def test_a_token_echoed_as_an_error_code_is_not_printed(tmp_path, echo):
    token = echo
    env = make_config(tmp_path, token=token)
    j = FakeJournal()
    b = mb.MatronBackend(mb.parse_config(env), transport=j.transport)
    num = b.add("t")
    j._route = lambda *a: (400, {}, json.dumps({"error": token}).encode())
    with pytest.raises(mb.MatronHTTPError) as exc:
        b.update_triage(num, triage_count=1)
    assert token not in str(exc.value)


def test_a_token_echoed_as_the_api_version_is_not_printed(tmp_path):
    token = "review_dummy_token"
    env = make_config(tmp_path, token=token)
    b = mb.MatronBackend(
        mb.parse_config(env), transport=lambda *a: (200, {"x-journal-api-version": token}, b"")
    )
    with pytest.raises(mb.MatronApiVersionError) as exc:
        b.list()
    assert token not in str(exc.value)


def test_close_refuses_damaged_metadata_with_or_without_a_mission(backend, backend_m, journal):
    for b in (backend, backend_m):
        num = b.add("t")
        journal.items[num - 1]["body"] = "hand-edited, no block"
        journal.requests.clear()
        with pytest.raises(mb.MatronWriteError, match="refusing to guess"):
            b.close(num)
        assert not [c for c in journal.calls() if c[0] != "GET"]
        assert journal.items[num - 1]["state"] == "open"


@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_committed_create_followed_by_5xx_is_reconciled_not_duplicated(backend, journal, status):
    orig = journal._route

    def commit_then_5xx(method, parts, doc, headers):
        res = orig(method, parts, doc, headers)
        if method == "POST" and parts.path == "/items":
            return status, {}, b"bad gateway"
        return res

    journal._route = commit_then_5xx
    assert backend.add("t") == 1
    assert len(journal.items) == 1


def test_an_unreconcilable_create_names_the_uuid_to_look_for(backend, journal):
    backend.list()  # probe done while the journal is still healthy

    def down(method, parts, doc, headers):
        if parts.path == "/help":
            return FIXTURES["help_ok"]
        return 502, {}, b""

    journal._route = down
    with pytest.raises(mb.BackendError, match=r"label gf-uuid:[0-9a-f]{32}"):
        backend.add("t")


def test_a_4xx_create_is_not_reconciled(backend, journal):
    backend.list()
    orig = journal._route
    journal._route = lambda *a: (400, {}, b'{"error":"bad_request"}') if a[1].path == "/items" and a[0] == "POST" else orig(*a)
    journal.requests.clear()
    with pytest.raises(mb.MatronHTTPError):
        backend.add("t")
    assert not [c for c in journal.calls("GET") if c[1].startswith("/items?")]


def _empty_meta_body_len():
    meta = {"v": 1, "owner": "operator", "source": None, "next_action": "", "lens": None,
            "triage_count": 0, "last_triaged": None}
    return len(mb.encode_body("", meta))


def test_exact_limits_are_accepted_and_one_past_is_refused(backend):
    assert backend.add("x" * mb.TITLE_MAX) >= 1
    with pytest.raises(mb.BackendError):
        backend.add("x" * (mb.TITLE_MAX + 1))
    # body: description + "\n\n" + block == BODY_MAX exactly
    room = mb.BODY_MAX - mb.BODY_RESERVE - _empty_meta_body_len() - 2
    assert backend.add("t", description="d" * room) >= 1
    with pytest.raises(mb.BackendError, match="too long"):
        backend.add("t", description="d" * (room + 1))
    # labels: goodfellow + priority + uuid + tags == LABELS_MAX
    tags = ["t%d" % i for i in range(mb.LABELS_MAX - 3)]
    assert backend.add("t", tags=tags) >= 1
    with pytest.raises(mb.BackendError, match="labels"):
        backend.add("t", tags=tags + ["extra"])
    # label length: gf: + 37 == 40 accepted, 38 refused
    assert backend.add("t", tags=["a" * 37]) >= 1
    with pytest.raises(mb.BackendError):
        backend.add("t", tags=["a" * 38])


def test_a_loop_filed_at_the_size_limit_can_still_be_triaged_and_closed_with_a_mission(backend_m, journal):
    meta = {"v": 1, "owner": "operator", "source": "s", "next_action": "", "lens": "L",
            "triage_count": 0, "last_triaged": None}
    room = mb.BODY_MAX - mb.BODY_RESERVE - len(mb.encode_body("", meta)) - 2
    num = backend_m.add("t", source="s", lens="L", description="x" * room)
    assert backend_m.update_triage(num, triage_count=12, last_triaged="2026-12-31")
    assert backend_m.close(num) is True
    assert len(journal.items[0]["body"]) <= mb.BODY_MAX
    assert len(journal.milestones) == 1


@pytest.mark.parametrize("answer", [(201, {}, b"<html>"), (201, {}, b'{"nope": 1}'), (200, {}, b"")])
def test_a_committed_create_with_a_garbled_answer_is_reconciled(backend, journal, answer):
    orig = journal._route

    def garble(method, parts, doc, headers):
        res = orig(method, parts, doc, headers)
        return answer if (method == "POST" and parts.path == "/items") else res

    journal._route = garble
    assert backend.add("t") == 1
    assert len(journal.items) == 1
