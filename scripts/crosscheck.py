"""Cross-check seats.aero results against FlightPoints (a second award-availability source).

seats.aero remains the primary source. When the FlightPoints MCP tools are available, Claude saves
their raw text output to files and passes them to search_awards.py via --cross-check. This module
parses those files into CrossCheckEntry records and matches them against the seats.aero options:

  * a "flight" match: same date, cabin and flight numbers (FlightPoints get-flight-details output)
  * a "program" match: same date, route, cabin, program and mileage price (search-flights summary)

A matched option is shown as confirmed by two sources and grouped at the top of the report.
Only the Python standard library is used.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

# FlightPoints identifies programs two ways: an airline-style code in get-flight-details
# ("[AA](...)") and a display name in search-flights ("- AAdvantage: First 40,000+$51").
# Both map onto seats.aero's source codes so matching can be done on one key.
PROGRAM_CODE_TO_SOURCE = {
    "AA": "american", "AC": "aeroplan", "AS": "alaska", "QF": "qantas", "QR": "qatar", "UA": "united",
    "SQ": "singapore", "AF": "flyingblue", "KL": "flyingblue", "VS": "virginatlantic", "B6": "jetblue",
    "DL": "delta", "EK": "emirates", "EY": "etihad", "LH": "lufthansa", "VA": "velocity", "SK": "eurobonus",
    "AY": "finnair", "G3": "smiles", "BA": "british", "TK": "turkish", "AM": "aeromexico", "CM": "connectmiles",
    "AD": "azul", "ET": "ethiopian", "SV": "saudia", "F9": "frontier", "NK": "spirit",
}
PROGRAM_NAME_TO_SOURCE = {
    "aadvantage": "american", "aeroplan": "aeroplan", "atmos rewards": "alaska", "mileage plan": "alaska",
    "frequent flyer": "qantas", "privilege club / avios": "qatar", "privilege club": "qatar", "mileageplus": "united",
    "krisflyer": "singapore", "flying blue": "flyingblue", "flying club": "virginatlantic", "trueblue": "jetblue",
    "skymiles": "delta", "skywards": "emirates", "etihad guest": "etihad", "miles & more": "lufthansa",
    "velocity": "velocity", "eurobonus": "eurobonus", "finnair plus": "finnair", "smiles": "smiles",
    "executive club": "british", "british airways club": "british", "miles&smiles": "turkish",
    "aeromexico rewards": "aeromexico", "connectmiles": "connectmiles", "tudoazul": "azul", "shebamiles": "ethiopian",
    "alfursan": "saudia", "frontier miles": "frontier", "free spirit": "spirit",
}
CABIN_WORDS = {"biz": "business", "business": "business", "first": "first", "econ": "economy", "economy": "economy",
               "prem": "premium", "premium": "premium", "premium economy": "premium"}


@dataclass
class CrossCheckEntry:
    """One award option as reported by FlightPoints, normalised for matching."""

    date: str                 # YYYY-MM-DD
    origin: str
    destination: str
    cabin: str                # business | first | economy | premium
    source: str               # seats.aero program code when known, else the raw FlightPoints label
    miles: int
    seats: int = 0
    flight_numbers: tuple[str, ...] = ()   # normalised, e.g. ("SQ968", "NH872"); empty for summary rows
    taxes_usd: float = 0.0
    program_label: str = ""
    provider: str = "flightpoints"

    @property
    def route(self) -> str:
        return f"{self.origin}-{self.destination}"

    @property
    def is_flight_level(self) -> bool:
        return bool(self.flight_numbers)


# FlightPoints says so explicitly when a query succeeded but found nothing; that is a real
# "the other source looked and saw no premium space" answer, not an unreadable file.
EMPTY_MARKERS = ("Found 0 award flight option", "No flights found matching your criteria",
                 "No detailed flight information found")


def is_empty_result(text: str) -> bool:
    return any(marker in text for marker in EMPTY_MARKERS)


@dataclass
class CrossCheckSummary:
    entries: int = 0
    files: int = 0
    empty_files: int = 0     # queries FlightPoints answered with no availability
    flight_matches: int = 0
    program_matches: int = 0
    price_disagreements: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)   # FlightPoints entries seats.aero did not have

    @property
    def answered(self) -> bool:
        """True when FlightPoints responded to every file we read, whether or not it found anything."""
        return self.files > 0 and (self.entries > 0 or self.empty_files == self.files)

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": "flightpoints", "files": self.files, "entries": self.entries, "empty_files": self.empty_files,
            "flight_matches": self.flight_matches, "program_matches": self.program_matches,
            "price_disagreements": self.price_disagreements, "unmatched": self.unmatched,
        }


# --------------------------------------------------------------------------- normalisation helpers


def normalise_flight_number(text: str) -> str:
    return re.sub(r"\s+", "", text.strip().upper())


def flight_key(numbers: Iterable[str]) -> tuple[str, ...]:
    return tuple(normalise_flight_number(n) for n in numbers if n and n.strip())


def source_for_program(label: str) -> str:
    label = label.strip()
    if label.upper() in PROGRAM_CODE_TO_SOURCE:
        return PROGRAM_CODE_TO_SOURCE[label.upper()]
    return PROGRAM_NAME_TO_SOURCE.get(label.lower(), label.lower())


def _int(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else 0


# --------------------------------------------------------------------------- parsers

_SEARCH_HEADER = re.compile(r"Award Flight Search:\s*([A-Z]{3})\s*(?:→|->)\s*([A-Z]{3})")
_SEARCH_DATE = re.compile(r"Date:\s*(\d{4}-\d{2}-\d{2})")
_PREMIUM_LINE = re.compile(r"^\s*-\s*(?P<program>[^:]+):\s*(?P<rest>.+)$")
_PREMIUM_ITEM = re.compile(r"(?P<cabin>Biz|Business|First|Econ|Economy|Prem(?:ium)?)\s+(?P<miles>[\d,]+)(?:\+\$(?P<tax>[\d.,]+))?", re.I)

_DETAIL_BLOCK = re.compile(r"^\s*\d+\.\s*\[(?P<program>[^\]]+)\]\([^)]*\):\s*(?P<origin>[A-Z]{3})\s*(?:→|->)\s*(?P<dest>[A-Z]{3})", re.M)
_DETAIL_DEPARTS = re.compile(r"Departs:\s*(\d{4}-\d{2}-\d{2})")
_DETAIL_PRICE = re.compile(r"^\s*(?P<cabin>Business|First|Economy|Premium(?: Economy)?):\s*(?P<miles>[\d,]+)\s*pts(?:\s*\+\s*(?P<tax>[\d.]+)\s*taxes)?(?:\s*\|\s*(?P<seats>\d+)\s*seat)?", re.I | re.M)
_SEGMENT = re.compile(r"^\s*\d+\.\s*[A-Z]{3}\s*(?:→|->)\s*[A-Z]{3}\s+(?P<flight>[A-Z0-9]{2}\s?\d{1,4})\b", re.M)


def parse_search_output(text: str) -> list[CrossCheckEntry]:
    """Program-level entries from a search-flights result (no flight numbers)."""
    header = _SEARCH_HEADER.search(text)
    date_match = _SEARCH_DATE.search(text)
    if not header or not date_match:
        return []
    origin, dest = header.group(1), header.group(2)
    date = date_match.group(1)
    entries: list[CrossCheckEntry] = []
    in_premium = False
    for line in text.splitlines():
        if line.strip().lower().startswith("premium cabins"):
            in_premium = True
            continue
        if not in_premium:
            continue
        m = _PREMIUM_LINE.match(line)
        if not m:
            if line.strip() == "" and entries:
                break
            continue
        label = m.group("program").strip().strip("*")
        for item in _PREMIUM_ITEM.finditer(m.group("rest")):
            cabin = CABIN_WORDS.get(item.group("cabin").lower(), item.group("cabin").lower())
            entries.append(CrossCheckEntry(
                date=date, origin=origin, destination=dest, cabin=cabin, source=source_for_program(label),
                miles=_int(item.group("miles")), taxes_usd=float((item.group("tax") or "0").replace(",", "")),
                program_label=label,
            ))
    return entries


def parse_details_output(text: str) -> list[CrossCheckEntry]:
    """Flight-level entries from a get-flight-details result."""
    entries: list[CrossCheckEntry] = []
    blocks = list(_DETAIL_BLOCK.finditer(text))
    for i, block in enumerate(blocks):
        chunk = text[block.end(): blocks[i + 1].start() if i + 1 < len(blocks) else len(text)]
        departs = _DETAIL_DEPARTS.search(chunk)
        if not departs:
            continue
        flights = flight_key(m.group("flight") for m in _SEGMENT.finditer(chunk))
        for price in _DETAIL_PRICE.finditer(chunk):
            cabin = CABIN_WORDS.get(price.group("cabin").lower(), price.group("cabin").lower())
            entries.append(CrossCheckEntry(
                date=departs.group(1), origin=block.group("origin"), destination=block.group("dest"), cabin=cabin,
                source=source_for_program(block.group("program")), miles=_int(price.group("miles")),
                seats=int(price.group("seats") or 0), flight_numbers=flights,
                taxes_usd=float(price.group("tax") or 0), program_label=block.group("program"),
            ))
    return entries


def parse_normalised_json(payload: Any) -> list[CrossCheckEntry]:
    """A hand-written or tool-generated list of {date, origin, destination, cabin, program, miles, seats, flight_numbers}."""
    if isinstance(payload, dict):
        payload = payload.get("entries") or payload.get("results") or []
    entries = []
    for item in payload or []:
        if not isinstance(item, dict) or not item.get("date"):
            continue
        numbers = item.get("flight_numbers") or item.get("flights") or ()
        if isinstance(numbers, str):
            numbers = numbers.split(",")   # "SQ 308, NH 872" -> two flights; inner spaces are normalised away
        entries.append(CrossCheckEntry(
            date=str(item["date"]), origin=str(item.get("origin", "")).upper(), destination=str(item.get("destination", "")).upper(),
            cabin=CABIN_WORDS.get(str(item.get("cabin", "")).lower(), str(item.get("cabin", "")).lower()),
            source=source_for_program(str(item.get("program") or item.get("source") or "")),
            miles=_int(str(item.get("miles", 0))), seats=_int(str(item.get("seats", 0))), flight_numbers=flight_key(numbers),
            program_label=str(item.get("program") or ""),
        ))
    return entries


def parse_text(text: str) -> list[CrossCheckEntry]:
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return parse_normalised_json(json.loads(stripped))
        except json.JSONDecodeError:
            return []
    if "Award Flight Search" in text:
        return parse_search_output(text)
    if "detailed flight option" in text or _DETAIL_BLOCK.search(text):
        return parse_details_output(text)
    return []


def load_files(paths: Sequence[Path]) -> tuple[list[CrossCheckEntry], int, int]:
    """Returns (entries, files read, files that were an explicit empty result)."""
    entries: list[CrossCheckEntry] = []
    files = empty = 0
    for path in paths:
        if path.is_dir():
            sub = sorted(p for p in path.iterdir() if p.is_file())
            more, n, e = load_files(sub)
            entries += more
            files += n
            empty += e
            continue
        files += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        found = parse_text(text)
        entries += found
        if not found and is_empty_result(text):
            empty += 1
    return entries, files, empty


# --------------------------------------------------------------------------- matching


def match_options(options: Sequence[Any], entries: Sequence[CrossCheckEntry]) -> CrossCheckSummary:
    """Annotate seats.aero options in place: sources, confirmation, crosscheck_note. Returns a summary.

    Options are duck-typed: they need travel_date, cabin, route, source, program, mileage_cost,
    flight_numbers (string), and writable sources/confirmation/crosscheck_note attributes.
    """
    summary = CrossCheckSummary(entries=len(entries))
    by_flight: dict[tuple[str, str, tuple[str, ...]], list[CrossCheckEntry]] = {}
    by_program: dict[tuple[str, str, str, str], list[CrossCheckEntry]] = {}
    for e in entries:
        if e.is_flight_level:
            by_flight.setdefault((e.date, e.cabin, e.flight_numbers), []).append(e)
        by_program.setdefault((e.date, e.route, e.cabin, e.source), []).append(e)

    used: set[int] = set()

    def mark_used(e: CrossCheckEntry) -> None:
        """A matched entry also retires its program-level twins (same seat, reported without flight numbers)."""
        used.add(id(e))
        for twin in by_program.get((e.date, e.route, e.cabin, e.source), []):
            if twin.miles == e.miles:
                used.add(id(twin))

    for o in options:
        o.sources = ["seats.aero"]
        o.confirmation = ""
        o.crosscheck_note = ""
        numbers = flight_key((o.flight_numbers or "").split(","))
        candidates = by_flight.get((o.travel_date, o.cabin, numbers), [])
        if candidates:
            # The same flights can be sold by several programs at different prices, so prefer the entry
            # from this row's own program; a different program still confirms the seat exists, but its
            # price is not comparable and is left for the "FlightPoints also lists" note.
            same_program = [e for e in candidates if e.source == o.source]
            e = same_program[0] if same_program else candidates[0]
            o.sources.append("flightpoints")
            o.confirmation = "flight"
            if same_program:
                mark_used(e)
                if e.miles and o.mileage_cost and e.miles != o.mileage_cost:
                    o.crosscheck_note = f"FlightPoints quotes {e.miles:,} miles"
                    summary.price_disagreements.append(f"{o.travel_date} {o.flight_numbers} {o.cabin} {o.program}: seats.aero {o.mileage_cost:,} vs FlightPoints {e.miles:,}")
            else:
                o.crosscheck_note = f"seen on FlightPoints via {e.program_label or e.source}"
            summary.flight_matches += 1
            continue
        candidates = by_program.get((o.travel_date, o.route, o.cabin, o.source), [])
        if candidates:
            same_price = [e for e in candidates if e.miles == o.mileage_cost]
            if same_price:
                e = same_price[0]
                mark_used(e)
                o.sources.append("flightpoints")
                o.confirmation = "program"
                summary.program_matches += 1
            else:
                quoted = sorted({e.miles for e in candidates if e.miles})
                if quoted:
                    o.crosscheck_note = "FlightPoints quotes " + "/".join(f"{m:,}" for m in quoted) + " miles"
                    summary.price_disagreements.append(f"{o.travel_date} {o.route} {o.cabin} {o.program}: seats.aero {o.mileage_cost:,} vs FlightPoints {'/'.join(f'{m:,}' for m in quoted)}")
                for e in candidates:
                    used.add(id(e))
    # Report what FlightPoints had that seats.aero did not, once per seat, preferring flight-level detail.
    leftovers = [e for e in entries if id(e) not in used and e.cabin in ("business", "first")]
    leftovers.sort(key=lambda e: (e.date, e.route, e.cabin, e.source, e.miles, not e.is_flight_level))
    seen_keys: set[tuple] = set()
    for e in leftovers:
        key = (e.date, e.route, e.cabin, e.source, e.miles)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        label = e.program_label if not e.program_label.isupper() or len(e.program_label) > 3 else e.source
        detail = f" ({', '.join(e.flight_numbers)})" if e.flight_numbers else ""
        summary.unmatched.append(f"{e.date} {e.route} {e.cabin} via {label}: {e.miles:,} miles{detail}")
    return summary


CONFIRMATION_RANK = {"flight": 0, "program": 1, "": 2}
