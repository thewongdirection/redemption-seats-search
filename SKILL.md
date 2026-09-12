---
name: redemption-seats-search
description: Find business class and first class award / redemption seats between two airports on a given date for a given number of passengers, using the seats.aero Partner API, and report every option with the airline, flight numbers, times, seats left, mileage cost, taxes and a booking link. Use this whenever the user asks about award availability, redemption seats, points/miles flights, "can I book X to Y in business with points", saver space, premium-cabin award search, or anything involving seats.aero, even if they don't say "award" explicitly. Do not use it for cash fares or economy searches.
compatibility: Requires python3 (standard library only), outbound HTTPS to seats.aero, and a seats.aero Partner API key (seats.aero Pro membership) in SEATS_AERO_API_KEY or ~/.config/seats-aero/api_key.
---

# Redemption seats search (business & first only)

Answer one question well: **for this route, date and party size, which premium-cabin award seats exist right now, on which airline, through which mileage program, and for how many miles plus taxes?**

The bundled script does the API work. Your job is to collect four inputs, run it, and present the
result so the user can act on it.

## 1. Collect the inputs

| Input | Required | Notes |
|---|---|---|
| Origin airport | yes | 3-letter IATA code. For a city with several airports pass them together, e.g. `PEK,PKX` for Beijing, `LHR,LGW` for London, `JFK,EWR` for New York (up to 4 per side). |
| Destination airport | yes | Same rules. |
| Date flying | yes | `YYYY-MM-DD`. If the user gives "14 Nov", assume the next occurrence and confirm the year in your reply. |
| Passengers | yes | 1–9. Default to 1 only if the user clearly means themselves alone. |
| Flexibility | optional | `--flex N` searches ±N days. Offer it when a date returns nothing. |
| Cabin | optional | Default is business **and** first. Use `--cabins first` when the user only wants first. Economy and premium economy are out of scope; the script refuses them. |
| Nonstop only | optional | `--direct-only`. |

If origin, destination, date or party size is missing, ask for it in one short message rather than guessing.

Prerequisites the user must already have (see README, "What you need to provide"): a seats.aero Pro membership,
a Partner API key in `SEATS_AERO_API_KEY` or `~/.config/seats-aero/api_key`, python3, and network access to
`seats.aero`. If the script exits with code 2 or 3, point the user at that README section rather than improvising.

## 2. Run the search

```bash
python3 scripts/search_awards.py SIN LHR --date 2026-11-14 --pax 2
```

Useful variants:

```bash
python3 scripts/search_awards.py JFK NRT --date 2027-03-02 --pax 1 --cabins first
python3 scripts/search_awards.py SIN PEK,PKX --date 2027-09-02 --pax 2          # both Beijing airports
python3 scripts/search_awards.py LAX SYD --date 2026-12-20 --pax 2 --flex 3 --direct-only
python3 scripts/search_awards.py SFO CDG --date 2026-10-05 --pax 3 --json   # machine-readable
```

Every run produces two things:

- **An HTML report** (dark theme, self-contained, no external assets) written to
  `award-reports/awards_<ORIGIN>-<DEST>_<date>_pax<N>.html` unless `--html PATH` or `--no-html` is given.
  Its **Book** column opens the mileage program's booking page for that itinerary (a deep link when seats.aero
  supplies one, otherwise the program's award-search page), and airline names link to the operating carrier.
  This is the deliverable the user reads.
- **A markdown summary** on stdout for you to read, ending with the report path. Progress goes to stderr.

Exit codes: 0 success (including "no availability"), 2 bad input or missing key, 3 API/auth/network failure.
Read stderr when it is non-zero; the message says what to fix.

What it does under the hood, so you can explain results:

1. Calls the seats.aero **Cached Search** endpoint for the route and date window.
2. Keeps only availability objects that report business (J) or first (F) space.
3. For each of those, calls **Get Trips** to obtain flight numbers, airline, times, seat count, taxes and a booking link.
4. Drops itineraries that report fewer seats than requested and itineraries flagged as dynamically priced.
5. Sorts by miles, then taxes.

Details of the API, response fields and program codes are in `references/seats-aero-api.md`. Read it only if you
need to debug an unexpected response or extend the script.

## 3. Present the answer

Hand over the HTML report first: if a file-delivery tool such as `SendUserFile` is available, send the report with
it (display `render`); otherwise give the absolute path and suggest opening it in a browser. Then, in chat, lead
with the best value option in one sentence and include the script's markdown table as-is so the answer is
readable without opening the file. Do not rewrite the numbers. Add anything the user needs in order to act:

- **Where to book.** The "Book" link goes to the mileage program that holds the space (e.g. Air Canada
  Aeroplan), which is often not the airline flying; the user books there, not on the operating airline's site. Say which transferable points (Amex, Chase, Citi,
  Capital One, Bilt) feed that program if you know, but label it as general knowledge, not something the API returned.
- **Seat counts.** `?` means the program does not publish a count. Say so plainly and suggest the user verify on
  the program's site before transferring points.
- **Freshness.** The "Updated" column is the cache age. Anything older than a day should be re-verified; the
  script also prints a note when this applies. seats.aero Pro keys only see cached data, not live searches.
- **Taxes** are per passenger in the program's billing currency. Multiply by party size if the user asks for a total.
- **Nothing found.** Say so in one line, then offer the concrete next steps: `--flex 3`, alternate airports, or
  checking whether space exists for fewer passengers (`--pax 1`) so they know if it is a party-size problem.

Keep the reply short. The table carries the detail; the prose carries the recommendation.

## 4. Things that go wrong

| Symptom | Cause and fix |
|---|---|
| `error: No seats.aero API key found` | Set `SEATS_AERO_API_KEY` or save the key to `~/.config/seats-aero/api_key` with `chmod 600`. Never paste the key on the command line or into a file that is committed. |
| HTTP 401/403 | Key wrong or seats.aero Pro membership lapsed. |
| HTTP 429 | Daily quota (about 1,000 calls per Pro key) or burst limit. The script already retries with backoff; if it still fails, reduce `--flex` or `--max-trip-lookups`. |
| Results look stale or thin | Cached Search only reflects what seats.aero last scraped. Suggest the user open the booking link and confirm. |
| User wants live, real-time search | The `/live` endpoint needs a commercial agreement with seats.aero; Pro keys cannot use it. Explain this rather than attempting it. |

## Alternative: a seats.aero MCP server

If the user already has a seats.aero MCP server configured (tools such as `get_flights` and `get_trips`),
you may use it instead of the script. Apply the same rules: filter to `JAvailable`/`FAvailable`, fetch trips,
drop itineraries with fewer seats than the party size, and ignore economy/premium. See
`references/mcp-alternative.md` for a known server and its configuration. Prefer the script when both are
available; it is tested and its output format is stable.
