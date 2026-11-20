"""Tests for ``ApiClient.download_takeout``.

Uses ``httpx.MockTransport`` like the rest of the client tests. The
interesting behavior here is streaming to disk (not buffering the whole
body) and picking a sensible output filename when the caller doesn't
supply one.
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from pepper_cli import api_client
from pepper_cli.secret import SecretStr


def _mock(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


_ZIP_BYTES = b"PK\x05\x06" + b"\x00" * 18  # minimal-looking zip EOCD, content is opaque here


def test_download_takeout_writes_bytes_and_sends_bearer_header(tmp_path) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            content=_ZIP_BYTES,
            headers={
                "content-type": "application/zip",
                "content-disposition": 'attachment; filename="pepper-takeout-alice-x.zip"',
            },
        )

    out_path = tmp_path / "out.zip"
    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        result = api.download_takeout(out_path=str(out_path))

    assert result == str(out_path)
    assert out_path.read_bytes() == _ZIP_BYTES
    assert seen["method"] == "GET"
    assert seen["url"] == "http://example.com/api/v1/takeout"
    assert seen["auth"] == "Bearer pep_pat_abc"


def test_download_takeout_sends_no_query_params(tmp_path) -> None:
    """There is no credential opt-in to send — the server never exports
    them, so the client has no flag to forward."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = str(request.url.params)
        return httpx.Response(200, content=_ZIP_BYTES)

    out_path = tmp_path / "out.zip"
    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        api.download_takeout(out_path=str(out_path))

    assert seen["query"] == ""


def test_download_takeout_uses_content_disposition_filename_when_out_omitted(tmp_path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_ZIP_BYTES,
            headers={"content-disposition": 'attachment; filename="pepper-takeout-bob-y.zip"'},
        )

    with (
        api_client.ApiClient(
            server_url="http://example.com",
            token=SecretStr("pep_pat_abc"),
            client=_mock(handler),
        ) as api,
        _chdir(tmp_path),
    ):
        result = api.download_takeout()

    assert result == "pepper-takeout-bob-y.zip"
    assert (tmp_path / "pepper-takeout-bob-y.zip").read_bytes() == _ZIP_BYTES


