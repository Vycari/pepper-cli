"""``artifacts`` and ``preferences`` against a mocked server (writes are mocks only)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import pytest

from pepper_cli import (
    api_client,
    artifacts_view,
    cli,
    credentials,
    feed,
    following_search,
    numbered_view,
    preferences_view,
)
from pepper_cli.numbered_view import NumberedView, RefError
from pepper_cli.secret import SecretStr

NOTE = "00000000-0000-0000-0000-0000000000e1"
FILE = "00000000-0000-0000-0000-0000000000e2"
ROOM = "00000000-0000-0000-0000-0000000000a1"


def _item(id_: str, name: str, kind: str, **over):
    return {
        "id": id_,
        "name": name,
        "kind": kind,
        "updated_at": "2026-10-01T12:00:00+00:00",
        "etag": "e",
        "labels": [],
        "summary": None,
        **over,
    }


PREFS = {
    "timezones": ["UTC"],
    "maps": True,
    "has_location": True,
    "location": "Atherton, CA",
    "timezone": "America/Los_Angeles",
    "work_location": None,
    "show_weather": True,
    "show_commute": True,
    "show_precip_amount": True,
    "show_humidity": True,
    "show_wind": True,
    "show_uv": True,
    "show_severe": True,
    "work_days": [0, 1, 2, 3, 4],
    "work_start": "09:00",
    "work_end": "17:30",
    "meals": {"lunch": {"start": "12:00", "minutes": 30}},
    "workout": {"at": "07:30", "minutes": 45},
    "run_minutes": 90,
    "break_minutes": 15,
}


class _Calls(list):
    def to(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self if r.method == method and r.url.path == path]


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("PEPPER_JSON", raising=False)
    monkeypatch.chdir(tmp_path)
    credentials.save(server_url="https://pepper.example.com", token=SecretStr("pep_pat_abc"))
    calls = _Calls()
    fail: dict[tuple[str, str], tuple[int, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        key = (request.method, request.url.path)
        if key in fail:
            status, detail = fail[key]
            return httpx.Response(status, json={"detail": detail})
        if key == ("GET", "/api/v1/artifacts"):
            return httpx.Response(
                200,
                json={
                    "truncated": True,
                    "items": [
                        _item(NOTE, "Trip plan", "inline_text", labels=["travel"]),
                        _item(FILE, "Receipt", "blob"),
                    ],
                },
            )
        if key == ("GET", f"/api/v1/artifacts/{NOTE}/detail"):
            return httpx.Response(
                200,
                json={
                    **_item(NOTE, "Trip plan", "inline_text"),
                    "shared": False,
                    "body": "# Plan\n\n- [ ] book",
                    "body_html": "<h1>Plan</h1>",
                    "mime_type": "text/plain",
                },
            )
        if key == ("GET", f"/api/v1/artifacts/{FILE}/detail"):
            return httpx.Response(
                200,
                json={
                    **_item(FILE, "Receipt", "blob"),
                    "shared": False,
                    "body": None,
                    "mime_type": "image/png",
                    "size_label": "4 B",
                    "viewer": "image",
                },
            )
        if key == ("GET", f"/api/v1/artifacts/{FILE}/raw"):
            return httpx.Response(200, content=b"\x89PNG")
        if key == ("POST", "/api/v1/artifacts/upload"):
            return httpx.Response(
                200, json={"id": FILE, "name": "pic.png", "mime_type": "image/png", "byte_size": 4}
            )
        if key == ("POST", "/api/v1/apps/artifacts/actions/delete"):
            return httpx.Response(200, json={"result": {"name": "Receipt"}})
        if key == ("GET", "/api/v1/groups"):
            return httpx.Response(
                200,
                json={
                    "enabled": True,
                    "degraded": False,
                    "total": 1,
                    "archived_total": 0,
                    "groups": [
                        {
                            "group_id": ROOM,
                            "display_name": "Trip planning",
                            "color": "#fff",
                            "sub": "",
                            "member_count": 2,
                            "archived": False,
                            "topics": [],
                        }
                    ],
                },
            )
        if key == ("GET", "/api/v1/calendars/preferences"):
            return httpx.Response(200, json=PREFS)
        if key == ("POST", "/api/v1/calendars/preferences"):
            return httpx.Response(200, json={**PREFS, **json.loads(request.content)})
        return httpx.Response(404, json={"detail": "nope"})

    real = api_client.ApiClient

    def factory(**kwargs):
        return real(client=httpx.Client(transport=httpx.MockTransport(handler)), **kwargs)

    monkeypatch.setattr(api_client, "ApiClient", factory)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    calls.fail = fail  # type: ignore[attr-defined]
    return calls


def _json(capsys):
    return json.loads(capsys.readouterr().out)


def _err(capsys) -> dict:
    return json.loads(capsys.readouterr().err)["error"]


# ---------------------------------------------------------------------------
# artifacts


def test_artifacts_list_numbers_the_rows_and_filters(server, capsys) -> None:
    assert cli.main(["artifacts", "list", "--kind", "file", "--search", "rec", "--json"]) == 0
    body = _json(capsys)
    assert [i["n"] for i in body["items"]] == [1, 2]
    assert body["truncated"] is True and body["next_before"] is None
    params = server.to("GET", "/api/v1/artifacts")[0].url.params
    assert params["kind"] == "blob" and params["q"] == "rec" and "principal" not in params


def test_artifacts_list_human_and_room_scope(server, capsys) -> None:
    assert cli.main(["artifacts", "list", "--room", "trip"]) == 0
    out = capsys.readouterr().out
    assert "1  Note  Trip plan" in out and "[travel]" in out and "2  File  Receipt" in out
    assert "more artifacts than shown" in out
    assert server.to("GET", "/api/v1/artifacts")[0].url.params["principal"] == ROOM


def test_artifacts_list_sends_every_filter_to_the_server(server, capsys) -> None:
    argv = ["artifacts", "list", "--kind", "link", "--label", "travel", "--search", "tri"]
    assert cli.main([*argv, "--limit", "500", "--json"]) == 0
    capsys.readouterr()
    params = server.to("GET", "/api/v1/artifacts")[0].url.params
    # Server params, not client filters: the mock returns both rows whatever was asked, and the
    # CLI prints them both. The limit is not range-checked here; the server owns its cap.
    assert (params["kind"], params["label"], params["q"], params["limit"]) == (
        "external_url",
        "travel",
        "tri",
        "500",
    )


def test_artifacts_list_prints_what_the_server_returned(server, capsys) -> None:
    assert cli.main(["artifacts", "list", "--label", "nomatch", "--json"]) == 0
    assert [i["name"] for i in _json(capsys)["items"]] == ["Trip plan", "Receipt"]


def test_artifacts_show_follows_the_number_and_drops_the_html(server, capsys) -> None:
    cli.main(["artifacts", "list", "--json"])
    capsys.readouterr()
    assert cli.main(["artifacts", "show", "1", "--json"]) == 0
    body = _json(capsys)
    assert body["body"].startswith("# Plan") and "body_html" not in body
    assert cli.main(["artifacts", "show", "1"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Trip plan") and "- [ ] book" in out


def test_artifacts_show_a_file_says_how_to_get_it(server, capsys) -> None:
    assert cli.main(["artifacts", "show", FILE]) == 0
    out = capsys.readouterr().out
    assert "File, image/png, 4 B" in out and "artifacts download" in out


def test_artifacts_refs(server, capsys) -> None:
    assert cli.main(["artifacts", "show", "3", "--json"]) == 2
    assert _err(capsys)["code"] == "not_found"  # no listing yet
    assert cli.main(["artifacts", "show", "nonsense", "--json"]) == 2
    assert _err(capsys)["code"] == "not_found"
    assert not server.to("GET", f"/api/v1/artifacts/{FILE}/detail")
    cli.main(["artifacts", "list", "--json"])
    capsys.readouterr()
    assert cli.main(["artifacts", "show", "9", "--json"]) == 2
    assert "it listed 2" in _err(capsys)["message"]
    assert cli.main(["artifacts", "show", FILE[:20], "--json"]) == 2  # a prefix is not an id
    capsys.readouterr()


def test_artifacts_show_404_is_not_found(server, capsys) -> None:
    server.fail[("GET", f"/api/v1/artifacts/{FILE}/detail")] = (404, "not found")
    assert cli.main(["artifacts", "show", FILE, "--json"]) == 2
    assert _err(capsys)["code"] == "not_found"


def test_artifacts_download_writes_the_bytes(server, capsys, tmp_path) -> None:
    assert cli.main(["artifacts", "download", FILE, "--json"]) == 0
    body = _json(capsys)
    assert body["bytes"] == 4 and body["path"].endswith("Receipt.png")  # extension from the type
    assert (tmp_path / "Receipt.png").read_bytes() == b"\x89PNG"
    assert cli.main(["artifacts", "download", FILE, "--json"]) == 2  # no silent overwrite
    assert "--force" in _err(capsys)["message"]
    assert cli.main(["artifacts", "download", FILE, "--force", "--out", "x.bin", "--json"]) == 0
    assert (tmp_path / "x.bin").exists()
    (tmp_path / "d").mkdir()
    assert cli.main(["artifacts", "download", FILE, "--out", "d", "--json"]) == 0
    assert (tmp_path / "d" / "Receipt.png").exists()


def test_artifacts_download_leaves_a_sibling_part_file_alone(server, capsys, tmp_path) -> None:
    (tmp_path / "Receipt.png.part").write_bytes(b"someone else's partial download")
    assert cli.main(["artifacts", "download", FILE, "--json"]) == 0
    assert (tmp_path / "Receipt.png.part").read_bytes() == b"someone else's partial download"
    assert (tmp_path / "Receipt.png").read_bytes() == b"\x89PNG"
    assert cli.main(["artifacts", "download", FILE, "--force", "--json"]) == 0
    assert sorted(p.name for p in tmp_path.iterdir() if "Receipt" in p.name) == [
        "Receipt.png",
        "Receipt.png.part",
    ]  # --force's temp file is gone too


def test_write_download_never_replaces_a_file_that_appeared_after_the_check(tmp_path) -> None:
    # The exclusive create is the guard itself, so a file made between the CLI's exists()
    # check and the write (simulated here by making it first) is refused, not replaced.
    target = tmp_path / "report.pdf"
    target.write_bytes(b"theirs")
    with pytest.raises(FileExistsError):
        artifacts_view.write_download(target, b"mine", force=False)
    assert target.read_bytes() == b"theirs"
    artifacts_view.write_download(target, b"mine", force=True)
    assert target.read_bytes() == b"mine" and [p.name for p in tmp_path.iterdir()] == ["report.pdf"]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_write_download_does_not_follow_a_planted_link(tmp_path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"keep")
    (tmp_path / "report.pdf").symlink_to(victim)
    with pytest.raises(FileExistsError):
        artifacts_view.write_download(tmp_path / "report.pdf", b"mine", force=False)
    assert victim.read_bytes() == b"keep"


def test_write_download_removes_its_own_partial_file_on_failure(tmp_path, monkeypatch) -> None:
    target = tmp_path / "a.bin"

    class Boom:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def write(self, data):
            raise OSError(28, "No space left on device")

    real_open = Path.open

    def fake_open(self, mode="r", *a, **k):
        if self == target:
            real_open(self, mode).close()  # the exclusive create happened
            return Boom()
        return real_open(self, mode, *a, **k)

    monkeypatch.setattr(Path, "open", fake_open)
    with pytest.raises(OSError):
        artifacts_view.write_download(target, b"x", force=False)
    assert not target.exists()


def test_artifacts_download_stdout_and_json_do_not_mix(server, capsys) -> None:
    assert cli.main(["artifacts", "download", FILE, "--out", "-", "--json"]) == 2
    assert _err(capsys)["code"] == "usage"


def test_artifacts_download_refuses_a_note(server, capsys) -> None:
    assert cli.main(["artifacts", "download", NOTE, "--json"]) == 2
    assert "no file to download" in _err(capsys)["message"]
    assert not server.to("GET", f"/api/v1/artifacts/{NOTE}/raw")


def test_artifacts_create_uploads_multipart(server, capsys, tmp_path) -> None:
    (tmp_path / "pic.png").write_bytes(b"\x89PNG")
    argv = ["artifacts", "create", "--file", "pic.png", "--name", "Pic", "--label", "a"]
    assert cli.main([*argv, "--label", "b", "--room", "trip", "--json"]) == 0
    assert _json(capsys)["id"] == FILE
    req = server.to("POST", "/api/v1/artifacts/upload")[0]
    assert req.url.params["principal"] == ROOM
    assert b'filename="pic.png"' in req.content and b"image/png" in req.content
    assert b'name="labels"\r\n\r\na,b' in req.content and b'name="name"\r\n\r\nPic' in req.content


def test_artifacts_create_local_refusals(server, capsys, tmp_path) -> None:
    assert cli.main(["artifacts", "create", "--file", "missing.png", "--json"]) == 2
    (tmp_path / "empty.txt").write_bytes(b"")
    assert cli.main(["artifacts", "create", "--file", "empty.txt", "--json"]) == 2
    assert not server.to("POST", "/api/v1/artifacts/upload")
    capsys.readouterr()


def test_artifacts_create_explains_a_refused_type(server, capsys, tmp_path) -> None:
    (tmp_path / "a.docx").write_bytes(b"PK")
    server.fail[("POST", "/api/v1/artifacts/upload")] = (415, "that file type isn't supported")
    assert cli.main(["artifacts", "create", "--file", "a.docx"]) == 1
    err = capsys.readouterr().err
    assert "pictures, PDFs" in err


def test_artifacts_delete_needs_yes_and_uses_the_listings_room(server, capsys) -> None:
    cli.main(["artifacts", "list", "--room", "trip", "--json"])
    capsys.readouterr()
    assert cli.main(["artifacts", "delete", "2", "--json"]) == 2
    assert not server.to("POST", "/api/v1/apps/artifacts/actions/delete")
    capsys.readouterr()
    assert cli.main(["artifacts", "delete", "2", "--yes", "--json"]) == 0
    assert _json(capsys) == {"ok": True, "id": FILE}
    req = server.to("POST", "/api/v1/apps/artifacts/actions/delete")[0]
    assert req.url.params["principal"] == ROOM
    assert json.loads(req.content) == {"params": {"id": FILE}}


def test_artifacts_delete_a_422_is_not_found(server, capsys) -> None:
    server.fail[("POST", "/api/v1/apps/artifacts/actions/delete")] = (422, "gone")
    assert cli.main(["artifacts", "delete", FILE, "--yes", "--json"]) == 2
    assert _err(capsys)["code"] == "not_found"


def test_artifact_helpers() -> None:
    assert artifacts_view.KINDS == {"note": "inline_text", "link": "external_url", "file": "blob"}
    assert artifacts_view.kind_label("blob") == "File"
    assert artifacts_view.guess_mime("x.HEIC") == "image/heic"
    assert artifacts_view.guess_mime("a.m4a") == "audio/m4a"
    assert artifacts_view.guess_mime("a.unknownext") == "application/octet-stream"
    assert artifacts_view.safe_filename("../../etc/passwd") == "_.._etc_passwd"
    assert artifacts_view.safe_filename("  ") == "artifact"
    assert artifacts_view.safe_filename('Receipt? <1>: "a|b*"') == "Receipt_ _1_ _a_b_"
    assert artifacts_view.safe_filename("CON") == "_CON"
    assert artifacts_view.safe_filename("nul.txt") == "_nul.txt"
    assert artifacts_view.safe_filename("Console") == "Console"
    assert artifacts_view.download_name("Receipt?", "image/png") == "Receipt_.png"
    assert artifacts_view.download_name("Shot", "image/png") == "Shot.png"
    assert artifacts_view.download_name("a.pdf", "application/pdf") == "a.pdf"


def test_numbered_view_resolution(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    credentials.save(server_url="http://x", token=SecretStr("t"))
    view = NumberedView("v.json", "thing", "pepper things list")
    with pytest.raises(RefError):
        view.resolve("1")
    view.save([{"id": "abc1", "slug": "s1"}, {"id": "abd2", "slug": "s2"}], {"k": 1})
    assert view.resolve("2") == ("abd2", {"id": "abd2", "slug": "s2"}, {"k": 1})
    assert view.resolve("s1", alt_keys=("slug",))[0] == "abc1"
    assert view.resolve("abd")[0] == "abd2"
    with pytest.raises(RefError, match="more than one"):
        view.resolve("ab")
    assert view.resolve("other") == ("other", None, {})
    with pytest.raises(RefError, match="listed 2"):
        view.resolve("3")


def _refuse(*a, **k):
    raise PermissionError(13, "Permission denied")


def test_a_failed_listing_save_drops_the_old_numbers(server, capsys, monkeypatch) -> None:
    assert cli.main(["artifacts", "list", "--json"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(numbered_view.tempfile, "mkstemp", _refuse)
    assert cli.main(["artifacts", "list"]) == 0
    cap = capsys.readouterr()
    assert "Receipt" in cap.out and "won't work in later commands" in cap.err
    assert not artifacts_view.VIEW.state_path().exists()
    assert cli.main(["artifacts", "delete", "2", "--yes", "--json"]) == 2
    assert _err(capsys)["code"] == "not_found"
    assert not server.to("POST", "/api/v1/apps/artifacts/actions/delete")


def test_a_stale_listing_that_cannot_be_removed_is_not_shown(server, capsys, monkeypatch) -> None:
    assert cli.main(["artifacts", "list", "--json"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(numbered_view.tempfile, "mkstemp", _refuse)
    monkeypatch.setattr(Path, "unlink", _refuse)
    assert cli.main(["artifacts", "list"]) == 1
    cap = capsys.readouterr()
    assert cap.out == "" and "could not be removed" in cap.err


def test_listing_save_uses_a_private_unshared_temp_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    view = NumberedView("v.json", "thing", "pepper things list")
    names: list[str] = []
    real = numbered_view.tempfile.mkstemp

    def spy(**kw):
        fd, name = real(**kw)
        names.append(name)
        return fd, name

    monkeypatch.setattr(numbered_view.tempfile, "mkstemp", spy)
    assert view.save([{"id": "a"}]) is None and view.save([{"id": "b"}]) is None
    assert len(set(names)) == 2  # two writers never share one temp file
    assert [p.name for p in view.state_path().parent.iterdir() if p.suffix == ".tmp"] == []
    if os.name == "posix":
        assert view.state_path().stat().st_mode & 0o777 == 0o600


def test_logout_forgets_the_saved_listings(server, capsys) -> None:
    cli.main(["artifacts", "list", "--json"])
    following_search.save("q", [{"n": 1, "ref": "r"}])
    feed.VIEW.save([{"id": "x"}])
    paths = [
        artifacts_view.VIEW.state_path(),
        feed.VIEW.state_path(),
        following_search.state_path(),
    ]
    assert all(p.exists() for p in paths)
    assert cli.main(["logout"]) == 0
    assert not any(p.exists() for p in paths)
    assert cli.main(["logout"]) == 0  # nothing left to remove is fine


# ---------------------------------------------------------------------------
# preferences


def test_preferences_show_hides_the_page_helpers(server, capsys) -> None:
    assert cli.main(["preferences", "show", "--json"]) == 0
    body = _json(capsys)
    assert "timezones" not in body and "maps" not in body
    assert body["timezone"] == "America/Los_Angeles"
    assert cli.main(["preferences", "show"]) == 0
    out = capsys.readouterr().out
    assert "Timezone" in out and "America/Los_Angeles" in out and "Mon-Fri" in out
    assert "Lunch" in out and "12:00, 30 min" in out and "UV index" in out


def test_preferences_edit_sends_only_what_was_given(server, capsys) -> None:
    argv = ["preferences", "edit", "--timezone", "America/Chicago", "--work-days", "mon,wed-fri"]
    assert cli.main([*argv, "--hide", "wind,uv", "--show", "commute", "--json"]) == 0
    sent = json.loads(server.to("POST", "/api/v1/calendars/preferences")[0].content)
    assert sent == {
        "timezone": "America/Chicago",
        "work_days": [0, 2, 3, 4],
        "show_wind": False,
        "show_uv": False,
        "show_commute": True,
    }
    assert _json(capsys)["timezone"] == "America/Chicago"


def test_preferences_edit_fills_the_other_half_of_a_meal(server, capsys) -> None:
    assert cli.main(["preferences", "edit", "--meal", "lunch=12:45", "--json"]) == 0
    sent = json.loads(server.to("POST", "/api/v1/calendars/preferences")[0].content)
    assert sent == {"meals": {"lunch": {"start": "12:45", "minutes": 30}}}
    assert cli.main(["preferences", "edit", "--workout-minutes", "60", "--json"]) == 0
    sent = json.loads(server.to("POST", "/api/v1/calendars/preferences")[1].content)
    assert sent == {"workout": {"at": "07:30", "minutes": 60}}
    assert cli.main(["preferences", "edit", "--meal", "dinner=19:00", "--json"]) == 2
    assert "no stored time" in _err(capsys)["message"]  # nothing sent for a half-known meal


def test_preferences_edit_sends_an_explicit_zero(server, capsys) -> None:
    assert cli.main(["preferences", "edit", "--meal", "lunch=12:30,0", "--json"]) == 0
    sent = json.loads(server.to("POST", "/api/v1/calendars/preferences")[0].content)
    assert sent == {"meals": {"lunch": {"start": "12:30", "minutes": 0}}}
    assert cli.main(["preferences", "edit", "--workout-minutes", "0", "--json"]) == 0
    assert len(server.to("GET", "/api/v1/calendars/preferences")) == 2  # fetched for the time
    sent = json.loads(server.to("POST", "/api/v1/calendars/preferences")[1].content)
    assert sent == {"workout": {"at": "07:30", "minutes": 0}}


def test_preferences_edit_work_days_none_clears_them(server, capsys) -> None:
    assert cli.main(["preferences", "edit", "--work-days", "none", "--json"]) == 0
    sent = json.loads(server.to("POST", "/api/v1/calendars/preferences")[0].content)
    assert sent == {"work_days": []}


def test_preferences_edit_clears_an_address_and_refuses_nothing(server, capsys) -> None:
    assert cli.main(["preferences", "edit", "--home-address", "", "--json"]) == 0
    sent = json.loads(server.to("POST", "/api/v1/calendars/preferences")[0].content)
    assert sent == {"location": ""}
    capsys.readouterr()
    assert cli.main(["preferences", "edit", "--json"]) == 2
    assert "nothing to change" in _err(capsys)["message"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--work-days", "someday"],
        ["--show", "rain"],
        ["--show", "wind", "--hide", "wind"],
        ["--meal", "brunch=10:00"],
        ["--meal", "lunch="],
    ],
)
def test_preferences_edit_bad_values_are_usage_errors(server, capsys, argv) -> None:
    assert cli.main(["preferences", "edit", *argv, "--json"]) == 2
    assert _err(capsys)["code"] == "usage"
    assert not server.to("POST", "/api/v1/calendars/preferences")


def test_preferences_edit_sends_times_as_typed_and_lets_the_server_refuse(server, capsys) -> None:
    server.fail[("POST", "/api/v1/calendars/preferences")] = (
        422,
        "work_start and work_end must be HH:MM times, start before end",
    )
    assert cli.main(["preferences", "edit", "--work-start", "25:00", "--json"]) == 1
    sent = json.loads(server.to("POST", "/api/v1/calendars/preferences")[0].content)
    assert sent == {"work_start": "25:00"}
    assert "HH:MM" in _err(capsys)["message"]


def test_preferences_edit_surfaces_a_server_refusal(server, capsys) -> None:
    server.fail[("POST", "/api/v1/calendars/preferences")] = (422, "timezone: unknown timezone")
    assert cli.main(["preferences", "edit", "--timezone", "Mars/Base", "--json"]) == 1
    err = _err(capsys)
    assert err["code"] == "validation" and "unknown timezone" in err["message"]


def test_preference_parsers() -> None:
    assert preferences_view.parse_days("weekdays") == [0, 1, 2, 3, 4]
    assert preferences_view.parse_days("sat,sun") == [5, 6]
    assert preferences_view.parse_days("fri-mon") == [0, 4, 5, 6]
    assert preferences_view.parse_meal("lunch=,45") == ("lunch", None, 45)
