"""``pepper feed`` (the server's ranked list, numbered) and ``pepper feed N`` (detail,
``--transcript``, ``--read``) against a mocked server and a mocked web (synthetic data only)."""

from __future__ import annotations

import html
import json
from pathlib import Path

import httpx
import pytest

from pepper_cli import api_client, article_reader, cli, credentials, feed, numbered_view
from pepper_cli.secret import SecretStr

SITE = "feeds:feed:aaaaaaaa-1111-4111-8111-111111111111"
POD = "feeds:podcast:bbbbbbbb-2222-4222-8222-222222222222"
TEAM = "sports:team:ncaaf:252"
EP = "cccccccc-3333-4333-8333-333333333333"

LONG = "A long enough paragraph to survive the reader's short-line filter, for sure."


def _entry(title: str, ref: str = SITE, **kw) -> dict:
    return {
        "source": "Example News",
        "ref": ref,
        "group": "Reading",
        "kind": "feed",
        "title": title,
        "url": f"https://news.example/{title.replace(' ', '-')}",
        "when_label": "Today",
        "published_at": "2026-10-17T13:02:00Z",
        "starts_at": None,
        "summary": None,
        "new": True,
        "upcoming": False,
        "live": False,
        "more": [],
        "fold": None,
        **kw,
    }


def _doc() -> dict:
    third = _entry("Third", published_at=None)
    fourth = _entry("Fourth", published_at=None)
    return {
        "rev": 1,
        "data": {
            "items": [
                {
                    "ref": POD,
                    "kind": "podcast",
                    "group": "Podcasts",
                    "name": "The Show",
                    "recent": {
                        "items": [
                            {
                                "title": "Ep 1",
                                "url": None,
                                "episode": {"id": EP, "show_title": "The Show"},
                            }
                        ]
                    },
                }
            ],
            "counts": {"by_group": {"Reading": 1}},
            # a deliberately odd order: the CLI must show it as sent
            "feed": [
                _entry(
                    "Notre Dame at BYU",
                    TEAM,
                    source="BYU Cougars",
                    group="Sports",
                    kind="team",
                    url=None,
                    when_label="Tomorrow 4:30 PM",
                    published_at=None,
                    starts_at="2026-10-17T23:30:00Z",
                    new=False,
                    upcoming=True,
                    fold="+ next: Oct 23 BYU at UCF",
                    league="NCAAF",
                    records="ND 5-1 · BYU 6-0",
                    headline="Two unbeatens meet in Provo.",
                    note="Ranked matchup",
                    status="4:30 PM",
                ),
                _entry(
                    "Rock &#038; Roll",
                    summary="Fish &amp; chips",
                    more=[
                        {k: v for k, v in third.items() if k not in ("more", "fold")},
                        {k: v for k, v in fourth.items() if k not in ("more", "fold")},
                    ],
                    fold="+2 more",
                ),
                _entry(
                    "Ep 1",
                    POD,
                    source="The Show",
                    group="Podcasts",
                    kind="podcast",
                    url=None,
                    episode_id=EP,
                ),
            ],
        },
    }


PAGE = (
    "<html><head><meta property='og:title' content='Rock Page'>"
    "<meta name='author' content='Ann Author'></head><body><article>"
    + "".join(f"<p>{LONG} #{i}</p>" for i in range(1, 16))
    + "</article></body></html>"
)


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("PEPPER_JSON", raising=False)
    credentials.save(server_url="http://pepper.test", token=SecretStr("pep_pat_abc"))
    routes: dict[str, httpx.Response] = {
        "/api/v1/whoami": httpx.Response(
            200, json={"user_id": "u", "display_name": "U", "timezone": "America/Los_Angeles"}
        ),
        "/api/v1/apps/following/data": httpx.Response(200, json=_doc()),
        "/api/v1/apps/following/actions/episode_transcript": httpx.Response(
            200,
            json={
                "result": {
                    "ok": True,
                    "message": "Saved.",
                    "item_id": EP,
                    "kind": "transcript",
                    "status": "completed",
                    "text": "Welcome to the show. " * 30,
                    "note": None,
                }
            },
        ),
    }
    seen: list[httpx.Request] = []
    web_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return routes.get(request.url.path, httpx.Response(404, json={}))

    def web(request: httpx.Request) -> httpx.Response:
        web_seen.append(request)
        return web_routes.get(request.url.host, httpx.Response(404))(request)

    web_routes: dict = {
        "news.example": lambda r: httpx.Response(
            200, text=PAGE, headers={"content-type": "text/html"}
        )
    }
    real = api_client.ApiClient
    monkeypatch.setattr(
        api_client,
        "ApiClient",
        lambda **kw: real(client=httpx.Client(transport=httpx.MockTransport(handler)), **kw),
    )
    monkeypatch.setattr(article_reader, "_transport", httpx.MockTransport(web))
    monkeypatch.setattr(article_reader, "_resolve", lambda host: ["93.184.215.14"])
    return type(
        "Server", (), {"routes": routes, "seen": seen, "web": web_seen, "web_routes": web_routes}
    )


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    rc = cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def _run(capsys, *argv: str) -> tuple[int, str, str]:
    rc = cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def _numbers(capsys) -> dict[str, int]:
    """``{title: n}`` from ``pepper feed --json``."""
    rc, out, _ = _run(capsys, "feed", "--json")
    assert rc == 0
    return {html.unescape(i["title"]): i["n"] for i in json.loads(out)["items"]}


