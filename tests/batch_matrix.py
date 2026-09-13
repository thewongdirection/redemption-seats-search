"""Batch-test the skill across a random draw of routes and a run of consecutive months.

This drives the real command line (`search_awards.py` via `main()`) against the fake Partner API
in `fake_seats_aero.py`, then checks the invariants a user relies on: the run succeeds, every row
respects the cabin, date-window and party-size filters, the price ordering holds, the HTML report
is self-contained and escapes hostile text, the API key never reaches any output, and the markdown
re-render of the same run agrees with the JSON.

    python3 tests/batch_matrix.py --routes 10 --months 10 --pax 2 --seed 20260913

Exit code 0 when every scenario passes, 1 otherwise. No network access and no API key needed.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import random
import re
import sys
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import crosscheck  # noqa: E402
import fake_seats_aero  # noqa: E402
import search_awards  # noqa: E402

# Obviously-fake credential: the harness asserts this string never appears in any output.
TEST_API_KEY = "batch-matrix-placeholder-not-a-real-key"

# Major airports, with the multi-airport cities the skill is expected to accept as one side.
MAJOR_AIRPORTS = [
    "JFK,EWR", "LHR,LGW", "NRT,HND", "PEK,PKX", "LAX", "SFO", "ORD", "MIA", "DFW", "YYZ",
    "YVR", "SEA", "BOS", "ATL", "IAH", "GRU", "EZE", "SCL", "MEX", "CDG", "AMS", "FRA",
    "MUC", "ZRH", "FCO", "MAD", "BCN", "LIS", "IST", "DXB", "DOH", "AUH", "TLV", "CAI",
    "JNB", "CPT", "DEL", "BOM", "SIN", "BKK", "KUL", "CGK", "HKG", "TPE", "ICN", "PVG",
    "MNL", "SYD", "MEL", "AKL",
]

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


# --------------------------------------------------------------------------- matrix


def draw_routes(count: int, rng: random.Random) -> list[tuple[str, str]]:
    """Pick `count` origin/destination pairs with no airport shared between the two sides."""
    routes: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    while len(routes) < count:
        origin, destination = rng.sample(MAJOR_AIRPORTS, 2)
        if set(origin.split(",")) & set(destination.split(",")):
            continue
        if (origin, destination) in seen or (destination, origin) in seen:
            continue
        seen.add((origin, destination))
        routes.append((origin, destination))
    return routes


def month_windows(count: int, today: date) -> list[tuple[date, date]]:
    """The first and last day of each of the next `count` whole calendar months."""
    windows = []
    year, month = today.year, today.month
    for _ in range(count):
        month += 1
        if month > 12:
            year, month = year + 1, 1
        first = date(year, month, 1)
        next_first = date(year + (month == 12), month % 12 + 1, 1)
        windows.append((first, next_first - timedelta(days=1)))
    return windows


def scenario_flags(rng: random.Random) -> tuple[bool, tuple[str, ...], bool]:
    """Spread the awkward cases (no availability, API faults, hostile text) over the matrix."""
    empty = rng.random() < 0.12
    faults = tuple(
        fault for fault in
        ("search_rate_limited", "search_server_error", "trip_not_found", "refresh_partly_fails", "refresh_never_completes")
        if rng.random() < 0.25
    )
    return empty, faults, rng.random() < 0.25


# --------------------------------------------------------------------------- running the CLI


@contextlib.contextmanager
def _working_dir(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


@contextlib.contextmanager
def _fake_api(api: fake_seats_aero.FakeSeatsAero | None) -> Iterator[None]:
    """Point the script's client at the fake API and make its retry/poll sleeps instant."""
    real_client = search_awards.SeatsAeroClient
    if api is not None:
        def factory(api_key: str, **kwargs: Any) -> search_awards.SeatsAeroClient:
            kwargs.pop("opener", None)
            kwargs.pop("sleep", None)
            return real_client(api_key, opener=api.opener, sleep=lambda _seconds: None, **kwargs)
        search_awards.SeatsAeroClient = factory  # type: ignore[assignment]
    # resolve_api_key reads SEATS_AERO_API_KEY first, so setting it wins over any SEATS_API_KEY
    # the shell happens to carry; that one is deliberately left alone.
    previous_key = os.environ.get("SEATS_AERO_API_KEY")
    os.environ["SEATS_AERO_API_KEY"] = TEST_API_KEY
    try:
        yield
    finally:
        search_awards.SeatsAeroClient = real_client  # type: ignore[assignment]
        if previous_key is None:
            os.environ.pop("SEATS_AERO_API_KEY", None)
        else:
            os.environ["SEATS_AERO_API_KEY"] = previous_key


