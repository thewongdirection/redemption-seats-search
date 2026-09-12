#!/usr/bin/env python3
"""Find business- and first-class award (redemption) seats via the seats.aero Partner API.

Usage:
    search_awards.py SIN LHR --date 2026-11-14 --pax 2

The script calls the seats.aero "Cached Search" endpoint for the route and date,
keeps only availability objects that report business (J) or first (F) space,
then pulls flight-level trip detail for each of those so the answer names the
airline, flight numbers, times, seats, mileage cost, taxes and a booking link.

It deliberately never looks at economy (Y) or premium economy (W) space.

Authentication: a seats.aero Partner API key (seats.aero Pro membership) read from
the SEATS_AERO_API_KEY environment variable, or from ~/.config/seats-aero/api_key.
The key is never accepted on the command line (it would leak into shell history
and process listings) and is never printed.

Only the Python standard library is used, so there is nothing to install.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import crosscheck  # noqa: E402  (sibling module: FlightPoints cross-check parsing and matching)

BASE_URL = "https://seats.aero/partnerapi"
API_KEY_ENV_VARS = ("SEATS_AERO_API_KEY", "SEATS_API_KEY")
API_KEY_FILE = Path.home() / ".config" / "seats-aero" / "api_key"

# Premium cabins only. Economy (Y) and premium economy (W) are intentionally absent.
CABIN_CODES = {"business": "J", "first": "F"}
DEFAULT_CABINS = ("business", "first")

MAX_PAGES = 20            # safety cap on cached-search pagination
REFRESH_BATCH = 250        # seats.aero accepts at most 250 availability IDs per refresh request
REFRESH_POLL_SECONDS = 5.0
# Every search re-scrapes its matches by default so the report reflects what the programs
# show right now, not what seats.aero happened to cache. 0 hours means "everything that matched".
DEFAULT_REFRESH_OLDER_THAN_HOURS = 0.0
DEFAULT_REFRESH_TIMEOUT_SECONDS = 120.0
MAX_REFRESH_RECORDS = 100  # each refreshed record spends one call of the 1,000/day quota; oldest go first
# With no --date, scan the far edge of the booking window: most airlines load award
# inventory 354-355 days ahead, so this is where fresh premium space first appears.
SCHEDULE_OPENING_DAYS = (354, 355)
DEFAULT_TRIP_LOOKUPS = 40  # cap on /trips/{id} calls per run (Pro keys get ~1000 calls/day)
STALE_AFTER_HOURS = 24     # flag cached availability older than this
CACHE_HORIZON_DAYS = 340   # observed live: seats.aero's cache ends 340-355 days out depending on route; messaging only

IATA_RE = re.compile(r"^[A-Z]{3}$")
MAX_AIRPORTS_PER_SIDE = 4  # e.g. "PEK,PKX" for Beijing or "LHR,LGW" for London
MAX_RANGE_DAYS = 62        # --date/--end-date span; two months keeps trip lookups and refreshes within a day's quota
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

PROGRAM_NAMES = {
    "aeromexico": "Aeromexico Rewards",
    "aeroplan": "Air Canada Aeroplan",
    "alaska": "Alaska Atmos Rewards",
    "american": "American AAdvantage",
    "azul": "Azul Fidelidade",
    "british": "British Airways Club",
    "connectmiles": "Copa ConnectMiles",
    "delta": "Delta SkyMiles",
    "emirates": "Emirates Skywards",
    "ethiopian": "Ethiopian ShebaMiles",
    "etihad": "Etihad Guest",
    "eurobonus": "SAS EuroBonus",
    "finnair": "Finnair Plus",
    "flyingblue": "Air France-KLM Flying Blue",
    "frontier": "Frontier Miles",
    "jetblue": "JetBlue TrueBlue",
    "lufthansa": "Lufthansa Miles & More",
    "qantas": "Qantas Frequent Flyer",
    "qatar": "Qatar Privilege Club",
    "saudia": "Saudia AlFursan",
    "singapore": "Singapore KrisFlyer",
    "smiles": "GOL Smiles",
    "spirit": "Spirit Free Spirit",
    "turkish": "Turkish Miles&Smiles",
    "united": "United MileagePlus",
    "velocity": "Virgin Australia Velocity",
    "virginatlantic": "Virgin Atlantic Flying Club",
}

# Compact labels for the HTML table; the full PROGRAM_NAMES value is shown on hover.
PROGRAM_SHORT_NAMES = {
    "aeromexico": "Aeromexico", "aeroplan": "Aeroplan", "alaska": "Alaska Atmos", "american": "AAdvantage",
    "azul": "Azul", "british": "BA Club", "connectmiles": "ConnectMiles", "delta": "SkyMiles",
    "emirates": "Skywards", "ethiopian": "ShebaMiles", "etihad": "Etihad Guest", "eurobonus": "EuroBonus",
    "finnair": "Finnair Plus", "flyingblue": "Flying Blue", "frontier": "Frontier", "jetblue": "TrueBlue",
    "lufthansa": "Miles & More", "qantas": "Qantas FF", "qatar": "Privilege Club", "saudia": "AlFursan",
    "singapore": "KrisFlyer", "smiles": "Smiles", "spirit": "Free Spirit", "turkish": "Miles&Smiles",
    "united": "MileagePlus", "velocity": "Velocity", "virginatlantic": "Flying Club",
}

AIRLINE_NAMES = {
    "AA": "American", "AC": "Air Canada", "AF": "Air France", "AI": "Air India",
    "AM": "Aeromexico", "AS": "Alaska", "AV": "Avianca", "AY": "Finnair",
    "AZ": "ITA Airways", "B6": "JetBlue", "BA": "British Airways", "BR": "EVA Air",
    "CA": "Air China", "CI": "China Airlines", "CM": "Copa", "CX": "Cathay Pacific",
    "DL": "Delta", "EK": "Emirates", "ET": "Ethiopian", "EY": "Etihad",
    "FJ": "Fiji Airways", "GA": "Garuda", "IB": "Iberia", "JL": "Japan Airlines",
    "KE": "Korean Air", "KL": "KLM", "LA": "LATAM", "LH": "Lufthansa", "LO": "LOT",
    "LX": "SWISS", "MH": "Malaysia Airlines", "MS": "EgyptAir", "NH": "ANA",
    "NZ": "Air New Zealand", "OS": "Austrian", "OZ": "Asiana", "PR": "Philippine Airlines",
    "QF": "Qantas", "QR": "Qatar Airways", "SA": "South African", "SK": "SAS",
    "SN": "Brussels Airlines", "SQ": "Singapore Airlines", "SV": "Saudia",
    "TG": "Thai Airways", "TK": "Turkish Airlines", "TP": "TAP Air Portugal",
    "UA": "United", "UL": "SriLankan", "VA": "Virgin Australia", "VN": "Vietnam Airlines",
    "VS": "Virgin Atlantic", "WY": "Oman Air",
    "ZH": "Shenzhen Airlines", "MU": "China Eastern", "CZ": "China Southern", "HU": "Hainan Airlines",
    "3U": "Sichuan Airlines", "MF": "Xiamen Air", "HO": "Juneyao Air", "SC": "Shandong Airlines",
}

DEFAULT_REPORT_DIR = Path("award-reports")

# Where to go to actually book, per mileage program. Used when seats.aero does not
# return a deep link for a trip. These are the programs' award-search entry points.
PROGRAM_BOOKING_URLS = {
    "aeromexico": "https://www.aeromexico.com/en-us/aeromexico-rewards",
    "aeroplan": "https://www.aircanada.com/aeroplan/redeem/availability/outbound",
    "alaska": "https://www.alaskaair.com/",
    "american": "https://www.aa.com/booking/find-flights?redeemMiles=true",
    "azul": "https://www.voeazul.com.br/",
    "british": "https://www.britishairways.com/travel/redeem/execclub/_gf/en_gb",
    "connectmiles": "https://www.copaair.com/en-us/connectmiles/",
    "delta": "https://www.delta.com/flight-search/book-a-flight",
    "emirates": "https://www.emirates.com/skywards/",
    "ethiopian": "https://www.ethiopianairlines.com/shebamiles",
    "etihad": "https://www.etihad.com/en/book",
    "eurobonus": "https://www.flysas.com/en/eurobonus/",
    "finnair": "https://www.finnair.com/en/finnair-plus",
    "flyingblue": "https://www.flyingblue.com/",
    "frontier": "https://www.flyfrontier.com/",
    "jetblue": "https://www.jetblue.com/",
    "lufthansa": "https://www.miles-and-more.com/",
    "qantas": "https://www.qantas.com/au/en/book-a-trip/flights.html",
    "qatar": "https://www.qatarairways.com/en/Privilege-Club.html",
    "saudia": "https://www.saudia.com/",
    "singapore": "https://www.singaporeair.com/en_UK/us/ppsclub-krisflyer/use-miles/",
    "smiles": "https://www.smiles.com.br/",
    "spirit": "https://www.spirit.com/",
    "turkish": "https://www.turkishairlines.com/en-int/miles-and-smiles/",
    "united": "https://www.united.com/en/us/book-flight/united-reservations",
    "velocity": "https://experience.velocityfrequentflyer.com/",
    "virginatlantic": "https://www.virginatlantic.com/flying-club",
}

# Operating airline homepages, linked from the Airline column so the user can
# check the product, seat map or manage the booking after redeeming.
AIRLINE_URLS = {
    "AA": "https://www.aa.com/", "AC": "https://www.aircanada.com/", "AF": "https://www.airfrance.com/",
    "AI": "https://www.airindia.com/", "AM": "https://www.aeromexico.com/", "AS": "https://www.alaskaair.com/",
    "AV": "https://www.avianca.com/", "AY": "https://www.finnair.com/", "AZ": "https://www.ita-airways.com/",
    "B6": "https://www.jetblue.com/", "BA": "https://www.britishairways.com/", "BR": "https://www.evaair.com/",
    "CA": "https://www.airchina.com/", "CI": "https://www.china-airlines.com/", "CM": "https://www.copaair.com/",
    "CX": "https://www.cathaypacific.com/", "DL": "https://www.delta.com/", "EK": "https://www.emirates.com/",
    "ET": "https://www.ethiopianairlines.com/", "EY": "https://www.etihad.com/", "FJ": "https://www.fijiairways.com/",
    "GA": "https://www.garuda-indonesia.com/", "IB": "https://www.iberia.com/", "JL": "https://www.jal.co.jp/",
    "KE": "https://www.koreanair.com/", "KL": "https://www.klm.com/", "LA": "https://www.latamairlines.com/",
    "LH": "https://www.lufthansa.com/", "LO": "https://www.lot.com/", "LX": "https://www.swiss.com/",
    "MH": "https://www.malaysiaairlines.com/", "MS": "https://www.egyptair.com/", "NH": "https://www.ana.co.jp/",
    "NZ": "https://www.airnewzealand.com/", "OS": "https://www.austrian.com/", "OZ": "https://flyasiana.com/",
    "PR": "https://www.philippineairlines.com/", "QF": "https://www.qantas.com/", "QR": "https://www.qatarairways.com/",
    "SA": "https://www.flysaa.com/", "SK": "https://www.flysas.com/", "SN": "https://www.brusselsairlines.com/",
    "SQ": "https://www.singaporeair.com/", "SV": "https://www.saudia.com/", "TG": "https://www.thaiairways.com/",
    "TK": "https://www.turkishairlines.com/", "TP": "https://www.flytap.com/", "UA": "https://www.united.com/",
    "UL": "https://www.srilankan.com/", "VA": "https://www.virginaustralia.com/", "VN": "https://www.vietnamairlines.com/",
    "VS": "https://www.virginatlantic.com/", "WY": "https://www.omanair.com/",
    "ZH": "https://www.shenzhenair.com/", "MU": "https://www.ceair.com/", "CZ": "https://www.csair.com/",
    "HU": "https://www.hainanairlines.com/", "3U": "https://www.sichuanair.com/", "MF": "https://www.xiamenair.com/",
    "HO": "https://www.juneyaoair.com/", "SC": "https://www.sda.cn/",
}


# --------------------------------------------------------------------------- errors


class UsageError(Exception):
    """Bad input from the caller; exit code 2."""


class SeatsAeroError(Exception):
    """The API refused or failed the request; exit code 3."""


# --------------------------------------------------------------------------- auth


def resolve_api_key(environ: dict[str, str] | None = None, key_file: Path | None = None) -> str:
    """Return the Partner API key from the environment or the key file, or raise UsageError."""
    env = os.environ if environ is None else environ
    key_file = API_KEY_FILE if key_file is None else key_file
    for name in API_KEY_ENV_VARS:
        value = env.get(name, "").strip()
        if value:
            return value
    if key_file.is_file():
        mode = stat.S_IMODE(key_file.stat().st_mode)
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            print(f"warning: {key_file} is readable by other users; run: chmod 600 {key_file}", file=sys.stderr)
        value = key_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    raise UsageError(
        "No seats.aero API key found.\n"
        f"  Set {API_KEY_ENV_VARS[0]} in your environment, or save the key to {key_file} (chmod 600).\n"
        "  Keys are generated in seats.aero Settings and require a seats.aero Pro membership."
    )


# --------------------------------------------------------------------------- http client


Opener = Callable[[urllib.request.Request, float], Any]


class SeatsAeroClient:
    """Minimal client for the seats.aero Partner API with retry and pagination."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 4,
        opener: Opener | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._opener = opener or (lambda req, timeout: urllib.request.urlopen(req, timeout=timeout))
        self._sleep = sleep
        self.calls_made = 0

    # -- low level

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request(path, params=params)

    def post(self, path: str, body: dict[str, Any], *, counted: bool = True) -> dict[str, Any]:
        return self._request(path, body=body, counted=counted)

    def _request(self, path: str, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None, *, counted: bool = True) -> dict[str, Any]:
        query = {k: _param_value(v) for k, v in (params or {}).items() if v is not None}
        url = f"{self._base_url}/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"accept": "application/json", "Partner-Authorization": self._api_key}
        data = None
        if body is not None:
            headers["content-type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
        for attempt in range(self._max_retries + 1):
            try:
                if counted:
                    self.calls_made += 1
                with self._opener(request, self._timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as err:
                body = _read_error_body(err)
                if err.code in (401, 403):
                    raise SeatsAeroError(
                        f"seats.aero rejected the API key (HTTP {err.code}). "
                        "Check the key and that your seats.aero Pro membership is active."
                    ) from None
                if err.code == 429 or err.code >= 500:
                    if attempt < self._max_retries:
                        self._sleep(_backoff_seconds(err, attempt))
                        continue
                    raise SeatsAeroError(f"seats.aero HTTP {err.code} after {attempt + 1} attempts: {body}") from None
                raise SeatsAeroError(f"seats.aero HTTP {err.code} for {path}: {body}") from None
            except urllib.error.URLError as err:
                if attempt < self._max_retries:
                    self._sleep(_backoff_seconds(None, attempt))
                    continue
                raise SeatsAeroError(f"could not reach seats.aero: {err.reason}") from None
        raise SeatsAeroError("unreachable")  # pragma: no cover

    # -- endpoints

    def search(
        self,
        origin: str,
        destination: str,
        start_date: date,
        end_date: date,
        *,
        sources: Sequence[str] | None = None,
        take: int = 500,
    ) -> Iterator[dict[str, Any]]:
        """Yield every cached Availability object for the route/date window, following pagination."""
        params: dict[str, Any] = {
            "origin_airport": origin,
            "destination_airport": destination,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "take": take,
        }
        if sources:
            params["sources"] = ",".join(sources)
        for _ in range(MAX_PAGES):
            page = self.get("search", params)
            yield from page.get("data") or []
            if not page.get("hasMore") or page.get("cursor") in (None, 0):
                return
            params["cursor"] = page["cursor"]

    def trips(self, availability_id: str) -> dict[str, Any]:
        """Flight-level detail (segments, taxes, booking links) for one Availability."""
        return self.get(f"trips/{urllib.parse.quote(availability_id, safe='')}")

    def refresh(self, availability_ids: Sequence[str], *, poll: bool = False) -> dict[str, Any]:
        """Queue (or poll) a re-scrape of up to 250 Availability objects. Pro keys only.

        The same call both queues and polls: re-posting IDs that are already processing
        does not re-queue them or spend quota. Response shape observed live:
          {"items":[{"availability_id","status","updated_at"}], "queued", "refunded",
           "counts":{"processing","succeeded","failed"}, "complete": bool,
           "quota":{"limit","used","remaining","reset_seconds"}}
        Statuses seen: queued, processing, succeeded, failed, skipped_outage.
        Polls (poll=True) are free on the seats.aero side, so they are left out of calls_made.
        """
        return self.post("refresh", {"availability_ids": list(availability_ids)}, counted=not poll)

    def refresh_and_wait(
        self,
        availability_ids: Sequence[str],
        *,
        timeout_seconds: float = DEFAULT_REFRESH_TIMEOUT_SECONDS,
        poll_seconds: float = REFRESH_POLL_SECONDS,
        log: Callable[[str], None] = lambda _: None,
    ) -> "RefreshOutcome":
        """Queue refreshes in batches and poll until every batch reports complete or time runs out."""
        ids = list(dict.fromkeys(availability_ids))
        batches = [ids[i:i + REFRESH_BATCH] for i in range(0, len(ids), REFRESH_BATCH)]
        outcome = RefreshOutcome(requested=len(ids))
        pending = []
        for batch in batches:
            response = self.refresh(batch)
            outcome.absorb(response)
            if not response.get("complete"):
                pending.append(batch)
        waited = 0.0
        last_logged = (outcome.succeeded, outcome.processing, 0.0)
        while pending and waited < timeout_seconds:
            self._sleep(poll_seconds)
            waited += poll_seconds
            still_pending = []
            for batch in pending:
                response = self.refresh(batch, poll=True)
                outcome.absorb(response)
                if not response.get("complete"):
                    still_pending.append(batch)
            pending = still_pending
            progress = (outcome.succeeded, outcome.processing)
            if progress != last_logged[:2] or waited - last_logged[2] >= 30:
                log(f"refresh: {outcome.succeeded} done, {outcome.processing} in progress after {waited:.0f}s")
                last_logged = (*progress, waited)
        outcome.timed_out = bool(pending)
        outcome.waited_seconds = waited
        return outcome


@dataclass
class RefreshOutcome:
    """Aggregated result of one or more /refresh calls."""

    requested: int = 0
    statuses: dict[str, str] = field(default_factory=dict)
    quota: dict[str, Any] = field(default_factory=dict)
    timed_out: bool = False
    waited_seconds: float = 0.0
    capped_from: int = 0  # matches that existed when only MAX_REFRESH_RECORDS of them were refreshed

    def absorb(self, response: dict[str, Any]) -> None:
        for item in response.get("items") or []:
            if item.get("availability_id"):
                self.statuses[str(item["availability_id"])] = str(item.get("status", ""))
        if response.get("quota"):
            self.quota = dict(response["quota"])

    def count(self, *statuses: str) -> int:
        return sum(1 for s in self.statuses.values() if s in statuses)

    @property
    def succeeded(self) -> int:
        return self.count("succeeded")

    @property
    def failed(self) -> int:
        return self.count("failed")

    @property
    def processing(self) -> int:
        return self.count("queued", "processing")

    @property
    def skipped(self) -> int:
        return sum(1 for s in self.statuses.values() if s.startswith("skipped"))

    def summary(self) -> str:
        parts = [f"{self.succeeded} refreshed"]
        if self.skipped:
            outage = self.count("skipped_outage")
            parts.append(f"{self.skipped} skipped" + (f" ({outage} because seats.aero has that program's scraping paused)" if outage else ""))
        if self.failed:
            parts.append(f"{self.failed} failed on seats.aero's side (cached figures shown for those)")
        if self.processing:
            parts.append(f"{self.processing} still processing when the wait timed out (seats.aero finishes in the background; re-run in a few minutes for the updated figures)")
        text = f"Refreshed before reporting ({self.requested} record{'s' if self.requested != 1 else ''}): " + ", ".join(parts) + "."
        if self.capped_from:
            text += f" Only the {self.requested} oldest of {self.capped_from} matches were refreshed to protect the daily quota."
        if self.quota:
            text += f" Daily API quota: {self.quota.get('remaining')}/{self.quota.get('limit')} calls remaining."
        return text

    def to_json(self) -> dict[str, Any]:
        return {
            "requested": self.requested, "succeeded": self.succeeded, "skipped": self.skipped, "failed": self.failed,
            "still_processing": self.processing, "timed_out": self.timed_out, "waited_seconds": self.waited_seconds,
            "capped_from": self.capped_from, "statuses": self.statuses, "quota": self.quota,
        }


def _param_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _read_error_body(err: urllib.error.HTTPError) -> str:
    try:
        text = err.read().decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - defensive
        return ""
    return text.strip()[:300]


def _backoff_seconds(err: urllib.error.HTTPError | None, attempt: int) -> float:
    if err is not None:
        retry_after = err.headers.get("Retry-After") if err.headers else None
        if retry_after and retry_after.isdigit():
            return float(retry_after)
    return float(2 ** attempt)


# --------------------------------------------------------------------------- domain


@dataclass
class AwardOption:
    """One bookable premium-cabin itinerary (or a summary row when trip detail is unavailable)."""

    program: str
    source: str
    cabin: str
    travel_date: str
    route: str
    airlines: list[str]
    flight_numbers: str
    departs_at: str
    arrives_at: str
    duration_minutes: int
    stops: int
    remaining_seats: int
    mileage_cost: int
    taxes_minor_units: int
    taxes_currency: str
    booking_link: str
    aircraft: list[str] = field(default_factory=list)
    availability_id: str = ""
    trip_id: str = ""
    updated_at: str = ""
    detail_level: str = "trip"  # "trip" or "summary"
    sources: list[str] = field(default_factory=lambda: ["seats.aero"])
    confirmation: str = ""      # "" | "program" | "flight" - how a second source agreed with this row
    crosscheck_note: str = ""   # e.g. "FlightPoints quotes 55,000 miles" when the sources disagree on price

    @property
    def seats_known(self) -> bool:
        return self.remaining_seats > 0

    @property
    def confirmed(self) -> bool:
        return bool(self.confirmation)


@dataclass
class SearchQuery:
    origin: str          # one or more IATA codes joined with commas, e.g. "SIN" or "PEK,PKX"
    destination: str
    start_date: date
    end_date: date
    pax: int
    cabins: tuple[str, ...] = DEFAULT_CABINS
    direct_only: bool = False
    sources: tuple[str, ...] = ()
    max_trip_lookups: int = DEFAULT_TRIP_LOOKUPS
    date_mode: str = "exact"           # exact | flex | range | schedule-opening
    refresh: bool = True
    refresh_older_than_hours: float = DEFAULT_REFRESH_OLDER_THAN_HOURS
    refresh_timeout_seconds: float = DEFAULT_REFRESH_TIMEOUT_SECONDS

    @property
    def single_day(self) -> bool:
        return self.start_date == self.end_date

    @property
    def window_label(self) -> str:
        if self.single_day:
            return self.start_date.isoformat()
        return f"{self.start_date} to {self.end_date}"

    @property
    def multi_airport(self) -> bool:
        return "," in self.origin or "," in self.destination

    @property
    def cabins_label(self) -> str:
        """'business or first', 'business', or 'first' - whatever this search actually asked for."""
        return " or ".join(self.cabins)

    @property
    def programs_label(self) -> str:
        if self.sources:
            return "the requested program" + ("s" if len(self.sources) > 1 else "") + f" ({', '.join(self.sources)})"
        return "any program that seats.aero tracks"

    @property
    def origin_label(self) -> str:
        return self.origin.replace(",", "/")

    @property
    def destination_label(self) -> str:
        return self.destination.replace(",", "/")


@dataclass
class SearchResult:
    query: SearchQuery
    options: list[AwardOption]
    notes: list[str]
    api_calls: int
    availabilities_seen: int
    generated_at: str
    refresh: RefreshOutcome | None = None
    premium_matches: int = 0          # availability records that reported space in a requested cabin
    searched_on: date | None = None   # calendar date of the search; horizon messaging is relative to this
    crosscheck: crosscheck.CrossCheckSummary | None = None

    def explain_no_results(self) -> str:
        """Why the table is empty, in terms the user can act on. Only meaningful when options is empty."""
        q = self.query
        if self.premium_matches > 0:
            filters = []
            if q.pax > 1:
                filters.append(f"room for {q.pax} passengers")
            if q.direct_only:
                filters.append("a nonstop routing")
            why = " and ".join(filters) if filters else "a bookable itinerary after seats.aero's dynamic-pricing filter"
            return (f"{self.premium_matches} cached record{'s' if self.premium_matches != 1 else ''} show {q.cabins_label} class space, "
                    f"but none has {why}.")
        if self.availabilities_seen > 0:
            return (f"seats.aero has {self.availabilities_seen} cached record{'s' if self.availabilities_seen != 1 else ''} for this search, "
                    f"but none with {q.cabins_label} class space.")
        searched_on = self.searched_on or date.today()
        if q.date_mode == "schedule-opening":
            return (f"seats.aero has not cached {q.origin_label} → {q.destination_label} this far ahead yet; its cache typically "
                    f"reaches {CACHE_HORIZON_DAYS}-{SCHEDULE_OPENING_DAYS[1]} days out and this route may take a few more days to appear.")
        if q.start_date > searched_on + timedelta(days=CACHE_HORIZON_DAYS):
            return (f"seats.aero has nothing cached this far ahead: its data usually ends about {CACHE_HORIZON_DAYS}-{SCHEDULE_OPENING_DAYS[1]} "
                    "days out, so re-run once the date is closer.")
        return (f"{q.programs_label[0].upper() + q.programs_label[1:]} has no award space cached for this route on these dates, in any cabin. "
                "That usually means the space is genuinely gone, though it can also mean seats.aero does not monitor this pair.")


def premium_cabins_available(availability: dict[str, Any], cabins: Sequence[str]) -> list[str]:
    """Cabins (from the requested set) that this Availability object reports as open."""
    return [cabin for cabin in cabins if availability.get(f"{CABIN_CODES[cabin]}Available")]


def _fetch_candidates(client: SeatsAeroClient, query: SearchQuery, log: Callable[[str], None]) -> tuple[int, list[dict[str, Any]]]:
    log(f"Searching seats.aero cache: {query.origin}->{query.destination} {query.start_date}..{query.end_date}")
    availabilities = list(client.search(query.origin, query.destination, query.start_date, query.end_date, sources=query.sources or None))
    candidates = [a for a in availabilities if premium_cabins_available(a, query.cabins)]
    candidates.sort(key=lambda a: (a.get("Date", ""), _cheapest_premium_cost(a, query.cabins)))
    log(f"{len(availabilities)} availability records, {len(candidates)} with {'/'.join(query.cabins)} space")
    return len(availabilities), candidates


def _refresh_stale(client: SeatsAeroClient, candidates: list[dict[str, Any]], query: SearchQuery, log: Callable[[str], None]) -> RefreshOutcome | None:
    stale = [a for a in candidates if _hours_since(str(a.get("UpdatedAt") or "")) > query.refresh_older_than_hours]
    if not stale:
        log(f"refresh: nothing older than {query.refresh_older_than_hours:g}h to refresh")
        return None
    stale.sort(key=lambda a: _hours_since(str(a.get("UpdatedAt") or "")), reverse=True)
    capped = len(stale) > MAX_REFRESH_RECORDS
    stale_ids = [a["ID"] for a in stale[:MAX_REFRESH_RECORDS]]
    log(f"refresh: asking seats.aero to re-scrape {len(stale_ids)} of {len(stale)} matching record(s)")
    outcome = client.refresh_and_wait(stale_ids, timeout_seconds=query.refresh_timeout_seconds, log=log)
    outcome.capped_from = len(stale) if capped else 0
    return outcome


def run_search(client: SeatsAeroClient, query: SearchQuery, log: Callable[[str], None] = lambda _: None) -> SearchResult:
    notes: list[str] = []
    options: list[AwardOption] = []
    lookups = 0

    seen, candidates = _fetch_candidates(client, query, log)
    refresh_outcome: RefreshOutcome | None = None
    if query.refresh:
        try:
            refresh_outcome = _refresh_stale(client, candidates, query, log)
        except SeatsAeroError as err:
            notes.append(f"Refresh was not possible ({err}); showing cached data as-is.")
        else:
            if refresh_outcome is None:
                if candidates:
                    notes.append(f"No refresh needed: every matching record was already newer than {query.refresh_older_than_hours:g}h.")
            else:
                notes.append(refresh_outcome.summary())
                if refresh_outcome.succeeded or refresh_outcome.processing:
                    seen, candidates = _fetch_candidates(client, query, log)

    for availability in candidates:
        cabins_open = premium_cabins_available(availability, query.cabins)
        if lookups >= query.max_trip_lookups:
            options.extend(_summary_options(availability, cabins_open, query))
            continue
        lookups += 1
        try:
            trip_payload = client.trips(availability["ID"])
        except SeatsAeroError as err:
            log(f"trip lookup failed for {availability.get('ID')}: {err}")
            options.extend(_summary_options(availability, cabins_open, query))
            continue
        trip_options = _trip_options(availability, trip_payload, query)
        if trip_options:
            options.extend(trip_options)
        else:
            # The cache says the cabin is open but no itinerary survives our filters
            # (pax count, direct-only, dynamic-pricing filter). Keep the summary so the
            # user knows the program has *something* and can check manually.
            options.extend(_summary_options(availability, cabins_open, query, reason="no itinerary matched filters"))

    if lookups >= query.max_trip_lookups and len(candidates) > lookups:
        notes.append(
            f"Trip detail was fetched for the first {lookups} matches only (--max-trip-lookups); "
            f"{len(candidates) - lookups} further matches are shown as program-level summaries."
        )
    if any(not o.seats_known for o in options):
        notes.append("Seats shown as '?' mean the program does not publish a seat count; confirm capacity at booking.")
    if any(_hours_since(o.updated_at) > STALE_AFTER_HOURS for o in options if o.updated_at):
        notes.append(f"Some results are cached data older than {STALE_AFTER_HOURS}h (see Updated column); re-verify on the program's site before transferring points.")
    if query.pax > 1:
        notes.append(f"Filtered to itineraries reporting at least {query.pax} seats; programs that hide seat counts are kept and flagged.")
    if query.date_mode == "schedule-opening":
        notes.append(f"No date was given, so this scanned {SCHEDULE_OPENING_DAYS[0]}-{SCHEDULE_OPENING_DAYS[1]} days out, where airlines first release award inventory.")

    sort_options(options)
    now = datetime.now(timezone.utc)
    return SearchResult(
        query=query,
        options=options,
        notes=notes,
        api_calls=client.calls_made,
        availabilities_seen=seen,
        generated_at=now.isoformat(timespec="seconds"),
        refresh=refresh_outcome,
        premium_matches=len(candidates),
        searched_on=now.date(),
    )


def _cheapest_premium_cost(availability: dict[str, Any], cabins: Sequence[str]) -> int:
    costs = [
        _availability_cost(availability, cabin)
        for cabin in cabins
        if availability.get(f"{CABIN_CODES[cabin]}Available")
    ]
    return min((c for c in costs if c > 0), default=10**9)


def _availability_cost(availability: dict[str, Any], cabin: str) -> int:
    code = CABIN_CODES[cabin]
    raw = availability.get(f"{code}MileageCostRaw")
    if isinstance(raw, int) and raw > 0:
        return raw
    return _to_int(availability.get(f"{code}MileageCost"))


def _trip_options(availability: dict[str, Any], payload: dict[str, Any], query: SearchQuery) -> list[AwardOption]:
    source = availability.get("Source") or payload.get("source") or ""
    booking_link = _primary_booking_link(payload.get("booking_links") or [])
    route = _route_label(availability, query)
    options: list[AwardOption] = []
    for trip in payload.get("data") or []:
        cabin = str(trip.get("Cabin", "")).lower()
        if cabin not in query.cabins:
            continue
        if trip.get("Filtered"):
            continue
        stops = _to_int(trip.get("Stops"))
        if query.direct_only and stops > 0:
            continue
        seats = _to_int(trip.get("RemainingSeats"))
        if 0 < seats < query.pax:
            continue
        segments = sorted(trip.get("AvailabilitySegments") or [], key=lambda s: _to_int(s.get("Order")))
        options.append(
            AwardOption(
                program=PROGRAM_NAMES.get(source, source),
                source=source,
                cabin=cabin,
                travel_date=str(availability.get("Date", query.start_date.isoformat())),
                route=route,
                airlines=_unique(_split_codes(trip.get("Carriers"))),
                flight_numbers=_normalise_flight_numbers(trip.get("FlightNumbers")),
                departs_at=str(trip.get("DepartsAt", "")),
                arrives_at=str(trip.get("ArrivesAt", "")),
                duration_minutes=_to_int(trip.get("TotalDuration")),
                stops=stops,
                remaining_seats=seats,
                mileage_cost=_to_int(trip.get("MileageCost")),
                taxes_minor_units=_to_int(trip.get("TotalTaxes")),
                taxes_currency=str(trip.get("TaxesCurrency") or availability.get("TaxesCurrency") or ""),
                booking_link=booking_link,
                aircraft=[s.get("AircraftName") or s.get("AircraftCode") or "" for s in segments],
                availability_id=str(availability.get("ID", "")),
                trip_id=str(trip.get("ID", "")),
                updated_at=str(trip.get("UpdatedAt") or availability.get("UpdatedAt") or ""),
            )
        )
    return options


def _summary_options(availability: dict[str, Any], cabins_open: Sequence[str], query: SearchQuery, reason: str = "") -> list[AwardOption]:
    """Program-level rows built from the Availability object alone (no flight numbers)."""
    source = availability.get("Source", "")
    options = []
    for cabin in cabins_open:
        code = CABIN_CODES[cabin]
        seats = _to_int(availability.get(f"{code}RemainingSeats"))
        if 0 < seats < query.pax:
            continue
        options.append(
            AwardOption(
                program=PROGRAM_NAMES.get(source, source),
                source=source,
                cabin=cabin,
                travel_date=str(availability.get("Date", query.start_date.isoformat())),
                route=_route_label(availability, query),
                airlines=_unique(_split_codes(availability.get(f"{code}Airlines"))),
                flight_numbers=reason or "see program site",
                departs_at="",
                arrives_at="",
                duration_minutes=0,
                stops=-1 if not availability.get(f"{code}Direct") else 0,
                remaining_seats=seats,
                mileage_cost=_availability_cost(availability, cabin),
                taxes_minor_units=_to_int(availability.get(f"{code}TotalTaxes")),
                taxes_currency=str(availability.get("TaxesCurrency") or ""),
                booking_link="",
                availability_id=str(availability.get("ID", "")),
                updated_at=str(availability.get("UpdatedAt") or ""),
                detail_level="summary",
            )
        )
    return options


def _route_label(availability: dict[str, Any], query: SearchQuery) -> str:
    route_info = availability.get("Route") or {}
    return f"{route_info.get('OriginAirport') or query.origin_label}-{route_info.get('DestinationAirport') or query.destination_label}"


def _primary_booking_link(links: list[dict[str, Any]]) -> str:
    for link in links:
        if link.get("primary") and link.get("link"):
            return str(link["link"])
    for link in links:
        if link.get("link"):
            return str(link["link"])
    return ""


# --------------------------------------------------------------------------- formatting helpers


def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"[^\d-]", "", value)
        if digits and digits != "-":
            return int(digits)
    return 0


def _split_codes(value: Any) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).replace(";", ",").split(",") if part.strip()]


