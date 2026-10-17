"""``article_reader``: fetch limits, no credentials, and reading a page's text (mock HTTP only)."""

from __future__ import annotations

import json

import httpx
import pytest

from pepper_cli import article_reader

LONG = "This is a sentence long enough to count as a real paragraph of the article."

PAGE = f"""<!doctype html><html><head>
<title>Fallback title</title>
<meta property="og:title" content="The Real Title">
<meta name="description" content="Plain description">
<meta property="og:description" content="Social description">
<meta property="og:site_name" content="Example News">
<meta property="article:published_time" content="2026-10-16T09:00:00Z">
<script type="application/ld+json">
{{"@type": "NewsArticle", "author": [{{"name": "Ada Writer"}}]}}
</script>
<style>p {{ color: red }}</style></head>
<body>
<nav><p>Subscribe to our newsletter now and never miss a story again</p></nav>
<p>An outside paragraph that is long enough but sits outside the article element.</p>
<article>
<h1>Headline</h1>
<p>{LONG} It has <a href="/x">a link</a> and &amp; an entity &#8217;.</p>
<script>var x = "<p>not a paragraph at all, just script text pretending</p>";</script>
<p>Short.</p>
<p>Second paragraph of the article body, again long enough to be kept.</p>
<aside><p>Related: a sidebar paragraph that is long enough to be kept by mistake.</p></aside>
<p>Third paragraph of the article body, again long enough to be kept.</p>
</article>
<footer><p>Copyright notice that is long enough to count as a paragraph.</p></footer>
</body></html>"""


@pytest.fixture
def web(monkeypatch):
    """Route ``fetch`` through a mock transport; ``web.handler`` sets the answer."""
    seen: list[httpx.Request] = []
    state = {
        "handler": lambda req: httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
    }

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return state["handler"](request)

    monkeypatch.setattr(article_reader, "_transport", httpx.MockTransport(handle))
    # Test hosts resolve to a public documentation address unless a test says otherwise.
    resolved: dict[str, list[str]] = {}
    monkeypatch.setattr(
        article_reader,
        "_resolve",
        lambda host: resolved.get(host) or article_reader._literal(host) or ["93.184.215.14"],
    )
    return type("Web", (), {"seen": seen, "state": state, "resolved": resolved})


def test_reads_title_meta_author_date_and_article_body(web) -> None:
    art = article_reader.fetch("https://news.example/a")
    assert art.error is None
    assert art.title == "The Real Title"
    assert art.description == "Social description"
    assert art.author == "Ada Writer"  # from JSON-LD
    assert art.published == "2026-10-16T09:00:00Z"
    assert art.site == "Example News"
    # article scope wins; nav, aside, footer, script and short lines are out
    assert art.paragraphs == [
        f"{LONG} It has a link and & an entity ’.",
        "Second paragraph of the article body, again long enough to be kept.",
        "Third paragraph of the article body, again long enough to be kept.",
    ]


def test_without_an_article_element_every_paragraph_counts() -> None:
    art = article_reader.parse(
        "u", f"<html><body><nav><p>{LONG}</p></nav><div><p>{LONG}</p><p>{LONG} more</p></div>"
    )
    assert art.paragraphs == [LONG, f"{LONG} more"]
    assert art.title is None and art.author is None


def test_meta_author_wins_over_json_ld_and_title_tag_is_the_fallback() -> None:
    art = article_reader.parse(
        "u",
        '<title> Only   title </title><meta name="author" content="Meta Person">'
        '<script type="application/ld+json">not json</script>',
    )
    assert art.author == "Meta Person" and art.title == "Only title"


def test_sends_a_browser_user_agent_and_no_credentials(web) -> None:
    article_reader.fetch("https://news.example/a")
    req = web.seen[0]
    assert req.headers["user-agent"].startswith("Mozilla/5.0")
    for name in ("authorization", "cookie", "x-api-key"):
        assert name not in req.headers