# --- the list: the server's order, numbered ------------------------------------


def test_feed_lists_the_servers_order_numbered_in_text_and_json(server, capsys) -> None:
    rc, out, _ = _run(capsys, "feed", "--json")
    items = json.loads(out)["items"]
    assert rc == 0 and [i["n"] for i in items] == [1, 2, 3]
    assert [i["ref"] for i in items] == [TEAM, SITE, POD]  # as sent, not re-ranked
    assert "score" not in items[0] and "why" not in items[0]
    rc, out, _ = _run(capsys, "feed")
    assert out.index("Notre Dame") < out.index("Rock & Roll") < out.index("Ep 1")
    assert " 1. " in out and "+2 more" in out and "Fish & chips" in out
    assert "&#038;" not in out and "&amp;" not in out


def test_feed_saves_identities_not_ranks(server, capsys) -> None:
    _numbers(capsys)
    rows, _ = feed.VIEW.load() or ([], {})
    assert {"ref", "title", "url"} <= set(rows[0])
    assert rows[0]["id"].startswith(rows[0]["ref"])
    assert feed.VIEW.state_path().parent == credentials.credentials_path().parent
    assert feed.VIEW.state_path().name == "feed-view.json"


def test_limit_and_group_are_plain_filters(server, capsys) -> None:
    _, out, _ = _run(capsys, "feed", "--json", "--limit", "1")
    assert [i["ref"] for i in json.loads(out)["items"]] == [TEAM]
    _, out, _ = _run(capsys, "feed", "--json", "--group", "pod")
    assert [i["ref"] for i in json.loads(out)["items"]] == [POD]


def test_no_server_ranking_is_one_line_and_never_a_client_ranking(server, capsys) -> None:
    doc = _doc()
    del doc["data"]["feed"]
    server.routes["/api/v1/apps/following/data"] = httpx.Response(200, json=doc)
    rc, out, _ = _run(capsys, "feed")
    assert rc == 0 and "pepper#4852" in out and out.count("\n") == 1
    rc, out, _ = _run(capsys, "feed", "--json")
    body = json.loads(out)
    assert rc == 0 and body["items"] == [] and body["next_before"] is None
    assert "pepper#4852" in body["note"]


def test_an_empty_ranked_list_is_not_the_unranked_note(server, capsys) -> None:
    doc = _doc()
    doc["data"]["feed"] = []
    server.routes["/api/v1/apps/following/data"] = httpx.Response(200, json=doc)
    rc, out, _ = _run(capsys, "feed")
    assert rc == 0 and "nothing in your feed" in out and "4852" not in out


def test_the_list_carries_a_coloured_type_tag() -> None:
    from pepper_cli import following_render

    tags = {g: following_render.group_tag(g) for g in following_render._GROUP_TAG}
    assert "yellow" in tags["Sports"] and "magenta" in tags["Podcasts"]
    assert "blue" in tags["Reading"] and "red" in tags["Shows"] and "cyan" in tags["Events"]


# --- feed N ------------------------------------------------------------------


def test_item_in_full_shows_summary_exact_time_fold_and_more(server, capsys) -> None:
    n = _numbers(capsys)["Rock & Roll"]
    rc, out, _ = _run(capsys, "feed", str(n))
    assert rc == 0
    assert f"{n}. Rock & Roll" in out and "&#038;" not in out
    for needle in ("Example News", "Reading", "https://news.example/", "+2 more", "Fish & chips"):
        assert needle in out
    assert "Sat Oct 17, 6:02 AM PDT" in out  # published_at in the account timezone
    assert "Also from Example News" in out and "Third" in out and "Fourth" in out
    assert "score" not in out


