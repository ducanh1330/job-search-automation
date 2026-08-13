"""Template for a WAT tool.

Tools are deterministic: same inputs -> same behavior. They take arguments from
the CLI, do one job, and print a JSON result to stdout so the agent can read it.

Usage:
    python tools/_template.py --input "some value" [--out .tmp/result.json]
"""

import argparse
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

TMP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".tmp")


def run(value: str) -> dict:
    """Do the actual work. Raise on unrecoverable errors."""
    return {"echo": value}


def main() -> int:
    parser = argparse.ArgumentParser(description="One-line description of this tool.")
    parser.add_argument("--input", required=True, help="What this tool operates on.")
    parser.add_argument("--out", help="Optional path to write JSON output.")
    args = parser.parse_args()

    try:
        result = {"ok": True, "data": run(args.input)}
    except Exception as exc:  # surface a readable failure to the agent
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), file=sys.stdout)
        return 1

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or TMP_DIR, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        result["written_to"] = args.out

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
