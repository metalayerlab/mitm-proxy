"""The built-in ``capture`` middleware: the flows its rules match, as JSONL files (design 0012).

Observe-only, step (e): the bodies are collected from the chunk hooks, so a flow on which
capture is the only middleware streams, and ``streamed`` says whether its response did. Every
record is redacted before anything reaches disk: headers (on by default), ``json_paths`` in
JSON bodies and in server-sent events' JSON ``data:`` lines, and ``query_params`` in the URL,
then truncated to ``max_body_bytes``. A record that can't be written is dropped and counted;
nothing here ever affects the traffic (fail open, ``on_error`` stays ``"skip"``).

What a streamed body costs to hold (``_Collected``): a body that is redacted or decoded whole
(JSON with ``json_paths``, gzip or deflate) is kept up to ``MAX_DECODED_BYTES``, past which it
is omitted as oversize; an event stream keeps ``max_body_bytes`` of redacted lines plus the one
line still arriving (up to ``MAX_DECODED_BYTES``); any other body keeps ``max_body_bytes``. A
buffered flow's body comes as one chunk, already in memory, and is recorded as in step (c).
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
from mitm_proxy.http.body import PassthroughBody, StreamingBody
from mitm_proxy.http.flow import Flow
from mitm_proxy.http.framing import response_allows_body
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
_LINE_END = re.compile(rb"\r\n|\r|\n")  # an event stream's line ends (the WHATWG SSE format)
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


EVENT_STREAM = "text/event-stream"


def _encoding(headers: Headers) -> str:
    """The Content-Encoding, lower case; "" for none (or identity)."""
    encoding = (headers.get("content-encoding") or "").strip().lower()
    return "" if encoding == "identity" else encoding


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

    Observe-only (design 0012): its chunk hooks collect the bodies and never change them.

    Constructed with ``middleware.config.capture`` (a read-only mapping). ``clock`` is the wall
    clock (``time.time``), injectable for tests: it names the day's file and places the timings.
    """

    observe_only = True

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
        # Each matched flow's bodies, as the chunk hooks saw them: "request" and "response".
        self._bodies: weakref.WeakKeyDictionary[Flow, dict[str, _Collected]] = (
            weakref.WeakKeyDictionary()
        )
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

    async def on_request_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> None:
        self._collect(flow, "request", flow.request.headers, chunk, is_last)

    async def on_response_chunk(self, flow: Flow, chunk: bytes, is_last: bool) -> None:
        response = flow.response
        if response is not None:
            self._collect(flow, "response", response.headers, chunk, is_last)

    def _collect(self, flow: Flow, which: str, headers: Headers, chunk: bytes, last: bool) -> None:
        if flow not in self._matched:  # pragma: no cover - chunk hooks follow applies_to
            return
        bodies = self._bodies.setdefault(flow, {})
        collected = bodies.get(which)
        if collected is None:
            collected = bodies[which] = _Collected(self, headers)
        collected.add(chunk, last)

    async def on_done(self, flow: Flow) -> None:
        rule = self._matched.pop(flow, None)
        bodies = self._bodies.pop(flow, {})
        if rule is None:  # pragma: no cover - on_done follows applies_to
            return
        try:
            wall = self._clock()
            record = await self.record(flow, rule, wall, bodies)
            line = (json.dumps(record, ensure_ascii=False) + "\n").encode()
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

    async def record(
        self,
        flow: Flow,
        rule: Rule,
        wall: float,
        bodies: Mapping[str, _Collected] | None = None,
    ) -> dict[str, Any]:
        """The flow as capture writes it; wall is now on the wall clock, bodies what the chunk
        hooks collected ("request", "response")."""
        bodies = bodies or {}
        request, response, t = flow.request, flow.response, flow.timings
        offset = wall - time.monotonic()
        req: dict[str, Any] = {
            "method": request.method,
            "url": self._url(f"{request.scheme}://{request.authority}{request.path}"),
            "headers": self._headers(request.headers),
        }
        if request.trailers is not None:
            req["trailers"] = self._headers(request.trailers)
        req.update(await self._body(bodies.get("request"), request.body, request.headers))
        resp: dict[str, Any] | None = None
        if response is not None:
            resp = {"status": response.status, "headers": self._headers(response.headers)}
            if response.trailers is not None:
                resp["trailers"] = self._headers(response.trailers)
            empty = not response_allows_body(request.method, response.status)
            resp.update(
                await self._body(bodies.get("response"), response.body, response.headers, empty)
            )
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
            # The response went to the client chunk by chunk, unbuffered (design 0012).
            "streamed": response is not None and isinstance(response.body, StreamingBody),
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

    async def _body(
        self,
        collected: _Collected | None,
        body: object,
        headers: Headers,
        no_body: bool = False,
    ) -> dict[str, Any]:
        """The body's fields: body, and body_encoding, content_decoded, truncated, omitted,
        incomplete. From what the chunk hooks collected, else from the flow's body."""
        if collected is not None:
            return collected.fields(headers)
        if isinstance(body, StreamingBody):
            # Streamed, and no chunk hook saw it: a response that can't have one, or a body
            # that was never read (a deny on the head, an upstream that failed first).
            if no_body:
                return self._fields(b"", headers)
            return {"body": None, "omitted": "incomplete"}
        if isinstance(body, PassthroughBody):
            # Over body.max_buffer_bytes: the hooks were skipped and nothing was buffered.
            return {"body": None, "omitted": "oversize"}
        try:
            data: bytes = await body.read_all()  # type: ignore[attr-defined]  # a BodyStream
        except RuntimeError:  # pragma: no cover - only a passthrough body refuses
            return {"body": None, "omitted": "oversize"}
        return self._fields(data, headers)

    def _mode(self, headers: Headers) -> str:
        """How a streamed body is collected: "whole" (it is decoded or redacted as one),
        "events" (redacted line by line) or "prefix" (only max_body_bytes are kept)."""
        encoding = _encoding(headers)
        if encoding in ("gzip", "deflate"):
            return "whole"
        if encoding or not self.json_paths:
            return "prefix"
        media_type = _media_type(headers)
        if _is_json(media_type):
            return "whole"
        return "events" if media_type == EVENT_STREAM else "prefix"

    def _fields(self, data: bytes, headers: Headers) -> dict[str, Any]:
        """A whole body's fields: decoded, redacted, then cut and encoded."""
        fields: dict[str, Any] = {}
        encoding = _encoding(headers)
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
        if self.json_paths and (decoded or not encoding):
            if _is_json(media_type):
                data = self._redact_json(data)
            elif media_type == EVENT_STREAM:
                data = _Events(self).feed(data, final=True)
            # Any other body is stored with json_paths skipped (the wiki says so).
        fields.update(self._encoded(data, len(data), bool(encoding) and not decoded))
        return {"body": fields.pop("body"), **fields}

    def _encoded(
        self, data: bytes, original: int, binary: bool, partial: bool = False
    ) -> dict[str, Any]:
        """The body, cut to max_body_bytes, as text or base64; original is its whole length,
        partial that it stopped before its end (so it may end inside a character too)."""
        fields: dict[str, Any] = {}
        truncated = original > self.max_body_bytes
        if len(data) > self.max_body_bytes:
            data = data[: self.max_body_bytes]
        text: str | None = None
        if not binary:
            try:
                # A cut can split a character: the incomplete tail is dropped, not the whole text.
                text = codecs.getincrementaldecoder("utf-8")().decode(
                    data, final=not (truncated or partial)
                )
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

    def _redact_event_line(self, line: bytes) -> bytes:
        """One event-stream line, without its line end: a data: line whose value parses as
        JSON gets json_paths; every other line is kept as it is."""
        if not line.startswith(b"data:"):
            return line
        # "data:" and the one optional space the format allows are kept as they came.
        prefix = b"data: " if line.startswith(b"data: ") else b"data:"
        value = line[len(prefix) :]
        redacted = self._redact_json(value)
        return line if redacted is value else prefix + redacted


