"""A deterministic stand-in for the seats.aero Partner API, for offline batch testing.

`FakeSeatsAero` builds a realistic availability/trip dataset from a seed plus the route and
date window, then answers the three endpoints `search_awards.py` uses (`/search`, `/trips/{id}`,
`/refresh`) through an opener that can be handed to `SeatsAeroClient`. It deliberately mixes in
the awkward shapes seen live -- pagination, rate limiting, stale caches, hidden seat counts,
dynamically priced itineraries, refresh outages, missing fields and hostile text -- so a batch
run exercises the paths a single happy-path test never reaches.

No network access and no API key are involved.
"""
from __future__ import annotations

import email.message
import io
import json
import random
import urllib.error
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Any

PAGE_SIZE = 50  # small on purpose: every scenario with 50+ records exercises cursor pagination

PROGRAMS = [
    ("aeroplan", ["AC", "NH", "SQ"], "CAD"),
    ("american", ["AA", "QR", "JL"], "USD"),
    ("united", ["UA", "LH", "NH"], "USD"),
    ("qantas", ["QF", "EK", "CX"], "AUD"),
    ("alaska", ["AS", "CX", "JL"], "USD"),
    ("flyingblue", ["AF", "KL", "DL"], "EUR"),
    ("virginatlantic", ["VS", "DL", "AF"], "GBP"),
    ("qatar", ["QR", "BA", "AY"], "USD"),
    ("singapore", ["SQ", "LH", "NZ"], "SGD"),
    ("british", ["BA", "AY", "QR"], "GBP"),
    ("turkish", ["TK", "UA", "SQ"], "USD"),
    ("delta", ["DL", "KL", "VS"], "USD"),
]

AIRCRAFT = ["Boeing 777-300ER", "Airbus A350-900", "Boeing 787-9", "Airbus A380-800", "Boeing 777-200LR"]

