#!/usr/bin/env python3
"""Optional Matron backend for the goodfellow loop store.

Files goodfellow's loops as tracker items on a Matron journal instead of in
``.goodfellow/loops.json``. Goodfellow stays independent: with no
``GOODFELLOW_MATRON_*`` variable set, none of this runs.

Configuration (environment only):

  GOODFELLOW_MATRON_URL         journal base URL. https, or http on a loopback host.
  GOODFELLOW_MATRON_TOKEN_FILE  path of a file holding the agent token. The token is
                                never read from an environment value, logged, echoed
                                in an error, or written under ``.goodfellow/``.
  GOODFELLOW_MATRON_CONVO       the conversation items are filed on.
  GOODFELLOW_MATRON_MISSION     optional. ``close`` posts a progress milestone to
                                this mission. Missions are never closed.

Misconfiguration is an error: a missing or unknown ``GOODFELLOW_MATRON_*`` variable,
a bad URL, an unreadable or world-readable token file. There is no silent fall
back to the local file.

Item mapping (goodfellow's own fields, no domain vocabulary):

  label ``goodfellow``       marks an item as goodfellow's
  label ``p1``..``p4``       priority
  label ``gf:<tag>``         one per tag
  label ``gf-uuid:<hex>``    the loop's durable uuid (idempotent create lookup)
  body                       description prose, then a fenced ``work-meta`` JSON block
                             holding owner, source, next_action, lens, triage_count,
                             last_triaged
  loop id                    the item number

Before its first request the backend runs a read-only capability probe (see
``MatronBackend._probe``) that checks exactly the contract it relies on: the
journal's own ``GET /help`` documents the routes, fields and idempotency headers
used, and ``GET /items`` answers in the documented shape (envelope with
``next_cursor``; items with ``id``, ``num``, ``state``, ``labels``, ``awaiting``,
epoch-millisecond ``created_at``, ``origin_convo_id``; the ``label`` and ``convo``
filters honoured). A missing capability is an error naming it. If the journal
also announces an API version (header ``X-Journal-Api-Version`` or a body line
``Journal API version: <n>``), only versions in SUPPORTED_API_VERSIONS are used and
any other is refused; not announcing one is fine when the probe passes. The only requests this module makes are the
(method, path) pairs in ALLOWED_REQUESTS.

Standard library only.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote, urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loop_backend import (  # noqa: E402
    BACKEND_PROTOCOL_VERSION,
    BackendConfigError,
    BackendError,
)

ENV_PREFIX = "GOODFELLOW_MATRON_"
ENV_URL = ENV_PREFIX + "URL"
ENV_TOKEN_FILE = ENV_PREFIX + "TOKEN_FILE"
ENV_CONVO = ENV_PREFIX + "CONVO"
ENV_MISSION = ENV_PREFIX + "MISSION"
ENV_LIVE_TEST = ENV_PREFIX + "LIVE_TEST"
KNOWN_ENV = frozenset({ENV_URL, ENV_TOKEN_FILE, ENV_CONVO, ENV_MISSION, ENV_LIVE_TEST})

SUPPORTED_API_VERSIONS = frozenset({"1"})

# Every request this module may send. Anything else is refused before it leaves.
ALLOWED_REQUESTS: Tuple[Tuple[str, str], ...] = (
    ("GET", r"/help"),
    ("GET", r"/items"),
    ("POST", r"/items"),
    ("GET", r"/items/%23[0-9]{1,12}"),
    ("PATCH", r"/items/%23[0-9]{1,12}"),
    ("POST", r"/items/%23[0-9]{1,12}/close"),
    ("POST", r"/milestones"),
)
_ALLOWED_RES = tuple((m, re.compile(p + r"\Z")) for m, p in ALLOWED_REQUESTS)

VALID_PRIORITIES = ("p1", "p2", "p3", "p4")
GF_LABEL = "goodfellow"
TAG_PREFIX = "gf:"
UUID_PREFIX = "gf-uuid:"
META_VERSION = 1
LABEL_MAX = 40
LABELS_MAX = 50
TITLE_MAX = 200
BODY_MAX = 32768
# Headroom left when a loop is filed, so later metadata writes (triage fields, the
# pending-milestone flag) can never push a stored body over the journal's limit.
BODY_RESERVE = 512
TOKEN_MAX = 4096
RESPONSE_MAX = 4 * 1024 * 1024
TIMEOUT_S = 10.0
WRITE_RETRIES = 3
MAX_PAGES = 200

_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")
_TOKEN_RE = re.compile(r"^[!-~]{1,%d}$" % TOKEN_MAX)
_CONVO_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_MISSION_RE = re.compile(r"^(#?[0-9]{1,12}|[A-Za-z0-9_]{1,64})$")
_VERSION_RE = re.compile(r"^[0-9][0-9.]{0,9}$")
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# What the journal's own GET /help must document (whitespace-normalised). Each entry
# is (capability name, pattern); the name is what an error shows.
_HELP_CAPABILITIES = tuple(
    (name, re.compile(rx))
    for name, rx in (
        ("list items with label and convo filters", r"GET /items\?[^ ]*convo=[^ ]*label="),
        ("paged item lists (next_cursor)", r"\{items, next_cursor\}"),
        ("read one item by #num", r"GET /items/:id"),
        ("create item with Idempotency-Key", r"POST /items .{0,400}Idempotency-Key"),
        ("patch item body", r"PATCH /items/:id \{[^}]*body\?"),
        ("close item with a resolution", r"POST /items/:id/close \{resolution:[^}]*'done'"),
        ("milestones with Idempotency-Key", r"POST /milestones.{0,60}Idempotency-Key"),
    )
)
_VERSION_LINE_RE = re.compile(r"^Journal API version:[ \t]*(\S+)[ \t]*$", re.MULTILINE)
_FENCE_OPEN = "```work-meta\n"


class MatronAuthError(BackendError):
    """The journal rejected the agent token."""


class MatronUnreachable(BackendError):
    """The journal did not answer."""


class MatronHTTPError(BackendError):
    def __init__(self, status: int, detail: str, what: str) -> None:
        super().__init__(f"journal answered HTTP {status} on {what}: {detail}")
        self.status = status


class MatronApiVersionError(BackendError):
    """The journal's API version is unknown or unannounced."""


