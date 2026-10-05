"""Optional, polite SEC HTTP acquisition client; never used implicitly."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler
from urllib.parse import urlparse


class SECRequestError(RuntimeError):
    """SEC request failed; status is available for explicit handling."""

    def __init__(self, status: int | None, message: str):
        self.status = status
        super().__init__(message)


class SECRateLimited(SECRequestError):
    """SEC explicitly returned a rate-limit or access-denied response."""


@dataclass(frozen=True)
class SECAcquisition:
    url: str
    body: bytes
    retrieved_at: datetime
    sha256: str
    actual_response_url: str | None = None
    content_type: str | None = None


class SECClient:
    """Fetch SEC resources with a required contact User-Agent and URL cache.

    The caller must opt in by constructing this client. ``opener`` is injectable
    for tests. A maximum 9 requests/second is enforced per client instance.
    """

    def __init__(self, user_agent: str, *, opener: Callable | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 sleeper: Callable[[float], None] = time.sleep,
                 min_interval: float = 1 / 9):
        if not isinstance(user_agent, str) or "@" not in user_agent or len(user_agent.strip()) < 8:
            raise ValueError("SEC User-Agent must identify the application and a monitored contact email")
        if min_interval < 1 / 10:
            raise ValueError("SEC request rate must remain below 10 requests per second")
        self.user_agent = user_agent
        self._opener = opener or urlopen
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleep = sleeper
        self._min_interval = min_interval
        self._last_request: float | None = None
        self._cache: dict[str, bytes] = {}
        self._acquisitions: dict[str, SECAcquisition] = {}
        self._response_metadata: dict[str, tuple[str, str | None]] = {}

    def acquire(self, url: str) -> SECAcquisition:
        """Fetch a resource and preserve its first retrieval time and body hash."""
        cached = self._acquisitions.get(url)
        if cached is not None:
            return cached
        body = self.get(url)
        retrieved = self._wall_clock()
        if retrieved.tzinfo is None or retrieved.utcoffset() is None:
            raise ValueError("wall_clock must return a timezone-aware datetime")
        actual_url, content_type = self._response_metadata.get(url, (url, None))
        result = SECAcquisition(url, body, retrieved, hashlib.sha256(body).hexdigest(), actual_url, content_type)
        self._acquisitions[url] = result
        return result

    def get(self, url: str) -> bytes:
        """Return response body; successful URL responses are cached in memory."""
        if url in self._cache:
            return self._cache[url]
        if self._last_request is not None:
            delay = self._min_interval - (self._clock() - self._last_request)
            if delay > 0:
                self._sleep(delay)
        request = Request(url, headers={"User-Agent": self.user_agent, "Accept-Encoding": "identity"})
        self._last_request = self._clock()
        try:
            response = self._opener(request, timeout=30)
            try:
                body = response.read()
            finally:
                close = getattr(response, "close", None)
                if close:
                    close()
        except HTTPError as exc:
            if exc.code in (403, 429):
                raise SECRateLimited(exc.code, f"SEC returned HTTP {exc.code}; request was not retried") from exc
            raise SECRequestError(exc.code, f"SEC request failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise SECRequestError(None, f"SEC request failed: {exc.reason}") from exc
        self._cache[url] = body
        return body


MAX_SEC_RESPONSE_BYTES = 12 * 1024 * 1024


def _allowed_sec_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443) or parsed.query or parsed.fragment:
        return False
    if any(segment in {".", ".."} for segment in parsed.path.split("/")) or "%2e" in parsed.path.lower() or "\\\\" in parsed.path:
        return False
    host = (parsed.hostname or "").lower()
    path = parsed.path
    if host == "data.sec.gov":
        return path in {
            "/submissions/CIK0000092380.json",
            "/submissions/CIK0000027904.json",
            "/submissions/CIK0000006201.json",
            "/submissions/CIK0000100517.json",
        }
    if host == "www.sec.gov":
        return path in {
            "/Archives/edgar/data/92380/000009238026000004/index.json",
            "/Archives/edgar/data/92380/000009238026000004/luv-20251231.htm",
            "/Archives/edgar/data/27904/000002790426000013/index.json",
            "/Archives/edgar/data/27904/000002790426000013/dal-20251231.htm",
            "/Archives/edgar/data/6201/000000620126000014/index.json",
            "/Archives/edgar/data/6201/000000620126000014/aal-20251231.htm",
            "/Archives/edgar/data/100517/000010051726000023/index.json",
            "/Archives/edgar/data/100517/000010051726000023/ual-20251231.htm",
        }
    return False


class _SECRedirectHandler(HTTPRedirectHandler):
    """Reject non-allowlisted redirect targets before urllib follows them."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed_sec_url(newurl):
            raise SECRequestError(code, "SEC redirect target is outside the allowlist")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SECFilingClient(SECClient):
    """Opt-in bounded client restricted to SEC submission and filing resources."""
    def __init__(self, user_agent: str, **kwargs):
        super().__init__(user_agent, **kwargs)
        if kwargs.get("opener") is None:
            self._opener = build_opener(_SECRedirectHandler()).open

    def get(self, url: str) -> bytes:
        if not _allowed_sec_url(url):
            raise ValueError("URL is outside the SEC filing acquisition allowlist")
        if url in self._cache:
            return self._cache[url]
        if self._last_request is not None:
            delay = self._min_interval - (self._clock() - self._last_request)
            if delay > 0:
                self._sleep(delay)
        request = Request(url, headers={"User-Agent": self.user_agent, "Accept-Encoding": "identity"})
        self._last_request = self._clock()
        try:
            response = self._opener(request, timeout=30)
            try:
                final_url = response.geturl() if hasattr(response, "geturl") else url
                if not _allowed_sec_url(final_url):
                    raise SECRequestError(None, "final SEC response URL is outside the allowlist")
                body = response.read(MAX_SEC_RESPONSE_BYTES + 1)
                headers = getattr(response, "headers", None)
                self._response_metadata[url] = (final_url, headers.get("Content-Type") if headers is not None else None)
                if len(body) > MAX_SEC_RESPONSE_BYTES:
                    raise SECRequestError(None, "SEC response exceeds maximum byte limit")
            finally:
                close = getattr(response, "close", None)
                if close:
                    close()
        except HTTPError as exc:
            if exc.code in (403, 429):
                raise SECRateLimited(exc.code, f"SEC returned HTTP {exc.code}; request was not retried") from exc
            raise SECRequestError(exc.code, f"SEC request failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise SECRequestError(None, f"SEC request failed: {exc.reason}") from exc
        self._cache[url] = body
        return body