# Text that must never reach the report unescaped. Injected into a few records per matrix so the
# HTML renderer's escaping and URL filtering are tested with real data flowing through the CLI.
XSS_TEXT = '<script>alert("xss")</script>'
XSS_LINK = 'javascript:alert(1)'


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class FakeSeatsAero:
    """One scenario's worth of fake API state: the dataset plus the faults to inject."""

    def __init__(
        self,
        seed: str,
        origin: str,
        destination: str,
        start_date: date,
        end_date: date,
        *,
        now: datetime | None = None,
        empty: bool = False,
        faults: tuple[str, ...] = (),
        hostile: bool = False,
        records: int | None = None,
    ) -> None:
        self.rng = random.Random(f"{seed}|{origin}|{destination}|{start_date}")
        self.origin_codes = origin.split(",")
        self.destination_codes = destination.split(",")
        self.start_date = start_date
        self.end_date = end_date
        self.now = now or datetime.now(timezone.utc)
        self.faults = set(faults)
        self.hostile = hostile
        self.record_count = records
        self.requests: list[str] = []
        self.refresh_calls = 0
        self.search_calls = 0
        self.trip_calls = 0
        self._fired: set[str] = set()
        self.availabilities: dict[str, dict[str, Any]] = {}
        self.trips: dict[str, dict[str, Any]] = {}
        if not empty:
            self._build()

    # ----------------------------------------------------------------- dataset

    def _build(self) -> None:
        days = (self.end_date - self.start_date).days + 1
        record_count = self.record_count if self.record_count is not None else self.rng.choice([3, 12, 40, 55, 90])
        for index in range(record_count):
            day = self.start_date + timedelta(days=self.rng.randrange(days))
            source, carriers, currency = self.rng.choice(PROGRAMS)
            availability = self._availability(index, day, source, carriers, currency)
            self.availabilities[availability["ID"]] = availability
            self.trips[availability["ID"]] = self._trips_payload(availability, carriers, index)

    def _availability(self, index: int, day: date, source: str, carriers: list[str], currency: str) -> dict[str, Any]:
        rng = self.rng
        j_open = rng.random() < 0.8
        f_open = rng.random() < 0.3
        if not (j_open or f_open):
            j_open = True  # every record reports something; economy-only rows are covered by the cabin filter below
        age_hours = rng.choice([0.2, 1.5, 6.0, 30.0, 96.0])  # the last two are stale (> 24h)
        updated = _iso(self.now - timedelta(hours=age_hours))
        j_seats = rng.choice([0, 1, 2, 2, 4, 9])  # 0 means "program hides the count"
        f_seats = rng.choice([0, 1, 2])
        airline_text = ", ".join(rng.sample(carriers, k=rng.randint(1, len(carriers))))
        if self.hostile and index % 17 == 0:
            airline_text = XSS_TEXT
        record: dict[str, Any] = {
            "ID": f"{source}-{index}-{day.isoformat()}",
            "Date": day.isoformat(),
            "ParsedDate": day.isoformat(),
            "Source": source,
            "Route": {
                "OriginAirport": rng.choice(self.origin_codes),
                "DestinationAirport": rng.choice(self.destination_codes),
                "Distance": rng.randrange(2000, 9000),
            },
            "JAvailable": j_open,
            "FAvailable": f_open,
            "TaxesCurrency": currency,
            "UpdatedAt": updated,
        }
        if j_open:
            record.update(self._cabin_fields("J", rng.randrange(45, 220) * 1000, j_seats, airline_text, 0.45, index))
        if f_open:
            record.update(self._cabin_fields("F", rng.randrange(90, 400) * 1000, f_seats, airline_text, 0.3, index))
        if index % 13 == 0:
            # Records with fields missing entirely: the renderer must cope without them.
            record.pop("TaxesCurrency", None)
            record["JMileageCost"] = None
        return record

    def _cabin_fields(self, code: str, cost: int, seats: int, airlines: str, direct_odds: float, index: int) -> dict[str, Any]:
        """One cabin's block of an Availability object, in the shapes seats.aero actually returns."""
        rng = self.rng
        fields: dict[str, Any] = {
            f"{code}MileageCost": str(cost),          # seats.aero returns these as strings
            f"{code}MileageCostRaw": cost,
            f"{code}RemainingSeats": seats,
            f"{code}Airlines": airlines,
            f"{code}TotalTaxes": rng.randrange(500, 120000),
        }
        if index % 9 == 0:
            return fields   # some records omit {X}Direct entirely: "unknown", not "connecting"
        direct = rng.random() < direct_odds
        fields[f"{code}Direct"] = direct
        if direct and index % 3 == 0:
            # The newer per-cabin nonstop figures: dearer than the cabin's cheapest space, and
            # sometimes with a different seat count, which is exactly what a nonstop-only run must show.
            fields[f"{code}DirectMileageCost"] = str(cost + rng.randrange(5, 60) * 1000)
            fields[f"{code}DirectRemainingSeats"] = rng.choice([0, 2, 4, 9])
            fields[f"{code}DirectAirlines"] = airlines.split(",")[0].strip()
        return fields

    def _trips_payload(self, availability: dict[str, Any], carriers: list[str], index: int) -> dict[str, Any]:
        rng = self.rng
        trips = []
        for n in range(rng.randint(0, 4)):
            cabin = rng.choices(["business", "first", "economy"], weights=[6, 2, 2])[0]
            if cabin == "business" and not availability.get("JAvailable"):
                cabin = "first" if availability.get("FAvailable") else "economy"
            if cabin == "first" and not availability.get("FAvailable"):
                cabin = "business" if availability.get("JAvailable") else "economy"
            stops = rng.choice([0, 0, 1, 2])
            legs = rng.sample(carriers, k=min(stops + 1, len(carriers)))
            depart = datetime.fromisoformat(availability["Date"]).replace(tzinfo=timezone.utc) + timedelta(hours=rng.randrange(0, 22))
            duration = rng.randrange(180, 1100)
            flight_numbers = ", ".join(f"{code}{rng.randrange(1, 999)}" for code in legs)
            if self.hostile and n == 0 and index % 5 == 0:
                flight_numbers = XSS_TEXT
            trips.append({
                "ID": f"{availability['ID']}-trip{n}",
                "AvailabilityID": availability["ID"],
                "Cabin": cabin,
                "Filtered": rng.random() < 0.2,          # seats.aero's dynamic-pricing flag
                "Stops": stops,
                "Carriers": ", ".join(legs),
                "FlightNumbers": flight_numbers,
                "DepartsAt": _iso(depart),
                "ArrivesAt": _iso(depart + timedelta(minutes=duration)),
                "TotalDuration": duration,
                "RemainingSeats": rng.choice([0, 1, 2, 3, 6]),
                "MileageCost": rng.randrange(40, 300) * 1000,
                "TotalTaxes": rng.randrange(300, 110000),
                "TaxesCurrency": availability.get("TaxesCurrency", "USD"),
                "UpdatedAt": availability["UpdatedAt"],
                "AvailabilitySegments": [
                    {
                        "Order": order,
                        "FlightNumber": f"{code}{rng.randrange(1, 999)}",
                        "AircraftName": rng.choice(AIRCRAFT),
                        "OriginAirport": rng.choice(self.origin_codes),
                        "DestinationAirport": rng.choice(self.destination_codes),
                    }
                    for order, code in enumerate(reversed(legs))   # deliberately out of order; the script re-sorts
                ],
            })
        link = XSS_LINK if (self.hostile and index % 7 == 0) else f"https://seats.aero/booking/{availability['ID']}"
        return {
            "source": availability["Source"],
            "count": len(trips),
            "data": trips,
            "booking_links": [{"label": "Book", "link": link, "primary": True}],
        }

    # ----------------------------------------------------------------- opener

    def opener(self, request, timeout):  # noqa: ANN001 - matches SeatsAeroClient's Opener type
        url = request.full_url
        self.requests.append(url)
        if request.headers.get("Partner-authorization") in (None, ""):
            raise AssertionError("request left seats.aero without the Partner-Authorization header")
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.rsplit("/partnerapi/", 1)[-1]
        params = dict(urllib.parse.parse_qsl(parsed.query))
        if path == "search":
            self.search_calls += 1
            self._maybe_fail("search", "search_rate_limited", 429)
            self._maybe_fail("search", "search_server_error", 503)
            return _Response(self._search(params))
        if path.startswith("trips/"):
            self.trip_calls += 1
            availability_id = urllib.parse.unquote(path[len("trips/"):])
            if "trip_not_found" in self.faults and self.trip_calls % 7 == 0:
                raise _http_error(url, 404, b'{"error":"availability not found"}')
            return _Response(self.trips.get(availability_id, {"data": [], "booking_links": []}))
        if path == "refresh":
            self.refresh_calls += 1
            body = json.loads(request.data.decode("utf-8"))
            return _Response(self._refresh(body.get("availability_ids") or []))
        raise _http_error(url, 404, b'{"error":"unknown endpoint"}')

    def _maybe_fail(self, key: str, fault: str, code: int) -> None:
        if fault in self.faults and fault not in self._fired:
            self._fired.add(fault)
            raise _http_error("https://seats.aero/partnerapi/" + key, code, b'{"error":"slow down"}')

    def _search(self, params: dict[str, str]) -> dict[str, Any]:
        wanted_origins = set(params.get("origin_airport", "").split(","))
        wanted_destinations = set(params.get("destination_airport", "").split(","))
        start = date.fromisoformat(params["start_date"])
        end = date.fromisoformat(params["end_date"])
        sources = {s for s in params.get("sources", "").split(",") if s}
        rows = [
            record for record in self.availabilities.values()
            if record["Route"]["OriginAirport"] in wanted_origins
            and record["Route"]["DestinationAirport"] in wanted_destinations
            and start <= date.fromisoformat(record["Date"]) <= end
            and (not sources or record["Source"] in sources)
        ]
        rows.sort(key=lambda r: (r["Date"], r["ID"]))
        cursor = int(params.get("cursor", 0))
        page = rows[cursor:cursor + PAGE_SIZE]
        has_more = cursor + PAGE_SIZE < len(rows)
        return {
            "data": page,
            "count": len(page),
            "hasMore": has_more,
            "cursor": cursor + PAGE_SIZE if has_more else 0,
        }

    def _refresh(self, ids: list[str]) -> dict[str, Any]:
        items = []
        incomplete = "refresh_never_completes" in self.faults
        for position, availability_id in enumerate(ids):
            record = self.availabilities.get(availability_id)
            source = record["Source"] if record else ""
            if source == "singapore":
                status = "skipped_outage"        # seats.aero pauses some programs' scraping
            elif incomplete and position % 3 == 0:
                status = "processing"
            elif "refresh_partly_fails" in self.faults and position % 11 == 0:
                status = "failed"
            else:
                status = "succeeded"
                if record is not None:
                    record["UpdatedAt"] = _iso(self.now)   # a real refresh makes the record current
                    for trip in self.trips.get(availability_id, {}).get("data", []):
                        trip["UpdatedAt"] = record["UpdatedAt"]
            items.append({"availability_id": availability_id, "status": status, "updated_at": _iso(self.now)})
        pending = sum(1 for item in items if item["status"] in ("queued", "processing"))
        return {
            "items": items,
            "queued": len(items),
            "refunded": 0,
            "counts": {
                "processing": pending,
                "succeeded": sum(1 for item in items if item["status"] == "succeeded"),
                "failed": sum(1 for item in items if item["status"] == "failed"),
            },
            "complete": pending == 0,
            "quota": {"limit": 1000, "used": 40 + len(items), "remaining": 960 - len(items), "reset_seconds": 3600},
        }


class _Response:
    """Minimal stand-in for the object urlopen returns."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _http_error(url: str, code: int, body: bytes) -> urllib.error.HTTPError:
    headers = email.message.Message()
    headers["Retry-After"] = "0"   # keeps the client's backoff instant in tests
    return urllib.error.HTTPError(url, code, "fake failure", headers, io.BytesIO(body))