def _unique(items: Sequence[str]) -> list[str]:
    """Order-preserving de-duplication; seats.aero repeats the carrier once per segment ("VN, VN")."""
    return list(dict.fromkeys(items))


def _normalise_flight_numbers(value: Any) -> str:
    return ", ".join(_split_codes(value)) or "-"


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _hours_since(value: str) -> float:
    parsed = _parse_time(value)
    if parsed is None:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 3600


def format_clock(value: str, reference: str = "") -> str:
    """Times from seats.aero are airport-local wall-clock times tagged 'Z'; show them as local."""
    parsed = _parse_time(value)
    if parsed is None:
        return "-"
    text = parsed.strftime("%H:%M")
    ref = _parse_time(reference)
    if ref is not None:
        day_diff = (parsed.date() - ref.date()).days
        if day_diff:
            text += f" (+{day_diff})" if day_diff > 0 else f" ({day_diff})"
    return text


def format_duration(minutes: int) -> str:
    if minutes <= 0:
        return "-"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins:02d}m"


def format_miles(value: int) -> str:
    return f"{value:,}" if value > 0 else "n/a"


def format_taxes(minor_units: int, currency: str) -> str:
    """seats.aero reports taxes in the currency's minor unit (e.g. cents); show major units."""
    if minor_units <= 0 and not currency:
        return "-"
    return f"{minor_units / 100:,.2f} {currency}".strip()


