"""Scrape Seek job listings for a classification + location into JSON.

Seek caps pagination at 17 pages (~544 results), so any search larger than that
cannot be fully retrieved in one query. This tool works around that by splitting
the search into subclassification partitions -- each small enough to enumerate
completely -- then de-duplicating by job id.

It reports coverage honestly: if a partition is still too big to retrieve in
full, the run is flagged `incomplete` rather than quietly returning a subset.

Usage:
    python tools/scrape_seek_jobs.py --classification 6281 --where "All Melbourne VIC"
    python tools/scrape_seek_jobs.py --url "https://au.seek.com/it-internship-jobs/in-Melbourne-VIC-3000"
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seek_client import (  # noqa: E402
    MAX_REACHABLE,
    SEARCH_URL,
    TMP_DIR,
    CloudflareChallenge,
    SeekClient,
    SeekError,
    discover_subclassifications,
    extract_jobs,
    extract_location_label,
    extract_total,
    normalise,
    paginate,
    parse_redux,
)

# Seek AU work types, used only to sub-split a partition that is still too big.
WORK_TYPES = [("242", "Full time"), ("243", "Part time"), ("244", "Contract/Temp"),
              ("245", "Casual/Vacation")]


def log(msg: str) -> None:
    """Progress goes to stderr so stdout stays a clean JSON contract."""
    print(msg, file=sys.stderr, flush=True)


def checkpoint(path: str, jobs: dict, meta: dict) -> None:
    """Write partial results to disk after each partition.

    Without this an interrupted run loses everything it has paid for -- which
    is exactly what happened once, destroying ~90 credits of collected pages.
    """
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or TMP_DIR, exist_ok=True)
        rows = [normalise(job) for job in jobs.values()]
        rows.sort(key=lambda r: r.get("listing_date", ""), reverse=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"run_meta": {**meta, "partial": True}, "jobs": rows},
                      fh, ensure_ascii=False, indent=2)
    except OSError as exc:  # never let a checkpoint failure kill the scrape
        log(f"      (checkpoint failed: {exc})")


def scrape_partitioned(client: SeekClient, classification: str, where: str,
                       max_partitions: int | None = None,
                       checkpoint_path: str = "") -> dict:
    """Enumerate a classification by splitting it into subclassifications."""
    log(f"[1/3] reading parent total for classification={classification} where={where!r}")
    base_params = {"classification": classification, "where": where}
    parent_page = client.fetch(SEARCH_URL, base_params)
    state = parse_redux(parent_page)
    parent_total = extract_total(state)
    parent_location = extract_location_label(state)

    if parent_total and not parent_location:
        raise SeekError(
            "location filter was dropped by Seek (SEO redirect) -- refusing to "
            "scrape an unfiltered result set"
        )
    log(f"      parent reports {parent_total} jobs")

    log("[2/3] discovering subclassifications")
    subclasses = discover_subclassifications(client, classification, where, page=parent_page)
    log(f"      {len(subclasses)} partitions found")

    if max_partitions and max_partitions < len(subclasses):
        subclasses = subclasses[:max_partitions]
        log(f"      limited to the first {max_partitions} (--max-partitions)")

    jobs: dict = {}
    partitions = []
    warnings = []

    log(f"[3/3] walking partitions (delay ~{client.delay}s, this takes a few minutes)")
    for idx, sub in enumerate(subclasses, 1):
        params = {**base_params, "subclassification": sub["id"]}
        result = paginate(client, params, max_total=parent_total or None)
        diag = result["diagnostics"]
        diag["subclassification"] = sub["label"]
        diag["subclassification_id"] = sub["id"]

        # A subclassification cannot contain more jobs than its own parent. If it
        # reports more, the filter was not applied and we are looking at a much
        # broader result set -- discard it rather than spend a full page-cap of
        # requests collecting jobs that do not belong to this search.
        if parent_total and diag["reported_total"] > parent_total:
            warnings.append(
                f"{sub['label']}: reported {diag['reported_total']} jobs, more than the "
                f"parent's {parent_total} -- not a real subclassification, discarded"
            )
            partitions.append(diag)
            log(f"      [{idx}/{len(subclasses)}] {sub['label']}: DISCARDED "
                f"({diag['reported_total']} > parent {parent_total})")
            continue

        # Seek dropped our filters on this request -- the results belong to some
        # broader search, so discard them rather than polluting the set.
        if diag["filters_dropped"]:
            warnings.append(f"{sub['label']}: Seek dropped the filters, partition discarded")
            partitions.append(diag)
            log(f"      [{idx}/{len(subclasses)}] {sub['label']}: DISCARDED (filters dropped)")
            continue

        for job in result["jobs"]:
            jobs.setdefault(str(job.get("id")), job)

        # Too big for one query -> split it further by work type.
        if diag["truncated"] or diag["reported_total"] > MAX_REACHABLE:
            log(f"      ! {sub['label']} exceeds the page cap; splitting by work type")
            recovered = 0
            for wt_id, wt_label in WORK_TYPES:
                sub_result = paginate(client, {**params, "worktype": wt_id})
                sub_diag = sub_result["diagnostics"]
                sub_diag["subclassification"] = f"{sub['label']} / {wt_label}"
                sub_diag["subclassification_id"] = sub["id"]
                for job in sub_result["jobs"]:
                    if str(job.get("id")) not in jobs:
                        recovered += 1
                    jobs.setdefault(str(job.get("id")), job)
                partitions.append(sub_diag)
                if sub_diag["truncated"]:
                    warnings.append(
                        f"{sub_diag['subclassification']} still exceeds the page cap "
                        f"({sub_diag['reported_total']} jobs) -- results incomplete"
                    )
            diag["work_type_split"] = True
            diag["recovered_by_split"] = recovered

        partitions.append(diag)
        log(
            f"      [{idx}/{len(subclasses)}] {sub['label']}: "
            f"{diag['collected']}/{diag['reported_total']} "
            f"({diag['pages_fetched']}p, {diag['stop_reason']}) | total unique {len(jobs)}"
        )
        checkpoint(checkpoint_path, jobs, {
            "mode": "partitioned", "classification": classification, "where": where,
            "parent_reported_total": parent_total, "unique_jobs": len(jobs),
            "partitions_done": idx, "partitions_total": len(subclasses),
            "coverage_pct": round(100.0 * len(jobs) / parent_total, 1) if parent_total else 0.0,
            "partitions": partitions, "warnings": warnings,
        })

    coverage = round(100.0 * len(jobs) / parent_total, 1) if parent_total else 100.0
    if coverage < 95:
        warnings.append(f"coverage {coverage}% is below 95% -- some jobs were not retrieved")

    return {
        "jobs": jobs,
        "run_meta": {
            "mode": "partitioned",
            "classification": classification,
            "where": where,
            "parent_reported_total": parent_total,
            "unique_jobs": len(jobs),
            "coverage_pct": coverage,
            "partitions": partitions,
            "warnings": warnings,
            "incomplete": bool(warnings),
        },
    }


def scrape_url(client: SeekClient, url: str) -> dict:
    """Enumerate a single Seek search URL as-is (no partitioning).

    Handles both the /jobs query form and SEO slug paths, which carry their
    filters in the path. Used for narrow searches and as the known-good
    cross-check against a hand-verified job count.
    """
    parsed = urlparse(url)
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    params.pop("page", None)
    base = f"https://au.seek.com{parsed.path}"

    log(f"[1/1] paginating {parsed.path}")
    result = paginate(client, params, base_url=base)
    diag = result["diagnostics"]
    diag["url"] = url

    jobs = {str(j.get("id")): j for j in result["jobs"]}
    total = diag["reported_total"]
    coverage = round(100.0 * len(jobs) / total, 1) if total else 100.0

    warnings = []
    if diag["truncated"]:
        warnings.append("hit the 17-page cap -- use --classification partitioning instead")

    return {
        "jobs": jobs,
        "run_meta": {
            "mode": "url",
            "url": url,
            "parent_reported_total": total,
            "unique_jobs": len(jobs),
            "coverage_pct": coverage,
            "partitions": [diag],
            "warnings": warnings,
            "incomplete": bool(warnings),
        },
    }


def scrape_saved_html(paths: list) -> dict:
    """Parse Seek search pages saved from a browser (Ctrl+S) instead of fetching.

    Needs no network at all, so it works while Cloudflare is blocking automated
    access. The saved page contains the same SEEK_REDUX_DATA blob a live fetch
    would return, so downstream processing is byte-for-byte identical.
    """
    jobs: dict = {}
    partitions = []
    total = 0

    for path in sorted(paths):
        with open(path, encoding="utf-8", errors="replace") as fh:
            state = parse_redux(fh.read())

        page_jobs = extract_jobs(state)
        page_total = extract_total(state)
        total = max(total, page_total)
        for job in page_jobs:
            jobs.setdefault(str(job.get("id")), job)

        partitions.append({
            "subclassification": os.path.basename(path),
            "collected": len(page_jobs),
            "reported_total": page_total,
            "pages_fetched": 1,
            "stop_reason": "saved_html",
            "location_label": extract_location_label(state),
            "truncated": False,
            "filters_dropped": False,
        })
        log(f"      {os.path.basename(path)}: {len(page_jobs)} jobs (search reports {page_total})")

    warnings = []
    if total and len(jobs) < total:
        warnings.append(
            f"saved pages hold {len(jobs)} of {total} jobs -- save the remaining "
            "result pages and re-run"
        )

    return {
        "jobs": jobs,
        "run_meta": {
            "mode": "saved_html",
            "source_files": [os.path.abspath(p) for p in sorted(paths)],
            "parent_reported_total": total,
            "unique_jobs": len(jobs),
            "coverage_pct": round(100.0 * len(jobs) / total, 1) if total else 100.0,
            "partitions": partitions,
            "warnings": warnings,
            "incomplete": bool(warnings),
        },
    }


def _scrape(client, args) -> dict:
    log(f"      backend: {client.backend}")
    if args.url:
        return scrape_url(client, args.url)
    return scrape_partitioned(
        client, args.classification, args.where,
        max_partitions=args.max_partitions, checkpoint_path=args.out,
    )


def run(args) -> dict:
    client = None
    # Each backend has its own safe pacing; only override when asked to.
    if args.delay is None:
        args.delay = 6.5 if args.firecrawl else 4.0

    if args.html:
        paths = []
        for entry in args.html:
            paths.extend(glob.glob(entry) if any(c in entry for c in "*?[") else [entry])
        missing = [p for p in paths if not os.path.isfile(p)]
        if not paths or missing:
            raise FileNotFoundError(f"no readable HTML files: {missing or args.html}")
        log(f"[1/1] parsing {len(paths)} saved page(s)")
        result = scrape_saved_html(paths)
    elif args.firecrawl:
        from seek_firecrawl import SeekFirecrawl

        with SeekFirecrawl(delay=args.delay, cache=not args.no_cache) as client:
            result = _scrape(client, args)
    else:
        client = SeekClient(delay=args.delay)
        result = _scrape(client, args)

    rows = [normalise(job) for job in result["jobs"].values()]
    rows.sort(key=lambda r: r.get("listing_date", ""), reverse=True)

    meta = result["run_meta"]
    meta["scraped_at"] = datetime.now(timezone.utc).isoformat()
    meta["requests_made"] = client.request_count if client else 0
    meta["backend"] = client.backend if client else "saved_html"
    if client is not None and getattr(client, "credits_used", None) is not None:
        meta["credits_used"] = client.credits_used

    for warning in meta["warnings"]:
        log(f"      WARNING: {warning}")
    log(
        f"      done: {len(rows)} unique jobs, coverage {meta['coverage_pct']}%, "
        f"{meta['requests_made']} requests"
    )

    return {"run_meta": meta, "jobs": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Seek job listings into JSON.")
    parser.add_argument("--classification", default="6281",
                        help="Seek classification id (6281 = Information & Communication Technology).")
    parser.add_argument("--where", default="All Melbourne VIC", help="Seek location string.")
    parser.add_argument("--url", help="Scrape one search URL as-is instead of partitioning.")
    parser.add_argument("--html", nargs="+", metavar="FILE",
                        help="Parse Seek search pages saved from a browser (paths or globs) "
                             "instead of fetching. Works with no network access.")
    parser.add_argument("--delay", type=float,
                        help="Seconds between requests. Defaults to 4.0 for direct HTTP and "
                             "6.5 for --firecrawl (whose free tier allows ~10/min).")
    parser.add_argument("--max-partitions", type=int, metavar="N",
                        help="Only walk the first N subclassifications. Use for cheap test "
                             "runs -- a full ICT sweep is ~56 credits, 5 partitions is ~15.")
    parser.add_argument("--no-cache", action="store_true",
                        help="Ignore the on-disk page cache and re-fetch everything (costs credits).")
    parser.add_argument("--firecrawl", action="store_true",
                        help="Fetch via the Firecrawl API (needs FIRECRAWL_API_KEY in .env). "
                             "Bypasses an IP-level Cloudflare block. ~1 credit per page.")
    parser.add_argument("--out", default=os.path.join(TMP_DIR, "seek_jobs.json"),
                        help="Path to write JSON output.")
    args = parser.parse_args()

    try:
        result = {"ok": True, "data": run(args)}
    except CloudflareChallenge as exc:
        print(json.dumps({"ok": False, "error": f"CloudflareChallenge: {exc}",
                          "retry_after_minutes": 30}), file=sys.stdout)
        return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), file=sys.stdout)
        return 1

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or TMP_DIR, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result["data"], fh, ensure_ascii=False, indent=2)
        result["written_to"] = args.out

    # Keep stdout small: the agent reads the summary, the file holds the jobs.
    summary = {"ok": True, "data": result["data"]["run_meta"],
               "written_to": result.get("written_to")}
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
