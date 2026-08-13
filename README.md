# Job Search Automation

Scrapes early-career IT job listings from [Seek](https://au.seek.com) and turns them into a
filterable Excel workbook — including **work-rights / visa requirements**, which Seek does not
expose in search results and which decide whether you can actually apply.

Built on the WAT framework (Workflows, Agents, Tools): markdown SOPs in `workflows/`, deterministic
Python in `tools/`.

## What you get

An `.xlsx` with three sheets — **Internships** (early-career matches), **All Jobs** (everything
scraped), and **Run Summary** (coverage diagnostics) — with these columns:

| Column | Notes |
|---|---|
| Status | `New` / `Existing`, tracked across runs. Closed and expired listings are dropped |
| Job Title | Hyperlinked to the listing |
| Company | |
| Position | `Internship`, `Graduate Program`, `Junior`, `Entry Level`, … |
| Employment | Work type with arrangement folded in — `Full time (Hybrid)` |
| Location · Experience · Category | |
| Salary | Literal `N/A` when the employer published none |
| **Work Rights** | `PR or citizen required`, `No sponsorship`, `Full work rights required`, `Asked at application`, `Not stated`. Disqualifying values are filled red |
| Description Summary | One condensed paragraph |
| First / Last Seen | |

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # then paste your Firecrawl key into .env
```

Requires Python 3.12+ and a [Firecrawl](https://firecrawl.dev) key. Free tier is enough — a full
sweep is ~56 credits, a targeted search ~2.

## Usage

```bash
# 1. Scrape. A narrow keyword search is far more efficient than a broad sweep (see below).
python tools/scrape_seek_jobs.py --firecrawl \
    --url "https://au.seek.com/it-internship-jobs/in-Melbourne-VIC-3000" \
    --out .tmp/seek_narrow.json

# 2. Add work-rights / visa detail (only early-career matches; cached, so re-runs are free)
python tools/enrich_job_details.py --input .tmp/seek_narrow.json --out .tmp/seek_jobs.json

# 3. Build the workbook
python tools/export_jobs_excel.py --input .tmp/seek_jobs.json --out "Seek Jobs.xlsx"
```

Steps 2 and 3 accept several `--input` files and merge them, de-duplicating by job id.

### Broad sweep

```bash
python tools/scrape_seek_jobs.py --firecrawl \
    --classification 6281 --where "All Melbourne VIC" \
    --max-partitions 5          # cheap test run; omit for all 22
```

### No Firecrawl key?

Save the search pages from your browser (Ctrl+S) and parse them offline, free:

```bash
python tools/scrape_seek_jobs.py --html ".tmp/saved/*.html"
```

## Tools

| Tool | Purpose |
|---|---|
| `seek_client.py` | Shared library — parsing, pagination, Cloudflare detection. Not a CLI |
| `seek_firecrawl.py` | Firecrawl fetcher with disk cache. The working backend |
| `scrape_seek_jobs.py` | Sweeps listings into JSON with coverage diagnostics |
| `enrich_job_details.py` | Adds work-rights, visa and closing-date detail from job pages |
| `export_jobs_excel.py` | Builds the formatted, tracked workbook |

## Things worth knowing

Discovered while building this; the full set with reproduction details lives in
[`workflows/scrape_seek_jobs.md`](workflows/scrape_seek_jobs.md).

- **A narrow keyword search beats a broad sweep, by a lot.** The targeted internship URL returned
  42 early-career roles for 2 credits. Sweeping 897 general ICT listings for 39 credits found 22.
  Only ~2.5% of ICT listings are early-career.
- **Seek caps pagination at page 17** (~544 results). Page 18 returns HTTP 200 with an empty result
  set, so a naive "page until empty" loop silently truncates and still looks successful.
- **Cloudflare blocks direct scraping by IP.** Once triggered, nothing client-side clears it —
  browser headers, TLS impersonation via `curl_cffi`, and Playwright (headless, headed, and real
  Edge) were all still refused. Firecrawl works because the request originates elsewhere.
- **Firecrawl must return `rawHtml`.** Its default markdown conversion strips `<script>` tags, and
  all the job data lives in one.
- **Filter-panel IDs don't identify subclassifications.** Sibling classifications sit in the same
  numeric range; passing one returns a far broader set and burns a full page-cap. Match on the
  href path instead, and sanity-check against the parent's total.
- **Work Rights is inferred from advertiser prose**, not a structured field. Treat it as a lead,
  and read `Not stated` as unknown rather than permissive.

## Disclaimer

For personal job-hunting use. Automated bulk scraping may conflict with Seek's terms of service —
check them and pace your requests. Defaults are deliberately conservative.