def format_sources(o: AwardOption) -> str:
    if o.confirmed:
        return "✓ seats.aero + FlightPoints" + (f" ({o.crosscheck_note})" if o.crosscheck_note else "")
    return "seats.aero" + (f" ({o.crosscheck_note})" if o.crosscheck_note else "")


def format_airlines(codes: Sequence[str]) -> str:
    if not codes:
        return "-"
    return ", ".join(f"{AIRLINE_NAMES[c]} ({c})" if c in AIRLINE_NAMES else c for c in codes)


def format_stops(stops: int) -> str:
    if stops < 0:
        return "?"
    return "nonstop" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''}"


def format_age(updated_at: str) -> str:
    if not updated_at:
        return "-"
    hours = _hours_since(updated_at)
    if hours < 0:
        return "just now"
    if hours < 1:
        return f"{int(hours * 60)}m ago"
    if hours < 48:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f}d ago"


def sort_options(options: list[AwardOption]) -> None:
    """Rows confirmed by two sources first (flight-level before program-level), then by price."""
    options.sort(key=lambda o: (crosscheck.CONFIRMATION_RANK.get(o.confirmation, 2), o.mileage_cost or 10**9, o.taxes_minor_units, o.departs_at))


CROSS_CHECK_NOTE_MARKERS = ("FlightPoints", "Cross-check")


