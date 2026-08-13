# Workflow: <Name>

## Objective
What this workflow accomplishes, in one or two sentences.

## Required Inputs
- `input_name` — what it is, where it comes from, whether it's optional.

## Tools Used
| Step | Tool | Purpose |
|------|------|---------|
| 1 | `tools/<script>.py` | ... |

## Steps
1. Confirm the required inputs are present. Ask the user for anything missing.
2. Run `python tools/<script>.py --input ...`.
3. Read the JSON result; if `ok` is false, see Edge Cases.
4. Deliver the output (cloud service link, not a local file).

## Expected Output
What the user should end up with, and where it lives.

## Edge Cases & Gotchas
- **<failure mode>** — what it looks like, what to do about it.
- Record rate limits, timing quirks, and API surprises here as they're discovered.

## Change Log
- YYYY-MM-DD — created.
