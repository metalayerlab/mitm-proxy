"""The built-in ``capture`` middleware: the flows its rules match, as JSONL files (design 0012).

Buffered, step (c): the bodies are read with ``read_all()`` in ``on_done``, so ``streamed`` is
always false. Every record is redacted before anything reaches disk: headers (on by default),
``json_paths`` in JSON bodies and ``query_params`` in the URL, then truncated to
``max_body_bytes``. A record that can't be written is dropped and counted; nothing here ever
affects the traffic (fail open, ``on_error`` stays ``"skip"``).
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import datetime
import json
import logging
import os
import re
import threading
import time
import weakref
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus

from mitm_proxy.errors import StartupError
from mitm_proxy.http.body import PassthroughBody
from mitm_proxy.http.flow import Flow
from mitm_proxy.http.headers import Headers
from mitm_proxy.pipeline import Middleware
from mitm_proxy.scope import Pattern, normalize_host, parse_pattern

__all__ = ["DEFAULT_REDACT_HEADERS", "Capture", "JsonPath", "Rule"]

log = logging.getLogger(__name__)

DEFAULT_MAX_BODY_BYTES = 1048576
DEFAULT_REDACT_HEADERS = (
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "cookie",
    "set-cookie",
)
DEFAULT_MARKER = "[REDACTED]"
DIRECTIONS = ("inbound", "egress", "any")
WARN_EVERY = 60.0  # seconds: at most one write-failure WARNING per rule in this long
# How long a write waits for its file's lock before the record is dropped; below the default
# hook timeout, so the drop is counted here rather than cut off by the pipeline.
LOCK_TIMEOUT = 2.0
# The most bytes a gzip/deflate body is decoded to. A body that decodes to more is not stored
# (omitted, like an oversize one): storing it encoded would put unredacted bytes on disk.
MAX_DECODED_BYTES = 64 * 1024 * 1024

_TOP_KEYS = frozenset({"dir", "max_body_bytes", "redact", "rules"})
_REDACT_KEYS = frozenset({"headers", "json_paths", "query_params", "marker"})
_RULE_KEYS = frozenset({"name", "direction", "host", "path", "methods"})
_RULE_NAME = re.compile(r"^[a-z0-9_-]+$")
_PATH_NAME = r"[^.\[\]\s]+"
_STEP = re.compile(rf"\.\.(?P<deep>{_PATH_NAME})|\.(?P<name>{_PATH_NAME})|\[(?P<index>\d+|\*)\]")

# One lock per file, shared by every Capture instance (a reload builds new ones that write to the
# same files): each record is one write of one line, never interleaved with another.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _fail(message: str) -> StartupError:
    return StartupError(f"capture: {message}")


def _check_keys(where: str, mapping: Mapping[str, Any], allowed: frozenset[str]) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        # A typo must never quietly turn redaction off (design 0012).
        raise _fail(
            f"{where}: unknown key {', '.join(repr(k) for k in unknown)} "
            f"(known: {', '.join(sorted(allowed))})"
        )


def _mapping(where: str, value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(f"{where}: expected a mapping")
    return value


def _str(where: str, value: object) -> str:
    if not isinstance(value, str):
        raise _fail(f"{where}: expected a string")
    return value


def _str_list(where: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise _fail(f"{where}: expected a list of strings")
    items = tuple(value)
    if not all(isinstance(item, str) and item for item in items):
        raise _fail(f"{where}: expected a list of non-empty strings")
    return items


# JSON paths: $ followed by .name, [n], [*] and ..name steps.


@dataclass(frozen=True)
class _Step:
    kind: str  # "name", "index", "all" or "deep"
    name: str = ""
    index: int = 0


@dataclass(frozen=True)
class JsonPath:
    text: str
    steps: tuple[_Step, ...]

    @classmethod
    def parse(cls, text: str) -> JsonPath:
        """``$`` followed by ``.name``, ``[n]``, ``[*]`` and ``..name`` steps; ValueError if not."""
        if not text.startswith("$"):
            raise ValueError(f"{text!r}: a path starts with $")
        steps: list[_Step] = []
        pos = 1
        while pos < len(text):
            m = _STEP.match(text, pos)
            if m is None:
                raise ValueError(
                    f"{text!r}: unexpected {text[pos:]!r} (steps are .name, [n], [*] and ..name)"
                )
            if m["deep"] is not None:
                steps.append(_Step("deep", name=m["deep"]))
            elif m["name"] is not None:
                steps.append(_Step("name", name=m["name"]))
            elif m["index"] == "*":
                steps.append(_Step("all"))
            else:
                steps.append(_Step("index", index=int(m["index"])))
            pos = m.end()
        return cls(text, tuple(steps))

    def redact(self, document: Any, marker: str) -> tuple[Any, bool]:
        """The document with every value this path names replaced by marker; and whether any was."""
        return _redact(document, self.steps, marker)


def _redact(node: Any, steps: tuple[_Step, ...], marker: str) -> tuple[Any, bool]:
    if not steps:
        return marker, True
    step, rest = steps[0], steps[1:]
    changed = False

    def at(key: Any) -> None:
        nonlocal changed
        node[key], hit = _redact(node[key], rest, marker)
        changed = changed or hit

    if step.kind == "name":
        if isinstance(node, dict) and step.name in node:
            at(step.name)
    elif step.kind == "index":
        if isinstance(node, list) and step.index < len(node):
            at(step.index)
    elif step.kind == "all":
        if isinstance(node, dict):
            for key in list(node):
                at(key)
        elif isinstance(node, list):
            for i in range(len(node)):
                at(i)
    else:  # "deep": this name at any depth below here; deeper matches first, then this level
        children = (
            node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
        )
        for child in children:
            changed = _redact(child, steps, marker)[1] or changed
        if isinstance(node, dict) and step.name in node:
            at(step.name)
    return node, changed


# Rules.


def _glob(pattern: str) -> re.Pattern[str]:
    """A path glob: ``**`` across segments, ``*`` within one; everything else literal."""
    parts = re.split(r"(\*\*|\*)", pattern)
    body = "".join(".*" if p == "**" else "[^/]*" if p == "*" else re.escape(p) for p in parts)
    return re.compile(rf"\A{body}\Z", re.DOTALL)


@dataclass(frozen=True)
class Rule:
    name: str
    direction: str = "any"
    host: Pattern | None = None
    path: re.Pattern[str] | None = None
    methods: frozenset[str] | None = None

    def matches(self, flow: Flow) -> bool:
        if self.direction != "any" and flow.direction != self.direction:
            return False
        if self.host is not None:
            try:
                host = normalize_host(flow.host)
            except ValueError:
                return False
            if not self.host.matches(host):
                return False
        if self.path is not None:
            path = flow.request.path.partition("?")[0]
            if self.path.match(path) is None:
                return False
        return self.methods is None or flow.method.upper() in self.methods


def _rule(i: int, value: object) -> Rule:
    where = f"rules[{i}]"
    item = _mapping(where, value)
    _check_keys(where, item, _RULE_KEYS)
    if "name" not in item:
        raise _fail(f"{where}: name is required")
    name = _str(f"{where}.name", item["name"])
    if not _RULE_NAME.match(name):
        raise _fail(
            f"{where}.name: {name!r} must be lower-case letters, digits, - and _ (it names files)"
        )
    direction = _str(f"{where}.direction", item.get("direction", "any"))
    if direction not in DIRECTIONS:
        raise _fail(f"{where}.direction: {direction!r} is not one of {', '.join(DIRECTIONS)}")
    host = None
    if "host" in item:
        try:
            host = parse_pattern(_str(f"{where}.host", item["host"]))
        except ValueError as e:
            raise _fail(f"{where}.host: {e}") from None
    path = None
    if "path" in item:
        text = _str(f"{where}.path", item["path"])
        if not text.startswith("/"):
            raise _fail(f"{where}.path: {text!r} must start with /")
        if "?" in text:
            raise _fail(f"{where}.path: {text!r}: the path is matched without its query")
        path = _glob(text)
    methods = None
    if "methods" in item:
        methods = frozenset(m.upper() for m in _str_list(f"{where}.methods", item["methods"]))
        if not methods:
            raise _fail(f"{where}.methods: give at least one method, or leave it out")
    return Rule(name, direction, host, path, methods)


# The record.


def _utc(wall: float | None) -> str | None:
    if wall is None:
        return None
    moment = datetime.datetime.fromtimestamp(wall, datetime.UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _day(wall: float) -> str:
    """The UTC date: a new file at midnight UTC (rotation is a new name; old files are kept)."""
    return datetime.datetime.fromtimestamp(wall, datetime.UTC).strftime("%Y-%m-%d")


def _media_type(headers: Headers) -> str:
    value = headers.get("content-type") or ""
    return value.partition(";")[0].strip().lower()


def _is_json(media_type: str) -> bool:
    return media_type == "application/json" or (
        media_type.startswith("application/") and media_type.endswith("+json")
    )


class _TooLarge(Exception):
    """A gzip/deflate body that decodes to more than MAX_DECODED_BYTES."""


def _decompress(data: bytes, encoding: str) -> bytes:
    """Decode one gzip or deflate layer. zlib.error if it isn't one; _TooLarge if it's huge."""
    if encoding == "gzip":
        out, rest = _inflate(data, 16 + zlib.MAX_WBITS)
        while rest:  # concatenated gzip members, as gzip.decompress reads them
            more, rest = _inflate(rest, 16 + zlib.MAX_WBITS)
            out += more
            if len(out) > MAX_DECODED_BYTES:
                raise _TooLarge
        return out
    try:
        return _inflate(data, zlib.MAX_WBITS)[0]
    except zlib.error:
        # deflate is meant to be zlib-wrapped (RFC 9110), but raw deflate is common too.
        return _inflate(data, -zlib.MAX_WBITS)[0]


def _inflate(data: bytes, wbits: int) -> tuple[bytes, bytes]:
    """One stream's bytes, bounded by MAX_DECODED_BYTES, and what follows it."""
    decoder = zlib.decompressobj(wbits)
    out = decoder.decompress(data, MAX_DECODED_BYTES + 1)
    if len(out) > MAX_DECODED_BYTES or decoder.unconsumed_tail:
        raise _TooLarge
    if not decoder.eof:
        raise zlib.error("the stream ends early")
    return out, decoder.unused_data