def apply_cross_check(result: SearchResult, paths: Sequence[Path], log: Callable[[str], None] = lambda _: None) -> None:
    """Match FlightPoints output files against the seats.aero rows and regroup the report."""
    # A --load run may already carry notes from an earlier cross-check; this one supersedes them.
    result.notes = [n for n in result.notes if not any(m in n for m in CROSS_CHECK_NOTE_MARKERS)]
    entries, files, empty_files = crosscheck.load_files(list(paths))
    before = len(entries)
    entries = [e for e in entries if crosscheck.entry_in_scope(e, result.query)]
    if len(entries) < before:
        log(f"cross-check: ignored {before - len(entries)} entry(ies) outside {result.query.origin_label}->{result.query.destination_label} {result.query.window_label}")
    summary = crosscheck.match_options(result.options, entries)
    summary.out_of_scope = before - len(entries)
    summary.files = files
    summary.empty_files = empty_files
    result.crosscheck = summary
    sort_options(result.options)
    confirmed = summary.flight_matches + summary.program_matches
    log(f"cross-check: {files} FlightPoints file(s), {len(entries)} entries, {confirmed} of {len(result.options)} rows confirmed")
    if not entries:
        if summary.out_of_scope:
            result.notes.insert(0, f"Cross-check found no usable FlightPoints data for this search: all {summary.out_of_scope} entry(ies) in "
                                   f"{files} file(s) describe other routes or dates. Rows are seats.aero only.")
        elif summary.answered:
            result.notes.insert(0, f"FlightPoints was queried ({files} searches) and reported no business or first space on this route and date(s), "
                                   "so none of these rows is confirmed by a second source. Treat them as seats.aero-only until you check the program's site.")
        else:
            result.notes.insert(0, f"Cross-check requested but no FlightPoints entries could be read from {files} file(s); rows are seats.aero only.")
        return
    note = (f"Cross-checked against FlightPoints ({len(entries)} entries): {confirmed} of {len(result.options)} rows confirmed by both sources"
            f" ({summary.flight_matches} by exact flight, {summary.program_matches} by program and price) and grouped at the top.")
    if summary.price_disagreements:
        note += f" Price differed on {len(summary.price_disagreements)}: " + "; ".join(summary.price_disagreements[:3]) + ("…" if len(summary.price_disagreements) > 3 else "") + "."
    if summary.unmatched:
        note += f" FlightPoints also lists {len(summary.unmatched)} premium option(s) seats.aero did not: " + "; ".join(summary.unmatched[:4]) + ("…" if len(summary.unmatched) > 4 else "") + "."
    if summary.out_of_scope:
        note += f" ({summary.out_of_scope} FlightPoints entry(ies) for other routes or dates were ignored.)"
    result.notes.insert(0, note)


