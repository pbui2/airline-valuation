from datetime import datetime, timezone
from hashlib import sha256

import pytest

from airline_valuation.sec import (SECClient, SECAcquisition, SECRateLimited,
                                    SECFilingClient, SECRequestError, _SECRedirectHandler,
                                    MAX_SEC_RESPONSE_BYTES, _allowed_sec_url)


class Response:
    def __init__(self, body, url=None, headers=None):
        self.body = body
        self.closed = False
        self.url = url
        self.headers = headers or {"Content-Type": "application/json"}

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def geturl(self):
        return self.url or "https://www.sec.gov/test"

    def close(self):
        self.closed = True


def test_acquire_records_hash_retrieval_and_sec_headers_with_mocked_clock_http():
    response = Response(b"filing body")
    requests = []
    def opener(request, timeout):
        requests.append((request, timeout))
        return response
    timestamp = datetime(2026, 2, 7, 12, tzinfo=timezone.utc)
    client = SECClient("AirlineValuation contact@example.com", opener=opener,
                       clock=lambda: 1.0, wall_clock=lambda: timestamp)
    result = client.acquire("https://www.sec.gov/test")
    assert isinstance(result, SECAcquisition)
    assert result.body == b"filing body"
    assert result.retrieved_at == timestamp
    assert result.sha256 == sha256(b"filing body").hexdigest()
    request, timeout = requests[0]
    assert request.get_header("User-agent") == "AirlineValuation contact@example.com"
    assert request.get_header("Accept-encoding") == "identity"
    assert timeout == 30
    assert response.closed


def test_repeated_acquire_preserves_original_timestamp_and_hash():
    calls = []
    times = iter([datetime(2026, 2, 7, 12, tzinfo=timezone.utc), datetime(2026, 2, 8, 12, tzinfo=timezone.utc)])
    def opener(request, timeout):
        calls.append(request)
        return Response(b"first acquired body")
    client = SECClient("AirlineValuation contact@example.com", opener=opener, clock=lambda: 1.0, wall_clock=lambda: next(times))
    first = client.acquire("https://www.sec.gov/test")
    assert client.acquire("https://www.sec.gov/test") is first
    assert len(calls) == 1


def test_acquisition_retrieval_time_must_be_aware():
    client = SECClient("AirlineValuation contact@example.com", opener=lambda *_args, **_kwargs: Response(b"x"), wall_clock=lambda: datetime(2026, 2, 7))
    with pytest.raises(ValueError, match="timezone-aware"):
        client.acquire("https://www.sec.gov/test")


def test_http_429_is_not_retried():
    from urllib.error import HTTPError
    calls = []
    def opener(request, timeout):
        calls.append(request)
        raise HTTPError(request.full_url, 429, "limited", {}, None)
    client = SECClient("AirlineValuation contact@example.com", opener=opener, clock=lambda: 1)
    with pytest.raises(SECRateLimited):
        client.get("https://www.sec.gov/test")
    assert len(calls) == 1


def test_sec_client_redirect_and_host_policy():
    handler = _SECRedirectHandler()
    url = "https://www.sec.gov/Archives/edgar/data/92380/000009238026000004/index.json"
    request = __import__("urllib.request", fromlist=["Request"]).Request(url)
    with pytest.raises(SECRequestError, match="outside the allowlist"):
        handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/file")
    client = SECFilingClient("AirlineValuation contact@example.com", opener=lambda *_a, **_k: Response(b"ok", url))
    assert client.get(url) == b"ok"
    for bad in (url + "?x=1", url + "#frag", url.replace("/Archives/", "/Archives/../Archives/"), url.replace("https://", "https://user@")):
        with pytest.raises(ValueError, match="allowlist"):
            client.get(bad)


def test_registered_submission_urls_are_allowed_and_fetchable():
    urls = (
        "https://data.sec.gov/submissions/CIK0000092380.json",
        "https://data.sec.gov/submissions/CIK0000027904.json",
        "https://data.sec.gov/submissions/CIK0000006201.json",
        "https://data.sec.gov/submissions/CIK0000100517.json",
    )
    for url in urls:
        assert _allowed_sec_url(url)
        client = SECFilingClient(
            "AirlineValuation contact@example.com",
            opener=lambda _request, *, timeout, requested_url=url: Response(b"ok", requested_url),
        )
        assert client.get(url) == b"ok"


def test_unregistered_submission_url_rejected_directly_and_as_redirect():
    url = "https://data.sec.gov/submissions/CIK0000000001.json"
    assert not _allowed_sec_url(url)
    client = SECFilingClient(
        "AirlineValuation contact@example.com",
        opener=lambda *_args, **_kwargs: pytest.fail("unregistered URL was fetched"),
    )
    with pytest.raises(ValueError, match="allowlist"):
        client.get(url)

    allowed_request = __import__("urllib.request", fromlist=["Request"]).Request(
        "https://data.sec.gov/submissions/CIK0000092380.json"
    )
    with pytest.raises(SECRequestError, match="outside the allowlist"):
        _SECRedirectHandler().redirect_request(
            allowed_request, None, 302, "Found", {}, url
        )


def test_sec_client_does_not_cache_oversize_response():
    url = "https://www.sec.gov/Archives/edgar/data/92380/000009238026000004/index.json"
    calls = []
    def opener(*_args, **_kwargs):
        calls.append(1)
        return Response(b"x" * (MAX_SEC_RESPONSE_BYTES + 1), url)
    client = SECFilingClient("AirlineValuation contact@example.com", opener=opener)
    for _ in range(2):
        with pytest.raises(SECRequestError, match="maximum byte limit"):
            client.get(url)
    assert url not in client._cache
    assert len(calls) == 2