class Capture(Middleware):
    """Records the flows its rules match, one JSON line each, in ``<dir>/<rule>-<date>.jsonl``.

    Constructed with ``middleware.config.capture`` (a read-only mapping). ``clock`` is the wall
    clock (``time.time``), injectable for tests: it names the day's file and places the timings.
    """

    def __init__(
        self,
        options: Mapping[str, Any] | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if options is None:
            raise _fail("needs options: middleware.config.capture, with dir and rules")
        top = _mapping("middleware.config.capture", options)
        _check_keys("middleware.config.capture", top, _TOP_KEYS)
        if "dir" not in top:
            raise _fail("dir is required")
        directory = _str("dir", top["dir"])
        if not directory:
            raise _fail("dir is required")
        self.dir = Path(directory)
        max_body = top.get("max_body_bytes", DEFAULT_MAX_BODY_BYTES)
        if isinstance(max_body, bool) or not isinstance(max_body, int) or max_body < 0:
            raise _fail("max_body_bytes: expected a whole number, 0 or more")
        self.max_body_bytes = max_body

        redact = _mapping("redact", top.get("redact", {}))
        _check_keys("redact", redact, _REDACT_KEYS)
        self.redact_headers = frozenset(
            h.lower()
            for h in _str_list("redact.headers", redact.get("headers", DEFAULT_REDACT_HEADERS))
        )
        self.json_paths: tuple[JsonPath, ...] = ()
        paths = []
        for i, text in enumerate(_str_list("redact.json_paths", redact.get("json_paths", ()))):
            try:
                paths.append(JsonPath.parse(text))
            except ValueError as e:
                raise _fail(f"redact.json_paths[{i}]: {e}") from None
        self.json_paths = tuple(paths)
        self.query_params = frozenset(
            _str_list("redact.query_params", redact.get("query_params", ()))
        )
        self.marker = _str("redact.marker", redact.get("marker", DEFAULT_MARKER))

        if "rules" not in top:
            raise _fail("rules is required: one or more")
        rules = top["rules"]
        if not isinstance(rules, Sequence) or isinstance(rules, str) or not rules:
            raise _fail("rules: expected a list of one or more rules")
        self.rules = tuple(_rule(i, item) for i, item in enumerate(rules))
        seen: set[str] = set()
        for rule in self.rules:
            if rule.name in seen:
                raise _fail(f"rules: the name {rule.name!r} is used twice")
            seen.add(rule.name)

        self._clock = clock
        # Called with (rule, "written" | "failed") for each record: the server binds the
        # mitm_proxy_capture_records_total counter here once the rule names are declared.
        self.on_record: Callable[[str, str], None] | None = None
        self._matched: weakref.WeakKeyDictionary[Flow, Rule] = weakref.WeakKeyDictionary()
        self._warned: dict[str, float] = {}
        self._check_dir()

    @property
    def rule_names(self) -> tuple[str, ...]:
        return tuple(rule.name for rule in self.rules)

    def _check_dir(self) -> None:
        """Create dir if it's missing; if that fails or it isn't writable, warn and load anyway."""
        try:
            self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not os.access(self.dir, os.W_OK | os.X_OK):
                raise PermissionError(f"{self.dir} is not writable")
        except OSError as e:
            log.warning(
                "capture: dir %s can't be created or written (%s); records will be dropped "
                "until it can",
                self.dir,
                e.strerror or e,
            )

    # The hooks.

    def applies_to(self, flow: Flow) -> bool:
        for rule in self.rules:
            if rule.matches(flow):
                self._matched[flow] = rule
                return True
        return False  # the flow keeps the unbuffered fast path

    async def on_done(self, flow: Flow) -> None:
        rule = self._matched.pop(flow, None)
        if rule is None:  # pragma: no cover - on_done follows applies_to
            return
        try:
            wall = self._clock()
            line = (
                json.dumps(await self.record(flow, rule, wall), ensure_ascii=False) + "\n"
            ).encode()
            path = self.dir / f"{rule.name}-{_day(wall)}.jsonl"
            await asyncio.to_thread(_append, path, line)
        except (OSError, TimeoutError) as e:
            self._count(rule.name, "failed")
            self._warn(rule.name, e)
        else:
            self._count(rule.name, "written")

    def _count(self, rule: str, result: str) -> None:
        if self.on_record is not None:
            self.on_record(rule, result)

    def _warn(self, rule: str, error: Exception) -> None:
        now = time.monotonic()
        last = self._warned.get(rule)
        if last is not None and now - last < WARN_EVERY:
            return
        self._warned[rule] = now
        reason = (
            error.strerror
            if isinstance(error, OSError) and error.strerror
            else type(error).__name__
        )
        log.warning(
            "capture: a record for rule %r can't be written to %s (%s); dropped "
            "(at most one warning a minute per rule)",
            rule,
            self.dir,
            reason,
        )

    # The record, redacted.

    async def record(self, flow: Flow, rule: Rule, wall: float) -> dict[str, Any]:
        """The flow as capture writes it; wall is now on the wall clock."""
        request, response, t = flow.request, flow.response, flow.timings
        offset = wall - time.monotonic()
        req: dict[str, Any] = {
            "method": request.method,
            "url": self._url(f"{request.scheme}://{request.authority}{request.path}"),
            "headers": self._headers(request.headers),
        }
        if request.trailers is not None:
            req["trailers"] = self._headers(request.trailers)
        req.update(await self._body(request.body, request.headers))
        resp: dict[str, Any] | None = None
        if response is not None:
            resp = {"status": response.status, "headers": self._headers(response.headers)}
            if response.trailers is not None:
                resp["trailers"] = self._headers(response.trailers)
            resp.update(await self._body(response.body, response.headers))
        outcome = flow.outcome
        return {
            "rule": rule.name,
            "direction": flow.direction,
            "flow": flow.id,
            "outcome": str(outcome) if outcome is not None else None,
            "request": req,
            "response": resp,
            "timings": {
                "start": _utc(t.started + offset),
                "ttfb_ms": None if t.ttfb is None else round(t.ttfb * 1000, 1),
                "end": _utc(None if t.complete is None else t.complete + offset),
            },
            "streamed": False,
        }

    def _headers(self, headers: Headers) -> list[list[str]]:
        return [[k, self.marker if k in self.redact_headers else v] for k, v in headers.items()]

    def _url(self, url: str) -> str:
        if not self.query_params:
            return url
        base, sep, rest = url.partition("?")
        if not sep:
            return url
        query, hash_sep, fragment = rest.partition("#")
        parts = []
        for part in query.split("&"):
            name, eq, _ = part.partition("=")
            if eq and (name in self.query_params or unquote_plus(name) in self.query_params):
                part = f"{name}={self.marker}"
            parts.append(part)
        return f"{base}?{'&'.join(parts)}{hash_sep}{fragment}"

    async def _body(self, body: object, headers: Headers) -> dict[str, Any]:
        """The body's fields: body, and body_encoding, content_decoded, truncated, omitted."""
        if isinstance(body, PassthroughBody):
            # Over body.max_buffer_bytes: the hooks were skipped and nothing was buffered.
            return {"body": None, "omitted": "oversize"}
        try:
            data: bytes = await body.read_all()  # type: ignore[attr-defined]  # a BodyStream
        except RuntimeError:  # pragma: no cover - only a passthrough body refuses
            return {"body": None, "omitted": "oversize"}
        fields: dict[str, Any] = {}
        encoding = (headers.get("content-encoding") or "").strip().lower()
        if encoding == "identity":
            encoding = ""
        decoded = False
        if data and encoding in ("gzip", "deflate"):
            try:
                data = _decompress(data, encoding)
            except _TooLarge:
                return {"body": None, "omitted": "oversize"}
            except zlib.error:
                pass  # not what it says it is: stored as it came, in base64
            else:
                decoded = True
                fields["content_decoded"] = encoding
        media_type = _media_type(headers)
        if self.json_paths and (decoded or not encoding) and _is_json(media_type):
            # An event stream (text/event-stream) is stored with json_paths skipped until
            # observe-only streaming lands (step e); any other non-JSON body is too.
            data = self._redact_json(data)
        original = len(data)
        truncated = original > self.max_body_bytes
        if truncated:
            data = data[: self.max_body_bytes]
        binary = bool(encoding) and not decoded
        text: str | None = None
        if not binary:
            try:
                # A cut can split a character: the incomplete tail is dropped, not the whole text.
                text = codecs.getincrementaldecoder("utf-8")().decode(data, final=not truncated)
            except UnicodeDecodeError:
                text = None
        if text is not None:
            fields["body"] = text
        else:
            fields["body"] = base64.b64encode(data).decode("ascii")
            fields["body_encoding"] = "base64"
        if truncated:
            fields["truncated"] = original
        return {"body": fields.pop("body"), **fields}

    def _redact_json(self, data: bytes) -> bytes:
        try:
            document = json.loads(data)
        except ValueError:
            return data  # doesn't parse: stored as it is, json_paths skipped
        changed = False
        for path in self.json_paths:
            document, hit = path.redact(document, self.marker)
            changed = changed or hit
        if not changed:
            return data  # nothing matched: keep the body exactly as it was sent
        return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()


def _lock(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
        return lock


def _append(path: Path, line: bytes) -> None:
    """Append one line to path, created 0600 in a 0700 directory. Runs in a worker thread."""
    lock = _lock(path)
    if not lock.acquire(timeout=LOCK_TIMEOUT):
        raise TimeoutError(f"waiting for {path}")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            written = os.write(fd, line)
            if written != len(line):  # pragma: no cover - a regular file takes the whole line
                raise OSError(0, f"short write to {path}")
        finally:
            os.close(fd)
    finally:
        lock.release()
