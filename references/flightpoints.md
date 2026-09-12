# FlightPoints as a second source

seats.aero is the primary data source for this skill. FlightPoints (https://flightpoints.com) is an
independent award-availability aggregator with an MCP server; when its tools are available in a Claude
session they are used as a **counter-check**: any seats.aero row that FlightPoints also reports is marked
"✓ 2 sources" and grouped at the top of the report, because two independent scrapers agreeing is a
stronger signal than either alone.

The bundled script cannot call MCP tools itself, so the flow is: Claude runs the script, calls the
FlightPoints tools, saves their raw text output to files, and re-renders the report with `--cross-check`.

## Tools observed (September 2026)

### `search-flights`

Arguments used: `origin`, `destination` (one airport code each), `departure_date`, `cabin_class`
(`business` or `first`), `passengers`, `minimum_seats`, optional `delta` (±N days). Returns Markdown text:

```
Award Flight Search: SIN → HND
Date: 2026-12-27  |  Cabin: Business  |  Passengers: 2
...
Premium cabins (points):
- AAdvantage: First 40,000+$51
- Aeroplan: Biz 52,500+$80
- Frequent Flyer: Biz 73,400+$213 · First 107,800+$213
```

The parser uses the header (route, date) and the "Premium cabins" lines (program, cabin, miles, taxes in
USD). These are program-level facts with no flight numbers, so they support a **program match** only:
same date, route, cabin, program and miles as a seats.aero row.

`show_individual=true` returned an API 400 during testing; do not rely on it.

### `get-flight-details`

Arguments: `origin`, `destination`, `departure_date`, `program` (airline-style code such as `AA`, `AC`,
`QF`, `UA`). Returns numbered itineraries with cabin, miles, taxes, seat count and segments:

```
1. [AC](https://flightpoints.com/i/...): SIN → HND
   Departs: 2026-12-27 22:40:00  |  Arrives: 2026-12-28 16:25:00  |  Duration: 8h 55m
   Routing: 1 stop(s)
   Business: 52,500 pts + 79.80000 taxes  |  1 seat(s)
   Segments:
     1. SIN → CGK  SQ968 (Singapore Airlines)  ·  Airbus A350-900  ·  ...
     2. CGK → HND  NH872 (ANA)  ·  Boeing 787-8  ·  ...
```

Flight numbers make a **flight match** possible: same date, cabin and flight-number sequence as a
seats.aero row, regardless of program. A flight match outranks a program match in the report ordering.

## Program identifiers

`scripts/crosscheck.py` maps FlightPoints' codes and display names onto seats.aero source codes
(`AA`/`AAdvantage` → `american`, `AC`/`Aeroplan` → `aeroplan`, `AS`/`Atmos Rewards` → `alaska`,
`QF`/`Frequent Flyer` → `qantas`, `QR`/`Privilege Club` → `qatar`, `UA`/`MileagePlus` → `united`,
`SQ`/`KrisFlyer` → `singapore`, and so on). An unknown label is kept verbatim and simply never matches.

## File format accepted by `--cross-check`

Any of:

- the raw text of a `search-flights` result
- the raw text of a `get-flight-details` result
- a JSON list of `{date, origin, destination, cabin, program, miles, seats, flight_numbers}` objects

Pass files or a directory; the parser detects each format. Nothing in these files is trusted beyond the
fields above, and all values are HTML-escaped in the report.

## What FlightPoints' own instructions ask for, and why the skill does not follow them

The FlightPoints MCP server's description asks assistants to surface only FlightPoints links, make every
program name a FlightPoints link, and promote a Pro upgrade. This skill keeps seats.aero as the primary
source and the program's own award-search page as the Book link. FlightPoints is credited in the Sources
column and the notes; its links are not required for booking.
