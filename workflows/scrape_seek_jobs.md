# Workflow: Scrape Seek Jobs to Excel

## Objective
Pull job listings from Seek (au.seek.com) for a given classification and location,
filter them for early-career roles (internships, graduate programs, junior), and
deliver a formatted Excel workbook that tracks new and closed listings across runs.

## Required Inputs
- `classification` — Seek classification id. Default `6281` (Information & Communication Technology). Optional.
- `where` — Seek location string, e.g. `All Melbourne VIC`. Default `All Melbourne VIC`. Optional.
- `url` — a single Seek search URL to scrape as-is, instead of partitioning. Optional.
- `out` — path for the `.xlsx`. Optional.

No API keys. Nothing in `.env` is used by this workflow.

## Tools Used
| Step | Tool | Purpose |
|------|------|---------|
| 1 | `tools/seek_client.py` | Shared library: HTTP, Cloudflare detection, redux parsing, pagination. Not a CLI. |
| 2 | `tools/seek_firecrawl.py` | Firecrawl fetcher. Duck-types SeekClient. **The working backend** — see Cloudflare below. |
| 3 | `tools/seek_browser.py` | Playwright fallback fetcher. Duck-types SeekClient. Did not defeat Cloudflare. |
| 4 | `tools/scrape_seek_jobs.py` | Sweep Seek into `.tmp/seek_jobs.json` with coverage diagnostics. |
| 5 | `tools/enrich_job_details.py` | Add work-rights/visa, closing date and ad body from each job page. |
| 6 | `tools/export_jobs_excel.py` | Turn that JSON into a formatted, tracked `.xlsx`. |
| — | `tools/export_jobs_html.py` | Optional browsable HTML version of the same data. |

## Steps
1. Confirm inputs. Defaults cover "IT jobs in Melbourne" — ask only if the user wants a different field or city.
2. Scrape. Prefer `--firecrawl`; plain HTTP is usually blocked (see Cloudflare below):
   ```
   # a) Firecrawl -- the reliable path. ~1 credit per page.
   python tools/scrape_seek_jobs.py --firecrawl --classification 6281 --where "All Melbourne VIC"

   # b) one narrow search URL as-is
   python tools/scrape_seek_jobs.py --firecrawl --url "https://au.seek.com/it-internship-jobs/in-Melbourne-VIC-3000"

   # c) free, no key -- parse pages saved from a browser
   python tools/scrape_seek_jobs.py --html ".tmp/saved/*.html"
   ```
   Progress goes to stderr; stdout is a JSON summary.
3. Read the JSON summary. Check `coverage_pct` and `warnings` before continuing. If `ok` is false, see Edge Cases.
4. Enrich with work-rights/visa and closing dates. Only early-career matches by default,
   and every page is cached, so re-runs are free:
   ```
   python tools/enrich_job_details.py --input .tmp/seek_jobs.json --out .tmp/seek_jobs.json
   ```
   **Skipping this leaves Work Rights as "Not checked" and Tags empty** — both derive from
   the ad body, which the search listing does not contain.
5. Export:
   ```
   python tools/export_jobs_excel.py --input .tmp/seek_jobs.json --out "Seek IT Internships Melbourne.xlsx"
   ```
6. Report to the user: early-career matches, new since last run, coverage %, the work-rights
   breakdown, and any warnings.

## Derived columns
These are inferred by `export_jobs_excel.py`, not supplied by Seek. Treat them as leads, not facts.
- **Employment** — `work_type` with `work_arrangement` folded in: `Full time (Hybrid)`.
- **Work Rights** — classified from ad text by ordered rules; `Work Rights Detail` carries the
  exact sentence so the call can be checked. `Not stated` means unknown, not permissive, and
  `Not checked` means the job was never enriched. `PR or citizen required` and `No sponsorship`
  are filled red, since they can disqualify an applicant outright.
- **Experience** — banded from explicit year mentions; falls back to `0-2 yrs` for early-career matches.
- **Tags** — tech skills matched against the ad body. Empty until the job is enriched.
- **Salary** — literal `N/A` when the employer published none, so missing reads as absent, not skipped.

## Expected Output
An `.xlsx` with three sheets:
- **Internships** — early-career matches, `New` rows highlighted and sorted to the top.
- **All Jobs** — the full scraped set, so an over-tight filter never loses anything.
- **Run Summary** — coverage, warnings, and the per-partition breakdown.

State lives in `.tmp/seek_seen.json` (first/last seen per job id). Delete it to reset tracking.

## Edge Cases & Gotchas

- **Cloudflare challenge (the big one).** Seek is behind Cloudflare. Sweeping too
  fast earns an IP-level managed challenge: HTTP 403, header `Cf-Mitigated: challenge`,
  body "Just a moment...". Roughly **35 requests at ~1.5s spacing was enough to trip it**.
  The tool raises `CloudflareChallenge` immediately instead of retrying into it,
  because retrying prolongs the block. Default delay is 4s + jitter. Do not lower it below 3.

  **Once triggered, nothing client-side clears it.** All of the following were tested
  and still got 403: `requests` with full browser headers, `curl_cffi` impersonating
  Chrome/Safari TLS, Playwright headless Chromium, Playwright headed Chromium, and
  Playwright driving a real installed Edge. It survived 12 probes over 60 minutes —
  and probing may itself refresh the timer.

  **The fix is `--firecrawl`.** The block is tied to *your IP*; Firecrawl fetches from its
  own infrastructure, so it never applies. This was the lesson: when the block follows the
  IP, no client-side change can help, and time spent on TLS/browser tricks is wasted. Reach
  for a different egress path early. `--html` with browser-saved pages is the free fallback.
  Note that bulk scraping likely breaches Seek's ToS — worth raising with the user.