class MatronCapabilityError(BackendError):
    """The journal lacks something this backend relies on."""

    def __init__(self, capability: str, detail: str) -> None:
        super().__init__(f"journal lacks required capability '{capability}': {detail}")
        self.capability = capability


class MatronWriteError(BackendError):
    """A write was not confirmed by a re-read."""


class MilestoneNotPosted(BackendError):
    """The item is closed but the milestone could not be posted."""


class _Secret:
    """Holds the token; prints as nothing useful."""

    __slots__ = ("_v",)

    def __init__(self, value: str) -> None:
        self._v = value

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "<token redacted>"

    __str__ = __repr__


class Config:
    __slots__ = ("base_url", "token_file", "convo", "mission", "_token")

    def __init__(self, base_url: str, token_file: str, convo: str,
                 mission: Optional[str], token: _Secret) -> None:
        self.base_url = base_url
        self.token_file = token_file
        self.convo = convo
        self.mission = mission
        self._token = token

    def __repr__(self) -> str:
        return (f"Config(base_url={self.base_url!r}, token_file={self.token_file!r}, "
                f"convo={self.convo!r}, mission={self.mission!r})")


# ----------------------------------------------------------------- configuration
def is_configured(env: Mapping[str, str]) -> bool:
    return any(k.startswith(ENV_PREFIX) for k in env)


