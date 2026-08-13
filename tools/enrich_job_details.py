"""Enrich scraped Seek jobs with work-rights, visa and closing-date detail.

The search listing does not carry any visa or work-rights information -- it only
exists on each job's own page. This tool fetches those pages and extracts:

  work_rights            a classification (see RULES below)
  work_rights_evidence   the sentence it was derived from, so you can check it
  screening_questions    the employer's application questions
  closes_at              the listing expiry date

The classification is INFERRED FROM ADVERTISER TEXT, not a structured Seek
field. Always read the evidence column before relying on it, and treat
"Not stated" as unknown rather than permissive.

Fetching costs ~1 credit per job, so by default only early-career matches are
enriched, and every fetched page is cached in .tmp/details_cache/ -- re-runs
cost nothing for jobs already seen.

Usage:
    python tools/enrich_job_details.py --input .tmp/seek_jobs.json --out .tmp/seek_jobs.json
"""

import argparse
import html as ihtml
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from export_jobs_excel import match_early_career  # noqa: E402
from seek_client import TMP_DIR, parse_redux  # noqa: E402

CACHE_DIR = os.path.join(TMP_DIR, "details_cache")

# Ordered most-specific first: the first rule that matches wins, so an explicit
# "no sponsorship" beats a generic "right to work" mention in the same ad.
RULES = [
    ("PR or citizen required", [
        r"\b(permanent resident|australian citizen|pr or citizen|citizen or permanent)\w*\b",
        r"\bpermanent residency\b",
    ]),
    ("No sponsorship", [
        r"\b(not|unable to|cannot|won'?t|do not)\s+(be able to\s+)?(offer|provide|consider)?\s*sponsor\w*",
        r"\bno (visa )?sponsorship\b",
        r"\bsponsorship is not\b",
    ]),
    ("Sponsorship available", [
        r"\bsponsorship (is )?(available|offered|provided|considered)\b",
        r"\bwe (can |will )?sponsor\b",
        r"\bvisa sponsorship\b(?!.{0,30}\bnot\b)",
    ]),
    ("Full work rights required", [
        r"\b(full|unrestricted|unlimited)\s+(working|work)\s+rights?\b",
        r"\brights? to work in australia\b",
        r"\bright to work\b",
        r"\bwork authorisation\b",
        r"\beligible to work in australia\b",
    ]),
    ("Student visa considered", [
        r"\bstudent visa\b",
        r"\bcurrently studying\b.{0,60}\bvisa\b",
    ]),
]

SENTENCE_PATTERN = (
    r"[^.!?\n]*\b(?:permanent resident\w*|australian citizen\w*|citizenship|working rights?|"
    r"right to work|work authorisation|visa|sponsor\w*|eligible to work)\b[^.!?\n]*[.!?]?"
)

WORK_RIGHTS_QUESTION = re.compile(r"right to work|working rights|visa|citizen", re.I)


def html_to_text(markup: str) -> str:
    """Strip tags, then unescape, then collapse whitespace -- in that order.

    Unescaping last leaves &nbsp; as U+00A0 after the collapse has already run,
    which glues words together in the evidence excerpts. Collapsing must also
    cover non-breaking and zero-width spaces, which plain \\s+ leaves behind.
    """
    text = re.sub(r"<[^>]+>", " ", markup or "")
    text = ihtml.unescape(text)
    return re.sub(r"[\s ​‌﻿]+", " ", text).strip()


def excerpt_around(text: str, match: re.Match, before: int = 130, after: int = 190) -> str:
    """Quote the text surrounding a match, centred on the match itself.

    Centring matters: many ads are semicolon-separated bullet runs with no full
    stops, so a naive sentence grab plus truncation can return 400 characters
    that never contain the phrase the classification was based on.
    """
    start = max(0, match.start() - before)
    end = min(len(text), match.end() + after)
    excerpt = text[start:end].strip()
    if start > 0:
        excerpt = "…" + excerpt
    if end < len(text):
        excerpt = excerpt + "…"
    return excerpt


def classify_work_rights(text: str, questions: list) -> tuple:
    """Return (classification, evidence excerpt centred on the matched phrase)."""
    for label, patterns in RULES:
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                return label, excerpt_around(text, match)

    if any(WORK_RIGHTS_QUESTION.search(q) for q in questions):
        return "Asked at application", next(
            (q for q in questions if WORK_RIGHTS_QUESTION.search(q)), ""
        )

    return "Not stated", ""