@pytest.mark.parametrize(
    ("status", "words"),
    [(403, "paywall or bot"), (402, "paywall"), (401, "login"), (429, "rate"), (500, "500")],
)
def test_refusals_are_one_clear_line(web, status, words) -> None:
    web.state["handler"] = lambda req: httpx.Response(status)
    art = article_reader.fetch("https://news.example/a")
    assert art.error and words in art.error and "\n" not in art.error
    assert art.paragraphs == []


def test_non_html_and_non_http_links_are_declined(web) -> None:
    web.state["handler"] = lambda req: httpx.Response(
        200, content=b"%PDF", headers={"content-type": "application/pdf"}
    )
    assert "not a web page" in (article_reader.fetch("https://x.example/a.pdf").error or "")
    assert "not a web page" in (article_reader.fetch("ftp://x.example/a").error or "")
    assert len(web.seen) == 1  # the ftp link never went out


def test_a_page_with_nothing_readable_says_so(web) -> None:
    web.state["handler"] = lambda req: httpx.Response(
        200, text="<html><body><div id=app></div><script>boot()</script></body></html>"
    )
    art = article_reader.fetch("https://spa.example/")
    assert art.error and "no readable text" in art.error


def test_redirects_are_followed_and_capped(web) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/loop":
            return httpx.Response(302, headers={"location": "/loop"})
        if req.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})

    web.state["handler"] = handler
    ok = article_reader.fetch("https://news.example/old")
    assert ok.final_url == "https://news.example/new" and ok.error is None
    looped = article_reader.fetch("https://news.example/loop")
    assert looped.error and "redirects" in looped.error


def test_timeouts_and_connection_errors_degrade(web) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    web.state["handler"] = boom
    assert "did not answer" in (article_reader.fetch("https://slow.example/").error or "")

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=req)

    web.state["handler"] = down
    assert "could not fetch" in (article_reader.fetch("https://down.example/").error or "")


def test_body_is_capped(web, monkeypatch) -> None:
    monkeypatch.setattr(article_reader, "MAX_BYTES", 400)
    head = f"<article><p>{LONG}</p><p>Second paragraph, long enough to be kept as one.</p>"
    web.state["handler"] = lambda req: httpx.Response(
        200, text=head + "<p>" + "x" * 5000 + "</p>", headers={"content-type": "text/html"}
    )
    art = article_reader.fetch("https://big.example/")
    assert art.paragraphs[0] == LONG
    assert all(len(p) < 1000 for p in art.paragraphs)


def test_charset_from_header_is_honoured(web) -> None:
    body = f"<article><p>{LONG} café crème</p><p>{LONG}</p></article>".encode("latin-1")
    web.state["handler"] = lambda req: httpx.Response(
        200, content=body, headers={"content-type": "text/html; charset=iso-8859-1"}
    )
    assert "café crème" in article_reader.fetch("https://old.example/").paragraphs[0]


def test_as_json_limits_paragraphs_but_counts_all() -> None:
    art = article_reader.Article(url="u", paragraphs=["a", "b", "c"])
    data = art.as_json(limit=2)
    assert data["paragraphs"] == ["a", "b"] and data["paragraph_count"] == 3
    assert json.loads(json.dumps(art.as_json()))["paragraphs"] == ["a", "b", "c"]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://100.100.100.100/",
    ],
)
def test_a_link_to_a_non_public_address_is_refused_before_any_request(web, url) -> None:
    art = article_reader.fetch(url)
    assert art.error and "not a public web address" in art.error
    assert web.seen == []


def test_a_host_resolving_to_a_private_address_is_refused(web) -> None:
    web.resolved["router.example"] = ["93.184.215.14", "192.168.1.1"]
    art = article_reader.fetch("https://router.example/")
    assert art.error and "router.example" in art.error and web.seen == []


def test_a_redirect_to_a_private_address_is_refused(web) -> None:
    web.state["handler"] = lambda req: httpx.Response(
        302, headers={"location": "http://127.0.0.1:8080/secret"}
    )
    art = article_reader.fetch("https://news.example/go")
    assert art.error and "127.0.0.1" in art.error
    assert [str(r.url) for r in web.seen] == ["https://news.example/go"]