def load_result(path: Path) -> SearchResult:
    """Rebuild a SearchResult from a previous --json run so a report can be re-rendered without API calls."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    q = dict(payload["query"])
    query = SearchQuery(
        origin=q["origin"], destination=q["destination"],
        start_date=date.fromisoformat(q["start_date"]), end_date=date.fromisoformat(q["end_date"]),
        pax=int(q["pax"]), cabins=tuple(q.get("cabins") or DEFAULT_CABINS), direct_only=bool(q.get("direct_only")),
        sources=tuple(q.get("sources") or ()), max_trip_lookups=int(q.get("max_trip_lookups", DEFAULT_TRIP_LOOKUPS)),
        date_mode=q.get("date_mode", "exact"), refresh=bool(q.get("refresh", True)),
        refresh_older_than_hours=float(q.get("refresh_older_than_hours", DEFAULT_REFRESH_OLDER_THAN_HOURS)),
        refresh_timeout_seconds=float(q.get("refresh_timeout_seconds", DEFAULT_REFRESH_TIMEOUT_SECONDS)),
    )
    field_names = {f.name for f in AwardOption.__dataclass_fields__.values()}
    options = [AwardOption(**{k: v for k, v in item.items() if k in field_names}) for item in payload.get("options", [])]
    refresh = None
    if payload.get("refresh"):
        r = payload["refresh"]
        refresh = RefreshOutcome(requested=int(r.get("requested", 0)), statuses=dict(r.get("statuses") or {}), quota=dict(r.get("quota") or {}),
                                 timed_out=bool(r.get("timed_out")), waited_seconds=float(r.get("waited_seconds", 0)), capped_from=int(r.get("capped_from", 0)))
    cross = None
    if payload.get("crosscheck"):
        c = payload["crosscheck"]
        cross = crosscheck.CrossCheckSummary(
            entries=int(c.get("entries", 0)), files=int(c.get("files", 0)), empty_files=int(c.get("empty_files", 0)),
            flight_matches=int(c.get("flight_matches", 0)), program_matches=int(c.get("program_matches", 0)),
            price_disagreements=list(c.get("price_disagreements") or []), unmatched=list(c.get("unmatched") or []),
            out_of_scope=int(c.get("out_of_scope", 0)),
        )
    elif any(o.confirmed for o in options):
        # Rows were confirmed by a cross-check whose summary is missing; keep the Sources column so the
        # highlighted, confirmation-first ordering is explained rather than unexplained.
        cross = crosscheck.CrossCheckSummary(entries=sum(1 for o in options if o.confirmed))
    searched_on = payload.get("searched_on")
    return SearchResult(
        query=query, options=options, notes=list(payload.get("notes") or []), api_calls=int(payload.get("api_calls", 0)),
        availabilities_seen=int(payload.get("availabilities_seen", 0)), generated_at=str(payload.get("generated_at", "")),
        refresh=refresh, premium_matches=int(payload.get("premium_matches", 0)),
        searched_on=date.fromisoformat(searched_on) if searched_on else None, crosscheck=cross,
    )


def safe_url(url: str) -> str:
    """Only http(s) URLs are ever emitted into a report; anything else becomes empty."""
    parsed = urllib.parse.urlparse(url or "")
    return url if parsed.scheme in ("http", "https") and parsed.netloc else ""


def booking_url(option: AwardOption) -> str:
    """Deep link from seats.aero when present, else the program's award-search page."""
    return safe_url(option.booking_link) or PROGRAM_BOOKING_URLS.get(option.source, "")


def airline_url(code: str) -> str:
    return AIRLINE_URLS.get(code, "")


def default_report_path(query: SearchQuery, report_dir: Path = DEFAULT_REPORT_DIR) -> Path:
    name = f"awards_{query.origin.replace(',', '+')}-{query.destination.replace(',', '+')}_{query.start_date.isoformat()}"
    if not query.single_day:
        name += f"_to_{query.end_date.isoformat()}"
    return report_dir / f"{name}_pax{query.pax}.html"


