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

Exit codes: 0 ready to search, 2 something the user must fix (no key, python too old, a file that
cannot be read or deleted), 3 seats.aero unreachable or the key rejected. Warnings never fail the run.
`ready` without `verified` means the key was never put to seats.aero, so a search may still fail on it.

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
import crosscheck  # noqa: E402  (sibling module: it owns what a cross-check dump is called)
import search_awards as sa  # noqa: E402  (sibling module: the search tool itself)

SKILL_ROOT = Path(__file__).resolve().parent.parent
MIN_PYTHON = (3, 9)
GIT_TIMEOUT_SECONDS = 60.0
BATCH_MODE = "-oBatchMode=yes"

# What a search leaves behind, and how long any of it stays useful. Award space moves hourly, so a
# report from this morning is not evidence about this afternoon. Only the two names this skill
# actually writes: reports (sa.REPORT_GLOB owns that name) and the saved run SKILL.md step 5 dumps.
REPORT_PATTERNS = (sa.REPORT_GLOB, "run.json")
DEFAULT_MAX_AGE_HOURS = 12.0
# Cross-check dumps are scratch for one search: a file naming another route or date can only mislead
# the next one, so they expire fast. The pattern is crosscheck.DUMP_SUFFIX, the same set a search
# reads out of that directory - so nothing survives a flush only to confirm a row in the next search.
CROSSCHECK_DIR = "crosscheck"
CROSSCHECK_PATTERNS = (f"*{crosscheck.DUMP_SUFFIX}",)
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
    verified: bool = False     # True only once seats.aero has answered this key, live
    unverified: str = ""       # why not, in the words the closing line should use

    def add(self, step: str, status: str, detail: str, **extra: Any) -> None:
        self.steps.append({"step": step, "status": status, "detail": detail, **extra})

    def fail(self, step: str, detail: str, exit_code: int, **extra: Any) -> None:
        self.add(step, "blocked", detail, **extra)
        self.blocked = detail
        self.exit_code = exit_code

    def to_json(self) -> dict[str, Any]:
        return {"ready": self.exit_code == 0, "verified": self.verified, "unverified": self.unverified,
                "exit_code": self.exit_code, "blocked": self.blocked, "steps": self.steps}

    def to_text(self) -> str:
        marks = {"ok": "ok", "updated": "updated", "skipped": "skipped",
                 "note": "note", "warning": "warning", "blocked": "BLOCKED"}
        lines = [f"{marks.get(s['status'], s['status']):>8}  {s['step']}: {s['detail']}" for s in self.steps]
        lines.append("")
        if self.exit_code:
            lines.append(f"cannot search: {self.blocked}")
        elif self.verified:
            lines.append("ready to search")
        else:
            lines.append("ready to search, but the key was never checked against seats.aero "
                         f"({self.unverified or 'not checked'}), "
                         "so a search may still fail on it")
        return "\n".join(lines)


# --------------------------------------------------------------------------- 1. update


