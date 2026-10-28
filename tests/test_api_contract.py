"""Check the wire dataclasses against the vendored ``/api/v1`` schema snapshot.

``contracts/api-v1.openapi.json`` is a copy of the server's published OpenAPI schema. Every
``# MIRROR: <server path>::<Model>`` comment under ``src/pepper_cli/`` names the server model a
dataclass follows. This test resolves each one to a schema in the snapshot and asserts that
every field of the annotated dataclass is a property of that schema (the server may have more
fields; the CLI tolerates them, but it must never read one the server does not send).

Every ``MIRROR:`` comment must be either checked or listed in an allowlist below, and every
allowlist entry must still be needed, so the allowlists cannot rot silently.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "pepper_cli"
SNAPSHOT = ROOT / "contracts" / "api-v1.openapi.json"

# A MIRROR comment: ``# MIRROR: <server file>[::<Name>[ / <Name>...]]``. Matched anywhere in a
# comment line, so ``# Groups (MIRROR: src/pepper/api/groups.py)`` is seen (and must be
# allowlisted as prose) rather than slipping past.
_MIRROR_RE = re.compile(
    r"#.*?\bMIRROR:\s*(?P<path>[\w/.]+\.py)(?:::(?P<name>\w+)(?P<more>(?:\s*/\s*\w+)*))?"
)

# Any comment that mentions the marker at all. A comment this matches but ``_MIRROR_RE`` does not
# (no ``.py`` path, a missing colon) is malformed and fails the suite: it would otherwise be
# skipped by the scan and never checked.
_MARKER_RE = re.compile(r"#.*\bMIRROR\b")

_JSON = ("application/json",)
_SSE = ("text/event-stream",)


@dataclass(frozen=True)
class NotCheckable:
    """Why a MIRROR comment is exempt, and the route that keeps the exemption honest.

    ``route`` is the ``(METHOD, path)`` the comment's dataclass is read from, spelled as the
    snapshot's ``paths`` key. While the route's 200 response declares no schema under any of
    ``media``, there is nothing to check the dataclass against. Once the server declares one,
    ``test_exempt_routes_are_still_untyped`` fails so the exemption is replaced by a real
    ``::Model`` mapping instead of silently leaving the dataclass unchecked. ``route`` is
    ``None`` only for a comment that names no model (module prose) or annotates no dataclass
    (a constant).
    """

    reason: str
    route: tuple[str, str] | None = None
    media: tuple[str, ...] = _JSON


_UNTYPED = (
    "Its response is an untyped dict and the snapshot declares no schema for it (until the "
    "server declares response models), so there is nothing to resolve."
)

# MIRROR comments that do not map a dataclass to a snapshot schema. Keyed by
# (file under src/pepper_cli/, the ``path[::Name]`` the comment names). Each must really need
# the exemption: it is an error if the comment would in fact be checked, or if its route has
# since gained a response schema.
NOT_CHECKABLE: dict[tuple[str, str], NotCheckable] = {
    ("api_client.py", "src/pepper/api/groups.py"): NotCheckable(
        "Prose: a section header and a method-block comment about the whole groups route "
        "module. No model is named, so there is nothing to resolve."
    ),
    ("api_client.py", "src/pepper/web/auth/device_router.py::device_poll"): NotCheckable(
        "Names a route function, not a model. The poll response is an untyped dict "
        "(pending/denied/approved/...) with no schema in the snapshot, and DevicePoll also "
        "folds in a client-synthesised 'slow_down' state from an HTTP 429.",
        route=("POST", "/auth/device/poll"),
    ),
    ("api_client.py", "src/pepper/api/groups.py::room_topics"): NotCheckable(
        f"Route handler behind GroupTopicSummary. {_UNTYPED}",
        route=("GET", "/api/v1/groups/{group_id}/topics"),
    ),
    ("api_client.py", "src/pepper/api/groups.py::list_groups"): NotCheckable(
        f"Route handler behind GroupSummary and GroupList. {_UNTYPED}",
        route=("GET", "/api/v1/groups"),
    ),
    ("api_client.py", "src/pepper/api/groups.py::topic_messages"): NotCheckable(
        f"Route handler behind GroupCard, GroupMessage and GroupMessagePage. {_UNTYPED}",
        route=("GET", "/api/v1/groups/{group_id}/topics/{topic_id}/messages"),
    ),
    ("api_client.py", "src/pepper/api/groups.py::room_roster"): NotCheckable(
        f"Route handler behind GroupRosterMember. {_UNTYPED}",
        route=("GET", "/api/v1/groups/{group_id}/roster"),
    ),
    ("api_client.py", "src/pepper/api/groups.py::person_unread"): NotCheckable(
        "Route handler behind GroupUnreadRow and GroupsUnread (via ``get_groups_unread``) "
        "(``{topics: [{group_id, topic_id, read_through, floor, unread_count, "
        "has_unread_mention}], unread_total}``). "
        f"{_UNTYPED}",
        route=("GET", "/api/v1/groups/unread"),
    ),
    ("api_client.py", "src/pepper/api/groups.py::app_launcher"): NotCheckable(
        f"Route handler behind GroupAppTile. {_UNTYPED}",
        route=("GET", "/api/v1/groups/{group_id}/apps/launcher"),
    ),
    ("api_client.py", "src/pepper/api/groups.py::agent_events_stream"): NotCheckable(
        "Names the agent event-feed SSE route (FeedFrame). The snapshot lists the route with "
        "an empty response schema and does not describe the stream frames.",
        route=("GET", "/api/v1/groups/events/stream"),
        media=_JSON + _SSE,
    ),
    ("api_client.py", "src/pepper/api/groups.py::room_events_stream"): NotCheckable(
        "Names the room-scoped event-feed SSE route (FeedFrame). The snapshot lists the route "
        "with an empty response schema and does not describe the stream frames.",
        route=("GET", "/api/v1/groups/{group_id}/events/stream"),
        media=_JSON + _SSE,
    ),
    ("api_client.py", "src/pepper/api/groups.py::topic_events_stream"): NotCheckable(
        "Names the topic-scoped event-feed SSE route (FeedFrame). The snapshot lists the route "
        "with an empty response schema and does not describe the stream frames.",
        route=("GET", "/api/v1/groups/{group_id}/topics/{topic_id}/events/stream"),
        media=_JSON + _SSE,
    ),
    (
        "api_client.py",
        "src/pepper/gateway/channels/groups_service/agent_feed.py::scope_frame",
    ): NotCheckable(
        "Names the function that builds the scoped stream's first frame (WatchScope). It is "
        "an SSE frame on the scoped routes, which the snapshot does not describe.",
        route=("GET", "/api/v1/groups/{group_id}/events/stream"),
        media=_JSON + _SSE,
    ),
    ("api_client.py", "src/pepper/api/router.py::chat_send"): NotCheckable(
        "Names the chat route whose SSE path emits StreamEvent frames. The snapshot describes "
        "only the JSON reply (ChatSendResponse, mirrored on ChatReply), not the stream "
        "frames, so only the event-stream media type is guarded.",
        route=("POST", "/api/v1/chat/send"),
        media=_SSE,
    ),
    ("api_client.py", "src/pepper/api/router.py::list_room_todos"): NotCheckable(
        "Route handler behind RoomTodos and RoomTodosList (``{rooms: [{group_id, name, items, "
        "notes}], degraded}``, built by ``room_todos_response``). The snapshot declares the "
        "answer only as an open object (``additionalProperties: true``, no properties to "
        "check against), and its items are stored documents, not TodoEntry rows.",
    ),
    ("api_client.py", "src/pepper/agent/artifacts/applet.py::read_detail"): NotCheckable(
        "Names the function behind the artifact detail route (ArtifactDetail), which answers "
        f"a bare JSONResponse. {_UNTYPED}",
        route=("GET", "/api/v1/artifacts/{artifact_id}/detail"),
    ),
    ("cli.py", "src/pepper/agent/artifacts/service.py::INLINE_TEXT"): NotCheckable(
        "Mirrors a string constant (the artifact kind), not a model, and annotates no dataclass."
    ),
}

# Dataclass fields that are not properties of the mirrored schema, and are not mismatches.
# Keyed by (dataclass name, field name). Each must be a real field that the schema really
# lacks: it is an error if the field is gone or the schema grew it.
# A genuine mismatch (a CLI field the server does not send) belongs here too, but its reason
# must start with "KNOWN MISMATCH".
FIELD_EXCEPTIONS: dict[tuple[str, str], str] = {
    ("RoomSkillDetail", "row"): (
        "Client-composed: the server returns the skill-row fields flattened beside 'skill'; "
        "the CLI groups those into a RoomSkill under 'row'. The flattened fields are checked "
        "through RoomSkill, which mirrors RoomSkillRow."
    ),
}


@dataclass(frozen=True)
class Mirror:
    file: str  # relative to src/pepper_cli/
    line: int
    path: str
    name: str | None
    extra_names: tuple[str, ...]
    cls: str | None  # the class the comment annotates, if any

    @property
    def target(self) -> str:
        return f"{self.path}::{self.name}" if self.name else self.path

    @property
    def where(self) -> str:
        return f"{self.file}:{self.line}"


def _load_snapshot() -> dict:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def _load_schemas() -> dict[str, dict]:
    return _load_snapshot()["components"]["schemas"]


def typed_route_problems(paths: dict[str, dict], exemption: NotCheckable) -> list[str]:
    """Why ``exemption``'s route no longer justifies it: gone, or now has a response schema.

    An empty schema (``{}``) or a media type the route does not list counts as untyped.
    """
    if exemption.route is None:
        return []
    method, path = exemption.route
    operation = paths.get(path, {}).get(method.lower())
    if operation is None:
        return [f"{method} {path} is not in the snapshot; fix the route or drop the exemption"]
    content = operation.get("responses", {}).get("200", {}).get("content", {})
    return [
        f"{method} {path} now declares a response schema under {media} ({schema}); replace the "
        "NOT_CHECKABLE entry with a ``# MIRROR: <path>::<Model>`` mapping to it"
        for media in exemption.media
        if (schema := content.get(media, {}).get("schema", {}))
    ]


def resolve_schema(schemas: dict[str, dict], name: str) -> dict | None:
    """Find ``name`` in the snapshot.

    The server qualifies a schema name with its module path (``pkg__mod__Name``) when two
    models share a bare name; accept that form only when it is unambiguous.
    """
    if name in schemas:
        return schemas[name]
    qualified = [v for k, v in schemas.items() if k.endswith(f"__{name}")]
    return qualified[0] if len(qualified) == 1 else None


def _annotated_class(tree: ast.Module, lines: list[str], lineno: int) -> str | None:
    """The class defined right after the comment block containing line ``lineno``."""
    idx = lineno  # 0-based index of the line after the comment
    while idx < len(lines) and (not lines[idx].strip() or lines[idx].lstrip().startswith("#")):
        idx += 1
    first_code = idx + 1
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            if start == first_code:
                return node.name
    return None


def malformed_markers(lines: list[str]) -> list[int]:
    """1-based numbers of the lines that mention the marker but do not parse as a MIRROR."""
    return [
        i
        for i, line in enumerate(lines, 1)
        if _MARKER_RE.search(line) and not _MIRROR_RE.search(line)
    ]


def discover_mirrors() -> list[Mirror]:
    found = []
    for py in sorted(SRC.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        lines = text.splitlines()
        tree = ast.parse(text)
        for i, line in enumerate(lines, 1):
            m = _MIRROR_RE.search(line)
            if not m:
                continue
            extra = tuple(re.findall(r"\w+", m.group("more") or ""))
            found.append(
                Mirror(
                    file=str(py.relative_to(SRC)),
                    line=i,
                    path=m.group("path"),
                    name=m.group("name"),
                    extra_names=extra,
                    cls=_annotated_class(tree, lines, i),
                )
            )
    return found


def _dataclass_fields(file: str, cls: str) -> list[str] | None:
    module = importlib.import_module("pepper_cli." + file.removesuffix(".py").replace("/", "."))
    obj = getattr(module, cls, None)
    if obj is None or not dataclasses.is_dataclass(obj):
        return None
    return [f.name for f in dataclasses.fields(obj)]


def _checks(mirror: Mirror) -> list[tuple[str, str, str]]:
    """The (dataclass, schema name, where) pairs this comment asks us to verify."""
    pairs = []
    if mirror.name and mirror.cls:
        pairs.append((mirror.cls, mirror.name))
    # ``Foo / Bar``: the comment also covers a sibling dataclass named after the extra schema.
    pairs.extend((extra, extra) for extra in mirror.extra_names)
    return [(cls, schema, mirror.where) for cls, schema in pairs]


def _is_checkable(mirror: Mirror, schemas: dict[str, dict]) -> bool:
    if not (mirror.name and mirror.cls):
        return False
    return (
        resolve_schema(schemas, mirror.name) is not None
        and _dataclass_fields(mirror.file, mirror.cls) is not None
    )


MIRRORS = discover_mirrors()


def test_mirrors_are_discovered():
    # Guard against the scan silently finding nothing (a moved directory, a changed marker).
    assert len(MIRRORS) >= 20


def test_every_marker_parses():
    problems = [
        f"{py.relative_to(SRC)}:{n}: mentions MIRROR but is not ``# MIRROR: <path>.py[::Model]``"
        for py in sorted(SRC.rglob("*.py"))
        for n in malformed_markers(py.read_text(encoding="utf-8").splitlines())
    ]
    assert not problems, "\n".join(problems)


def test_malformed_markers_are_reported():
    lines = [
        "# MIRROR: src/pepper/api/models.py::Good",
        "# MIRROR: BareModelName",
        "# MIRROR src/pepper/api/models.py::NoColon",
        "x = 1  # mirror: lower case is not the marker",
        "# Groups (MIRROR: src/pepper/api/groups.py)",
    ]
    assert malformed_markers(lines) == [2, 3]


def test_snapshot_has_schemas():
    assert SNAPSHOT.is_file()
    assert _load_schemas()


def test_every_mirror_is_checked_or_allowlisted():
    schemas = _load_schemas()
    problems = []
    for m in MIRRORS:
        allowlisted = (m.file, m.target) in NOT_CHECKABLE
        checkable = _is_checkable(m, schemas)
        if not allowlisted and not checkable:
            problems.append(
                f"{m.where}: MIRROR {m.target!r} is neither checkable (needs ``::Model`` in the "
                "snapshot, directly above a dataclass) nor listed in NOT_CHECKABLE"
            )
        if allowlisted and checkable:
            problems.append(
                f"{m.where}: MIRROR {m.target!r} is in NOT_CHECKABLE but is now checkable; "
                "remove the allowlist entry"
            )
    assert not problems, "\n".join(problems)


def test_not_checkable_entries_are_all_used():
    seen = {(m.file, m.target) for m in MIRRORS}
    stale = sorted(set(NOT_CHECKABLE) - seen)
    assert not stale, f"NOT_CHECKABLE entries match no MIRROR comment: {stale}"


def test_exemptions_on_a_dataclass_name_a_route():
    # An exemption that leaves a dataclass unchecked must name the route it is read from, so
    # the route guard below notices when that route gains a response schema. Module prose
    # (no ``::Name``) names nothing to guard.
    unguarded = sorted(
        m.where
        for m in MIRRORS
        if (exemption := NOT_CHECKABLE.get((m.file, m.target))) is not None
        and exemption.route is None
        and m.name is not None
        and m.cls is not None
        and _dataclass_fields(m.file, m.cls) is not None
    )
    assert not unguarded, f"NOT_CHECKABLE entries above a dataclass need a route: {unguarded}"


@pytest.mark.parametrize("key", sorted(k for k, v in NOT_CHECKABLE.items() if v.route is not None))
def test_exempt_routes_are_still_untyped(key):
    problems = typed_route_problems(_load_snapshot()["paths"], NOT_CHECKABLE[key])
    assert not problems, "\n".join(problems)


def _route(method: str, media: str, schema: dict) -> dict:
    return {method: {"responses": {"200": {"content": {media: {"schema": schema}}}}}}


def test_typed_route_problems_reports_a_new_response_model():
    paths = {
        "/typed": _route("get", "application/json", {"$ref": "#/components/schemas/Typed"}),
        "/untyped": _route("get", "application/json", {}),
        "/stream": _route("post", "text/event-stream", {"$ref": "#/components/schemas/Frame"}),
    }
    sse = ("text/event-stream",)
    assert typed_route_problems(paths, NotCheckable("r", ("GET", "/untyped"))) == []
    assert typed_route_problems(paths, NotCheckable("r", None)) == []
    typed = typed_route_problems(paths, NotCheckable("r", ("GET", "/typed")))
    assert len(typed) == 1
    assert "GET /typed now declares" in typed[0]
    # A media type the route does not declare is untyped; the one it does is caught.
    assert typed_route_problems(paths, NotCheckable("r", ("GET", "/typed"), sse)) == []
    assert len(typed_route_problems(paths, NotCheckable("r", ("POST", "/stream"), sse))) == 1
    gone = typed_route_problems(paths, NotCheckable("r", ("GET", "/gone")))
    assert gone == ["GET /gone is not in the snapshot; fix the route or drop the exemption"]


def test_dataclass_fields_exist_in_schema():
    schemas = _load_schemas()
    problems = []
    used_exceptions: set[tuple[str, str]] = set()
    for mirror in MIRRORS:
        if (mirror.file, mirror.target) in NOT_CHECKABLE:
            continue
        for cls, schema_name, where in _checks(mirror):
            schema = resolve_schema(schemas, schema_name)
            fields = _dataclass_fields(mirror.file, cls)
            if schema is None or fields is None:
                problems.append(f"{where}: cannot check {cls} against {schema_name}")
                continue
            props = set(schema.get("properties", {}))
            for fname in fields:
                if fname in props:
                    if (cls, fname) in FIELD_EXCEPTIONS:
                        problems.append(
                            f"FIELD_EXCEPTIONS[{cls}.{fname}] is stale: {schema_name} now has it"
                        )
                    continue
                if (cls, fname) in FIELD_EXCEPTIONS:
                    used_exceptions.add((cls, fname))
                else:
                    problems.append(
                        f"{where}: {cls}.{fname} is not a property of schema {schema_name}"
                    )
    stale = sorted(set(FIELD_EXCEPTIONS) - used_exceptions)
    problems.extend(f"FIELD_EXCEPTIONS[{c}.{f}] matches no mirrored field" for c, f in stale)
    assert not problems, "\n".join(problems)


def test_resolve_schema_handles_qualified_names():
    schemas = {"Plain": {"n": 1}, "pkg__mod__Dup": {"n": 2}, "a__Amb": {}, "b__Amb": {}}
    assert resolve_schema(schemas, "Plain") == {"n": 1}
    assert resolve_schema(schemas, "Dup") == {"n": 2}
    assert resolve_schema(schemas, "Amb") is None  # ambiguous: refuse to guess
    assert resolve_schema(schemas, "Missing") is None


@pytest.mark.parametrize("key", sorted(FIELD_EXCEPTIONS) + sorted(NOT_CHECKABLE))
def test_allowlist_reasons_are_written(key):
    reasons = {**FIELD_EXCEPTIONS, **{k: v.reason for k, v in NOT_CHECKABLE.items()}}
    reason = reasons[key]
    assert len(reason) > 20