def write_report(result: SearchResult, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(result), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- output


@dataclass(frozen=True)
class Column:
    """One report column, rendered by both the markdown and HTML writers from the same definition."""

    header: str
    markdown: Callable[[int, AwardOption], str]
    html: Callable[[int, AwardOption], str]
    css: str = ""
    in_html: bool = True      # the HTML dashboard is kept narrow enough to read without scrolling
    in_markdown: bool = True  # the markdown summary is what Claude reads, so it can carry more
    # Value the dashboard sorts on. Numbers sort numerically, anything else as lowercase text;
    # None makes the column unsortable (the Book button has no meaningful order).
    sort: Callable[[int, AwardOption], str | int | float] | None = None


def _dep_arr(o: AwardOption) -> str:
    return f"{format_clock(o.departs_at)} → {format_clock(o.arrives_at, o.departs_at)}" if o.departs_at else "-"


def _seats_text(o: AwardOption) -> str:
    return str(o.remaining_seats) if o.seats_known else "?"


def _book_markdown(o: AwardOption) -> str:
    url = booking_url(o)
    return f"[book]({url})" if url else "-"


def _book_html(o: AwardOption) -> str:
    url = booking_url(o)
    if url and safe_url(o.booking_link):
        return _html_link(url, "Book", "book")
    if url:
        return _html_link(url, "Site", "book book-fallback")
    return "-"


def _flights_html(o: AwardOption) -> str:
    flights = _esc(o.flight_numbers)
    if o.detail_level == "summary":
        return f'<span class="badge summary">summary</span> <span class="muted">{flights}</span>'
    aircraft = ", ".join(a for a in o.aircraft if a)
    return flights + (f'<div class="muted">{_esc(aircraft)}</div>' if aircraft else "")


def _updated_html(o: AwardOption) -> str:
    text = _esc(format_age(o.updated_at))
    if o.updated_at and _hours_since(o.updated_at) > STALE_AFTER_HOURS:
        text += ' <span class="badge stale">stale</span>'
    return text


def html_columns(q: SearchQuery, with_sources: bool = False) -> list[Column]:
    return [c for c in table_columns(q, with_sources) if c.in_html]


def markdown_columns(q: SearchQuery, with_sources: bool = False) -> list[Column]:
    return [c for c in table_columns(q, with_sources) if c.in_markdown]


def table_columns(q: SearchQuery, with_sources: bool = False) -> list[Column]:
    """Date and Route only earn a column when they vary between rows; Sources only when a cross-check ran."""
    cols = [
        Column("#", lambda i, o: str(i), lambda i, o: str(i), "num", sort=lambda i, o: i),
        Column("Program", lambda i, o: o.program, lambda i, o: _html_program(o), "wrap", sort=lambda i, o: o.program),
        Column("Cabin", lambda i, o: o.cabin.title(), lambda i, o: f'<span class="badge {_esc(o.cabin)}">{_esc(o.cabin.title())}</span>',
               sort=lambda i, o: 0 if o.cabin == "first" else 1),   # first above business
    ]
    if with_sources:
        cols.append(Column("Sources", lambda i, o: format_sources(o), lambda i, o: _html_sources(o), "src",
                           sort=lambda i, o: crosscheck.CONFIRMATION_RANK.get(o.confirmation, 2)))
    cols += [
        Column("Airline", lambda i, o: format_airlines(o.airlines), lambda i, o: _html_airlines(o.airlines), "wrap",
               sort=lambda i, o: format_airlines(o.airlines)),
        Column("Flights", lambda i, o: o.flight_numbers, lambda i, o: _flights_html(o), sort=lambda i, o: o.flight_numbers),
    ]
    if not q.single_day:
        cols.append(Column("Date", lambda i, o: o.travel_date, lambda i, o: _esc(o.travel_date), sort=lambda i, o: o.travel_date))
    if q.multi_airport:
        cols.append(Column("Route", lambda i, o: o.route, lambda i, o: _esc(o.route), sort=lambda i, o: o.route))
    cols += [
        Column("Dep → Arr", lambda i, o: _dep_arr(o), lambda i, o: _esc(_dep_arr(o)), sort=lambda i, o: _minutes_of_day(o.departs_at)),
        Column("Duration", lambda i, o: format_duration(o.duration_minutes), lambda i, o: _html_duration_stops(o),
               sort=lambda i, o: o.duration_minutes or 10**6),   # unknown durations last when sorting shortest first
        Column("Stops", lambda i, o: format_stops(o.stops), lambda i, o: "", in_html=False, sort=lambda i, o: o.stops if o.stops >= 0 else 99),
        Column("Seats", lambda i, o: _seats_text(o),
               lambda i, o: _esc(_seats_text(o)) if o.seats_known else '<span title="Program does not publish seat counts">?</span>', "num",
               sort=lambda i, o: -o.remaining_seats if o.seats_known else 1),   # most seats first; unknown last
        Column("Miles / pax", lambda i, o: format_miles(o.mileage_cost), lambda i, o: _esc(format_miles(o.mileage_cost)), "num miles",
               sort=lambda i, o: o.mileage_cost or 10**9),
        Column("Taxes / pax", lambda i, o: format_taxes(o.taxes_minor_units, o.taxes_currency),
               lambda i, o: "", "num", in_html=False, sort=lambda i, o: o.taxes_minor_units),
        Column("Updated", lambda i, o: format_age(o.updated_at), lambda i, o: _updated_html(o),
               sort=lambda i, o: round(max(_hours_since(o.updated_at), 0.0), 3) if o.updated_at else 10**6),   # freshest first
        Column("Book", lambda i, o: _book_markdown(o), lambda i, o: _book_html(o), "book-cell"),
    ]
    return cols


def _minutes_of_day(value: str) -> int:
    parsed = _parse_time(value)
    return parsed.hour * 60 + parsed.minute if parsed else 10**6


def _next_steps_hint(q: SearchQuery) -> str:
    hints = []
    if q.date_mode not in ("flex", "range"):
        hints.append("`--flex 3` for nearby dates")
    hints.append("a nearby hub or alternate airport")
    if q.pax > 1:
        hints.append("`--pax 1` to see whether space exists for a smaller party")
    if q.direct_only:
        hints.append("dropping `--direct-only`")
    return "Try " + ", ".join(hints) + "."


def render_markdown(result: SearchResult, report_path: Path | None = None) -> str:
    q = result.query
    lines = [
        f"## Premium-cabin award seats {q.origin_label} → {q.destination_label}",
        f"{'Date' if q.single_day else 'Dates'}: {q.window_label} · Passengers: {q.pax} · Cabins: {', '.join(c.title() for c in q.cabins)}"
        + (" · Nonstop only" if q.direct_only else "") + (" · Refreshed before reporting" if q.refresh else " · Cached data, no refresh")
        + (" · Cross-checked with FlightPoints" if result.crosscheck is not None else ""),
        "",
    ]
    if not result.options:
        lines.append(f"No {q.cabins_label} class award space found. " + result.explain_no_results())
        lines.append(_next_steps_hint(q))
        if result.notes:
            lines.append("")
            lines.append("Notes:")
            lines += [f"- {n}" for n in result.notes]
        if report_path is not None:
            lines.append(f"\n_HTML report: {report_path}_")
        return "\n".join(lines)

    columns = markdown_columns(q, result.crosscheck is not None)
    lines += ["| " + " | ".join(c.header for c in columns) + " |", "|" + "---|" * len(columns)]
    for idx, o in enumerate(result.options, start=1):
        lines.append("| " + " | ".join(c.markdown(idx, o).replace("|", "/") for c in columns) + " |")
    if result.notes:
        lines.append("")
        lines.append("Notes:")
        lines += [f"- {n}" for n in result.notes]
    lines.append("")
    lines.append(f"_Source: seats.aero cached availability ({result.api_calls} API calls). Miles and taxes are per passenger._")
    if report_path is not None:
        lines.append(f"_HTML report: {report_path}_")
    return "\n".join(lines)


def render_json(result: SearchResult, report_path: Path | None = None) -> str:
    payload = {
        "query": {
            **asdict(result.query),
            "start_date": result.query.start_date.isoformat(),
            "end_date": result.query.end_date.isoformat(),
        },
        "refresh": result.refresh.to_json() if result.refresh else None,
        "crosscheck": result.crosscheck.to_json() if result.crosscheck else None,
        "generated_at": result.generated_at,
        "api_calls": result.api_calls,
        "availabilities_seen": result.availabilities_seen,
        "premium_matches": result.premium_matches,
        "searched_on": result.searched_on.isoformat() if result.searched_on else None,
        "notes": result.notes,
        "report_path": str(report_path) if report_path is not None else None,
        "options": [
            {
                **asdict(o),
                "taxes_display": format_taxes(o.taxes_minor_units, o.taxes_currency),
                "seats_known": o.seats_known,
                "book_url": booking_url(o),
                "airline_urls": {code: airline_url(code) for code in o.airlines if airline_url(code)},
            }
            for o in result.options
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=False)


HTML_STYLE = """
:root{--bg:#0f1115;--surface:#171a21;--surface-2:#1e222b;--border:#2a303c;--text:#e6e8ee;--muted:#9aa3b2;
--accent:#7aa2f7;--business:#73daca;--first:#e0af68;--warn:#f7768e;--ok:#9ece6a;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:1240px;margin:0 auto;padding:28px 20px 44px}
h1{font-size:28px;margin:0 0 6px;letter-spacing:-.01em}
h1 .arrow{color:var(--accent);margin:0 8px}
.sub{color:var(--muted);margin:0 0 20px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:24px}
.chip{background:var(--surface);border:1px solid var(--border);border-radius:999px;padding:4px 12px;font-size:13px;color:var(--muted)}
.chip b{color:var(--text);font-weight:600}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:28px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.card .label{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.card .value{font-size:22px;font-weight:600;margin-top:4px}
.card .detail{font-size:13px;color:var(--muted);margin-top:2px}
.tablewrap{overflow-x:auto;border:1px solid var(--border);border-radius:12px;background:var(--surface)}
table{border-collapse:separate;border-spacing:0;width:100%;font-size:13px}
th,td{padding:7px 6px;text-align:left;vertical-align:top;border-bottom:1px solid var(--border);white-space:nowrap;background:var(--surface)}
th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);background:var(--surface-2);position:sticky;top:0;z-index:1}
th[data-col]{cursor:pointer;user-select:none}
th[data-col]:hover,th[data-col]:focus-visible{color:var(--text);outline:none}
th[data-col]::after{content:"↕";opacity:.35;margin-left:4px;font-size:10px}
th[aria-sort=ascending]::after{content:"↑";opacity:1}
th[aria-sort=descending]::after{content:"↓";opacity:1}
th.sorted{color:var(--accent)}
.tablehint{color:var(--muted);font-size:12px;margin:8px 0 0}
td.wrap,th.wrap{white-space:normal;min-width:96px;max-width:150px}
td.book-cell,th.book-cell{text-align:right}
tbody tr:hover td{background:var(--surface-2)}
tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;font-weight:600}
.badge.business{background:rgba(115,218,202,.15);color:var(--business)}
.badge.first{background:rgba(224,175,104,.18);color:var(--first)}
.badge.stale{background:rgba(247,118,142,.15);color:var(--warn)}
.badge.summary{background:rgba(154,163,178,.15);color:var(--muted)}
.badge.confirmed{background:rgba(158,206,106,.16);color:var(--ok)}
td.src,th.src{white-space:normal;max-width:110px}
tr.confirmed td{background:rgba(158,206,106,.05)}
.muted{color:var(--muted);font-size:11px;white-space:normal}
td.miles{font-weight:600;font-size:15px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
a.book{display:inline-block;background:var(--accent);color:#0b0e14;font-weight:600;padding:5px 11px;border-radius:7px;font-size:12px}
a.book:hover{filter:brightness(1.1);text-decoration:none}
.book-fallback{background:transparent;color:var(--accent);border:1px solid var(--accent)}
.empty{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:28px;text-align:center;color:var(--muted)}
.notes{margin-top:24px;background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 20px}
.notes h2{font-size:14px;margin:0 0 8px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.notes ul{margin:0;padding-left:20px;color:var(--muted)}
footer{margin-top:28px;color:var(--muted);font-size:13px}
@media (max-width:640px){main{padding:20px 14px}h1{font-size:22px}}
"""


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _html_link(url: str, text: str, css_class: str = "") -> str:
    url = safe_url(url)
    if not url:
        return _esc(text)
    cls = f' class="{css_class}"' if css_class else ""
    return f'<a{cls} href="{_esc(url)}" target="_blank" rel="noopener noreferrer">{_esc(text)}</a>'


def _html_airlines(codes: Sequence[str]) -> str:
    """Airline names only (the code is in the flight number already), each linked to the carrier."""
    if not codes:
        return "-"
    return ", ".join(_html_link(airline_url(code), AIRLINE_NAMES.get(code, code)) for code in codes)


def _html_sources(o: AwardOption) -> str:
    if o.confirmed:
        how = "same flight" if o.confirmation == "flight" else "same program and price"
        title = f"Found by seats.aero and FlightPoints ({how})" + (f"; {o.crosscheck_note}" if o.crosscheck_note else "")
        return f'<span class="badge confirmed" title="{_esc(title)}">✓ 2 sources</span>'
    title = "seats.aero only" + (f"; {o.crosscheck_note}" if o.crosscheck_note else "")
    label = "seats.aero" if not o.crosscheck_note else "seats.aero ≠"
    return f'<span class="muted" title="{_esc(title)}">{_esc(label)}</span>'


def _html_program(o: AwardOption) -> str:
    short = PROGRAM_SHORT_NAMES.get(o.source, o.program)
    return f'<span title="{_esc(o.program)}">{_esc(short)}</span>'


def _html_duration_stops(o: AwardOption) -> str:
    return f'{_esc(format_duration(o.duration_minutes))}<div class="muted">{_esc(format_stops(o.stops))}</div>'


def _html_summary_cards(result: SearchResult) -> str:
    def best(cabin: str) -> AwardOption | None:
        priced = [o for o in result.options if o.cabin == cabin and o.mileage_cost > 0]
        return min(priced, key=lambda o: (o.mileage_cost, o.taxes_minor_units)) if priced else None

    cards = [
        ("Options found", str(len(result.options)), f"across {len({o.source for o in result.options})} programs"),
    ]
    for cabin in result.query.cabins:
        top = best(cabin)
        if top:
            cards.append((f"Best {cabin}", f"{format_miles(top.mileage_cost)} miles",
                          f"{top.program} · {format_taxes(top.taxes_minor_units, top.taxes_currency)} taxes"))
        else:
            cards.append((f"Best {cabin}", "none", "no space cached"))
    nonstop = sum(1 for o in result.options if o.stops == 0)
    cards.append(("Nonstop options", str(nonstop), "of all itineraries listed"))
    if result.crosscheck is not None:
        confirmed = sum(1 for o in result.options if o.confirmed)
        cards.append(("Confirmed by FlightPoints", f"{confirmed} of {len(result.options)}", "same seats seen by both sources"))
    return "".join(
        f'<div class="card"><div class="label">{_esc(label)}</div><div class="value">{_esc(value)}</div><div class="detail">{_esc(detail)}</div></div>'
        for label, value, detail in cards
    )


def _html_table(result: SearchResult) -> str:
    columns = html_columns(result.query, result.crosscheck is not None)
    head = "".join(
        f'<th{_css(c.css)}{_sortable_attrs(n, c)}>{_esc(c.header)}</th>' for n, c in enumerate(columns)
    )
    rows = "".join(
        _css_tr(o) + "".join(f"<td{_css(c.css)}{_sort_attr(c, idx, o)}>{c.html(idx, o)}</td>" for c in columns) + "</tr>"
        for idx, o in enumerate(result.options, start=1)
    )
    return (f'<div class="tablewrap"><table id="awards"><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>'
            f"<p class=\"tablehint\">Click a column heading to sort; click again to reverse. The default order puts "
            f"{'rows confirmed by both sources first, then ' if result.crosscheck is not None else ''}the cheapest first.</p>"
            f"<script>{SORT_SCRIPT}</script>")


def _sortable_attrs(index: int, column: Column) -> str:
    """Headers are styled and wired through data-col, so no extra class is needed."""
    if column.sort is None:
        return ""
    return f' data-col="{index}" tabindex="0" role="button" aria-sort="none" title="Sort by {_esc(column.header)}"'


def _sort_attr(column: Column, idx: int, option: AwardOption) -> str:
    if column.sort is None:
        return ""
    value = column.sort(idx, option)
    return f' data-sort="{_esc(value if not isinstance(value, str) else value.lower())}"'


# Progressive enhancement: the table is complete and readable without this script.
SORT_SCRIPT = """
(function () {
  var table = document.getElementById('awards');
  if (!table) return;
  var body = table.tBodies[0];
  var heads = [].slice.call(table.tHead.rows[0].cells);
  function keyOf(row, col) {
    var cell = row.cells[col];
    return cell && cell.hasAttribute('data-sort') ? cell.getAttribute('data-sort') : '';
  }
  function sortBy(col, dir) {
    var rows = [].slice.call(body.rows);
    var numeric = rows.every(function (r) { var k = keyOf(r, col); return k === '' || !isNaN(parseFloat(k)); });
    rows.sort(function (a, b) {
      var x = keyOf(a, col), y = keyOf(b, col);
      var cmp = numeric ? (parseFloat(x || 'Infinity') - parseFloat(y || 'Infinity')) : x.localeCompare(y);
      return dir === 'descending' ? -cmp : cmp;
    });
    rows.forEach(function (r) { body.appendChild(r); });
    heads.forEach(function (h, i) {
      h.setAttribute('aria-sort', i === col ? dir : 'none');
      h.classList.toggle('sorted', i === col);
    });
  }
  function activate(head) {
    var col = parseInt(head.getAttribute('data-col'), 10);
    var dir = head.getAttribute('aria-sort') === 'ascending' ? 'descending' : 'ascending';
    sortBy(col, dir);
  }
  heads.forEach(function (head) {
    if (!head.hasAttribute('data-col')) return;
    head.addEventListener('click', function () { activate(head); });
    head.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(head); }
    });
  });
  var m = /^#sort=(\\d+):(asc|desc)$/.exec(location.hash || '');
  if (m) sortBy(parseInt(m[1], 10), m[2] === 'desc' ? 'descending' : 'ascending');
})();
"""


def _css(css: str) -> str:
    return f' class="{css}"' if css else ""


def _css_tr(o: AwardOption) -> str:
    return '<tr class="confirmed">' if o.confirmed else "<tr>"


def render_html(result: SearchResult) -> str:
    q = result.query
    title = f"Award seats {q.origin_label} → {q.destination_label} · {q.window_label}"
    chips = [
        ("Date" if q.single_day else "Dates", q.window_label), ("Passengers", str(q.pax)), ("Cabins", ", ".join(c.title() for c in q.cabins)),
    ]
    if q.direct_only:
        chips.append(("Routing", "Nonstop only"))
    chips.append(("Data", "refreshed before reporting" if q.refresh else "cached as-is (--no-refresh)"))
    chips.append(("Sources", "seats.aero + FlightPoints cross-check" if result.crosscheck is not None else "seats.aero"))
    if q.date_mode == "schedule-opening":
        chips.append(("Mode", f"schedule opening, {SCHEDULE_OPENING_DAYS[0]}-{SCHEDULE_OPENING_DAYS[1]} days out"))
    if q.sources:
        chips.append(("Programs", ", ".join(q.sources)))
    chips_html = "".join(f'<span class="chip">{_esc(k)}: <b>{_esc(v)}</b></span>' for k, v in chips)

    if result.options:
        body = f'<div class="cards">{_html_summary_cards(result)}</div>{_html_table(result)}'
    else:
        body = (
            f'<div class="empty"><p><b>No {_esc(q.cabins_label)} class award space found.</b></p>'
            f"<p>{_esc(result.explain_no_results())}</p>"
            f"<p>{_esc(_next_steps_hint(q).replace('`', ''))}</p></div>"
        )
    notes_html = ""
    if result.notes:
        notes_html = '<section class="notes"><h2>Notes</h2><ul>' + "".join(f"<li>{_esc(n)}</li>" for n in result.notes) + "</ul></section>"

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_esc(title)}</title><style>{HTML_STYLE}</style></head><body><main>"
        f'<h1>{_esc(q.origin_label)}<span class="arrow">→</span>{_esc(q.destination_label)}</h1>'
        f'<p class="sub">{_esc(q.cabins_label.capitalize())} class award availability from seats.aero. Miles and taxes are per passenger. '
        "Book links open the mileage program that holds the space; airline links open the operating carrier."
        + (" Rows marked ✓ 2 sources were also found by FlightPoints and are listed first." if result.crosscheck is not None else "") + "</p>"
        f'<div class="chips">{chips_html}</div>{body}{notes_html}'
        f"<footer>Generated {_esc(result.generated_at)} · {result.api_calls} seats.aero API calls · cached data, verify before transferring points.</footer>"
        "</main></body></html>"
    )