def test_event_time_comes_from_starts_at(server, capsys) -> None:
    n = _numbers(capsys)["Notre Dame at BYU"]
    rc, out, _ = _run(capsys, "feed", str(n))
    assert rc == 0 and "Tomorrow 4:30 PM (Sat Oct 17, 4:30 PM PDT)" in out and "upcoming" in out


def test_item_json_is_the_servers_entry_plus_n_and_local_time(server, capsys) -> None:
    n = _numbers(capsys)["Rock & Roll"]
    rc, out, _ = _run(capsys, "feed", str(n), "--json")
    d = json.loads(out)
    assert rc == 0 and d["n"] == n and d["fold"] == "+2 more"
    assert {m["title"] for m in d["more"]} == {"Third", "Fourth"}
    assert d["when"].startswith("2026-10-17T06:02") and "article" not in d


def test_transcript_calls_the_read_only_action_with_the_episode(server, capsys) -> None:
    n = _numbers(capsys)["Ep 1"]
    rc, out, _ = _run(capsys, "feed", str(n), "--transcript")
    assert rc == 0 and "Transcript" in out and "Welcome to the show." in out
    call = next(r for r in server.seen if r.url.path.endswith("episode_transcript"))
    assert json.loads(call.content) == {"params": {"ref": POD, "item_id": EP}}
    rc, out, _ = _run(capsys, "feed", str(n), "--transcript", "--json")
    assert json.loads(out)["transcript"]["kind"] == "transcript"


def test_a_podcast_entry_without_episode_id_says_it_needs_a_newer_server(server, capsys) -> None:
    doc = _doc()
    next(e for e in doc["data"]["feed"] if e["title"] == "Ep 1").pop("episode_id")
    server.routes["/api/v1/apps/following/data"] = httpx.Response(200, json=doc)
    n = _numbers(capsys)["Ep 1"]
    rc, out, _ = _run(capsys, "feed", str(n), "--transcript")
    assert rc == 0 and "needs a newer server" in out
    assert not any(r.url.path.endswith("episode_transcript") for r in server.seen)


def test_a_sports_entry_renders_a_game_block(server, capsys) -> None:
    n = _numbers(capsys)["Notre Dame at BYU"]
    rc, out, _ = _run(capsys, "feed", str(n))
    assert rc == 0 and "Game" in out
    for text in (
        "NCAAF",
        "4:30 PM",
        "ND 5-1 · BYU 6-0",
        "Two unbeatens meet in Provo.",
        "Ranked matchup",
    ):
        assert text in out
    assert "venue" not in out and "broadcast" not in out


def test_an_entry_with_no_game_fields_has_no_game_block(server, capsys) -> None:
    n = _numbers(capsys)["Rock & Roll"]
    rc, out, _ = _run(capsys, "feed", str(n))
    assert rc == 0 and "Game" not in out


def test_transcript_not_ready_is_labelled_show_notes(server, capsys) -> None:
    server.routes["/api/v1/apps/following/actions/episode_transcript"] = httpx.Response(
        200,
        json={
            "result": {
                "kind": "notes",
                "status": "pending",
                "text": "Publisher blurb.",
                "note": "No transcript yet.",
            }
        },
    )
    n = _numbers(capsys)["Ep 1"]
    rc, out, _ = _run(capsys, "feed", str(n), "--transcript")
    assert (
        rc == 0 and "Show notes" in out and "no transcript yet" in out and "Publisher blurb." in out
    )


def test_transcript_on_a_non_podcast_is_a_line_not_a_failure(server, capsys) -> None:
    n = _numbers(capsys)["Rock & Roll"]
    rc, out, _ = _run(capsys, "feed", str(n), "--transcript")
    assert rc == 0 and "no podcast episode" in out
    assert not any(r.url.path.endswith("episode_transcript") for r in server.seen)


def test_read_fetches_the_page_without_pepper_credentials(server, capsys) -> None:
    n = _numbers(capsys)["Rock & Roll"]
    rc, out, _ = _run(capsys, "feed", str(n), "--read")
    assert rc == 0
    assert "Rock Page" in out and "Ann Author" in out
    assert "#12" in out and "#13" not in out  # the first twelve paragraphs
    assert "3 more paragraphs" in out and "--full" in out
    (req,) = server.web
    assert req.url.host == "news.example"
    assert "authorization" not in req.headers and "pep_pat_abc" not in str(req.headers)


def test_read_full_and_json(server, capsys) -> None:
    n = _numbers(capsys)["Rock & Roll"]
    rc, out, _ = _run(capsys, "feed", str(n), "--read", "--full")
    assert rc == 0 and "#15" in out and "more paragraphs" not in out
    rc, out, _ = _run(capsys, "feed", str(n), "--read", "--json")
    art = json.loads(out)["article"]
    assert len(art["paragraphs"]) == 12 and art["paragraph_count"] == 15
    assert art["title"] == "Rock Page" and art["author"] == "Ann Author"