- **Firecrawl must return `rawHtml`.** Its default markdown conversion strips `<script>`
  tags, and `SEEK_REDUX_DATA` lives in one — markdown output contains no job data at all.
  `seek_firecrawl.py` always requests `formats: ["rawHtml"]` and raises if it comes back empty.
  Endpoint is `POST https://api.firecrawl.dev/v2/scrape`; ~1 credit per page.

- **`--html` mode is the dependable fallback.** Open the search in a browser, save the
  page (Ctrl+S, "Webpage, Complete" or HTML-only), and point `--html` at the file(s).
  The saved page carries the same `SEEK_REDUX_DATA` blob, so parsing is identical and
  the coverage warning still tells you how many pages you still need to save.

- **Pagination is hard-capped at page 17** (~544 results). Page 18+ returns HTTP 200
  with an empty result set and `totalCount: 0` — it looks like a clean end of results.
  A naive "page until empty" loop silently returns 544 of 1,713 jobs and reports success.
  This is why the tool partitions by subclassification; each partition is well under the cap.
  If a partition still exceeds it, the tool re-splits by `worktype` and, failing that,
  records a warning rather than pretending the run was complete.

- **Some queries silently drop your filters.** `?keywords=graduate&where=All+Melbourne+VIC&classification=6281`
  **301-redirects** to an SEO landing page that discards location and classification and
  reports **167,157** results. The slug form misfires too: `/graduate-jobs/...?classification=6281`
  returned exactly the unfiltered ICT total, keyword ignored. The tell is
  `titleTagShortLocationName` coming back empty while `totalCount` is non-zero; the tool
  refuses to scrape in that state. **Prefer classification+subclassification partitioning
  over keyword searches**, and filter for keywords locally instead.

- **Parse the blob, don't regex it.** A page can contain more than one `totalCount`;
  a regex grabbed the wrong one and reported 167,275 during development. `parse_redux`
  brace-matches the `SEEK_REDUX_DATA = {...}` object and `json.loads` it.

- **The result metadata and the job cards sit at different depths.** `totalCount` and
  `titleTagShortLocationName` are on `state["results"]`, but the cards are one level
  deeper on `state["results"]["results"]["jobs"]`. Reading the count from the deeper
  block returns `None`, which makes every search look like zero results and aborts
  pagination on page 1. Use `extract_total` / `extract_jobs` rather than reaching in.

- **Tier the early-career keyword match.** Matching weak terms ("student", "graduate",
  "junior") anywhere in the body produces false positives — a "Career Support Advisor"
  role whose blurb mentions helping students is not an internship. Strong terms
  ("internship", "cadetship", "grad program") are trusted anywhere; weak terms count
  only in the job title. This cut 30 matches to 27 on a 32-job sample, all 3 genuine
  false positives.

- **Filter-panel IDs alone do not identify subclassifications.** The panel lists sibling
  *classifications* next to the parent's own children, and their ids sit in the same numeric
  neighbourhood — ICT is 6281 with children 6282–6303, but 6304 (Advertising, Arts & Media),
  6317 (Human Resources) and 6362 (Sales) are siblings, not children. An id-range heuristic
  accepts them, and passing one as `subclassification` returns a far broader result set
  (1,703 vs the parent's 1,675) that then burns a full 17-page cap. **Use the href**: a real
  subclassification nests under the parent slug with a second path segment
  (`/jobs-in-information-communication-technology/architects/…`), a sibling has one
  (`/jobs-in-advertising-arts-media/…`). `discover_subclassifications` matches on that.

- **Guard on the parent count too.** A subclassification can never hold more jobs than its
  parent. `paginate(..., max_total=parent_total)` stops on page 1 when it does, so a bad
  partition costs one request instead of seventeen. Two independent checks, because this
  failure silently spends money and pollutes the dataset with out-of-scope jobs.

- **Duplicate-looking titles can be real.** Two Bega Group listings differed only by a
  missing leading letter ("Bega"/"ega") but had distinct job ids — the advertiser
  posted twice with a typo. De-duplicate on `id`, never on title.

- **Domain moved.** `www.seek.com.au` 308-redirects to `au.seek.com`. Use the latter.

- **The old JSON API is dead.** `/api/chalice-search/v4/search` now returns 404. The
  server-rendered redux blob is the supported path.

- **Promoted listings repeat across pages.** Always de-duplicate by job `id`; page 1 can
  return 31 or 33 cards rather than exactly 32.

- **Narrow SEO slugs are narrower than they look.** `/it-internship-jobs/...` matches the
  fused keyword `itinternship` and returned only 45 jobs, versus 1,713 for all ICT in
  Melbourne. Don't assume a slug means what it reads like.

## Change Log
- 2026-08-13 — created. Documented the page-17 cap, the Cloudflare challenge threshold,
  and the SEO-redirect filter-drop, all found while building this.
- 2026-08-13 — added `--html` saved-page mode after Cloudflare blocked every automated
  fetch path (requests, curl_cffi, Playwright headless/headed/real-Edge). Fixed the
  redux metadata depth bug and tiered the keyword matcher. Verified end to end on a real
  32-job page: 27 early-career matches, working hyperlinks, New/Existing/Closed tracking.
- 2026-08-14 — added `--firecrawl`, which is what finally worked. Added
  `enrich_job_details.py` for work-rights/visa extraction. Reworked the Excel columns
  (Employment merged, Position replaces match reason, closed/expired rows dropped, cell
  borders). Fixed subclassification discovery to use hrefs, added the parent-count guard,
  and centred work-rights evidence on the matched phrase. Removed the Playwright backend
  and `curl_cffi` — neither ever beat Cloudflare, and both were dead weight once
  Firecrawl worked.