# --------------------------------------------------------------------------- cli


def parse_args(argv: Sequence[str] | None = None, today: date | None = None) -> tuple[SearchQuery | None, argparse.Namespace]:
    parser = argparse.ArgumentParser(
        prog="search_awards.py",
        description="Find business/first class award seats on seats.aero for a route and date.",
    )
    parser.add_argument("origin", nargs="?", help="Origin airport IATA code(s), e.g. SIN or LHR,LGW")
    parser.add_argument("destination", nargs="?", help="Destination airport IATA code(s), e.g. PEK,PKX")
    parser.add_argument("--load", metavar="RUN.json", default=None, help="Re-render a previous --json run instead of calling seats.aero (no quota spent); origin/destination/--date are then ignored")
    parser.add_argument("--cross-check", metavar="PATH", nargs="+", action="extend", default=[], help="FlightPoints tool output file(s) or a directory of them; matching rows are marked confirmed and grouped first")
    parser.add_argument("--date", default=None, help=f"Departure date, YYYY-MM-DD. Omit to scan {SCHEDULE_OPENING_DAYS[0]}-{SCHEDULE_OPENING_DAYS[1]} days out (schedule opening)")
    parser.add_argument("--pax", type=int, default=1, help="Number of passengers (1-9). Default 1")
    parser.add_argument("--end-date", default=None, metavar="DATE", help=f"Search every day from --date to this date inclusive (up to {MAX_RANGE_DAYS} days), e.g. a whole month")
    parser.add_argument("--flex", type=int, default=0, metavar="DAYS", help="Also search +/- DAYS around --date (0-7). Not combined with --end-date")
    parser.add_argument("--cabins", default=",".join(DEFAULT_CABINS), help="Comma list from: business,first. Default both")
    parser.add_argument("--direct-only", action="store_true", help="Only nonstop itineraries")
    parser.add_argument("--sources", default="", help="Comma list of seats.aero program codes to restrict to (e.g. aeroplan,united)")
    parser.add_argument("--max-trip-lookups", type=int, default=DEFAULT_TRIP_LOOKUPS, help=f"Cap on per-availability trip detail calls. Default {DEFAULT_TRIP_LOOKUPS}")
    parser.add_argument("--no-refresh", dest="refresh", action="store_false", help="Report seats.aero's cached data as-is instead of re-scraping matches first (saves quota)")
    parser.add_argument("--refresh", dest="refresh", action="store_true", help=argparse.SUPPRESS)  # default; kept for older docs
    parser.set_defaults(refresh=True)
    parser.add_argument("--refresh-older-than", type=float, default=DEFAULT_REFRESH_OLDER_THAN_HOURS, metavar="HOURS", help=f"Only re-scrape records older than this many hours. Default {DEFAULT_REFRESH_OLDER_THAN_HOURS:g} (everything that matched, up to {MAX_REFRESH_RECORDS})")
    parser.add_argument("--refresh-timeout", type=float, default=DEFAULT_REFRESH_TIMEOUT_SECONDS, metavar="SECONDS", help=f"How long to wait for seats.aero to finish re-scraping. Default {DEFAULT_REFRESH_TIMEOUT_SECONDS:g}")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a markdown table")
    parser.add_argument("--html", metavar="PATH", default=None, help=f"Where to write the HTML report. Default {DEFAULT_REPORT_DIR}/awards_<route>_<date>_pax<N>.html")
    parser.add_argument("--no-html", action="store_true", help="Skip writing the HTML report")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress messages on stderr")
    args = parser.parse_args(argv)
    for path in args.cross_check:
        if not Path(path).exists():
            raise UsageError(f"--cross-check path not found: {path}")
    if args.load:
        if not Path(args.load).is_file():
            raise UsageError(f"--load file not found: {args.load}")
        return None, args
    if not args.origin or not args.destination:
        raise UsageError("origin and destination airports are required (or pass --load RUN.json)")

    origin = _parse_airports(args.origin, "origin")
    destination = _parse_airports(args.destination, "destination")
    if set(origin.split(",")) & set(destination.split(",")):
        raise UsageError("origin and destination airports must not overlap")
    today = today or date.today()
    if not 0 <= args.flex <= 7:
        raise UsageError("--flex must be between 0 and 7 days")
    if args.date is None:
        if args.flex or args.end_date:
            raise UsageError("--flex and --end-date need a --date to work from")
        start_date = today + timedelta(days=SCHEDULE_OPENING_DAYS[0])
        end_date = today + timedelta(days=SCHEDULE_OPENING_DAYS[1])
        date_mode = "schedule-opening"
    else:
        if not DATE_RE.match(args.date):
            raise UsageError("--date must be YYYY-MM-DD")
        try:
            travel_date = date.fromisoformat(args.date)
        except ValueError as err:
            raise UsageError(f"--date is not a real calendar date: {err}") from None
        if travel_date < today:
            raise UsageError(f"--date {travel_date} is in the past (today is {today})")
        if args.end_date:
            if args.flex:
                raise UsageError("use either --flex or --end-date, not both")
            if not DATE_RE.match(args.end_date):
                raise UsageError("--end-date must be YYYY-MM-DD")
            try:
                end_date = date.fromisoformat(args.end_date)
            except ValueError as err:
                raise UsageError(f"--end-date is not a real calendar date: {err}") from None
            if end_date < travel_date:
                raise UsageError("--end-date must not be before --date")
            if (end_date - travel_date).days + 1 > MAX_RANGE_DAYS:
                raise UsageError(f"--date to --end-date spans more than {MAX_RANGE_DAYS} days; split it into shorter runs")
            start_date, date_mode = travel_date, "range"
        else:
            start_date = travel_date - timedelta(days=args.flex)
            end_date = travel_date + timedelta(days=args.flex)
            date_mode = "flex" if args.flex else "exact"
    if not 1 <= args.pax <= 9:
        raise UsageError("--pax must be between 1 and 9")
    if args.refresh_older_than < 0 or args.refresh_timeout < 0:
        raise UsageError("--refresh-older-than and --refresh-timeout must be >= 0")
    cabins = tuple(c.strip().lower() for c in args.cabins.split(",") if c.strip())
    unknown = [c for c in cabins if c not in CABIN_CODES]
    if unknown or not cabins:
        raise UsageError(f"--cabins accepts only {', '.join(CABIN_CODES)} (got {args.cabins!r}); economy is out of scope for this tool")
    sources = tuple(s.strip().lower() for s in args.sources.split(",") if s.strip())
    bad_sources = [s for s in sources if s not in PROGRAM_NAMES]
    if bad_sources:
        raise UsageError(f"unknown program code(s) {', '.join(bad_sources)}; valid: {', '.join(sorted(PROGRAM_NAMES))}")
    if args.max_trip_lookups < 0:
        raise UsageError("--max-trip-lookups must be >= 0")

    query = SearchQuery(
        origin=origin,
        destination=destination,
        start_date=start_date,
        end_date=end_date,
        pax=args.pax,
        cabins=cabins,
        direct_only=args.direct_only,
        sources=sources,
        max_trip_lookups=args.max_trip_lookups,
        date_mode=date_mode,
        refresh=args.refresh,
        refresh_older_than_hours=args.refresh_older_than,
        refresh_timeout_seconds=args.refresh_timeout,
    )
    return query, args


