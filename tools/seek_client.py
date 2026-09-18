"""Shared Seek (au.seek.com) search client.

Not a CLI. Imported by scrape_seek_jobs.py and export_jobs_excel.py.

Seek server-renders every search result into the page HTML as a
`SEEK_REDUX_DATA = {...}` blob, so no browser automation or paid scraping API
is needed -- plain requests with a browser User-Agent returns HTTP 200 with
complete structured job data.

Three source quirks this module exists to absorb (all verified against the live
site, see workflows/scrape_seek_jobs.md):

1. Pagination is hard-capped at page 17 (~544 results). Page 18+ returns HTTP
   200 with an empty result set and totalCount 0. A naive "loop until empty"
   scraper silently truncates and still looks successful.
2. Some query shapes 301-redirect to SEO landing pages that silently drop the
   location/classification filters and report six-figure result counts.
3. The blob must be brace-matched, not regexed. A page can contain more than
   one `totalCount` and regex grabs the wrong one.
"""

import html
import json
import os
import random
import re
import time

import requests
from dotenv import load_dotenv

load_dotenv()

TMP_DIR = os.environ.get(
    "JOBSEARCH_TMP_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp"),
)

BASE = "https://au.seek.com"
SEARCH_URL = BASE + "/jobs"
JOB_URL = BASE + "/job/{job_id}"

# Verified against the live site 2026-08-13.
PAGE_SIZE = 32          # appConfig.zoneFeatures.SEARCH_PAGE_SIZE
MAX_PAGE = 17           # page 18+ yields an empty result set
MAX_REACHABLE = PAGE_SIZE * MAX_PAGE  # 544 -- ceiling on results per query

REDUX_MARKER = "SEEK_REDUX_DATA = "

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# Filter-panel checkboxes, e.g.
#   <a href="https://au.seek.com/jobs-in-information-communication-technology/architects/in-..."
#      data-automation="6282" role="checkbox" aria-label="Architects">
#
# The href is what separates a real subclassification from a sibling
# classification: a subclassification nests under the parent's slug
# (".../information-communication-technology/architects/..."), while a sibling
# classification has its own single slug (".../jobs-in-advertising-arts-media/").
# Filtering on the id alone wrongly accepts siblings, which then return the
# whole site's results and burn a full page-cap of requests.
_FILTER_LINK_RE = re.compile(
    r'<a href="([^"]+)"[^>]*data-automation="(\d{4})"[^>]*role="checkbox"[^>]*aria-label="([^"]+)"'
)
_SLUG_RE = re.compile(r"/(?:[a-z0-9-]*jobs)-in-([a-z0-9-]+)(/([a-z0-9-]+))?")


class SeekError(RuntimeError):
    """Raised when Seek returns something we refuse to treat as valid data."""


class CloudflareChallenge(SeekError):
    """Cloudflare is interstitially challenging us.

    Distinct from a generic error because the remedy is different: no amount of
    retrying helps, and hammering it extends the block. The only fix is to stop,
    let it expire, and resume with a larger --delay.
    """


def _is_challenge(status: int, body: str, headers) -> bool:
    if status in (403, 503):
        if headers and str(headers.get("Cf-Mitigated", "")).lower() == "challenge":
            return True
        marker = body[:4000]
        return "Just a moment" in marker or "challenges.cloudflare.com" in marker
    return False