def run_cli(argv: Sequence[str], api: fake_seats_aero.FakeSeatsAero | None, workdir: Path) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with _working_dir(workdir), _fake_api(api), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = search_awards.main(list(argv))
    return code, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------- HTML checks


class _TagBalance(HTMLParser):
    """Enough of a parser to notice an unclosed or mismatched tag in the report."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.problems: list[str] = []
        self.rows = 0
        self.in_body = False
        self.external_assets: list[str] = []
        self.scripts = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script":
            self.scripts += 1
            if values.get("src"):
                self.external_assets.append(f"script src={values['src']}")
        if tag == "link" and (values.get("rel") or "").lower() == "stylesheet":
            self.external_assets.append(f"link href={values.get('href')}")
        if tag == "img" and values.get("src"):
            self.external_assets.append(f"img src={values['src']}")
        if tag == "tbody":
            self.in_body = True
        if tag == "tr" and self.in_body:
            self.rows += 1
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "tbody":
            self.in_body = False
        if tag in VOID_TAGS:
            return
        if not self.stack:
            self.problems.append(f"</{tag}> with nothing open")
        elif self.stack[-1] != tag:
            self.problems.append(f"</{tag}> closed while <{self.stack[-1]}> was open")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
        else:
            self.stack.pop()

    def close(self) -> None:  # type: ignore[override]
        super().close()
        if self.stack:
            self.problems.append(f"unclosed tags: {', '.join(self.stack)}")


CARD_RE = re.compile(r'<div class="card"><div class="label">(.*?)</div><div class="value">(.*?)</div>')


def summary_cards(html: str) -> dict[str, str]:
    return {label: value for label, value in CARD_RE.findall(html)}


def check_html(html: str, check: Callable[[bool, str], None], options: list[dict[str, Any]]) -> None:
    parser = _TagBalance()
    parser.feed(html)
    parser.close()
    check(not parser.problems, f"HTML is not well formed: {'; '.join(parser.problems[:3])}")
    check(not parser.external_assets, f"report is not self-contained: {parser.external_assets[:3]}")
    check(parser.scripts <= 1, f"the report carries {parser.scripts} scripts; only the inline column sorter belongs there")
    check(parser.rows == len(options), f"HTML shows {parser.rows} rows but the run returned {len(options)} options")
    check(fake_seats_aero.XSS_TEXT not in html, "hostile text from the API reached the report unescaped")
    check("javascript:" not in html, "a javascript: URL reached the report")
    check(TEST_API_KEY not in html, "the API key was written into the report")

    cards = summary_cards(html)
    if options:
        check(cards.get("Options found") == str(len(options)),
              f"the 'Options found' card says {cards.get('Options found')} but the run returned {len(options)} options")
        nonstop = sum(1 for option in options if option["stops"] == 0)
        check(cards.get("Nonstop options") == str(nonstop),
              f"the 'Nonstop options' card says {cards.get('Nonstop options')} but {nonstop} rows are nonstop")
    for option in options[:5]:
        book_url = option.get("book_url") or ""
        if book_url:
            check(f'href="{book_url}"' in html, f"the Book link for {option['program']} is missing from the report")
        check(search_awards.format_miles(option["mileage_cost"]) in html or option["mileage_cost"] == 0,
              f"the mileage price for {option['program']} is missing from the report")


# --------------------------------------------------------------------------- one scenario


class Scenario:
    def __init__(self, origin: str, destination: str, window: tuple[date, date], pax: int, seed: str, rng: random.Random) -> None:
        self.origin, self.destination = origin, destination
        self.start, self.end = window
        self.pax = pax
        self.seed = seed
        self.empty, self.faults, self.hostile = scenario_flags(rng)

    @property
    def label(self) -> str:
        return f"{self.origin}->{self.destination} {self.start:%Y-%m} pax{self.pax}"

    def build_api(self, now: datetime) -> fake_seats_aero.FakeSeatsAero:
        return fake_seats_aero.FakeSeatsAero(
            self.seed, self.origin, self.destination, self.start, self.end,
            now=now, empty=self.empty, faults=self.faults, hostile=self.hostile,
        )


def run_scenario(scenario: Scenario, workdir: Path, now: datetime) -> tuple[list[str], dict[str, Any]]:
    """Run one route/month search end to end and return (failures, facts about what it covered)."""
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(f"{scenario.label}: {message}")

    api = scenario.build_api(now)
    run_json = workdir / "runs" / f"{scenario.origin.replace(',', '-')}_{scenario.destination.replace(',', '-')}_{scenario.start:%Y%m}.json"
    run_json.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        scenario.origin, scenario.destination,
        "--date", scenario.start.isoformat(), "--end-date", scenario.end.isoformat(),
        "--pax", str(scenario.pax), "--json", "--quiet",
    ]
    code, stdout, stderr = run_cli(argv, api, workdir)
    check(code == 0, f"exit code {code} (stderr: {stderr.strip()[:200]})")
    if code != 0:
        return failures, {}

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as err:
        check(False, f"--json output is not valid JSON: {err}")
        return failures, {}

    options = payload.get("options") or []
    check(TEST_API_KEY not in stdout and TEST_API_KEY not in stderr, "the API key leaked into the run output")
    check(payload["query"]["pax"] == scenario.pax, "the run reported a different party size than requested")

    for option in options:
        where = f"row {option.get('program')} {option.get('travel_date')}"
        check(option["cabin"] in ("business", "first"), f"{where}: cabin {option['cabin']!r} is not premium")
        travel_date = option.get("travel_date", "")
        check(bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", travel_date)), f"{where}: travel date {travel_date!r} is not a date")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", travel_date):
            check(scenario.start <= date.fromisoformat(travel_date) <= scenario.end,
                  f"{where}: travel date {travel_date} is outside the requested window")
        seats = option.get("remaining_seats", 0)
        check(not (0 < seats < scenario.pax), f"{where}: only {seats} seat(s) for {scenario.pax} passengers")
        check(option.get("mileage_cost", 0) >= 0 and option.get("taxes_minor_units", 0) >= 0, f"{where}: negative price")
        if option.get("detail_level") == "trip":
            check(bool(option.get("flight_numbers")), f"{where}: trip-level row without flight numbers")

    notes = " ".join(payload.get("notes") or [])
    stale = [o for o in options if o.get("updated_at") and search_awards._hours_since(o["updated_at"]) > search_awards.STALE_AFTER_HOURS]
    check(bool(stale) == ("older than" in notes and "cached data" in notes),
          f"{len(stale)} stale row(s) but the staleness note is {'absent' if stale else 'present'}")
    hidden_seats = [o for o in options if not o.get("remaining_seats")]
    check(bool(hidden_seats) == ("does not publish a seat count" in notes),
          f"{len(hidden_seats)} row(s) hide their seat count but the note is {'absent' if hidden_seats else 'present'}")
    if scenario.pax > 1 and options:
        check(f"at least {scenario.pax} seats" in notes, "the party-size filter was not explained in the notes")

    # No cross-check runs here, so every row ranks the same; the flight-before-program part of the
    # ordering is covered in run_cross_check_checks.
    keys = [(crosscheck.CONFIRMATION_RANK.get(opt.get("confirmation", ""), 2),
             opt.get("mileage_cost") or 10**9, opt.get("taxes_minor_units", 0)) for opt in options]
    check(keys == sorted(keys), "rows are not ordered by miles, then taxes")

    identities = [(opt.get("trip_id"), opt.get("cabin")) for opt in options if opt.get("trip_id")]
    check(len(identities) == len(set(identities)), "the same itinerary appears more than once")

    report_path = payload.get("report_path")
    check(bool(report_path), "the run did not report an HTML path")
    html = ""
    if report_path:
        report = Path(report_path)
        if not report.is_absolute():
            report = workdir / report
        check(report.is_file(), f"HTML report missing at {report}")
        if report.is_file():
            html = report.read_text(encoding="utf-8")
            check_html(html, check, options)

    # The documented re-render path: same run, no quota, markdown for the chat reply.
    run_json.write_text(stdout, encoding="utf-8")
    code, markdown, stderr = run_cli(["--load", str(run_json), "--no-html", "--quiet"], None, workdir)
    check(code == 0, f"--load re-render failed with exit code {code} ({stderr.strip()[:160]})")
    table_rows = [line for line in markdown.splitlines() if line.startswith("| ") and not re.match(r"^\|\s*-+", line)]
    data_rows = max(len(table_rows) - 1, 0)   # minus the header row
    check(data_rows == len(options), f"markdown shows {data_rows} rows but the run returned {len(options)} options")
    check(TEST_API_KEY not in markdown, "the API key leaked into the markdown summary")
    if not options:
        check("No " in markdown or "no " in markdown, "an empty result did not explain itself")

    repeat_api = scenario.build_api(now)
    repeat_code, repeat_stdout, _repeat_err = run_cli(argv, repeat_api, workdir)
    if repeat_code == 0:
        repeat = json.loads(repeat_stdout)
        check([o["trip_id"] or o["availability_id"] for o in repeat.get("options") or []]
              == [o["trip_id"] or o["availability_id"] for o in options],
              "two runs over identical cached data returned different rows")

    facts = {
        "options": len(options),
        "summary_rows": sum(1 for o in options if o.get("detail_level") == "summary"),
        "unknown_seats": sum(1 for o in options if not o.get("remaining_seats")),
        "stale_rows": len(stale),
        "empty": not options,
        "paginated": api.search_calls > 2,
        "faults": scenario.faults,
        "hostile": scenario.hostile,
        "refresh_calls": api.refresh_calls,
        "api_calls": payload.get("api_calls", 0),
        "html_bytes": len(html),
    }
    return failures, facts


# --------------------------------------------------------------------------- option variants


VARIANTS: list[tuple[str, list[str], str]] = [
    ("direct-only", ["--direct-only"], "every row must be nonstop"),
    ("first-only", ["--cabins", "first"], "every row must be first class"),
    ("two-programs", [], "every row must come from a requested program"),   # programs are chosen from the data
    ("no-refresh", ["--no-refresh"], "no refresh call may be made"),
    ("flex", ["--flex", "3"], "every row must fall inside the flexible window"),
    ("schedule-opening", [], "no date means the schedule-opening window"),
    ("no-trip-detail", ["--max-trip-lookups", "0"], "every row must be a program-level summary"),
    ("nine-seats", ["--pax", "9"], "every row must seat nine or hide its count"),
    ("direct-first", ["--direct-only", "--cabins", "first"], "nonstop first class only"),
    ("custom-html", ["--html", "reports/custom report.html"], "the report must land at the given path"),
]


def run_variant(name: str, extra: list[str], origin: str, destination: str, window: tuple[date, date],
                pax: int, seed: str, workdir: Path, now: datetime) -> list[str]:
    """Run one search with non-default options and check the promise those options make."""
    failures: list[str] = []
    start, end = window

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(f"variant {name} ({origin}->{destination} {start:%Y-%m}): {message}")

    argv = [origin, destination, "--pax", str(pax), "--json", "--quiet"]
    if name == "schedule-opening":
        start = date.today() + timedelta(days=search_awards.SCHEDULE_OPENING_DAYS[0])
        end = date.today() + timedelta(days=search_awards.SCHEDULE_OPENING_DAYS[1])
    elif name == "flex":
        middle = start + timedelta(days=14)
        argv += ["--date", middle.isoformat()]
        start, end = middle - timedelta(days=3), middle + timedelta(days=3)
    else:
        argv += ["--date", start.isoformat(), "--end-date", end.isoformat()]

    api = fake_seats_aero.FakeSeatsAero(f"{seed}-{name}", origin, destination, start, end, now=now, records=120)
    wanted_sources: list[str] = []
    if name == "two-programs":
        present = sorted({record["Source"] for record in api.availabilities.values()})
        wanted_sources = present[:2]
        extra = extra + ["--sources", ",".join(wanted_sources)]
    argv += extra
    code, stdout, stderr = run_cli(argv, api, workdir)
    check(code == 0, f"exit code {code} ({stderr.strip()[:160]})")
    if code != 0:
        return failures
    payload = json.loads(stdout)
    options = payload.get("options") or []
    check(bool(options), "the variant produced no rows at all, so it proves nothing")

    if name in ("direct-only", "direct-first"):
        offenders = [o for o in options if o["stops"] > 0]
        example = f" (e.g. {offenders[0]['program']} {offenders[0]['travel_date']} stops={offenders[0]['stops']})" if offenders else ""
        check(not offenders, f"{len(offenders)} row(s) are connecting despite --direct-only{example}")
        # A "?" row is only allowed where seats.aero itself did not say whether the space is nonstop.
        for option in [o for o in options if o["stops"] < 0]:
            record = api.availabilities.get(option["availability_id"], {})
            code = search_awards.CABIN_CODES[option["cabin"]]
            check(f"{code}Direct" not in record,
                  f"{option['program']} {option['travel_date']} shows an unknown stop count although the record says "
                  f"{code}Direct={record.get(f'{code}Direct')}")
        if any(o["stops"] < 0 for o in options):
            check(any("no nonstop flag" in note for note in payload["notes"]),
                  "rows with an unknown stop count were not explained in the notes")
    if name in ("first-only", "direct-first"):
        offenders = [o for o in options if o["cabin"] != "first"]
        check(not offenders, f"{len(offenders)} row(s) are not first class despite --cabins first")
    if name == "two-programs":
        offenders = [o for o in options if o["source"] not in wanted_sources]
        check(not offenders, f"{len(offenders)} row(s) come from a program that was not requested")
        encoded = urllib.parse.quote(",".join(wanted_sources), safe="")
        check(all(f"sources={encoded}" in url for url in api.requests if "/search" in url),
              "the program filter was not passed to seats.aero")
    if name == "no-refresh":
        check(api.refresh_calls == 0, f"--no-refresh still made {api.refresh_calls} refresh call(s)")
        check(payload["refresh"] is None, "--no-refresh still reported a refresh outcome")
    if name == "nine-seats":
        offenders = [o for o in options if 0 < o["remaining_seats"] < 9]
        check(not offenders, f"{len(offenders)} row(s) seat fewer than the nine passengers requested")
    if name == "no-trip-detail":
        offenders = [o for o in options if o["detail_level"] != "summary"]
        check(not offenders, f"{len(offenders)} row(s) fetched trip detail despite --max-trip-lookups 0")
        check(api.trip_calls == 0, f"--max-trip-lookups 0 still made {api.trip_calls} trip call(s)")
    if name == "schedule-opening":
        check(payload["query"]["date_mode"] == "schedule-opening", "omitting --date did not select the schedule-opening window")
        check(any("354" in note for note in payload["notes"]), "the schedule-opening scan was not explained in the notes")
    if name == "custom-html":
        report = workdir / "reports" / "custom report.html"
        check(report.is_file(), f"no report at the requested path {report}")
        check(payload["report_path"] == "reports/custom report.html", "the run reported a different path than requested")

    for option in options:
        travel_date = option["travel_date"]
        check(start.isoformat() <= travel_date <= end.isoformat(),
              f"row dated {travel_date} is outside the {start}..{end} window this variant asked for")
    return failures


def run_no_html(workdir: Path, now: datetime) -> list[str]:
    """--no-html must write nothing and still print the summary."""
    api = fake_seats_aero.FakeSeatsAero("no-html", "SIN", "LHR", date.today() + timedelta(days=30), date.today() + timedelta(days=31), now=now)
    before = {path for path in workdir.rglob("*.html")}
    code, stdout, stderr = run_cli(["SIN", "LHR", "--date", (date.today() + timedelta(days=30)).isoformat(),
                                    "--pax", "2", "--no-html", "--quiet"], api, workdir)
    failures = []
    if code != 0:
        failures.append(f"--no-html run exited {code} ({stderr.strip()[:160]})")
    if {path for path in workdir.rglob("*.html")} - before:
        failures.append("--no-html still wrote an HTML report")
    if "|" not in stdout and "No " not in stdout:
        failures.append("--no-html printed neither a table nor an explanation")
    return failures


# --------------------------------------------------------------------------- cross-check with live FlightPoints captures


LIVE_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "live"


def _crosscheck_run(workdir: Path) -> Path:
    """A seats.aero run shaped to meet the live JFK->AMS captures: one row per outcome."""
    query = search_awards.SearchQuery(
        origin="JFK", destination="AMS", start_date=date(2026, 10, 15), end_date=date(2026, 10, 15),
        pax=2, date_mode="exact", refresh=False,
    )

    def option(cabin: str, source: str, miles: int, flights: str, taxes: int = 10000) -> search_awards.AwardOption:
        return search_awards.AwardOption(
            program=search_awards.PROGRAM_NAMES.get(source, source), source=source, cabin=cabin,
            travel_date="2026-10-15", route="JFK-AMS", airlines=["LO"], flight_numbers=flights,
            departs_at="2026-10-15T22:20:00Z", arrives_at="2026-10-16T18:50:00Z", duration_minutes=645,
            stops=1, remaining_seats=2, mileage_cost=miles, taxes_minor_units=taxes, taxes_currency="USD",
            booking_link="", availability_id=f"{source}-x", trip_id=f"{source}-{miles}",
            updated_at=_iso_now(),
        )

    result = search_awards.SearchResult(
        query=query,
        options=[
            option("business", "aeroplan", 75_000, "LO27, LO267"),        # flight match, same price
            option("business", "aeroplan", 58_800, "see program site"),   # program match on price
            option("business", "united", 95_000, "UA900"),                # price disagreement (FP says 90,000)
            option("first", "qatar", 120_000, "QR1"),                     # nothing in the captures confirms this
        ],
        notes=[], api_calls=0, availabilities_seen=4, generated_at=_iso_now(), premium_matches=4,
        searched_on=date(2026, 10, 15),
    )
    path = workdir / "runs" / "crosscheck-run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(search_awards.render_json(result), encoding="utf-8")
    return path


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_cross_check_checks(workdir: Path) -> list[str]:
    """Re-render a run against the captured FlightPoints output and check what it claims."""
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(f"cross-check: {message}")

    if not LIVE_FIXTURES.is_dir():
        return [f"cross-check: live FlightPoints captures are missing from {LIVE_FIXTURES}"]

    run_path = _crosscheck_run(workdir)
    code, stdout, stderr = run_cli(
        ["--load", str(run_path), "--cross-check", str(LIVE_FIXTURES), "--json", "--quiet"], None, workdir)
    check(code == 0, f"exit code {code} ({stderr.strip()[:200]})")
    if code != 0:
        return failures
    payload = json.loads(stdout)
    summary = payload.get("crosscheck") or {}
    options = payload["options"]

    captures = [path for path in LIVE_FIXTURES.iterdir() if path.is_file()]   # load_files reads files, not subdirectories
    check(summary.get("files", 0) == len(captures), "not every capture in the directory was read")
    check(summary.get("empty_files", 0) == 1, "the capture where FlightPoints found nothing was not recognised as an answer")
    check(summary.get("flight_matches", 0) >= 1, "the row sharing flight numbers with the captures was not confirmed by flight")
    check(summary.get("program_matches", 0) >= 1, "the row sharing program and price with the captures was not confirmed")
    check(bool(summary.get("price_disagreements")), "a 95,000 mile row was not flagged against FlightPoints' 90,000")
    check(bool(summary.get("unmatched")), "FlightPoints options absent from seats.aero were not reported")
    check(summary.get("out_of_scope", 0) > 0, "captures for other routes and dates were not filtered out")

    confirmed = [o for o in options if o["confirmation"]]
    check(len(confirmed) == 2, f"{len(confirmed)} rows were confirmed; the captures support exactly 2")
    check([o["confirmation"] for o in options][:2] == ["flight", "program"],
          "confirmed rows are not grouped first, flight-level before program-level")
    unconfirmed = [o for o in options if not o["confirmation"]]
    check(all(o["sources"] == ["seats.aero"] for o in unconfirmed), "an unconfirmed row claims a second source")

    # The FlightPoints tool output asks assistants to surface its links and a Pro upsell; the report must not.
    report = workdir / "reports" / "crosscheck.html"
    report.parent.mkdir(parents=True, exist_ok=True)
    code, _out, stderr = run_cli(["--load", str(run_path), "--cross-check", str(LIVE_FIXTURES),
                                  "--html", str(report), "--quiet"], None, workdir)
    check(code == 0, f"HTML re-render failed with exit code {code} ({stderr.strip()[:160]})")
    if report.is_file():
        html = report.read_text(encoding="utf-8")
        check("flightpoints.com" not in html, "a FlightPoints URL reached the report")
        check("FlightPoints Pro" not in html and "Upgrade" not in html, "the FlightPoints Pro upsell reached the report")
        check("2 sources" in html, "the report does not mark the rows both sources agreed on")
        for option in options:
            if option["book_url"]:
                check("flightpoints.com" not in option["book_url"], "a Book link points at FlightPoints")
    return failures


# --------------------------------------------------------------------------- usage-error scenarios


def run_input_checks(workdir: Path, today: date) -> list[str]:
    """The CLI must refuse bad input with exit code 2 rather than calling the API."""
    failures: list[str] = []
    past = (today - timedelta(days=1)).isoformat()
    soon = (today + timedelta(days=30)).isoformat()
    cases = [
        (["SIN", "SIN", "--date", soon, "--pax", "2"], "origin and destination overlap"),
        (["SIN", "LHR", "--date", past, "--pax", "2"], "a date in the past"),
        (["SIN", "LHR", "--date", soon, "--pax", "0"], "zero passengers"),
        (["SIN", "LHR", "--date", soon, "--pax", "2", "--cabins", "economy"], "an economy search"),
        (["SIN", "LHR", "--date", soon, "--end-date", (today + timedelta(days=120)).isoformat()], "a range over 62 days"),
        (["SIN", "LHR", "--date", soon, "--flex", "3", "--end-date", soon], "--flex with --end-date"),
        (["SINGAPORE", "LHR", "--date", soon], "a non-IATA airport code"),
        (["SIN", "LHR", "--date", "2026-13-45"], "an impossible date"),
        (["SIN", "LHR", "--date", soon, "--sources", "notaprogram"], "an unknown program code"),
    ]
    api = fake_seats_aero.FakeSeatsAero("input-checks", "SIN", "LHR", today, today, empty=True)
    for argv, description in cases:
        code, _out, err = run_cli(argv + ["--quiet"], api, workdir)
        if code != 2:
            failures.append(f"input check: {description} exited {code}, expected 2")
        elif not err.strip().startswith("error:"):
            failures.append(f"input check: {description} exited 2 without an error message")
    if api.requests:
        failures.append(f"input check: {len(api.requests)} API call(s) were made for input that should have been refused")
    return failures


# --------------------------------------------------------------------------- entry point


def run_matrix(
    *,
    routes: int = 10,
    months: int = 10,
    pax: int = 2,
    seed: str = "20260913",
    today: date | None = None,
    workdir: Path | None = None,
    log: Callable[[str], None] = lambda _message: None,
) -> tuple[list[str], dict[str, Any]]:
    today = today or date.today()
    now = datetime.now(timezone.utc)
    rng = random.Random(seed)
    drawn = draw_routes(routes, rng)
    windows = month_windows(months, today)
    workdir = workdir or Path.cwd()
    workdir.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    coverage = {
        "scenarios": 0, "options": 0, "empty_results": 0, "summary_rows": 0, "unknown_seats": 0,
        "paginated": 0, "with_faults": 0, "hostile": 0, "api_calls": 0, "stale_rows": 0, "variants": 0,
    }
    for origin, destination in drawn:
        for window in windows:
            scenario = Scenario(origin, destination, window, pax, seed, rng)
            scenario_failures, facts = run_scenario(scenario, workdir, now)
            failures.extend(scenario_failures)
            coverage["scenarios"] += 1
            if facts:
                coverage["options"] += facts["options"]
                coverage["stale_rows"] += facts["stale_rows"]
                coverage["empty_results"] += int(facts["empty"])
                coverage["summary_rows"] += facts["summary_rows"]
                coverage["unknown_seats"] += facts["unknown_seats"]
                coverage["paginated"] += int(facts["paginated"])
                coverage["with_faults"] += int(bool(facts["faults"]))
                coverage["hostile"] += int(facts["hostile"])
                coverage["api_calls"] += facts["api_calls"]
            log(f"{'ok  ' if not scenario_failures else 'FAIL'} {scenario.label}: {facts.get('options', 0)} rows")
    for index, (name, extra, _promise) in enumerate(VARIANTS):
        origin, destination = drawn[index % len(drawn)]
        window = windows[index % len(windows)]
        variant_failures = run_variant(name, list(extra), origin, destination, window, pax, seed, workdir, now)
        failures.extend(variant_failures)
        coverage["variants"] += 1
        log(f"{'ok  ' if not variant_failures else 'FAIL'} variant {name}")
    failures.extend(run_no_html(workdir, now))
    failures.extend(run_cross_check_checks(workdir))
    failures.extend(run_input_checks(workdir, today))
    coverage["routes"] = [f"{o}->{d}" for o, d in drawn]
    coverage["months"] = [f"{start:%Y-%m}" for start, _end in windows]
    return failures, coverage


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--routes", type=int, default=10)
    parser.add_argument("--months", type=int, default=10)
    parser.add_argument("--pax", type=int, default=2)
    parser.add_argument("--seed", default="20260913")
    parser.add_argument("--out", default=None, help="Directory for the reports the batch writes (default: a temp dir)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    workdir = Path(args.out).resolve() if args.out else Path(os.environ.get("TMPDIR", "/tmp")) / "batch-matrix"
    log = (lambda _message: None) if args.quiet else (lambda message: print(message, file=sys.stderr))
    failures, coverage = run_matrix(routes=args.routes, months=args.months, pax=args.pax, seed=args.seed, workdir=workdir, log=log)

    print(json.dumps({"failures": failures, "coverage": coverage}, indent=2))
    print(
        f"\n{coverage['scenarios']} scenarios, {coverage['options']} rows, {len(failures)} failure(s); reports under {workdir}",
        file=sys.stderr,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