def extract_details(raw_html: str) -> dict:
    state = parse_redux(raw_html)
    result = (state.get("jobdetails") or {}).get("result") or {}
    job = result.get("job") or {}

    questions = ((job.get("products") or {}).get("questionnaire") or {}).get("questions") or []
    text = html_to_text(job.get("content") or job.get("content2") or "")
    label, evidence = classify_work_rights(text, questions)

    return {
        "work_rights": label,
        "work_rights_evidence": evidence,
        "screening_questions": " | ".join(questions),
        "closes_at": (job.get("expiresAt") or {}).get("dateTimeUtc", ""),
        "is_expired": bool(job.get("isExpired")),
        # Kept so the exporter can derive skill tags, experience level and the
        # summary from the real ad text rather than the marketing teaser.
        "description_text": text[:4000],
    }


def cache_path(job_id: str) -> str:
    return os.path.join(CACHE_DIR, f"{job_id}.html")


def load_merged(paths: list) -> dict:
    """Load one or more scrape files, de-duplicating jobs by id.

    A narrow keyword search and a broad classification sweep return overlapping
    sets; merging on id means each job is enriched once, not once per source.
    """
    merged: dict = {}
    metas = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        metas.append(payload.get("run_meta", {}))
        for job in payload.get("jobs", []):
            existing = merged.get(job["job_id"])
            # Keep whichever copy already carries enrichment.
            if existing and existing.get("work_rights") and not job.get("work_rights"):
                continue
            merged[job["job_id"]] = {**existing, **job} if existing else job

    base = dict(metas[0]) if metas else {}
    if len(metas) > 1:
        base["sources"] = [m.get("url") or m.get("where") or "?" for m in metas]
        base["parent_reported_total"] = sum(m.get("parent_reported_total", 0) for m in metas)
        base["warnings"] = [w for m in metas for w in m.get("warnings", [])]
    base["unique_jobs"] = len(merged)
    return {"run_meta": base, "jobs": list(merged.values())}


def run(args) -> dict:
    payload = load_merged(args.input)
    jobs = payload.get("jobs", [])
    targets = jobs
    if not args.all:
        targets = [j for j in jobs if match_early_career(j)]
    if args.limit:
        targets = targets[: args.limit]

    os.makedirs(CACHE_DIR, exist_ok=True)
    by_id = {j["job_id"]: j for j in jobs}

    client = None
    fetched = cached = failed = 0

    for idx, job in enumerate(targets, 1):
        job_id = job["job_id"]
        path = cache_path(job_id)

        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
            cached += 1
        else:
            if client is None:
                from seek_firecrawl import SeekFirecrawl

                client = SeekFirecrawl(delay=args.delay)
            try:
                raw = client.fetch(job["job_url"])
            except Exception as exc:
                print(f"      [{idx}/{len(targets)}] {job_id}: FAILED {type(exc).__name__}",
                      file=sys.stderr, flush=True)
                failed += 1
                continue
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(raw)
            fetched += 1

        try:
            by_id[job_id].update(extract_details(raw))
        except Exception as exc:
            print(f"      [{idx}/{len(targets)}] {job_id}: parse failed {exc}",
                  file=sys.stderr, flush=True)
            failed += 1
            continue

        if idx % 10 == 0 or idx == len(targets):
            print(f"      [{idx}/{len(targets)}] fetched={fetched} cached={cached} failed={failed}",
                  file=sys.stderr, flush=True)

    payload.setdefault("run_meta", {})["enriched"] = {
        "targets": len(targets),
        "fetched": fetched,
        "from_cache": cached,
        "failed": failed,
        "credits_used": getattr(client, "credits_used", 0),
    }

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    breakdown = {}
    for job in jobs:
        if job.get("work_rights"):
            breakdown[job["work_rights"]] = breakdown.get(job["work_rights"], 0) + 1

    return {
        "enriched": len([j for j in jobs if j.get("work_rights")]),
        "fetched": fetched,
        "from_cache": cached,
        "failed": failed,
        "credits_used": getattr(client, "credits_used", 0),
        "work_rights_breakdown": breakdown,
        "written_to": args.out,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add work-rights/visa and closing-date detail to scraped Seek jobs."
    )
    parser.add_argument("--input", nargs="+", default=[os.path.join(TMP_DIR, "seek_jobs.json")],
                        help="One or more scrape files. Multiple are merged and de-duplicated by job id.")
    parser.add_argument("--out", default=os.path.join(TMP_DIR, "seek_jobs.json"))
    parser.add_argument("--all", action="store_true",
                        help="Enrich every job, not just early-career matches. Costs far more.")
    parser.add_argument("--limit", type=int, help="Cap how many jobs to enrich this run.")
    parser.add_argument("--delay", type=float, default=1.0)
    args = parser.parse_args()

    try:
        result = {"ok": True, "data": run(args)}
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), file=sys.stdout)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
