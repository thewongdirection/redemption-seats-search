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

BASE_URL = "https://seats.aero/partnerapi"
API_KEY_ENV_VARS = ("SEATS_AERO_API_KEY", "SEATS_API_KEY")
API_KEY_FILE = Path.home() / ".config" / "seats-aero" / "api_key"

# Premium cabins only. Economy (Y) and premium economy (W) are intentionally absent.
CABIN_CODES = {"business": "J", "first": "F"}
DEFAULT_CABINS = ("business", "first")

MAX_PAGES = 20            # safety cap on cached-search pagination
REFRESH_BATCH = 250        # seats.aero accepts at most 250 availability IDs per refresh request
REFRESH_POLL_SECONDS = 5.0
DEFAULT_REFRESH_OLDER_THAN_HOURS = 24.0
DEFAULT_REFRESH_TIMEOUT_SECONDS = 120.0
# With no --date, scan the far edge of the booking window: most airlines load award
# inventory 354-355 days ahead, so this is where fresh premium space first appears.
SCHEDULE_OPENING_DAYS = (354, 355)
DEFAULT_TRIP_LOOKUPS = 40  # cap on /trips/{id} calls per run (Pro keys get ~1000 calls/day)
STALE_AFTER_HOURS = 24     # flag cached availability older than this

IATA_RE = re.compile(r"^[A-Z]{3}$")
MAX_AIRPORTS_PER_SIDE = 4  # e.g. "PEK,PKX" for Beijing or "LHR,LGW" for London
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