def _validate_url(raw: str) -> str:
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError as exc:
        raise BackendConfigError(f"{ENV_URL} is not a valid URL: {exc}") from None
    if parts.scheme not in ("http", "https"):
        raise BackendConfigError(f"{ENV_URL} must start with https:// (or http:// on loopback)")
    host = parts.hostname
    if not host:
        raise BackendConfigError(f"{ENV_URL} has no host")
    if parts.username is not None or parts.password is not None:
        raise BackendConfigError(f"{ENV_URL} must not carry credentials")
    if parts.query or parts.fragment:
        raise BackendConfigError(f"{ENV_URL} must not carry a query or fragment")
    if "%" in parts.path or ".." in parts.path.split("/"):
        raise BackendConfigError(f"{ENV_URL} has an unsafe path")
    if parts.scheme == "http" and not _is_loopback(host):
        raise BackendConfigError(
            f"{ENV_URL} is plain http to a non-loopback host; the token would travel in the clear. Use https."
        )
    # urlsplit lowercases the scheme: HTTP:// and http:// must behave the same everywhere below
    return parts.scheme + raw[len(raw.split("://", 1)[0]):].rstrip("/")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _read_token(path_str: str) -> _Secret:
    path = Path(os.path.expanduser(path_str))
    if not path.is_absolute():
        raise BackendConfigError(f"{ENV_TOKEN_FILE} must be an absolute path")
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise BackendConfigError(f"{ENV_TOKEN_FILE}: cannot read {path}: {exc.strerror}") from None
    try:
        # Check the descriptor we read from, not the path: no check/use gap.
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise BackendConfigError(f"{ENV_TOKEN_FILE}: {path} is not a regular file")
        if os.name == "posix" and st.st_mode & 0o077:
            raise BackendConfigError(
                f"{ENV_TOKEN_FILE}: {path} is accessible to other users or groups; chmod it to 600"
            )
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            raw = fh.read(TOKEN_MAX + 64)
    except OSError as exc:
        raise BackendConfigError(f"{ENV_TOKEN_FILE}: cannot read {path}: {exc.strerror}") from None
    finally:
        if fd >= 0:
            os.close(fd)
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        raise BackendConfigError(f"{ENV_TOKEN_FILE}: {path} does not hold a token") from None
    if not _TOKEN_RE.match(value):
        raise BackendConfigError(
            f"{ENV_TOKEN_FILE}: {path} must hold one token, printable ASCII without spaces, 1..{TOKEN_MAX} characters"
        )
    return _Secret(value)


def parse_config(env: Mapping[str, str]) -> Config:
    present = sorted(k for k in env if k.startswith(ENV_PREFIX))
    if ENV_PREFIX + "TOKEN" in present:
        raise BackendConfigError(
            f"{ENV_PREFIX}TOKEN is not supported: the token is never read from an environment value. Use {ENV_TOKEN_FILE}."
        )
    unknown = [k for k in present if k not in KNOWN_ENV]
    if unknown:
        raise BackendConfigError(f"unknown variable(s): {', '.join(unknown)}")
    missing = [k for k in (ENV_URL, ENV_TOKEN_FILE, ENV_CONVO) if not env.get(k, "").strip()]
    if missing:
        raise BackendConfigError(f"missing or empty: {', '.join(missing)}")
    convo = env[ENV_CONVO].strip()
    if not _CONVO_RE.match(convo):
        raise BackendConfigError(f"{ENV_CONVO} is not a valid conversation id")
    mission = env.get(ENV_MISSION)
    if mission is not None:
        mission = mission.strip()
        if not _MISSION_RE.match(mission):
            raise BackendConfigError(f"{ENV_MISSION} must be a mission number, #number or id")
    return Config(
        _validate_url(env[ENV_URL].strip()),
        env[ENV_TOKEN_FILE].strip(),
        convo,
        mission,
        _read_token(env[ENV_TOKEN_FILE].strip()),
    )


# --------------------------------------------------------------------- transport
Transport = Callable[[str, str, Dict[str, str], Optional[bytes], float], Tuple[int, Dict[str, str], bytes]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the bearer token to wherever it points."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def default_transport(
    method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float
) -> Tuple[int, Dict[str, str], bytes]:
    handlers: List[Any] = [_NoRedirect()]
    if urlsplit(url).scheme == "http":
        handlers.append(urllib.request.ProxyHandler({}))  # loopback: never via a proxy
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read(RESPONSE_MAX)
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read(RESPONSE_MAX)
        finally:
            exc.close()
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, data
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise MatronUnreachable(f"journal unreachable: {type(exc).__name__}") from None


# ----------------------------------------------------------------------- codec
def encode_body(prose: str, meta: Dict[str, Any], limit: int = BODY_MAX) -> str:
    block = _FENCE_OPEN + json.dumps(meta, sort_keys=True, ensure_ascii=False) + "\n```"
    body = (prose.rstrip() + "\n\n" if prose.strip() else "") + block
    if len(body) > limit:
        raise BackendError(f"description too long: body would be {len(body)} characters, limit {limit}")
    return body


def decode_body(body: Optional[str]) -> Tuple[str, Optional[Dict[str, Any]]]:
    """(prose, meta). meta is None when the block is missing or unreadable.

    The block is the LAST fence in the body, so a description that itself contains
    a ``work-meta`` fence cannot shadow the real one.
    """
    text = body or ""
    start = text.rfind(_FENCE_OPEN)
    if start < 0 or (start > 0 and not text[:start].endswith("\n\n")):
        return text, None
    rest = text[start + len(_FENCE_OPEN):].rstrip()
    if not rest.endswith("\n```"):
        return text, None
    try:
        meta = json.loads(rest[: -len("\n```")])
    except ValueError:
        return text, None
    if not isinstance(meta, dict) or meta.get("v") != META_VERSION:
        return text, None
    return text[:start].rstrip("\n"), meta


