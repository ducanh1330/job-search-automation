"""Firecrawl-backed page fetcher for Seek.

Fetches through Firecrawl's infrastructure rather than this machine's connection.
That is the point: when Cloudflare has challenged your IP, no client-side change
helps, because the block follows the IP. Firecrawl's requests originate from its
own proxy pool, so the block simply does not apply.

Duck-types seek_client.SeekClient -- same `.fetch(url, params)` returning HTML,
plus `.delay` / `.request_count` / `.backend` -- so `paginate()` and the rest of
seek_client work against it unchanged.

IMPORTANT: always request the `rawHtml` format. Firecrawl's default markdown
conversion strips <script> tags, which is where SEEK_REDUX_DATA lives -- the
markdown output contains no job data at all.

Costs roughly 1 credit per page fetched.
"""

import hashlib
import os
import random
import sys
import time

import requests
from dotenv import load_dotenv

from seek_client import TMP_DIR, SeekError

load_dotenv()

API_BASE = "https://api.firecrawl.dev"
DEFAULT_VERSION = "v2"
PAGE_CACHE = os.path.join(TMP_DIR, "page_cache")


class FirecrawlError(SeekError):
    """Firecrawl itself failed -- bad key, out of credits, or upstream error."""


class SeekFirecrawl:
    """Fetch Seek pages via the Firecrawl scrape API."""

    # Firecrawl's free tier allows roughly 10 scrapes per minute. A 6.5s spacing
    # keeps us just under that; going faster earns a 429 that no amount of
    # retrying clears quickly, and an interrupted sweep wastes every credit
    # already spent on it.
    def __init__(self, delay: float = 6.5, jitter: float = 0.5, timeout: int = 180,
                 api_key: str | None = None, version: str = DEFAULT_VERSION,
                 max_retries: int = 5, cache: bool = True, cache_ttl: int = 21600):
        self.api_key = (api_key or os.getenv("FIRECRAWL_API_KEY") or "").strip()
        if not self.api_key:
            raise FirecrawlError(
                "FIRECRAWL_API_KEY is not set. Add it to .env as "
                "FIRECRAWL_API_KEY=fc-... (no quotes)."
            )

        self.delay = delay
        self.jitter = jitter
        self.timeout = timeout
        self.version = version
        self.max_retries = max_retries
        self.backend = f"firecrawl:{version}"
        self.cache = cache
        self.cache_ttl = cache_ttl
        self.request_count = 0
        self.credits_used = 0
        self.cache_hits = 0
        self._last_request = 0.0
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._session.close()
        return False

    def _cache_file(self, url: str) -> str:
        return os.path.join(PAGE_CACHE, hashlib.sha256(url.encode()).hexdigest()[:32] + ".html")

    def fetch(self, url: str, params: dict | None = None) -> str:
        """Scrape a URL through Firecrawl and return its raw HTML.

        Cached on disk by URL. A re-run after an interrupted sweep then costs
        nothing for pages already fetched, which matters because every miss is
        a paid credit.
        """
        if params:
            query = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
            url = f"{url}?{query}"

        path = self._cache_file(url)
        if self.cache and os.path.exists(path):
            if time.time() - os.path.getmtime(path) < self.cache_ttl:
                with open(path, encoding="utf-8") as fh:
                    self.cache_hits += 1
                    return fh.read()

        wait = (self.delay + random.uniform(0, self.jitter)) - (
            time.monotonic() - self._last_request
        )
        if wait > 0:
            time.sleep(wait)

        payload = {"url": url, "formats": ["rawHtml"], "onlyMainContent": False}
        last_error = None

        for attempt in range(self.max_retries):
            try:
                resp = self._session.post(
                    f"{API_BASE}/{self.version}/scrape", json=payload, timeout=self.timeout
                )
                self.request_count += 1
                self._last_request = time.monotonic()

                if resp.status_code == 401:
                    raise FirecrawlError("Firecrawl rejected the API key (HTTP 401).")
                if resp.status_code == 402:
                    raise FirecrawlError(
                        "Firecrawl credits exhausted (HTTP 402). Top up or switch to "
                        "--html mode with saved pages."
                    )
                if resp.status_code == 429:
                    # Rate limits are per-minute, so waiting out a full window is
                    # the only thing that actually clears them. Honour Retry-After
                    # when Firecrawl sends it.
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        pause = float(retry_after) if retry_after else 0.0
                    except (TypeError, ValueError):
                        pause = 0.0
                    pause = max(pause, 45.0 * (attempt + 1))
                    last_error = f"rate limited (429), waited {pause:.0f}s"
                    print(f"      rate limited; pausing {pause:.0f}s", file=sys.stderr, flush=True)
                    time.sleep(pause)
                    continue
                if resp.status_code >= 500:
                    last_error = f"upstream {resp.status_code}"
                    time.sleep(5.0 * (attempt + 1))
                    continue

                resp.raise_for_status()
                body = resp.json()

                if not body.get("success"):
                    last_error = str(body.get("error") or body)[:200]
                    time.sleep(3.0 * (attempt + 1))
                    continue

                raw = (body.get("data") or {}).get("rawHtml") or ""
                if not raw:
                    raise FirecrawlError(
                        "Firecrawl returned no rawHtml. The 'formats' request may have "
                        "been altered -- markdown output does not contain the job data."
                    )

                self.credits_used += 1
                if self.cache:
                    os.makedirs(PAGE_CACHE, exist_ok=True)
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(raw)
                return raw

            except FirecrawlError:
                raise
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(5.0 * (attempt + 1))

        raise FirecrawlError(f"giving up on {url} after {self.max_retries} attempts: {last_error}")
