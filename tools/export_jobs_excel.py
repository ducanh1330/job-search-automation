"""Turn scraped Seek jobs into a formatted, re-runnable Excel workbook.

Produces three sheets:
  Internships   -- early-career matches only (the working sheet)
  All Jobs      -- the full scraped set, so nothing is lost to an over-tight filter
  Run Summary   -- coverage diagnostics and warnings from the scrape

Re-runnable: a small JSON state file records when each job id was first and last
seen, so every row carries New / Existing / Closed. Jobs that disappear from Seek
are kept and marked Closed rather than vanishing from your sheet.

Usage:
    python tools/export_jobs_excel.py --input .tmp/seek_jobs.json --out "Seek Jobs.xlsx"
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone

from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

load_dotenv()

TMP_DIR = os.environ.get(
    "JOBSEARCH_TMP_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp"),
)

# All word-boundary anchored so "internal"/"international" never match "intern".
#
# Split into two tiers because matching weak terms anywhere in the body text
# produces false positives: a "Career Support Advisor" role whose blurb mentions
# helping "students" and "graduates" is not an early-career job. Strong terms
# name the job type itself and are trusted anywhere; weak terms only count when
# they appear in the job title.
STRONG_PATTERNS = {
    "internship": r"\binternships?\b",
    "intern": r"\binterns?\b",
    "grad program": r"\bgrad(uate)?\s+(program|programme|opportunit|role|position|intake)",
    "trainee": r"\btrainees?(hip)?\b",
    "cadet": r"\bcadets?(hip)?\b",
    "apprentice": r"\bapprentices?(hip)?\b",
    "entry level": r"\bentry[\s-]?level\b",
    "vacation program": r"\bvacation(er)?\s+(program|programme)\b",
    "no experience": r"\bno (prior |previous )?experience\b",
}

WEAK_PATTERNS = {
    "graduate": r"\bgraduates?\b",
    "junior": r"\bjunior\b",
    "student": r"\bstudents?\b",
    "placement": r"\b(industry |industrial )?placements?\b",
    "work experience": r"\bwork experience\b",
}

STRONG = {name: re.compile(pat, re.I) for name, pat in STRONG_PATTERNS.items()}
WEAK = {name: re.compile(pat, re.I) for name, pat in WEAK_PATTERNS.items()}

# Experience bands, checked against explicit year mentions in the ad.
YEARS_RE = re.compile(r"(\d{1,2})\s*(?:\+|to|-|–)?\s*(\d{1,2})?\s*(?:\+)?\s*years?", re.I)
NO_EXPERIENCE_RE = re.compile(
    r"\bno (prior |previous )?experience\b|\bentry[\s-]?level\b|\bfirst (job|role)\b", re.I
)

COLUMNS = [
    ("status", "Status", 9),
    ("title", "Job Title", 46),
    ("company", "Company", 26),
    ("position", "Position", 18),
    ("employment", "Employment", 21),
    ("location", "Location", 22),
    ("experience_level", "Experience", 12),
    ("subclassification", "Category", 24),
    ("salary", "Salary", 22),
    ("work_rights", "Work Rights", 21),
    ("summary", "Description Summary", 64),
    ("first_seen", "First Seen", 11),
    ("last_seen", "Last Seen", 11),
]

_KEYS = [c[0] for c in COLUMNS]
# 1-based indices of the prose column that wraps, the linked title, and the
# work-rights column that gets a warning fill.
WRAP_COLUMNS = [_KEYS.index("summary") + 1]
TITLE_COLUMN = _KEYS.index("title") + 1
RIGHTS_COLUMN = _KEYS.index("work_rights") + 1

# Position is a single label, chosen most-specific first, replacing the old
# multi-value match reason. The keys are the matcher's term names.
POSITION_ORDER = [
    ("Internship", {"internship", "intern"}),
    ("Graduate Program", {"grad program"}),
    ("Cadetship", {"cadet"}),
    ("Apprenticeship", {"apprentice"}),
    ("Traineeship", {"trainee"}),
    ("Vacation Program", {"vacation program"}),
    ("Work Experience", {"work experience", "placement", "placement (title)"}),
    ("Entry Level", {"entry level", "no experience"}),
    ("Graduate", {"graduate (title)"}),
    ("Junior", {"junior (title)"}),
    ("Student", {"student (title)"}),
]

# Work-rights values that should stand out: these are the ones that can
# disqualify an applicant, so they get a warning fill rather than plain text.
BLOCKING_RIGHTS = {"PR or citizen required", "No sponsorship", "Student visa not accepted"}

# Values worth highlighting positively -- these are the ones that open a role up
# to a visa holder rather than closing it.
FAVOURABLE_RIGHTS = {"Student visa accepted", "Sponsorship available"}

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(bold=True, color="FFFFFF")
NEW_FILL = PatternFill("solid", fgColor="E2EFDA")
CLOSED_FONT = Font(color="9C9C9C", italic=True)
LINK_FONT = Font(color="0563C1", underline="single")
BLOCKING_FILL = PatternFill("solid", fgColor="FCE4E4")
BLOCKING_FONT = Font(color="9C2B2B", bold=True)
FAVOURABLE_FILL = PatternFill("solid", fgColor="DDEFE0")
FAVOURABLE_FONT = Font(color="1E6B34", bold=True)

# Every cell gets ruled on all four sides so columns and rows read as a grid,
# rather than relying on Excel's non-printing background gridlines.
_thin = Side(style="thin", color="B7BFC8")
_header_side = Side(style="thin", color="1F3864")
CELL_BORDER = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
HEADER_BORDER = Border(
    left=_header_side, right=_header_side, top=_header_side,
    bottom=Side(style="medium", color="1F3864"),
)


def match_early_career(job: dict) -> list:
    """Return the early-career terms this job matches.

    Strong terms count anywhere in the listing; weak terms only in the title.
    A weak title match is reported with a `(title)` suffix so the Match Reason
    column shows why the row qualified.
    """
    title = str(job.get("title", ""))
    body = " ".join(
        str(job.get(f, "")) for f in ("title", "teaser", "bullet_points", "role_id")
    )

    matches = [name for name, rx in STRONG.items() if rx.search(body)]
    matches += [f"{name} (title)" for name, rx in WEAK.items() if rx.search(title)]
    return matches


def job_text(job: dict) -> str:
    """Best available text for a job: the real ad body if enriched, else the card."""
    return " ".join(
        str(job.get(f, ""))
        for f in ("title", "description_text", "teaser", "bullet_points")
    )


def derive_employment(job: dict) -> str:
    """'Full time (Hybrid)' -- work type with the arrangement folded in."""
    work_type = (job.get("work_type") or "").strip()
    arrangement = (job.get("work_arrangement") or "").strip()
    if work_type and arrangement:
        return f"{work_type} ({arrangement})"
    return work_type or arrangement or "N/A"


def derive_experience(job: dict, is_early: bool) -> str:
    """Band the required experience from explicit year mentions in the ad."""
    text = job_text(job)

    years = []
    for match in YEARS_RE.finditer(text):
        low = int(match.group(1))
        # Ignore things like "2026 years" or degree lengths over a decade.
        if low <= 20:
            years.append(low)

    if years:
        low = min(years)
        if low < 2:
            return "0-2 yrs"
        if low < 5:
            return "2-5 yrs"
        if low < 10:
            return "5-10 yrs"
        return "10+ yrs"

    if NO_EXPERIENCE_RE.search(text) or is_early:
        return "0-2 yrs"
    return "Not stated"


def is_closed(job: dict, now: datetime) -> bool:
    """True if the listing is no longer open, so it should be dropped.

    Three ways a job counts as closed: Seek flags it expired, its advertised
    expiry has passed, or it vanished from search results since the last run.
    """
    if job.get("status") == "Closed" or job.get("is_expired"):
        return True

    closes = job.get("closes_at") or ""
    if closes:
        try:
            return datetime.fromisoformat(closes.replace("Z", "+00:00")) < now
        except ValueError:
            return False
    return False


def derive_position(matches: list) -> str:
    """Collapse the matched terms into one position label, most specific first."""
    found = set(matches)
    for label, terms in POSITION_ORDER:
        if found & terms:
            return label
    return ""


def derive_summary(job: dict, limit: int = 320) -> str:
    """One condensed paragraph: the ad's own summary, else its opening sentences."""
    teaser = (job.get("teaser") or "").strip()
    if teaser:
        summary = teaser
    else:
        body = (job.get("description_text") or "").strip()
        sentences = re.split(r"(?<=[.!?])\s+", body)
        summary = " ".join(sentences[:3]).strip()

    if not summary:
        bullets = (job.get("bullet_points") or "").replace(" | ", ". ")
        summary = bullets.strip()

    summary = re.sub(r"\s+", " ", summary)
    if len(summary) > limit:
        summary = summary[: limit - 1].rsplit(" ", 1)[0] + "…"
    return summary


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def apply_tracking(jobs: list, state: dict, today: str) -> list:
    """Stamp each job with New/Existing plus first/last seen, and re-add Closed jobs."""
    seen_now = set()
    rows = []

    for job in jobs:
        job_id = job["job_id"]
        seen_now.add(job_id)
        record = state.get(job_id)

        if record is None:
            job["status"] = "New"
            job["first_seen"] = today
            state[job_id] = {"first_seen": today, "last_seen": today}
        else:
            job["status"] = "Existing"
            job["first_seen"] = record.get("first_seen", today)
            record["last_seen"] = today

        job["last_seen"] = today
        rows.append(job)

    # Jobs previously seen but absent now: keep them, marked Closed.
    for job_id, record in state.items():
        if job_id in seen_now or not record.get("snapshot"):
            continue
        closed = dict(record["snapshot"])
        closed.update(
            {
                "status": "Closed",
                "first_seen": record.get("first_seen", ""),
                "last_seen": record.get("last_seen", ""),
            }
        )
        rows.append(closed)

    # Store a snapshot so a job can still be rendered after it disappears.
    for job in jobs:
        state[job["job_id"]]["snapshot"] = job

    return rows