def _parse_airports(raw: str, label: str) -> str:
    codes = []
    for code in raw.upper().replace(" ", "").split(","):
        if not code:
            continue
        if not IATA_RE.match(code):
            raise UsageError(f"'{code}' in {label} is not a 3-letter IATA airport code")
        if code not in codes:
            codes.append(code)
    if not codes:
        raise UsageError(f"{label} airport is required")
    if len(codes) > MAX_AIRPORTS_PER_SIDE:
        raise UsageError(f"at most {MAX_AIRPORTS_PER_SIDE} {label} airports may be given")
    return ",".join(codes)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        query, args = parse_args(argv)
        api_key = None if args.load else resolve_api_key()
    except UsageError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2

    log = (lambda _msg: None) if args.quiet else (lambda msg: print(msg, file=sys.stderr))
    if args.load:
        try:
            result = load_result(Path(args.load))
        except (OSError, ValueError, KeyError, TypeError) as err:
            print(f"error: could not read {args.load} as a previous run: {err}", file=sys.stderr)
            return 2
        query = result.query
        log(f"Loaded previous run from {args.load} ({len(result.options)} rows); no seats.aero calls made")
    else:
        client = SeatsAeroClient(api_key, timeout=args.timeout)
        try:
            result = run_search(client, query, log=log)
        except SeatsAeroError as err:
            print(f"error: {err}", file=sys.stderr)
            return 3
    if args.cross_check:
        apply_cross_check(result, [Path(p) for p in args.cross_check], log=log)

    report_path: Path | None = None
    if not args.no_html:
        report_path = Path(args.html) if args.html else default_report_path(query)
        try:
            write_report(result, report_path)
        except OSError as err:
            print(f"error: could not write HTML report to {report_path}: {err}", file=sys.stderr)
            return 3
        log(f"HTML report written to {report_path}")

    print(render_json(result, report_path) if args.json else render_markdown(result, report_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