def _check_labels(labels: List[str]) -> None:
    if len(labels) > LABELS_MAX:
        raise BackendError(f"{len(labels)} labels, limit {LABELS_MAX}")
    for label in labels:
        if not (1 <= len(label) <= LABEL_MAX) or not _LABEL_RE.match(label):
            raise BackendError(
                f"invalid label {label!r}: 1..{LABEL_MAX} characters of letters, digits and _ . : / -"
            )


def _iso_date(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def item_to_loop(item: Dict[str, Any]) -> Dict[str, Any]:
    labels = [l for l in (item.get("labels") or []) if isinstance(l, str)]
    prose, meta = decode_body(item.get("body"))
    meta = meta or {}
    prios = sorted(l for l in labels if l in VALID_PRIORITIES)
    uuids = [l[len(UUID_PREFIX):] for l in labels if l.startswith(UUID_PREFIX)]
    return {
        "id": item.get("num"),
        "uuid": uuids[0] if uuids else item.get("id"),
        "item_id": item.get("id"),
        "title": item.get("title", ""),
        "status": "closed" if item.get("state") == "closed" else "open",
        "priority": prios[0] if prios else "p3",
        "source": meta.get("source"),
        "opened": _iso_date(item.get("created_at")),
        "description": prose.strip(),
        "tags": [l[len(TAG_PREFIX):] for l in labels if l.startswith(TAG_PREFIX)],
        "owner": meta.get("owner", "operator"),
        "next_action": meta.get("next_action", ""),
        "lens": meta.get("lens"),
        "triage_count": meta.get("triage_count", 0),
        "last_triaged": meta.get("last_triaged"),
    }


# ---------------------------------------------------------------------- backend
class MatronBackend:
    protocol_version = BACKEND_PROTOCOL_VERSION
    name = "matron"

    def __init__(self, config: Config, transport: Optional[Transport] = None) -> None:
        self.config = config
        self._transport = transport or default_transport
        self._version_checked = False

    # -- transport -------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        idem_key: Optional[str] = None,
        retry: bool = False,
    ) -> Tuple[int, Dict[str, str], Any]:
        route = path.split("?", 1)[0]
        if not any(m == method and rx.match(route) for m, rx in _ALLOWED_RES):
            raise BackendError(f"refusing request outside the allowed set: {method} {route}")
        headers = {
            "Authorization": "Bearer " + self.config._token.reveal(),
            "Accept": "application/json",
            "User-Agent": "goodfellow-loop-backend",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idem_key:
            headers["Idempotency-Key"] = idem_key
        url = self.config.base_url + path
        last: Optional[Exception] = None
        for _ in range(2 if retry else 1):
            try:
                status, rheaders, raw = self._transport(method, url, headers, data, TIMEOUT_S)
            except MatronUnreachable as exc:
                last = exc
                continue
            if status in (401, 403):
                raise MatronAuthError(f"journal rejected the agent token (HTTP {status}) on {method} {route}")
            if status >= 500:
                last = MatronHTTPError(status, "server error", f"{method} {route}")
                continue
            return status, rheaders, self._parse(status, raw, f"{method} {route}")
        assert last is not None
        raise last

    def _scrub(self, text: str) -> str:
        token = self.config._token.reveal()
        return text.replace(token, "[redacted]") if token else text

    def _parse(self, status: int, raw: bytes, what: str) -> Any:
        """Only 2xx is success. Server text never reaches an error message: at most a
        short machine code (``not_found``) is kept, so a token echoed back by a server
        or proxy cannot be printed."""
        if status < 200 or status > 299:
            code = ""
            try:
                doc = json.loads(raw.decode("utf-8", errors="replace"))
                if isinstance(doc, dict) and isinstance(doc.get("error"), str):
                    code = doc["error"]
            except ValueError:
                pass
            token = self.config._token.reveal()
            safe = bool(_CODE_RE.match(code)) and token not in code and code not in token
            raise MatronHTTPError(status, code if safe else "no detail", what)
        text = raw.decode("utf-8", errors="replace")
        if what.endswith(" /help"):
            return text
        try:
            return json.loads(text) if text.strip() else None
        except ValueError:
            raise BackendError(f"journal sent a malformed body on {what}") from None

    def _ensure_ready(self) -> None:
        """Version gate (when announced) and capability probe; once per backend."""
        if self._version_checked:
            return
        _, headers, text = self._request("GET", "/help", retry=True)
        announced = headers.get("x-journal-api-version")
        if announced is None:
            m = _VERSION_LINE_RE.search(text or "")
            announced = m.group(1) if m else None
        if announced is not None and announced not in SUPPORTED_API_VERSIONS:
            shown = announced if _VERSION_RE.match(announced) else "(unprintable)"
            raise MatronApiVersionError(
                f"journal API version {shown} is not supported "
                f"(supported: {', '.join(sorted(SUPPORTED_API_VERSIONS))}); update goodfellow or the journal"
            )
        self._probe(text or "")
        self._version_checked = True

    def _probe(self, help_text: str) -> None:
        """Read-only check of the contract this backend depends on. Fails closed."""
        flat = " ".join(help_text.replace("`", "").split())
        for capability, rx in _HELP_CAPABILITIES:
            if not rx.search(flat):
                raise MatronCapabilityError(capability, "not documented by the journal's GET /help")
        base = {"limit": "1", "convo": self.config.convo}
        _, _, scoped = self._request("GET", "/items?" + urlencode({**base, "label": GF_LABEL}), retry=True)
        rows = self._probe_envelope(scoped, "items list envelope")
        for item in rows:
            self._probe_item(item)  # shape first: nothing below may assume it
            if GF_LABEL not in item["labels"]:
                raise MatronCapabilityError("label filter", "GET /items?label= returned an item without that label")
            if item.get("origin_convo_id") != self.config.convo:
                raise MatronCapabilityError("convo filter", "GET /items?convo= returned an item from another conversation")
        _, _, anyrows = self._request("GET", "/items?" + urlencode({"limit": "1"}), retry=True)
        for item in self._probe_envelope(anyrows, "items list envelope") + rows:
            self._probe_item(item)

    @staticmethod
    def _probe_envelope(doc: Any, what: str) -> List[Dict[str, Any]]:
        if not isinstance(doc, dict) or not isinstance(doc.get("items"), list) or "next_cursor" not in doc:
            raise MatronCapabilityError(what, "GET /items must answer {items: [...], next_cursor}")
        if len(doc["items"]) > 1:
            raise MatronCapabilityError("limit parameter", "GET /items?limit=1 returned more than one item")
        return doc["items"]

    @staticmethod
    def _probe_item(item: Any) -> None:
        def bad(field: str, why: str) -> MatronCapabilityError:
            return MatronCapabilityError(f"item field {field}", why)

        if not isinstance(item, dict):
            raise bad("(item)", "not an object")
        if not isinstance(item.get("id"), str) or not item["id"]:
            raise bad("id", "missing or not a string")
        if isinstance(item.get("num"), bool) or not isinstance(item.get("num"), int) or item["num"] < 1:
            raise bad("num", "missing or not a positive integer")
        if item.get("state") not in ("open", "closed"):
            raise bad("state", "must be 'open' or 'closed'")
        labels = item.get("labels")
        if not isinstance(labels, list) or not all(isinstance(l, str) for l in labels):
            raise bad("labels", "missing or not a list of strings")
        if "awaiting" not in item:
            raise bad("awaiting", "missing")
        ts = item.get("created_at")
        if isinstance(ts, bool) or not isinstance(ts, int) or not (10**12 <= ts < 10**14):
            raise bad("created_at", "must be an integer count of epoch milliseconds")
        if not isinstance(item.get("title"), str):
            raise bad("title", "missing or not a string")
        if item.get("body") is not None and not isinstance(item["body"], str):
            raise bad("body", "not a string")
        if "origin_convo_id" not in item:
            raise bad("origin_convo_id", "missing")

    def _call(self, *args: Any, **kwargs: Any) -> Any:
        self._ensure_ready()
        return self._request(*args, **kwargs)[2]

    # -- items -------------------------------------------------------------------
    def _list_items(self, **filters: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        seen = set()
        for _ in range(MAX_PAGES):
            q = {"label": GF_LABEL, "convo": self.config.convo, "limit": "500", **filters}
            if cursor:
                q["cursor"] = cursor
            page = self._call("GET", "/items?" + urlencode(q), retry=True)
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise BackendError("unexpected /items response shape")
            out.extend(i for i in page["items"] if self._is_ours(i))
            cursor = page.get("next_cursor")
            if not cursor:
                return out
            if cursor in seen:
                raise BackendError("journal repeated a page cursor")
            seen.add(cursor)
        raise BackendError(f"more than {MAX_PAGES} pages of items")

    def _is_ours(self, item: Any) -> bool:
        return (
            isinstance(item, dict)
            and isinstance(item.get("num"), int)
            and GF_LABEL in (item.get("labels") or [])
            and item.get("origin_convo_id") == self.config.convo
        )

    @staticmethod
    def _ref(loop_id: int) -> str:
        if isinstance(loop_id, bool) or not isinstance(loop_id, int) or loop_id < 1:
            raise ValueError(f"invalid loop id {loop_id!r}")
        return "%23" + str(loop_id)

    def _fetch(self, loop_id: int) -> Optional[Dict[str, Any]]:
        try:
            doc = self._call("GET", "/items/" + self._ref(loop_id), retry=True)
        except MatronHTTPError as exc:
            if exc.status == 404:
                return None
            raise
        item = doc.get("item") if isinstance(doc, dict) else None
        return item if self._is_ours(item) else None

    def add(self, title, priority="p3", source=None, description="", tags=None,
            owner="operator", next_action="", lens=None):
        if priority not in VALID_PRIORITIES:
            raise ValueError(
                f"Invalid priority {priority!r}; must be one of {', '.join(VALID_PRIORITIES)}"
            )
        if not isinstance(title, str) or not title.strip() or len(title) > TITLE_MAX:
            raise BackendError(f"title must be 1..{TITLE_MAX} characters")
        tag_list: List[str] = []
        for t in tags or []:
            if not isinstance(t, str) or not _TAG_RE.match(t) or len(TAG_PREFIX + t) > LABEL_MAX:
                raise BackendError(
                    f"invalid tag {t!r}: letters, digits and _ . : / -, at most {LABEL_MAX - len(TAG_PREFIX)} characters"
                )
            if t not in tag_list:
                tag_list.append(t)
        ident = uuid.uuid4().hex
        labels = [GF_LABEL, priority, *(TAG_PREFIX + t for t in tag_list), UUID_PREFIX + ident]
        _check_labels(labels)
        meta = {
            "v": META_VERSION,
            "owner": owner,
            "source": source,
            "next_action": next_action,
            "lens": lens,
            "triage_count": 0,
            "last_triaged": None,
        }
        payload = {
            "kind": "task",
            "title": title,
            "body": encode_body(description or "", meta, limit=BODY_MAX - BODY_RESERVE),
            "labels": labels,
            "convo_id": self.config.convo,
        }
        self._ensure_ready()  # a failure here means nothing was sent
        try:
            doc = self._call("POST", "/items", payload, idem_key="gf-add-" + ident, retry=True)
            item = doc.get("item") if isinstance(doc, dict) else None
            if not isinstance(item, dict) or not isinstance(item.get("num"), int):
                raise BackendError("unexpected /items create response shape")
            return item["num"]
        except BackendError as exc:
            if isinstance(exc, MatronAuthError) or (isinstance(exc, MatronHTTPError) and exc.status < 500):
                raise  # refused outright: nothing was created
            # The create may have committed before the answer was lost or garbled: look it up.
            try:
                found = [
                    i for i in self._list_items(label=UUID_PREFIX + ident)
                    if (UUID_PREFIX + ident) in (i.get("labels") or [])
                ]
            except BackendError:
                raise BackendError(
                    f"create outcome unknown ({exc}); check the journal for label {UUID_PREFIX}{ident} before retrying"
                ) from None
            if len(found) == 1:
                return found[0]["num"]
            if found:
                raise BackendError(f"{len(found)} items carry label {UUID_PREFIX}{ident}; duplicate create")
            raise

    def get(self, loop_id):
        item = self._fetch(loop_id)
        return item_to_loop(item) if item else None

    def list(self, status=None, min_age_days=None):
        if status not in (None, "open", "closed"):
            raise ValueError(f"status must be 'open' or 'closed', not {status!r}")
        filters = {"state": status} if status else {}  # omit state to span open and closed
        loops = [item_to_loop(i) for i in self._list_items(**filters)]
        loops.sort(key=lambda l: l["id"])
        if min_age_days is not None:
            today = datetime.now(timezone.utc).date()
            loops = [
                l for l in loops
                if l["opened"]
                and (today - datetime.strptime(l["opened"], "%Y-%m-%d").date()).days >= min_age_days
            ]
        return loops

    def close(self, loop_id):
        item = self._fetch(loop_id)
        if item is None:
            return False
        mission = self.config.mission
        _, meta = decode_body(item.get("body"))
        if meta is None:
            raise MatronWriteError(
                f"item #{item['num']} has no readable work-meta block; refusing to guess"
            )
        if item.get("state") != "closed":
            if mission:
                # Recorded first so a failed milestone can be resumed by a retry.
                self._write_meta(loop_id, {"milestone_pending": True}, item)
            try:
                self._call("POST", "/items/" + self._ref(loop_id) + "/close", {"resolution": "done"})
            except MatronHTTPError as exc:
                if exc.status != 409:  # already closed: idempotent
                    raise
            item = self._fetch(loop_id)
            if item is None or item.get("state") != "closed":
                raise MatronWriteError(f"item #{loop_id} is not closed after the close request")
        if mission:
            _, meta = decode_body(item.get("body"))
            if meta and meta.get("milestone_pending"):
                self._post_milestone(item)
                self._write_meta(loop_id, {"milestone_pending": None}, item)
        return True

    def _post_milestone(self, item: Dict[str, Any]) -> None:
        payload = {
            "convo_id": self.config.convo,
            "kind": "progress",
            "title": ("Closed: " + str(item.get("title", "")))[:TITLE_MAX],
            "body": f"Goodfellow closed follow-up #{item['num']}.",
            "mission": self.config.mission,
        }
        try:
            self._call("POST", "/milestones", payload, idem_key="gf-close-" + str(item.get("id")), retry=True)
        except BackendError as exc:
            raise MilestoneNotPosted(
                f"item #{item['num']} is closed, but the milestone to mission {self.config.mission} "
                f"was not posted ({exc}); run close again to retry it"
            ) from None

    def _write_meta(self, loop_id: int, updates: Dict[str, Any], seen: Optional[Dict[str, Any]] = None) -> bool:
        """Delta write of work-meta keys with verify. A value of None removes the key.

        The journal has no revision precondition on PATCH, so a concurrent edit made
        between our read and our write cannot be ruled out; it is narrowed by
        re-reading immediately before the write and giving up on any change, and by
        verifying the whole body afterwards. Labels are never sent (PATCH replaces
        whole arrays).
        """
        for _ in range(WRITE_RETRIES):
            item = seen or self._fetch(loop_id)
            seen = None
            if item is None:
                return False
            prose, meta = decode_body(item.get("body"))
            if meta is None:
                raise MatronWriteError(
                    f"item #{item['num']} has no readable work-meta block; refusing to guess"
                )
            new_meta = {k: v for k, v in {**meta, **updates}.items() if v is not None}
            if new_meta == meta:
                return True
            body = encode_body(prose, new_meta)
            latest = self._fetch(loop_id)
            if latest is None or latest.get("body") != item.get("body") or latest.get("updated_at") != item.get("updated_at"):
                continue  # edited under us: re-read and redo from the new state
            self._call("PATCH", "/items/" + self._ref(loop_id), {"body": body})
            fresh = self._fetch(loop_id)
            if fresh is not None and fresh.get("body") == body:
                return True
        raise MatronWriteError(f"item #{loop_id} write not confirmed after {WRITE_RETRIES} attempts")

    def update_triage(self, loop_id, triage_count=None, last_triaged=None):
        if triage_count is not None and (
            isinstance(triage_count, bool) or not isinstance(triage_count, int) or triage_count < 0
        ):
            raise ValueError("triage_count must be a non-negative integer")
        if last_triaged is not None and not (isinstance(last_triaged, str) and _DATE_RE.match(last_triaged)):
            raise ValueError("last_triaged must be YYYY-MM-DD")
        updates: Dict[str, Any] = {}
        if triage_count is not None:
            updates["triage_count"] = triage_count
        if last_triaged is not None:
            updates["last_triaged"] = last_triaged
        return self._write_meta(loop_id, updates)


def from_env(env: Mapping[str, str], project_root: str = ".") -> MatronBackend:
    return MatronBackend(parse_config(env))