def test_an_unresolvable_host_is_a_fetch_failure(web, monkeypatch) -> None:
    def gone(host: str) -> list[str]:
        raise OSError("no such host")

    monkeypatch.setattr(article_reader, "_resolve", gone)
    assert "could not fetch" in (article_reader.fetch("https://gone.example/").error or "")


def test_resolve_reads_literals_and_dns(monkeypatch) -> None:
    assert article_reader._resolve("10.1.2.3") == ["10.1.2.3"]
    monkeypatch.setattr(
        article_reader.socket,
        "getaddrinfo",
        lambda host, port, **kw: [(2, 1, 6, "", ("93.184.215.14", 0))],
    )
    assert article_reader._resolve("news.example") == ["93.184.215.14"]


def test_a_page_that_trickles_past_the_deadline_is_cut_off(web, monkeypatch) -> None:
    monkeypatch.setattr(article_reader, "DEADLINE_SECONDS", -1.0)
    web.state["handler"] = lambda req: httpx.Response(
        200, content=iter([b"<p>a</p>", b"<p>b</p>"]), headers={"content-type": "text/html"}
    )
    art = article_reader.fetch("https://slow.example/")
    assert art.error and "took longer than" in art.error


def _unread():
    """A body that fails the test if anyone reads it."""
    raise AssertionError("a redirect body was read")
    yield b""  # pragma: no cover


def test_a_redirect_body_is_never_read(web) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/old":
            return httpx.Response(302, headers={"location": "/new"}, content=_unread())
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})

    web.state["handler"] = handler
    art = article_reader.fetch("https://news.example/old")
    assert art.error is None and art.final_url == "https://news.example/new"


def test_five_hops_are_followed_and_a_sixth_is_refused(web) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        n = int(req.url.path.strip("/") or 0)
        if n < article_reader.MAX_REDIRECTS:
            return httpx.Response(302, headers={"location": f"/{n + 1}"}, content=_unread())
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})

    web.state["handler"] = handler
    assert article_reader.fetch("https://news.example/0").error is None
    assert len(web.seen) == article_reader.MAX_REDIRECTS + 1

    web.seen.clear()
    web.state["handler"] = lambda req: httpx.Response(
        302, headers={"location": f"/{len(web.seen)}"}, content=_unread()
    )
    art = article_reader.fetch("https://news.example/start")
    assert art.error and "too many redirects" in art.error
    assert len(web.seen) == article_reader.MAX_REDIRECTS + 1


def test_a_private_address_on_a_later_hop_is_refused(web) -> None:
    web.resolved["inner.example"] = ["10.0.0.5"]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "news.example":
            return httpx.Response(302, headers={"location": "https://inner.example/x"})
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})

    web.state["handler"] = handler
    art = article_reader.fetch("https://news.example/a")
    assert art.error and "inner.example" in art.error
    assert [r.url.host for r in web.seen] == ["news.example"]


def test_the_deadline_covers_every_hop(web, monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(article_reader.time, "monotonic", lambda: clock[0])

    def handler(req: httpx.Request) -> httpx.Response:
        clock[0] += article_reader.DEADLINE_SECONDS / 2 + 1  # each hop eats over half of it
        return httpx.Response(302, headers={"location": f"/{len(web.seen)}"})

    web.state["handler"] = handler
    art = article_reader.fetch("https://news.example/start")
    assert art.error and "took longer than" in art.error
    assert len(web.seen) == 2


def test_a_timeout_cut_short_by_the_deadline_names_the_deadline(web, monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(article_reader.time, "monotonic", lambda: clock[0])

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/start":
            clock[0] += article_reader.DEADLINE_SECONDS - 5
            return httpx.Response(302, headers={"location": "/next"})
        assert req.extensions["timeout"]["read"] == 5
        raise httpx.ReadTimeout("slow", request=req)

    web.state["handler"] = handler
    art = article_reader.fetch("https://news.example/start")
    assert art.error and "took longer than 45 seconds" in art.error


def test_a_plain_timeout_names_the_per_request_wait(web) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    web.state["handler"] = handler
    art = article_reader.fetch("https://news.example/start")
    assert art.error and "within 15 seconds" in art.error