def git_env() -> dict[str, str]:
    """The caller's environment, with prompting turned off.

    A preflight runs unattended, so git must fail rather than stop for a password or a key
    passphrase. Whatever the user configured still wins: their askpass helper and their ssh command
    (which may carry the identity the remote needs) are kept, only extended with batch mode.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_ASKPASS", "echo")            # unset would mean "ask the terminal"
    env.setdefault("SSH_ASKPASS", "echo")
    ssh = env.get("GIT_SSH_COMMAND", "ssh")
    env["GIT_SSH_COMMAND"] = ssh if BATCH_MODE in ssh else f"{ssh} {BATCH_MODE}"
    return env


def git(args: Sequence[str], root: Path) -> subprocess.CompletedProcess[str]:
    """Run one git command. Never prompts, never hangs, never raises: failures come back as a returncode."""
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL, env=git_env())
    except (FileNotFoundError, PermissionError) as err:      # git is not installed, or not runnable
        return subprocess.CompletedProcess(args, 127, "", str(err))
    except subprocess.TimeoutExpired:
        # A blackholed network (captive portal, VPN down) makes fetch hang rather than fail. The
        # update is optional, so this is a warning like any other fetch failure - never a dead search.
        return subprocess.CompletedProcess(args, 124, "", f"git did not answer within {GIT_TIMEOUT_SECONDS:g}s")


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
    pair = _two_counts(counts.stdout)
    if pair is None:                                   # advice or a warning where two numbers belong
        report.add("update", "warning", f"could not read how far this checkout is from {tracking}")
        return
    ahead, behind = pair

    if behind == 0:
        report.add("update", "ok", f"already the newest version on {tracking} ({_short_sha(root)})")
        return
    if ahead:
        report.add("update", "warning",
                   f"{behind} newer commit(s) on {tracking}, but this checkout has {ahead} of its own; "
                   "not touching them - merge or rebase by hand")
        return
    status = git(["status", "--porcelain"], root)
    if status.returncode != 0:
        # An index lock or a half-finished rebase looks exactly like a clean tree on stdout alone.
        report.add("update", "warning", f"could not tell whether this checkout is clean ({_first_line(status.stderr)}); "
                                        "left alone so nothing of yours is lost")
        return
    if status.stdout.strip():
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


def _two_counts(text: str) -> tuple[int, int] | None:
    """The (ahead, behind) pair from `rev-list --left-right --count`, or None if that is not what came back."""
    fields = (text or "").split()
    if len(fields) != 2 or not all(f.lstrip("-").isdigit() for f in fields):
        return None
    return int(fields[0]), int(fields[1])


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
    removed, kept, freed = [], [], 0
    stale = [(path, path.name) for path in _expired(report_dir, REPORT_PATTERNS, now, max_age_hours * 3600, flush_all)]
    crosscheck = report_dir / CROSSCHECK_DIR
    if crosscheck.is_dir() and not crosscheck.is_symlink():
        stale += [(path, f"{CROSSCHECK_DIR}/{path.name}")
                  for path in _expired(crosscheck, CROSSCHECK_PATTERNS, now, crosscheck_max_age_minutes * 60, flush_all)]
    for path, label in stale:
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError as err:
            # Another session deleted it first, or it is not ours to remove. A leftover file is worth
            # a warning, never a refused search.
            kept.append(f"{label} ({err.strerror or err})")
            continue
        freed += size
        removed.append(label)

    if kept:
        report.add("flush", "warning",
                   f"cleared {len(removed)} stale file(s) from {report_dir}, but could not remove "
                   f"{len(kept)}: {_shorten(kept)}. A search still runs; delete them by hand if a stale "
                   "report keeps turning up.", removed=len(removed), kept=len(kept), directory=str(report_dir))
        return
    if not removed:
        report.add("flush", "ok", f"nothing stale in {report_dir}")
        return
    report.add("flush", "ok",
               f"cleared {len(removed)} stale file(s) ({freed // 1024} KB) from {report_dir}: {_shorten(removed)}",
               removed=len(removed), directory=str(report_dir))


def _shorten(names: Sequence[str], keep: int = 4) -> str:
    return ", ".join(names[:keep]) + (f" and {len(names) - keep} more" if len(names) > keep else "")


def _expired(directory: Path, patterns: Sequence[str], now: float, max_age_seconds: float, flush_all: bool) -> list[Path]:
    """Files under `directory` (never below it, never elsewhere) older than the cutoff."""
    if directory.is_symlink():
        # Resolving a symlinked directory would move the "never elsewhere" fence with it, so the
        # guard below would happily pass files in whatever it points at.
        return []
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
            report.verified = True          # rate limiting is applied to a recognised account
            report.add("seats.aero", "warning",
                       "the key is accepted but seats.aero is rate-limiting this account right now "
                       "(HTTP 429). The daily quota may be spent; a search may fail or return cached data only.")
        elif code >= 500:
            report.unverified = f"seats.aero returned {code}"
            report.add("seats.aero", "warning",
                       f"seats.aero returned {code}, so the key could not be checked; it is having trouble, "
                       "so a search may fail. Worth retrying in a few minutes.")
        else:
            report.fail("seats.aero", detail, 3)
        return
    report.verified = True
    report.add("seats.aero", "ok",
               f"answered a live search for {PROBE_ROUTE[0]}-{PROBE_ROUTE[1]}; the key works "
               f"({client.calls_made} call spent of the ~1,000/day quota)")


def note_checks_this_script_cannot_make(report: Report) -> None:
    """Two things no subprocess can settle. Status "note", not "ok": nothing here was verified."""
    report.add("connectors", "note", "MCP tools are not visible from here: confirm in-session that the "
                                     "FlightPoints tools answer before planning a cross-check (SKILL.md step 0)")
    report.add("freshness", "note", "search without --no-refresh unless the user asked for cached data (SKILL.md step 0)")


# --------------------------------------------------------------------------- entry point


def run(args: argparse.Namespace) -> Report:
    """Everything preflight does, on one Report. Never raises: a broken filesystem is a blocked step."""
    report = Report()
    try:
        _run_steps(args, report)
    except OSError as err:
        # A file this skill cannot read, write or delete: the user's to fix (exit 2), never exit 3,
        # which SKILL.md defines as seats.aero refusing. Steps already taken stay in the report, so
        # their warnings still reach the caller.
        report.fail("preflight", f"preflight could not finish: {err}", 2)
    return report


def _run_steps(args: argparse.Namespace, report: Report) -> None:
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
        return
    key = check_api_key(report)
    if report.exit_code:
        return
    if args.offline:
        report.unverified = "--offline"
        report.add("seats.aero", "skipped", "--offline: the key was not checked against the API, so nothing here "
                                            "says it still works")
    else:
        check_seats_aero(key, report, timeout=args.timeout)
    if report.exit_code:
        return
    note_checks_this_script_cannot_make(report)


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
    report = run(args)
    print(json.dumps(report.to_json(), indent=2) if args.json else report.to_text())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