def test_read_degrades_to_one_line_when_the_site_refuses(server, capsys) -> None:
    server.web_routes["news.example"] = lambda r: httpx.Response(403)
    n = _numbers(capsys)["Rock & Roll"]
    rc, out, _ = _run(capsys, "feed", str(n), "--read")
    assert rc == 0 and "paywall or bot protection" in out
    rc, out, _ = _run(capsys, "feed", str(n), "--read", "--json")
    assert "403" not in out and json.loads(out)["article"]["error"]


def test_read_on_an_item_without_a_link(server, capsys) -> None:
    n = _numbers(capsys)["Ep 1"]
    rc, out, _ = _run(capsys, "feed", str(n), "--read")
    assert rc == 0 and "no link to read" in out and not server.web


# --- identity, not rank ------------------------------------------------------


def test_item_is_found_again_after_the_server_reorders(server, capsys) -> None:
    n = _numbers(capsys)["Ep 1"]
    doc = _doc()
    doc["data"]["feed"].reverse()
    server.routes["/api/v1/apps/following/data"] = httpx.Response(200, json=doc)
    rc, out, _ = _run(capsys, "feed", str(n))
    assert rc == 0 and "Ep 1" in out and "The Show" in out


def test_item_that_left_the_feed_is_reported_gone(server, capsys) -> None:
    n = _numbers(capsys)["Ep 1"]
    doc = _doc()
    doc["data"]["feed"] = [e for e in doc["data"]["feed"] if e["ref"] != POD]
    server.routes["/api/v1/apps/following/data"] = httpx.Response(200, json=doc)
    rc, out, err = _run(capsys, "feed", str(n))
    assert rc == 2 and out == ""
    assert "is gone" in err and "Ep 1" in err and "pepper feed" in err
    rc, out, err = _run(capsys, "feed", str(n), "--json")
    assert rc == 2 and out == "" and json.loads(err)["error"]["code"] == "not_found"


def test_an_item_the_server_folded_is_still_found(server, capsys) -> None:
    n = _numbers(capsys)["Rock & Roll"]
    doc = _doc()
    rock = doc["data"]["feed"][1]
    rock["more"], rock["fold"] = [], None
    doc["data"]["feed"][1] = rock | {"title": "Newer", "url": "https://news.example/Newer"}
    doc["data"]["feed"][1]["more"] = [_entry("Rock &#038; Roll") | {"more": [], "fold": None}]
    server.routes["/api/v1/apps/following/data"] = httpx.Response(200, json=doc)
    rc, out, _ = _run(capsys, "feed", str(n))
    assert rc == 0 and "Rock & Roll" in out


# --- usage -------------------------------------------------------------------


def test_no_list_yet_and_out_of_range(server, capsys) -> None:
    rc, _, err = _run(capsys, "feed", "1")
    assert rc == 2 and "run 'pepper feed' first" in err
    _numbers(capsys)
    rc, _, err = _run(capsys, "feed", "99")
    assert rc == 2 and "no feed item 99" in err


@pytest.mark.parametrize(
    "argv",
    [
        ["feed", "abc"],
        ["feed", "1", "--full"],
        ["feed", "--read"],
        ["feed", "--transcript"],
        ["feed", "--limit", "0"],
        ["feed", "--group", "nope"],
        ["feed", "list"],
    ],
)
def test_usage_errors_exit_2_and_print_nothing(server, capsys, argv) -> None:
    rc, out, err = _run(capsys, *argv)
    assert rc == 2 and out == "" and err


def test_server_error_is_reported(server, capsys) -> None:
    server.routes["/api/v1/apps/following/data"] = httpx.Response(500, json={"detail": "boom"})
    rc, _, err = _run(capsys, "feed")
    assert rc == 1 and "boom" in err


def test_a_failed_feed_save_drops_the_old_numbers(server, capsys, monkeypatch) -> None:
    _numbers(capsys)

    def refuse(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(numbered_view.tempfile, "mkstemp", refuse)
    rc, out, _ = _run(capsys, "feed", "--json")
    assert rc == 0 and "numbers were not saved" in json.loads(out)["note"]
    rc, _, err = _run(capsys, "feed", "1")
    assert rc == 2 and "run 'pepper feed' first" in err
    monkeypatch.setattr(Path, "unlink", refuse)
    feed.VIEW.state_path().write_text('{"rows": [], "meta": {}}')
    rc, out, err = _run(capsys, "feed")
    assert rc == 1 and out == "" and "could not be removed" in err