PROGRAM_NAMES = {
    "aeromexico": "Aeromexico Rewards",
    "aeroplan": "Air Canada Aeroplan",
    "alaska": "Alaska Atmos Rewards",
    "american": "American AAdvantage",
    "azul": "Azul Fidelidade",
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

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request(path, body=body)

    def _request(self, path: str, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
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

    def refresh(self, availability_ids: Sequence[str]) -> dict[str, Any]:
        """Queue (or poll) a re-scrape of up to 250 Availability objects. Pro keys only.

        The same call both queues and polls: re-posting IDs that are already processing
        does not re-queue them or spend quota. Response shape observed live:
          {"items":[{"availability_id","status","updated_at"}], "queued", "refunded",
           "counts":{"processing","succeeded","failed"}, "complete": bool,
           "quota":{"limit","used","remaining","reset_seconds"}}
        Statuses seen: queued, processing, succeeded, failed, skipped_outage.
        """
        return self.post("refresh", {"availability_ids": list(availability_ids)})

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
        while pending and waited < timeout_seconds:
            self._sleep(poll_seconds)
            waited += poll_seconds
            still_pending = []
            for batch in pending:
                response = self.refresh(batch)
                outcome.absorb(response)
                if not response.get("complete"):
                    still_pending.append(batch)
            pending = still_pending
            log(f"refresh: {outcome.succeeded} done, {outcome.processing} in progress after {waited:.0f}s")
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
            parts.append(f"{self.failed} failed")
        if self.processing:
            parts.append(f"{self.processing} still processing when the wait timed out")
        text = f"Refresh of {self.requested} stale record{'s' if self.requested != 1 else ''}: " + ", ".join(parts) + "."
        if self.quota:
            text += f" Daily API quota: {self.quota.get('remaining')}/{self.quota.get('limit')} calls remaining."
        return text

    def to_json(self) -> dict[str, Any]:
        return {
            "requested": self.requested, "succeeded": self.succeeded, "skipped": self.skipped, "failed": self.failed,
            "still_processing": self.processing, "timed_out": self.timed_out, "waited_seconds": self.waited_seconds,
            "statuses": self.statuses, "quota": self.quota,
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

    @property
    def seats_known(self) -> bool:
        return self.remaining_seats > 0


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
    date_mode: str = "exact"           # exact | flex | schedule-opening
    refresh: bool = False
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
    stale_ids = [a["ID"] for a in candidates if _hours_since(str(a.get("UpdatedAt") or "")) > query.refresh_older_than_hours]
    if not stale_ids:
        log(f"refresh: nothing older than {query.refresh_older_than_hours:g}h to refresh")
        return None
    log(f"refresh: asking seats.aero to re-scrape {len(stale_ids)} record(s) older than {query.refresh_older_than_hours:g}h")
    return client.refresh_and_wait(stale_ids, timeout_seconds=query.refresh_timeout_seconds, log=log)


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
                notes.append(f"Refresh requested, but every matching record was already newer than {query.refresh_older_than_hours:g}h.")
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

    options.sort(key=lambda o: (o.mileage_cost or 10**9, o.taxes_minor_units, o.departs_at))
    return SearchResult(
        query=query,
        options=options,
        notes=notes,
        api_calls=client.calls_made,
        availabilities_seen=seen,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        refresh=refresh_outcome,
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


def render_markdown(result: SearchResult, report_path: Path | None = None) -> str:
    q = result.query
    lines = [
        f"## Premium-cabin award seats {q.origin_label} → {q.destination_label}",
        f"{'Date' if q.single_day else 'Dates'}: {q.window_label} · Passengers: {q.pax} · Cabins: {', '.join(c.title() for c in q.cabins)}"
        + (" · Nonstop only" if q.direct_only else "") + (" · Refresh requested" if q.refresh else ""),
        "",
    ]
    if not result.options:
        lines.append(
            f"No business or first class award space is cached on seats.aero for this search "
            f"({result.availabilities_seen} availability records checked)."
        )
        lines.append("Try `--flex 3` for nearby dates, alternate airports, or a single passenger to see if space exists for fewer seats.")
        if result.notes:
            lines.append("")
            lines.append("Notes:")
            lines += [f"- {n}" for n in result.notes]
        if report_path is not None:
            lines.append(f"\n_HTML report: {report_path}_")
        return "\n".join(lines)

    header = "| # | Program | Cabin | Airline | Flights | Date | Route | Dep → Arr | Duration | Stops | Seats | Miles / pax | Taxes / pax | Updated | Book |"
    divider = "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    lines += [header, divider]
    for idx, o in enumerate(result.options, start=1):
        url = booking_url(o)
        book = f"[book]({url})" if url else "-"
        dep_arr = f"{format_clock(o.departs_at)} → {format_clock(o.arrives_at, o.departs_at)}" if o.departs_at else "-"
        seats = str(o.remaining_seats) if o.seats_known else "?"
        lines.append(
            f"| {idx} | {o.program} | {o.cabin.title()} | {format_airlines(o.airlines)} | {o.flight_numbers} | {o.travel_date} "
            f"| {o.route} | {dep_arr} | {format_duration(o.duration_minutes)} | {format_stops(o.stops)} | {seats} "
            f"| {format_miles(o.mileage_cost)} | {format_taxes(o.taxes_minor_units, o.taxes_currency)} | {format_age(o.updated_at)} | {book} |"
        )
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
        "generated_at": result.generated_at,
        "api_calls": result.api_calls,
        "availabilities_seen": result.availabilities_seen,
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
main{max-width:1400px;margin:0 auto;padding:32px 24px 48px}
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
table{border-collapse:separate;border-spacing:0;width:100%;min-width:960px;font-size:14px}
th,td{padding:9px 8px;text-align:left;vertical-align:top;border-bottom:1px solid var(--border);white-space:nowrap;background:var(--surface)}
th{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);background:var(--surface-2);position:sticky;top:0;z-index:1}
td.wrap,th.wrap{white-space:normal;min-width:150px}
th:last-child,td:last-child{position:sticky;right:0;z-index:2;box-shadow:-8px 0 12px -8px rgba(0,0,0,.6)}
tbody tr:hover td{background:var(--surface-2)}
tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600}
.badge.business{background:rgba(115,218,202,.15);color:var(--business)}
.badge.first{background:rgba(224,175,104,.18);color:var(--first)}
.badge.stale{background:rgba(247,118,142,.15);color:var(--warn)}
.badge.summary{background:rgba(154,163,178,.15);color:var(--muted)}
.muted{color:var(--muted);font-size:12px;white-space:normal}
.miles{font-weight:600;font-size:16px}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
a.book{display:inline-block;background:var(--accent);color:#0b0e14;font-weight:600;padding:6px 14px;border-radius:8px}
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
    if not codes:
        return "-"
    parts = []
    for code in codes:
        label = f"{AIRLINE_NAMES[code]} ({code})" if code in AIRLINE_NAMES else code
        parts.append(_html_link(airline_url(code), label))
    return ", ".join(parts)


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
    return "".join(
        f'<div class="card"><div class="label">{_esc(label)}</div><div class="value">{_esc(value)}</div><div class="detail">{_esc(detail)}</div></div>'
        for label, value, detail in cards
    )


def _html_row(idx: int, o: AwardOption) -> str:
    stale = o.updated_at and _hours_since(o.updated_at) > STALE_AFTER_HOURS
    seats = str(o.remaining_seats) if o.seats_known else '<span title="Program does not publish seat counts">?</span>'
    dep_arr = f"{format_clock(o.departs_at)} → {format_clock(o.arrives_at, o.departs_at)}" if o.departs_at else "-"
    aircraft = ", ".join(a for a in o.aircraft if a)
    flights = _esc(o.flight_numbers)
    if o.detail_level == "summary":
        flights = f'<span class="badge summary">summary</span> <span class="muted">{flights}</span>'
    elif aircraft:
        flights += f'<div class="muted">{_esc(aircraft)}</div>'
    url = booking_url(o)
    if url and safe_url(o.booking_link):
        book = _html_link(url, "Book →", "book")
    elif url:
        book = _html_link(url, "Program site →", "book book-fallback")
    else:
        book = "-"
    updated = _esc(format_age(o.updated_at))
    if stale:
        updated += ' <span class="badge stale">stale</span>'
    return (
        f'<tr><td class="num">{idx}</td><td class="wrap">{_esc(o.program)}</td>'
        f'<td><span class="badge {_esc(o.cabin)}">{_esc(o.cabin.title())}</span></td>'
        f'<td class="wrap">{_html_airlines(o.airlines)}</td><td>{flights}</td><td>{_esc(o.travel_date)}</td>'
        f"<td>{_esc(o.route)}</td>"
        f"<td>{_esc(dep_arr)}</td><td>{_esc(format_duration(o.duration_minutes))}</td><td>{_esc(format_stops(o.stops))}</td>"
        f'<td class="num">{seats}</td><td class="num miles">{_esc(format_miles(o.mileage_cost))}</td>'
        f'<td class="num">{_esc(format_taxes(o.taxes_minor_units, o.taxes_currency))}</td>'
        f"<td>{updated}</td><td>{book}</td></tr>"
    )


def render_html(result: SearchResult) -> str:
    q = result.query
    title = f"Award seats {q.origin_label} → {q.destination_label} · {q.window_label}"
    chips = [
        ("Date" if q.single_day else "Dates", q.window_label), ("Passengers", str(q.pax)), ("Cabins", ", ".join(c.title() for c in q.cabins)),
    ]
    if q.direct_only:
        chips.append(("Routing", "Nonstop only"))
    if q.refresh:
        chips.append(("Refresh", "requested for records older than %gh" % q.refresh_older_than_hours))
    if q.date_mode == "schedule-opening":
        chips.append(("Mode", f"schedule opening, {SCHEDULE_OPENING_DAYS[0]}-{SCHEDULE_OPENING_DAYS[1]} days out"))
    if q.sources:
        chips.append(("Programs", ", ".join(q.sources)))
    chips_html = "".join(f'<span class="chip">{_esc(k)}: <b>{_esc(v)}</b></span>' for k, v in chips)

    if result.options:
        rows = "".join(_html_row(i, o) for i, o in enumerate(result.options, start=1))
        body = (
            f'<div class="cards">{_html_summary_cards(result)}</div>'
            '<div class="tablewrap"><table><thead><tr>'
            '<th>#</th><th class="wrap">Program</th><th>Cabin</th><th class="wrap">Airline</th><th>Flights</th><th>Date</th><th>Route</th><th>Dep → Arr</th>'
            "<th>Duration</th><th>Stops</th><th>Seats</th><th>Miles / pax</th><th>Taxes / pax</th><th>Updated</th><th>Book</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div>"
        )
    else:
        body = (
            '<div class="empty"><p><b>No business or first class award space is cached for this search.</b></p>'
            f"<p>{result.availabilities_seen} availability records were checked. Try nearby dates (--flex 3), "
            "alternate airports, or fewer passengers to see whether space exists for a smaller party.</p></div>"
        )
    notes_html = ""
    if result.notes:
        notes_html = '<section class="notes"><h2>Notes</h2><ul>' + "".join(f"<li>{_esc(n)}</li>" for n in result.notes) + "</ul></section>"

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_esc(title)}</title><style>{HTML_STYLE}</style></head><body><main>"
        f'<h1>{_esc(q.origin_label)}<span class="arrow">→</span>{_esc(q.destination_label)}</h1>'
        '<p class="sub">Business &amp; first class award availability from seats.aero. Miles and taxes are per passenger. '
        "Book links open the mileage program that holds the space; airline links open the operating carrier.</p>"
        f'<div class="chips">{chips_html}</div>{body}{notes_html}'
        f"<footer>Generated {_esc(result.generated_at)} · {result.api_calls} seats.aero API calls · cached data, verify before transferring points.</footer>"
        "</main></body></html>"
    )


# --------------------------------------------------------------------------- cli


def parse_args(argv: Sequence[str] | None = None, today: date | None = None) -> tuple[SearchQuery, argparse.Namespace]:
    parser = argparse.ArgumentParser(
        prog="search_awards.py",
        description="Find business/first class award seats on seats.aero for a route and date.",
    )
    parser.add_argument("origin", help="Origin airport IATA code(s), e.g. SIN or LHR,LGW")
    parser.add_argument("destination", help="Destination airport IATA code(s), e.g. PEK,PKX")
    parser.add_argument("--date", default=None, help=f"Departure date, YYYY-MM-DD. Omit to scan {SCHEDULE_OPENING_DAYS[0]}-{SCHEDULE_OPENING_DAYS[1]} days out (schedule opening)")
    parser.add_argument("--pax", type=int, default=1, help="Number of passengers (1-9). Default 1")
    parser.add_argument("--flex", type=int, default=0, metavar="DAYS", help="Also search +/- DAYS around --date (0-7)")
    parser.add_argument("--cabins", default=",".join(DEFAULT_CABINS), help="Comma list from: business,first. Default both")
    parser.add_argument("--direct-only", action="store_true", help="Only nonstop itineraries")
    parser.add_argument("--sources", default="", help="Comma list of seats.aero program codes to restrict to (e.g. aeroplan,united)")
    parser.add_argument("--max-trip-lookups", type=int, default=DEFAULT_TRIP_LOOKUPS, help=f"Cap on per-availability trip detail calls. Default {DEFAULT_TRIP_LOOKUPS}")
    parser.add_argument("--refresh", action="store_true", help="Ask seats.aero to re-scrape stale matches before reporting (Pro keys; spends daily quota)")
    parser.add_argument("--refresh-older-than", type=float, default=DEFAULT_REFRESH_OLDER_THAN_HOURS, metavar="HOURS", help=f"With --refresh, only records older than this. Default {DEFAULT_REFRESH_OLDER_THAN_HOURS:g}")
    parser.add_argument("--refresh-timeout", type=float, default=DEFAULT_REFRESH_TIMEOUT_SECONDS, metavar="SECONDS", help=f"With --refresh, how long to wait for seats.aero. Default {DEFAULT_REFRESH_TIMEOUT_SECONDS:g}")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a markdown table")
    parser.add_argument("--html", metavar="PATH", default=None, help=f"Where to write the HTML report. Default {DEFAULT_REPORT_DIR}/awards_<route>_<date>_pax<N>.html")
    parser.add_argument("--no-html", action="store_true", help="Skip writing the HTML report")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress messages on stderr")
    args = parser.parse_args(argv)

    origin = _parse_airports(args.origin, "origin")
    destination = _parse_airports(args.destination, "destination")
    if set(origin.split(",")) & set(destination.split(",")):
        raise UsageError("origin and destination airports must not overlap")
    today = today or date.today()
    if not 0 <= args.flex <= 7:
        raise UsageError("--flex must be between 0 and 7 days")
    if args.date is None:
        if args.flex:
            raise UsageError("--flex needs a --date to be flexible around")
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
        api_key = resolve_api_key()
    except UsageError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2

    log = (lambda _msg: None) if args.quiet else (lambda msg: print(msg, file=sys.stderr))
    client = SeatsAeroClient(api_key, timeout=args.timeout)
    try:
        result = run_search(client, query, log=log)
    except SeatsAeroError as err:
        print(f"error: {err}", file=sys.stderr)
        return 3

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