class _Events:
    """Event-stream redaction as the lines arrive: complete lines go out redacted, and the
    line still arriving waits, in parts. Fed the whole body with final=True, it is the same
    redaction a buffered body gets."""

    def __init__(self, capture: Capture) -> None:
        self.capture = capture
        self._parts: list[bytes] = []  # the line still arriving
        self.pending = 0  # its length so far

    def clear(self) -> None:
        self._parts.clear()
        self.pending = 0

    def feed(self, chunk: bytes, final: bool) -> bytes:
        held_cr = bool(self._parts) and self._parts[-1].endswith(b"\r")
        if not final and not held_cr and b"\n" not in chunk and b"\r" not in chunk:
            # No line ends here: keep the part, without copying what came before (a long
            # line in many small chunks would otherwise be copied again with each one).
            if chunk:
                self._parts.append(chunk)
                self.pending += len(chunk)
            return b""
        data = b"".join([*self._parts, chunk]) if self._parts else chunk
        self.clear()
        out: list[bytes] = []
        pos = 0
        for m in _LINE_END.finditer(data):
            if not final and m.end() == len(data) and m.group() == b"\r":
                break  # a CR at the end may be half of a CRLF split across chunks
            out.append(self.capture._redact_event_line(data[pos : m.start()]) + m.group())
            pos = m.end()
        rest = data[pos:]
        if final and rest:
            out.append(self.capture._redact_event_line(rest))
        elif rest:
            self._parts.append(rest)
            self.pending = len(rest)
        return b"".join(out)


