#!/usr/bin/env python3
"""Run before every search: update the skill, clear stale data, prove the search can work.

Three jobs, in this order:

1. **Update** - fetch the repository this skill was installed from and fast-forward to the newest
   version, so a search never runs on stale code. Only a clean checkout with no local commits is
   moved, and only along the branch it already tracks; anything else is reported and left alone.
2. **Flush** - delete reports, saved runs and cross-check dumps left over from earlier searches, so
   nothing stale can be re-rendered or matched against a new search by mistake.
3. **Check** - python version, API key, and one live call to seats.aero to prove the key still works
   and the network is there, before any quota is spent on a real search.

Exit codes: 0 ready to search, 2 something the user must fix (no key, python too old),
3 seats.aero unreachable or the key rejected. Warnings never fail the run.

    python3 scripts/preflight.py            # update, flush, check
    python3 scripts/preflight.py --json     # same, machine-readable
    python3 scripts/preflight.py --offline  # skip the update fetch and the live API call
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Sequence

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.append(_SCRIPT_DIR)   # append, so a caller's own modules keep priority
import search_awards as sa  # noqa: E402  (sibling module: the search tool itself)

SKILL_ROOT = Path(__file__).resolve().parent.parent
MIN_PYTHON = (3, 9)
GIT_TIMEOUT_SECONDS = 60.0

# What a search leaves behind, and how long any of it stays useful. Award space moves hourly, so a
# report from this morning is not evidence about this afternoon.
REPORT_PATTERNS = ("awards_*.html", "awards_*.json", "run.json", "run-*.json", "run_*.json")
DEFAULT_MAX_AGE_HOURS = 12.0
# Cross-check dumps are scratch for one search: a file naming another route or date can only mislead
# the next one, so they expire fast.
CROSSCHECK_DIR = "crosscheck"
DEFAULT_CROSSCHECK_MAX_AGE_MINUTES = 60.0

# The cheapest authenticated call that proves a key: one cached-search page, one row.
PROBE_ROUTE = ("JFK", "LHR")
PROBE_DAYS_OUT = 90


@dataclass
class Report:
    """What preflight did and what the caller should know before searching."""

    steps: list[dict[str, Any]] = field(default_factory=list)
    blocked: str = ""          # set when the search cannot run at all
    exit_code: int = 0

    def add(self, step: str, status: str, detail: str, **extra: Any) -> None:
        self.steps.append({"step": step, "status": status, "detail": detail, **extra})

    def fail(self, step: str, detail: str, exit_code: int, **extra: Any) -> None:
        self.add(step, "blocked", detail, **extra)
        self.blocked = detail
        self.exit_code = exit_code

    def to_json(self) -> dict[str, Any]:
        return {"ready": self.exit_code == 0, "exit_code": self.exit_code, "blocked": self.blocked, "steps": self.steps}

    def to_text(self) -> str:
        marks = {"ok": "ok", "updated": "updated", "skipped": "skipped", "warning": "warning", "blocked": "BLOCKED"}
        lines = [f"{marks.get(s['status'], s['status']):>8}  {s['step']}: {s['detail']}" for s in self.steps]
        lines.append("")
        lines.append("ready to search" if self.exit_code == 0 else f"cannot search: {self.blocked}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- 1. update


def git(args: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS)
    except (FileNotFoundError, PermissionError) as err:      # git is not installed, or not runnable
        return subprocess.CompletedProcess(args, 127, "", str(err))


def update_skill(root: Path, report: Report, offline: bool = False) -> None:
    """Fast-forward the skill to the newest version its remote offers, when that is safe to do."""
    toplevel = git(["rev-parse", "--show-toplevel"], root)
    if toplevel.returncode == 127:
        report.add("update", "skipped", "git is not installed, so this copy cannot check for a newer version")
        return
    if toplevel.returncode != 0 or not toplevel.stdout.strip():
        report.add("update", "skipped", f"{root} is not a git checkout, so there is no version to update from")
        return
    if Path(toplevel.stdout.strip()).resolve() != root.resolve():
        # Vendored inside someone else's repository: that repo is theirs, and fast-forwarding it would
        # rewrite files that have nothing to do with this skill.
        report.add("update", "skipped",
                   f"this skill sits inside {Path(toplevel.stdout.strip()).resolve()}, which is not its own "
                   "checkout; update it the way you update that repository")
        return
    if offline:
        report.add("update", "skipped", "--offline: did not contact the remote")
        return

    upstream = git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], root)
    if upstream.returncode != 0 or not upstream.stdout.strip():
        report.add("update", "skipped", "this branch tracks no remote branch, so there is nothing to update from")
        return
    tracking = upstream.stdout.strip()                 # e.g. "origin/main"
    remote, _, branch = tracking.partition("/")
    if not branch:
        report.add("update", "skipped", f"cannot read a remote branch from {tracking!r}")
        return

    fetch = git(["fetch", "--quiet", remote, branch], root)
    if fetch.returncode != 0:
        report.add("update", "warning", f"could not reach {remote} ({_first_line(fetch.stderr)}); running the version already here")
        return

    counts = git(["rev-list", "--left-right", "--count", f"HEAD...{tracking}"], root)
    if counts.returncode != 0:
        report.add("update", "warning", f"could not compare with {tracking} ({_first_line(counts.stderr)})")
        return
    ahead, behind = (int(n) for n in counts.stdout.split())

    if behind == 0:
        report.add("update", "ok", f"already the newest version on {tracking} ({_short_sha(root)})")
        return
    if ahead:
        report.add("update", "warning",
                   f"{behind} newer commit(s) on {tracking}, but this checkout has {ahead} of its own; "
                   "not touching them - merge or rebase by hand")
        return
    if git(["status", "--porcelain"], root).stdout.strip():
        report.add("update", "warning",
                   f"{behind} newer commit(s) on {tracking}, but this checkout has uncommitted changes; "
                   "left alone so nothing of yours is lost")
        return

    before = _short_sha(root)
    merged = git(["merge", "--ff-only", tracking], root)
    if merged.returncode != 0:
        report.add("update", "warning", f"fast-forward to {tracking} failed ({_first_line(merged.stderr)})")
        return
    report.add("update", "updated",
               f"pulled {behind} commit(s) from {tracking}: {before} -> {_short_sha(root)}. "
               "The search below runs the new version.",
               commits=behind)


def _short_sha(root: Path) -> str:
    return git(["rev-parse", "--short", "HEAD"], root).stdout.strip() or "unknown"


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0][:160] if (text or "").strip() else "no detail"


# --------------------------------------------------------------------------- 2. flush


def flush_stale_data(report_dir: Path, report: Report, max_age_hours: float, crosscheck_max_age_minutes: float,
                     flush_all: bool = False) -> None:
    """Delete what earlier searches left behind, so this search starts from nothing."""
    report_dir = report_dir.resolve()
    if not report_dir.is_dir():
        report.add("flush", "ok", f"no reports yet at {report_dir}; nothing to clear")
        return

    now = time.time()
    removed, freed = [], 0
    for path in _expired(report_dir, REPORT_PATTERNS, now, max_age_hours * 3600, flush_all):
        freed += path.stat().st_size
        path.unlink()
        removed.append(path.name)
    crosscheck = report_dir / CROSSCHECK_DIR
    if crosscheck.is_dir():
        for path in _expired(crosscheck, ("*",), now, crosscheck_max_age_minutes * 60, flush_all):
            freed += path.stat().st_size
            path.unlink()
            removed.append(f"{CROSSCHECK_DIR}/{path.name}")

    if not removed:
        report.add("flush", "ok", f"nothing stale in {report_dir}")
        return
    shown = ", ".join(removed[:4]) + (f" and {len(removed) - 4} more" if len(removed) > 4 else "")
    report.add("flush", "ok", f"cleared {len(removed)} stale file(s) ({freed // 1024} KB) from {report_dir}: {shown}",
               removed=len(removed), directory=str(report_dir))


def _expired(directory: Path, patterns: Sequence[str], now: float, max_age_seconds: float, flush_all: bool) -> list[Path]:
    """Files under `directory` (never below it, never elsewhere) older than the cutoff."""
    directory = directory.resolve()
    found: dict[Path, None] = {}
    for pattern in patterns:
        for path in directory.glob(pattern):
            if not path.is_file() or path.is_symlink():
                continue
            if path.resolve().parent != directory:      # refuse anything a glob walked outside
                continue
            if flush_all or now - path.stat().st_mtime > max_age_seconds:
                found[path] = None
    return list(found)


# --------------------------------------------------------------------------- 3. check


def check_python(report: Report) -> None:
    version = ".".join(str(n) for n in sys.version_info[:3])
    if sys.version_info < MIN_PYTHON:
        report.fail("python", f"python {version} is too old; this skill needs {'.'.join(str(n) for n in MIN_PYTHON)} or newer", 2)
        return
    report.add("python", "ok", f"python {version}")


def check_api_key(report: Report) -> str:
    try:
        key = sa.resolve_api_key()
    except sa.UsageError as err:
        report.fail("api key", str(err).splitlines()[0], 2)
        return ""
    source = next((name for name in sa.API_KEY_ENV_VARS if os.environ.get(name, "").strip()), str(sa.API_KEY_FILE))
    report.add("api key", "ok", f"found in {source} ({len(key)} characters)")
    return key


def check_seats_aero(key: str, report: Report, timeout: float = 30.0) -> None:
    """One cached-search call: proves the network is up and seats.aero still accepts the key."""
    # No retries: a preflight must answer now, and a 429 with a long Retry-After would otherwise
    # stall the search behind an hour of backoff.
    client = sa.SeatsAeroClient(key, timeout=timeout, max_retries=0)
    day = date.today() + timedelta(days=PROBE_DAYS_OUT)
    try:
        client.get("search", {"origin_airport": PROBE_ROUTE[0], "destination_airport": PROBE_ROUTE[1],
                              "start_date": day.isoformat(), "end_date": day.isoformat(), "take": 1})
    except (ValueError, OSError) as err:                 # unreadable body, or the socket died mid-read
        report.fail("seats.aero", f"seats.aero answered with something this skill could not read: {err}", 3)
        return
    except sa.SeatsAeroError as err:
        detail = str(err)
        status = re.search(r"HTTP (\d{3})", detail)
        code = int(status.group(1)) if status else 0
        if "rejected the API key" in detail:
            report.fail("seats.aero", f"{detail} Nothing was searched.", 3)
        elif code == 429:
            report.add("seats.aero", "warning",
                       "the key is accepted but seats.aero is rate-limiting this account right now "
                       "(HTTP 429). The daily quota may be spent; a search may fail or return cached data only.")
        elif code >= 500:
            report.add("seats.aero", "warning",
                       f"the key is accepted but seats.aero returned {code}; it is having trouble, so a "
                       "search may fail. Worth retrying in a few minutes.")
        else:
            report.fail("seats.aero", detail, 3)
        return
    report.add("seats.aero", "ok",
               f"answered a live search for {PROBE_ROUTE[0]}-{PROBE_ROUTE[1]}; the key works "
               f"({client.calls_made} call spent of the ~1,000/day quota)")


def note_connectors(report: Report) -> None:
    """MCP connectors live in the session, not in this process; Claude checks them itself."""
    report.add("connectors", "ok",
               "this script cannot see MCP tools - before searching, confirm in-session whether the "
               "FlightPoints tools answer; if they do not, run seats.aero-only and say so in the reply")


def note_freshness(report: Report) -> None:
    report.add("freshness", "ok",
               "run the search without --no-refresh so seats.aero re-scrapes the matching records first; "
               "only fall back to cached data when the user asks or the quota is nearly spent, and say which was used")


# --------------------------------------------------------------------------- entry point


def run(args: argparse.Namespace) -> Report:
    report = Report()
    if args.update:
        update_skill(SKILL_ROOT, report, offline=args.offline)
    else:
        report.add("update", "skipped", "--no-update: version check not run")

    if args.flush:
        flush_stale_data(Path(args.report_dir), report, args.max_age_hours, args.crosscheck_max_age_minutes, args.flush_all)
    else:
        report.add("flush", "skipped", "--no-flush: earlier reports and cross-check files were left in place")

    check_python(report)
    if report.exit_code:
        return report
    key = check_api_key(report)
    if report.exit_code:
        return report
    if args.offline:
        report.add("seats.aero", "skipped", "--offline: the key was not checked against the API")
    else:
        check_seats_aero(key, report, timeout=args.timeout)
    if report.exit_code:
        return report
    note_connectors(report)
    note_freshness(report)
    return report


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="preflight.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-update", dest="update", action="store_false", help="Do not fetch or fast-forward the skill")
    parser.add_argument("--no-flush", dest="flush", action="store_false", help="Keep reports and cross-check files from earlier searches")
    parser.add_argument("--flush-all", action="store_true", help="Clear every report and cross-check file, whatever its age")
    parser.add_argument("--report-dir", default=str(sa.DEFAULT_REPORT_DIR), help=f"Where reports are written. Default {sa.DEFAULT_REPORT_DIR}")
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS,
                        help=f"Clear reports and saved runs older than this. Default {DEFAULT_MAX_AGE_HOURS:g}")
    parser.add_argument("--crosscheck-max-age-minutes", type=float, default=DEFAULT_CROSSCHECK_MAX_AGE_MINUTES,
                        help=f"Clear cross-check files older than this. Default {DEFAULT_CROSSCHECK_MAX_AGE_MINUTES:g}")
    parser.add_argument("--offline", action="store_true", help="Skip the remote fetch and the live seats.aero call")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout for the live check, in seconds")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = run(args)
    except subprocess.TimeoutExpired:
        print("error: git did not answer in time; re-run with --no-update to search on the version already here",
              file=sys.stderr)
        return 3
    except OSError as err:
        print(f"error: preflight could not finish: {err}", file=sys.stderr)
        return 3
    print(json.dumps(report.to_json(), indent=2) if args.json else report.to_text())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