class SeekClient:
    """Polite, retrying HTTP client for Seek search pages.

    Defaults are deliberately conservative. Seek is behind Cloudflare and will
    issue a managed challenge if you sweep it quickly -- roughly 35 requests at
    ~1.5s spacing was enough to trip it during development. A ~4s jittered delay
    keeps a full run comfortably under that.
    """

    def __init__(self, delay: float = 4.0, timeout: int = 60, max_retries: int = 3,
                 jitter: float = 1.5):
        self.delay = delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.jitter = jitter
        self.session = requests.Session()
        self.backend = "requests"
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-AU,en;q=0.9",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        self.request_count = 0
        self._last_request = 0.0

    def fetch(self, url: str, params: dict | None = None) -> str:
        """GET a page, honouring the delay and retrying on 429/5xx.

        Raises CloudflareChallenge immediately rather than retrying into it.
        """
        wait = (self.delay + random.uniform(0, self.jitter)) - (
            time.monotonic() - self._last_request
        )
        if wait > 0:
            time.sleep(wait)

        last_exc = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                self.request_count += 1
                self._last_request = time.monotonic()

                body = resp.text
                if _is_challenge(resp.status_code, body, resp.headers):
                    raise CloudflareChallenge(
                        "Cloudflare is challenging this IP (HTTP "
                        f"{resp.status_code}). Stop scraping for 15-30 minutes, "
                        "then retry with a larger --delay. Retrying now will "
                        "only prolong the block."
                    )

                if resp.status_code in (429, 500, 502, 503, 504):
                    last_exc = SeekError(f"HTTP {resp.status_code} from {url}")
                    time.sleep(5.0 * (attempt + 1))
                    continue

                if resp.status_code != 200:
                    raise SeekError(f"HTTP {resp.status_code} from {url}")

                return body
            except CloudflareChallenge:
                raise
            except SeekError:
                raise
            except Exception as exc:  # transport-level failure
                last_exc = exc
                time.sleep(5.0 * (attempt + 1))

        raise SeekError(f"giving up on {url} after {self.max_retries} attempts: {last_exc}")


def parse_redux(page_html: str) -> dict:
    """Extract and parse the SEEK_REDUX_DATA object from page HTML.

    Brace-matched rather than regexed: a search page can contain several
    `totalCount` keys and a regex will happily return the wrong one.
    """
    start = page_html.find(REDUX_MARKER)
    if start == -1:
        raise SeekError("SEEK_REDUX_DATA not found -- page shape changed or request was blocked")

    i = page_html.index("{", start)
    depth = 0
    in_string = False
    escaped = False

    for pos in range(i, len(page_html)):
        ch = page_html[pos]

        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(page_html[i : pos + 1])

    raise SeekError("SEEK_REDUX_DATA object never closed -- truncated response?")


# The search state is split across two levels: the result *metadata* (counts,
# location label) sits on state["results"], while the job cards themselves are
# one level deeper on state["results"]["results"]["jobs"]. Mixing these up makes
# every search look like zero results.
def _search_block(state: dict) -> dict:
    return state.get("results") or {}


def _results_block(state: dict) -> dict:
    return _search_block(state).get("results") or {}


def extract_jobs(state: dict) -> list:
    return _results_block(state).get("jobs") or []


def extract_total(state: dict) -> int:
    return int(_search_block(state).get("totalCount") or 0)


def extract_location_label(state: dict) -> str:
    """Non-empty only when the location filter actually survived the request.

    This is the tell for the SEO-redirect trap: when Seek 301s a query onto a
    landing page it drops the filters and leaves this blank.
    """
    return (_search_block(state).get("titleTagShortLocationName") or "").strip()


def discover_subclassifications(client: SeekClient, classification: str, where: str,
                                page: str | None = None) -> list:
    """Read the subclassification list out of the live filter panel.

    Discovered at runtime rather than hardcoded, so a Seek taxonomy change
    surfaces as different partitions instead of silently missing jobs.

    Pass `page` to reuse HTML you already fetched -- every request avoided is
    one less step toward a Cloudflare challenge.
    """
    if page is None:
        page = client.fetch(SEARCH_URL, {"classification": classification, "where": where})

    links = _FILTER_LINK_RE.findall(page)

    # The parent's own link gives us its slug, which every child href contains.
    parent_slug = ""
    for href, item_id, _label in links:
        if item_id == str(classification):
            match = _SLUG_RE.search(href)
            if match:
                parent_slug = match.group(1)
            break

    found = {}
    for href, item_id, label in links:
        if item_id == str(classification):
            continue
        match = _SLUG_RE.search(href)
        if not match:
            continue
        slug, child = match.group(1), match.group(3)
        # A subclassification sits under the parent slug AND has a child segment.
        if child and (not parent_slug or slug == parent_slug):
            found[item_id] = html.unescape(label).strip()

    if not found:
        raise SeekError(f"no subclassifications discovered for classification {classification}")

    return [{"id": k, "label": v} for k, v in sorted(found.items())]