class _Collected:
    """One message's body as capture's chunk hooks saw it, kept within bounds (the module
    docstring). A body that arrives as one last chunk (a buffered flow's) is kept as it is."""

    def __init__(self, capture: Capture, headers: Headers) -> None:
        self.capture = capture
        self.mode = capture._mode(headers)
        self.binary = bool(_encoding(headers))  # "prefix" mode: an encoding not decoded
        self.whole: bytes | None = None
        self.kept: list[bytes] = []
        self.size = 0  # bytes in kept
        self.total = 0  # the body's length (after redaction, for an event stream)
        self.events = _Events(capture) if self.mode == "events" else None
        self.started = False
        self.complete = False
        self.oversize = False

    def add(self, chunk: bytes, is_last: bool) -> None:
        if self.complete:  # pragma: no cover - the pipeline sends one last call
            return
        first, self.started = not self.started, True
        if first and is_last:
            self.whole, self.complete = chunk, True
            return
        if self.oversize:
            pass
        elif self.events is not None:
            self._keep(self.events.feed(chunk, is_last))
            if self.events.pending > MAX_DECODED_BYTES:
                self._drop()  # one line that never ends: it can't be redacted, so not stored
        elif self.mode == "whole":
            self.total += len(chunk)
            if self.total > MAX_DECODED_BYTES:
                self._drop()
            elif chunk:
                self.kept.append(chunk)
        else:
            self._keep(chunk)
        self.complete = is_last

    def _keep(self, data: bytes) -> None:
        self.total += len(data)
        room = self.capture.max_body_bytes - self.size
        if room > 0 and data:
            piece = data[:room]
            self.kept.append(piece)
            self.size += len(piece)

    def _drop(self) -> None:
        self.oversize = True
        self.kept.clear()
        if self.events is not None:
            self.events.clear()

    def fields(self, headers: Headers) -> dict[str, Any]:
        if self.whole is not None:
            return self.capture._fields(self.whole, headers)
        if self.oversize:
            return {"body": None, "omitted": "oversize"}
        data = b"".join(self.kept)
        if self.mode == "whole":
            if not self.complete:
                # Cut off before its end, it can't be decoded or redacted: not stored.
                return {"body": None, "omitted": "incomplete"}
            return self.capture._fields(data, headers)
        # An event stream's line still arriving when it was cut off was never redacted, so it
        # isn't kept (``pending``); every complete line was.
        fields = self.capture._encoded(data, self.total, self.binary, not self.complete)
        if not self.complete:
            fields["incomplete"] = True
        return fields


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
