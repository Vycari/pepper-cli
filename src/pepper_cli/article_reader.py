"""``pepper feed show N --read``: fetch an article's page and pull out its readable text.

Pepper keeps only a feed item's title and url, so the CLI fetches the page itself. This is a
plain third-party GET: a browser-like ``User-Agent``, a per-read timeout and an overall
deadline, a redirect cap and a byte cap, only to public internet addresses (a feed item's link
is third-party text, so a link or redirect to loopback, a private network or a link-local
metadata address is refused before anything is sent), and never the Pepper token, cookies or
any other credential (the client is built here, apart from the API client). Parsing is the
stdlib ``html.parser``: no new dependency, and no attempt at JavaScript or paywalls; a page
that will not read degrades to :attr:`Article.error`, one line.
"""

from __future__ import annotations

import contextlib
import dataclasses
import ipaddress
import json
import re
import socket
import time
from html.parser import HTMLParser
from typing import Any

import httpx

MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 15.0
#: The whole fetch, body included: the per-read timeout alone lets a page that sends a byte
#: every few seconds run on indefinitely.
DEADLINE_SECONDS = 45.0
DEFAULT_PARAGRAPHS = 12
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 pepper-cli"
)

#: Tests inject a ``httpx.MockTransport`` here; production leaves it ``None``.
_transport: httpx.BaseTransport | None = None


class _NotPublic(Exception):
    """The link (or a redirect) names a host that is not on the public internet."""

    def __init__(self, host: str) -> None:
        super().__init__(host)
        self.host = host


class _TooSlow(Exception):
    """The page took longer than :data:`DEADLINE_SECONDS` to arrive."""


def _literal(host: str) -> list[str] | None:
    """``[host]`` when ``host`` is an IP address literal, else ``None``."""
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        return None


def _resolve(host: str) -> list[str]:
    """Every address ``host`` resolves to (an address literal is itself). Tests replace this."""
    return _literal(host) or [
        str(info[4][0]) for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    ]


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _refuse_non_public(request: httpx.Request) -> None:
    """``httpx`` request hook, run for the first request and for every redirect."""
    host = request.url.host
    try:
        addresses = _resolve(host)
    except (OSError, UnicodeError) as exc:
        raise httpx.ConnectError(f"could not resolve {host}", request=request) from exc
    if not addresses or not all(_is_public(a) for a in addresses):
        raise _NotPublic(host)


_SKIP = frozenset(
    {"script", "style", "noscript", "nav", "footer", "aside", "form", "svg", "iframe", "template"}
    | {"button", "select"}
)
_MIN_PARAGRAPH = 25  # shorter lines are captions, bylines and buttons
_REFUSED = {
    401: "needs a login",
    402: "is behind a paywall",
    403: "refused the request (a paywall or bot protection)",
    429: "is rate limiting requests",
}


@dataclasses.dataclass
class Article:
    url: str
    final_url: str | None = None
    title: str | None = None
    description: str | None = None
    author: str | None = None
    published: str | None = None
    site: str | None = None
    paragraphs: list[str] = dataclasses.field(default_factory=list)
    error: str | None = None

    def as_json(self, *, limit: int | None = None) -> dict[str, Any]:
        shown = self.paragraphs if limit is None else self.paragraphs[:limit]
        return {
            "url": self.url,
            "final_url": self.final_url,
            "title": self.title,
            "description": self.description,
            "author": self.author,
            "published": self.published,
            "site": self.site,
            "paragraphs": shown,
            "paragraph_count": len(self.paragraphs),
            "error": self.error,
        }


def _clean(text: str) -> str:
    return " ".join(text.split())