def paginate(client: SeekClient, params: dict, max_pages: int = MAX_PAGE,
             base_url: str = SEARCH_URL, max_total: int | None = None) -> dict:
    """Page through one search query, stopping safely at the cap.

    `base_url` covers both search forms: the canonical /jobs endpoint and the
    SEO slug paths like /it-internship-jobs/in-Melbourne-VIC-3000, which carry
    their filters in the path rather than the query string.

    Returns the jobs plus a diagnostics record describing how the walk ended,
    so callers can tell "we got everything" apart from "we hit the ceiling".
    """
    jobs: dict = {}
    total = None
    location_label = ""
    pages_fetched = 0
    stop_reason = "exhausted"

    for page in range(1, max_pages + 1):
        state = parse_redux(client.fetch(base_url, {**params, "page": page}))
        page_jobs = extract_jobs(state)
        pages_fetched = page

        if page == 1:
            total = extract_total(state)
            location_label = extract_location_label(state)
            if total == 0:
                stop_reason = "empty"
                break
            # Bigger than it can possibly be means the filter did not apply.
            # Stop on page 1 rather than spending a full page-cap of requests.
            if max_total is not None and total > max_total:
                stop_reason = "over_expected_total"
                break

        if not page_jobs:
            stop_reason = "empty_page"
            break

        new = 0
        for job in page_jobs:
            job_id = str(job.get("id") or "")
            if job_id and job_id not in jobs:
                jobs[job_id] = job
                new += 1

        # Promoted listings repeat across pages; a page contributing nothing new
        # means we are looping, not progressing.
        if new == 0:
            stop_reason = "no_new_jobs"
            break

        if len(page_jobs) < PAGE_SIZE:
            stop_reason = "last_page"
            break

        if page == max_pages:
            stop_reason = "page_cap"

    total = total or 0
    collected = len(jobs)
    # Truncated iff the cap stopped us short of the reported total.
    truncated = stop_reason == "page_cap" and collected < total

    return {
        "jobs": list(jobs.values()),
        "diagnostics": {
            "base_url": base_url,
            "params": {k: v for k, v in params.items()},
            "reported_total": total,
            "collected": collected,
            "pages_fetched": pages_fetched,
            "stop_reason": stop_reason,
            "location_label": location_label,
            "truncated": truncated,
            "filters_dropped": bool(total) and not location_label,
        },
    }


def normalise(job: dict) -> dict:
    """Flatten a raw Seek job card into a flat, spreadsheet-ready row."""
    classifications = job.get("classifications") or [{}]
    first = classifications[0] if classifications else {}
    classification = (first.get("classification") or {}).get("description", "")
    subclassification = (first.get("subclassification") or {}).get("description", "")

    locations = job.get("locations") or [{}]
    location = (locations[0] or {}).get("label", "")
    hierarchy = (locations[0] or {}).get("seoHierarchy") or []
    area = hierarchy[0].get("contextualName", "") if hierarchy else ""

    employer = job.get("employer") or {}
    company_url = employer.get("companyUrl") or ""

    work_arrangements = job.get("workArrangements") or {}
    job_id = str(job.get("id") or "")

    return {
        "job_id": job_id,
        "title": job.get("title", ""),
        "company": job.get("companyName") or (job.get("advertiser") or {}).get("description", ""),
        "location": location,
        "area": area,
        "work_type": ", ".join(job.get("workTypes") or []),
        "work_arrangement": work_arrangements.get("displayText", "") or "",
        "salary": job.get("salaryLabel", "") or "",
        "classification": classification,
        "subclassification": subclassification,
        "posted": job.get("listingDateDisplay", "") or "",
        "listing_date": job.get("listingDate", "") or "",
        "teaser": job.get("teaser", "") or "",
        "bullet_points": " | ".join(job.get("bulletPoints") or []),
        "is_featured": bool(job.get("isFeatured")),
        "role_id": job.get("roleId", "") or "",
        "advertiser_id": (job.get("advertiser") or {}).get("id", ""),
        "job_url": JOB_URL.format(job_id=job_id),
        "company_url": (BASE + company_url) if company_url.startswith("/") else company_url,
    }