@pytest.mark.parametrize(
    "hostile",
    [
        "../../evil.zip",
        "/etc/evil.zip",
        "..\\..\\evil.zip",
        "..",
    ],
)
def test_download_takeout_sanitizes_hostile_content_disposition_filename(tmp_path, hostile) -> None:
    """A malicious server's filename= can't steer the write outside the
    working directory — traversal collapses to a basename, and a bare
    ``..`` falls back to the default name."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_ZIP_BYTES,
            headers={"content-disposition": f'attachment; filename="{hostile}"'},
        )

    with (
        api_client.ApiClient(
            server_url="http://example.com",
            token=SecretStr("pep_pat_abc"),
            client=_mock(handler),
        ) as api,
        _chdir(tmp_path),
    ):
        result = api.download_takeout()

    expected = "evil.zip" if "evil" in hostile else api_client.DEFAULT_TAKEOUT_FILENAME
    assert result == expected
    assert (tmp_path / expected).read_bytes() == _ZIP_BYTES
    # Nothing escaped tmp_path: the only entry is the sanitized file.
    assert [p.name for p in tmp_path.iterdir()] == [expected]


def test_download_takeout_falls_back_to_default_filename(tmp_path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_ZIP_BYTES)  # no content-disposition at all

    with (
        api_client.ApiClient(
            server_url="http://example.com",
            token=SecretStr("pep_pat_abc"),
            client=_mock(handler),
        ) as api,
        _chdir(tmp_path),
    ):
        result = api.download_takeout()

    assert result == api_client.DEFAULT_TAKEOUT_FILENAME
    assert (tmp_path / api_client.DEFAULT_TAKEOUT_FILENAME).read_bytes() == _ZIP_BYTES


def test_download_takeout_leaves_no_partial_file_when_stream_fails(tmp_path) -> None:
    """A connection dropped mid-body must not leave a truncated archive
    (or its .part staging file) behind — a half-written zip at the final
    path could later pass for a complete backup."""

    def _bomb():
        yield b"PK\x05\x06"
        raise RuntimeError("connection dropped mid-stream")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_bomb())

    out_path = tmp_path / "out.zip"
    with (
        api_client.ApiClient(
            server_url="http://example.com",
            token=SecretStr("pep_pat_abc"),
            client=_mock(handler),
        ) as api,
        pytest.raises(RuntimeError),
    ):
        api.download_takeout(out_path=str(out_path))

    assert list(tmp_path.iterdir()) == []


def test_download_takeout_keeps_the_previous_archive_when_the_stream_fails(tmp_path) -> None:
    out_path = tmp_path / "out.zip"
    out_path.write_bytes(b"previous complete archive")

    def _bomb():
        yield b"PK\x05\x06"
        raise RuntimeError("connection dropped mid-stream")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_bomb())

    with (
        api_client.ApiClient(
            server_url="http://example.com",
            token=SecretStr("pep_pat_abc"),
            client=_mock(handler),
        ) as api,
        pytest.raises(RuntimeError),
    ):
        api.download_takeout(out_path=str(out_path))

    assert out_path.read_bytes() == b"previous complete archive"
    assert [p.name for p in tmp_path.iterdir()] == ["out.zip"]


def test_download_takeout_leaves_a_sibling_part_file_alone(tmp_path) -> None:
    out_path = tmp_path / "out.zip"
    sibling = tmp_path / "out.zip.part"
    sibling.write_bytes(b"someone else's partial download")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_ZIP_BYTES)

    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        api.download_takeout(out_path=str(out_path))

    assert out_path.read_bytes() == _ZIP_BYTES
    assert sibling.read_bytes() == b"someone else's partial download"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.zip", "out.zip.part"]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the pytest temp path plus a 250-byte name can exceed MAX_PATH",
)
def test_download_takeout_stages_a_name_near_the_filename_limit(tmp_path) -> None:
    """A 250-byte destination fits beside the old five-byte ``.part`` suffix.
    The staging name must not add the full destination name."""
    out_name = "a" * 246 + ".zip"
    out_path = tmp_path / out_name

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_ZIP_BYTES)

    with api_client.ApiClient(
        server_url="http://example.com",
        token=SecretStr("pep_pat_abc"),
        client=_mock(handler),
    ) as api:
        result = api.download_takeout(out_path=str(out_path))

    assert result == str(out_path)
    assert out_path.read_bytes() == _ZIP_BYTES
    assert [p.name for p in tmp_path.iterdir()] == [out_name]


def test_concurrent_downloads_use_separate_staging_files(tmp_path) -> None:
    out_path = tmp_path / "out.zip"
    second_started = threading.Event()
    first_done = threading.Event()
    first_body = b"first-half-second-half"
    second_body = b"other-half-tail-half"

    def first_stream():
        yield first_body[:10]
        assert second_started.wait(5)
        yield first_body[10:]

    def second_stream():
        yield second_body[:10]
        second_started.set()
        assert first_done.wait(5)
        yield second_body[10:]

    def download(stream):
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=stream())

        with api_client.ApiClient(
            server_url="http://example.com",
            token=SecretStr("pep_pat_abc"),
            client=_mock(handler),
        ) as api:
            return api.download_takeout(out_path=str(out_path))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(download, first_stream)
        second = pool.submit(download, second_stream)
        try:
            first_result = first.result(timeout=10)
        finally:
            first_done.set()
        second_result = second.result(timeout=10)

    assert first_result == second_result == str(out_path)
    assert out_path.read_bytes() == second_body
    assert [p.name for p in tmp_path.iterdir()] == ["out.zip"]


def test_download_takeout_rejects_redirect_without_writing_file(tmp_path) -> None:
    """Httpx doesn't follow redirects by default — a 302's body must
    raise, not get written to disk as a fake archive."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "http://elsewhere.example.com/"},
            content=b"<html>moved</html>",
        )

    out_path = tmp_path / "out.zip"
    with (
        api_client.ApiClient(
            server_url="http://example.com",
            token=SecretStr("pep_pat_abc"),
            client=_mock(handler),
        ) as api,
        pytest.raises(api_client.ApiError) as ei,
    ):
        api.download_takeout(out_path=str(out_path))

    assert ei.value.status == 302
    assert list(tmp_path.iterdir()) == []


def test_download_takeout_raises_api_error_on_404_without_writing_file(tmp_path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    out_path = tmp_path / "out.zip"
    with (
        api_client.ApiClient(
            server_url="http://example.com",
            token=SecretStr("pep_pat_abc"),
            client=_mock(handler),
        ) as api,
        pytest.raises(api_client.ApiError) as ei,
    ):
        api.download_takeout(out_path=str(out_path))

    assert ei.value.status == 404
    assert ei.value.detail == "not found"
    assert not out_path.exists()


class _chdir:
    """Minimal context manager to run a block inside ``path`` and
    restore the original cwd on exit — avoids pulling in a fixture just
    for the two tests that rely on the client's relative-path default.
    """

    def __init__(self, path) -> None:
        self._path = path
        self._prev: str | None = None

    def __enter__(self) -> None:
        import os

        self._prev = os.getcwd()
        os.chdir(self._path)

    def __exit__(self, *exc_info) -> None:
        import os

        if self._prev is not None:
            os.chdir(self._prev)