def _format_date(iso: str) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return iso[:10]


def _write_sheet(ws, rows: list) -> None:
    ws.append([label for _, label, _ in COLUMNS])
    for idx, (_, _, width) in enumerate(COLUMNS, 1):
        letter = get_column_letter(idx)
        ws.column_dimensions[letter].width = width
        cell = ws.cell(row=1, column=idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.border = HEADER_BORDER
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 26

    for job in rows:
        ws.append([job.get(key, "") for key, _, _ in COLUMNS])
        r = ws.max_row
        status = job.get("status")

        for c in range(1, len(COLUMNS) + 1):
            cell = ws.cell(row=r, column=c)
            cell.border = CELL_BORDER
            cell.alignment = Alignment(vertical="top")
            if status == "New":
                cell.fill = NEW_FILL
            elif status == "Closed":
                cell.font = CLOSED_FONT

        # Make the title itself the clickable link -- less visual noise than a
        # separate link column, and the raw URL is still there to copy.
        if job.get("job_url"):
            title_cell = ws.cell(row=r, column=TITLE_COLUMN)
            title_cell.hyperlink = job["job_url"]
            title_cell.font = LINK_FONT

        # Flag work rights in both directions: red for values that disqualify an
        # applicant outright, green for ones that explicitly open the role up.
        rights = job.get("work_rights")
        if rights in BLOCKING_RIGHTS:
            cell = ws.cell(row=r, column=RIGHTS_COLUMN)
            cell.fill = BLOCKING_FILL
            cell.font = BLOCKING_FONT
        elif rights in FAVOURABLE_RIGHTS:
            cell = ws.cell(row=r, column=RIGHTS_COLUMN)
            cell.fill = FAVOURABLE_FILL
            cell.font = FAVOURABLE_FONT

        for col in WRAP_COLUMNS:
            ws.cell(row=r, column=col).alignment = Alignment(wrap_text=True, vertical="top")

    ws.freeze_panes = "A2"
    if ws.max_row >= 1:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{ws.max_row}"


def _write_summary(ws, meta: dict, counts: dict) -> None:
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 74

    def row(label, value, bold=False):
        ws.append([label, value])
        if bold:
            ws.cell(row=ws.max_row, column=1).font = Font(bold=True)

    row("Run Summary", "", bold=True)
    row("Scraped at (UTC)", meta.get("scraped_at", ""))
    row("Mode", meta.get("mode", ""))
    row("Search", meta.get("url") or f"classification={meta.get('classification')} / {meta.get('where')}")
    row("Seek reported total", meta.get("parent_reported_total", 0))
    row("Unique jobs retrieved", meta.get("unique_jobs", 0))
    row("Coverage", f"{meta.get('coverage_pct', 0)}%")
    row("Requests made", meta.get("requests_made", 0))
    ws.append([])
    row("Rows written", "", bold=True)
    row("Early-career matches", counts.get("internships", 0))
    row("All jobs", counts.get("all", 0))
    row("New this run", counts.get("new", 0))
    row("Closed/expired, dropped", counts.get("closed", 0))
    ws.append([])

    warnings = meta.get("warnings") or []
    row("Warnings", "none" if not warnings else "", bold=True)
    for warning in warnings:
        ws.append(["", warning])
        ws.cell(row=ws.max_row, column=2).font = Font(color="C00000")

    ws.append([])
    row("Partition breakdown", "", bold=True)
    ws.append(["Partition", "collected / reported (pages, stop reason)"])
    ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
    ws.cell(row=ws.max_row, column=2).font = Font(bold=True)
    for part in meta.get("partitions", []):
        name = part.get("subclassification") or part.get("url") or str(part.get("params"))
        ws.append([
            name,
            f"{part.get('collected')} / {part.get('reported_total')} "
            f"({part.get('pages_fetched')}p, {part.get('stop_reason')})",
        ])


def run(args) -> dict:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from enrich_job_details import load_merged

    payload = load_merged(args.input)
    meta = payload.get("run_meta", {})
    jobs = payload.get("jobs", [])

    for job in jobs:
        matches = match_early_career(job)
        job["match_reason"] = ", ".join(matches)
        job["position"] = derive_position(matches)
        job["employment"] = derive_employment(job)
        job["experience_level"] = derive_experience(job, bool(matches))
        job["summary"] = derive_summary(job)
        # "N/A" rather than blank so a missing salary reads as absent, not skipped.
        job["salary"] = (job.get("salary") or "").strip() or "N/A"
        job["work_rights"] = job.get("work_rights") or "Not checked"
        job["work_rights_evidence"] = job.get("work_rights_evidence") or ""

    today = date.today().isoformat()
    state = load_state(args.state)
    rows = apply_tracking(jobs, state, today)

    # Closed and expired listings are dropped rather than shown greyed out --
    # you can't apply to them, so they're noise. The count is reported instead.
    now = datetime.now(timezone.utc)
    dropped = [r for r in rows if is_closed(r, now)]
    rows = [r for r in rows if not is_closed(r, now)]

    # Newest first, then stable-sorted so New floats to the top and Closed sinks.
    status_rank = {"New": 0, "Existing": 1, "Closed": 2}
    rows.sort(key=lambda r: r.get("listing_date", ""), reverse=True)
    rows.sort(key=lambda r: status_rank.get(r.get("status"), 1))

    internships = [r for r in rows if r.get("match_reason")]

    wb = Workbook()
    ws_int = wb.active
    ws_int.title = "Internships"
    _write_sheet(ws_int, internships)
    _write_sheet(wb.create_sheet("All Jobs"), rows)

    counts = {
        "internships": len(internships),
        "all": len(rows),
        "new": sum(1 for r in rows if r.get("status") == "New"),
        "closed": len(dropped),
    }
    _write_summary(wb.create_sheet("Run Summary"), meta, counts)

    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    wb.save(args.out)

    os.makedirs(os.path.dirname(os.path.abspath(args.state)), exist_ok=True)
    with open(args.state, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)

    return {
        "excel": os.path.abspath(args.out),
        "internships": counts["internships"],
        "all_jobs": counts["all"],
        "new": counts["new"],
        "closed": counts["closed"],
        "coverage_pct": meta.get("coverage_pct"),
        "warnings": meta.get("warnings", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export scraped Seek jobs to a formatted Excel workbook.")
    parser.add_argument("--input", nargs="+", default=[os.path.join(TMP_DIR, "seek_jobs.json")],
                        help="One or more JSON files from scrape_seek_jobs.py. "
                             "Multiple are merged and de-duplicated by job id.")
    parser.add_argument("--out", default="Seek IT Internships Melbourne.xlsx",
                        help="Path of the .xlsx to write.")
    parser.add_argument("--state", default=os.path.join(TMP_DIR, "seek_seen.json"),
                        help="JSON state file tracking first/last seen job ids.")
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