class _Page(HTMLParser):
    """Collects meta tags, the title, JSON-LD blocks and paragraphs (article scope first)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self.jsonld: list[str] = []
        self.scoped: list[str] = []  # paragraphs inside <article>/<main>
        self.loose: list[str] = []  # every paragraph
        self._skip: dict[str, int] = {}
        self._scope = 0
        self._in_title = False
        self._ld = False
        self._ld_buf: list[str] = []
        self._para: list[str] | None = None

    def _skipping(self) -> bool:
        return any(self._skip.values())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = _clean(a["content"])
            return
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self._ld, self._ld_buf = True, []
        if tag == "title":
            self._in_title = True
        if tag in _SKIP:
            self._skip[tag] = self._skip.get(tag, 0) + 1
        if tag in ("article", "main"):
            self._scope += 1
        if tag == "p" and not self._skipping():
            self._flush()
            self._para = []
        if tag == "br" and self._para is not None:
            self._para.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "script" and self._ld:
            self._ld = False
            self.jsonld.append("".join(self._ld_buf))
        if tag in _SKIP and self._skip.get(tag):
            self._skip[tag] -= 1
        if tag in ("article", "main") and self._scope:
            self._flush()
            self._scope -= 1
        if tag == "p":
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._ld:
            self._ld_buf.append(data)
        elif self._in_title:
            self.title += data
        elif self._para is not None and not self._skipping():
            self._para.append(data)

    def _flush(self) -> None:
        if self._para is None:
            return
        text = _clean("".join(self._para))
        self._para = None
        if len(text) >= _MIN_PARAGRAPH:
            self.loose.append(text)
            if self._scope:
                self.scoped.append(text)


def _walk(node: Any, key: str) -> Any:
    """The first value under ``key`` anywhere in a JSON-LD structure."""
    if isinstance(node, dict):
        if node.get(key):
            return node[key]
        for v in node.values():
            if (found := _walk(v, key)) is not None:
                return found
    elif isinstance(node, list):
        for v in node:
            if (found := _walk(v, key)) is not None:
                return found
    return None


def _name(value: Any) -> str | None:
    if isinstance(value, str):
        return _clean(value) or None
    if isinstance(value, dict):
        return _name(value.get("name"))
    if isinstance(value, list):
        names = [n for v in value if (n := _name(v))]
        return ", ".join(names) or None
    return None


def parse(url: str, text: str) -> Article:
    """An :class:`Article` from page HTML."""
    page = _Page()
    with contextlib.suppress(Exception):  # lenient parser; a bad page must not crash the command
        page.feed(text)
        page.close()
    ld: list[Any] = []
    for block in page.jsonld:
        try:
            ld.append(json.loads(block))
        except ValueError:
            continue
    m = page.meta
    author = m.get("author") or m.get("article:author") or m.get("twitter:creator")
    if not author or author.startswith(("http://", "https://")):
        author = _name(_walk(ld, "author")) or author or None
    return Article(
        url=url,
        title=m.get("og:title") or m.get("twitter:title") or _clean(page.title) or None,
        description=m.get("og:description") or m.get("description") or m.get("twitter:description"),
        author=author,
        published=m.get("article:published_time")
        or m.get("datepublished")
        or m.get("date")
        or (_name(_walk(ld, "datePublished"))),
        site=m.get("og:site_name"),
        paragraphs=page.scoped if len(page.scoped) >= 2 else page.loose,
    )


def _decode(body: bytes, content_type: str) -> str:
    charset = None
    if m := re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE):
        charset = m.group(1)
    elif m := re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.IGNORECASE):
        charset = m.group(1).decode("ascii", "replace")
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def fetch(url: str) -> Article:
    """GET ``url`` and read it; failures come back as ``Article.error``, never an exception."""
    art = Article(url=url)
    if not re.match(r"^https?://", url, re.IGNORECASE):
        art.error = "this item's link is not a web page (http or https) I can fetch"
        return art
    deadline = time.monotonic() + DEADLINE_SECONDS
    cut_short = False  # the last request's timeout was the deadline's remainder, not 15 s
    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"},
            transport=_transport,
            event_hooks={"request": [_refuse_non_public]},
        ) as client:
            current = url
            # Redirects are followed here, not by httpx: httpx reads every redirect response's
            # body in full, outside the byte cap and the deadline. A redirect is read for its
            # headers only, then closed; each hop passes the public-address check again.
            for hop in range(MAX_REDIRECTS + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _TooSlow
                cut_short = remaining < TIMEOUT_SECONDS
                with client.stream("GET", current, timeout=min(TIMEOUT_SECONDS, remaining)) as resp:
                    location = resp.headers.get("location")
                    if resp.is_redirect and location:
                        if hop == MAX_REDIRECTS:
                            raise httpx.TooManyRedirects("too many redirects", request=resp.request)
                        current = str(resp.url.join(location))
                        continue
                    art.final_url = str(resp.url)
                    if resp.status_code >= 400:
                        why = _REFUSED.get(resp.status_code, f"answered {resp.status_code}")
                        art.error = f"the site {why}; open the link in a browser"
                        return art
                    ctype = resp.headers.get("content-type", "")
                    if ctype and not re.search(r"html|xml|text/plain", ctype, re.IGNORECASE):
                        art.error = f"the link is not a web page ({ctype.split(';')[0].strip()})"
                        return art
                    body = bytearray()
                    for chunk in resp.iter_bytes():
                        body.extend(chunk)
                        if len(body) >= MAX_BYTES:
                            break
                        if time.monotonic() > deadline:
                            raise _TooSlow
                    break
    except _NotPublic as exc:
        art.error = f"the link points at {exc.host}, which is not a public web address"
        return art
    except _TooSlow:
        art.error = f"the site took longer than {DEADLINE_SECONDS:.0f} seconds to send the page"
        return art
    except httpx.TooManyRedirects:
        art.error = f"too many redirects (more than {MAX_REDIRECTS})"
        return art
    except httpx.TimeoutException:
        art.error = (
            f"the site took longer than {DEADLINE_SECONDS:.0f} seconds to send the page"
            if cut_short
            else f"the site did not answer within {TIMEOUT_SECONDS:.0f} seconds"
        )
        return art
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        art.error = f"could not fetch the page ({type(exc).__name__})"
        return art
    parsed = parse(url, _decode(bytes(body[:MAX_BYTES]), ctype))
    parsed.final_url = art.final_url
    if not parsed.paragraphs and not parsed.description:
        parsed.error = "no readable text on the page (it may need JavaScript or a login)"
    return parsed
