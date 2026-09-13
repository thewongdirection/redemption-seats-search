# redemption-seats-search

A [Claude Code skill](https://docs.claude.com/en/docs/claude-code/skills) that finds **business and first class
award seats** between two airports on a date, for a given number of passengers, using the
[seats.aero](https://seats.aero) Partner API. It reports every option with the mileage program, airline, flight
numbers, times, seats remaining, miles, taxes and a booking link.

Every search writes a clean, dark-themed **HTML report** whose *Book* column takes you straight to the mileage
program's booking page for that itinerary, and prints a markdown summary in the terminal.

Economy and premium economy are deliberately out of scope.

**New here?** [SETUP.md](SETUP.md) walks you step by step from a seats.aero account to a working search on your
own Claude account, for both Claude Code on your computer and Claude Code on the web.

## What you need to provide

### One-time setup

| You provide | Why | Where to get it |
|---|---|---|
| A **seats.aero Pro** membership | The Partner API is only issued to Pro members | https://seats.aero/pro |
| A **Partner API key** | Authenticates every request | seats.aero → Settings → API. Pro keys allow 1,000 calls a day; a single-date search spends 5–30, a month-long range 60–150, `--no-refresh` runs 1–40 |
| `python3` 3.9 or newer | Runs the script; no packages to install | Already present on macOS and most Linux; https://python.org for Windows (where the command may be `python`) |
| Outbound HTTPS to `seats.aero` | The script calls `https://seats.aero/partnerapi` | Usually nothing to do. On Claude Code on the web, see below |

### For every search

| Input | Format | Example | Required |
|---|---|---|---|
| Origin airport(s) | 3-letter IATA code, comma-separated for several | `SIN` or `LHR,LGW` | yes |
| Destination airport(s) | same | `PEK,PKX` | yes |
| Travel date | `YYYY-MM-DD`, today or later | `2026-11-14` | no. Omit it to scan 354–355 days out, the window where airlines first release award seats |
| Passengers | 1–9 | `--pax 2` | no, defaults to 1 |
| Date flexibility | 0–7 days either side | `--flex 3` | no, defaults to exact date |
| Date range | last day of a span up to 62 days | `--end-date 2026-12-31` | no; use instead of `--flex` for a whole month |
| Cabins | `business`, `first`, or both | `--cabins first` | no, defaults to both |
| Nonstop only | flag | `--direct-only` | no |
| Programs | seats.aero program codes | `--sources aeroplan,united` | no, defaults to all |
| Skip the refresh | flag; refresh is on by default (`--refresh-older-than HOURS`, `--refresh-timeout SECONDS` tune it) | `--no-refresh` | no |
| Cross-check files | FlightPoints tool output files or a directory | `--cross-check award-reports/crosscheck/` | no |
| Re-render a saved run | path to a previous `--json` output | `--load award-reports/run.json` | no; replaces the airports and date |

seats.aero caches about 340–355 days ahead depending on the route. A date beyond that returns no records at
all rather than an error; the report says so, and you re-run once the date falls inside the window.

New users: [SETUP.md](SETUP.md) has the full checklist, the Windows equivalents, the per-search quota table
and a troubleshooting section keyed to the exact messages the script prints.

### Fresh data on every search

Every search re-scrapes before it reports. After the first cached lookup, the script sends every matching
business or first record to seats.aero's Pro-only refresh endpoint (oldest first, at most 100 per run), polls
until they complete (usually 10 to 30 seconds), then searches again and reports the fresh figures. The
"Updated" column should therefore read minutes ago; a row that still shows an old age is one seats.aero could
not refresh, and the notes say why. Each refreshed record spends one call of the shared 1,000 per day quota;
polling is free; a typical search spends 5 to 30 calls in total. Pass `--no-refresh` for a quota-free look at
the cache as-is, or `--refresh-older-than 24` to re-scrape only day-old rows. Records for a program whose
scraping seats.aero has paused come back as "skipped" and cannot be refreshed by anyone until seats.aero
restores that program.

### Claude Code on the web

Web sessions run in a sandbox whose network policy blocks most hosts by default. In the environment settings
on claude.ai/code:

1. Under **Network access**, allow `seats.aero` (or choose full access).
2. Under **Environment variables**, add `SEATS_AERO_API_KEY` with your key so it never has to be typed in chat.
3. Start a new session; changes apply to sessions created after the edit.

## Install the skill

```bash
git clone https://github.com/thewongdirection/redemption-seats-search.git
mkdir -p ~/.claude/skills
ln -s "$(pwd)/redemption-seats-search" ~/.claude/skills/redemption-seats-search
```

Or copy the folder into a project's `.claude/skills/` to scope it to that project.

## Give it your API key (never commit it)

Pick one:

```bash
# Option A: environment variable (add to ~/.zshrc or ~/.bashrc)
export SEATS_AERO_API_KEY="…"

# Option B: key file, readable only by you
mkdir -p ~/.config/seats-aero
printf '%s' "…" > ~/.config/seats-aero/api_key
chmod 600 ~/.config/seats-aero/api_key
```

The script refuses to take the key as a command-line argument, never prints it, and warns if the key file is
readable by other users. `.gitignore` excludes `.env`, `*.key` and `api_key` files, and
`scripts/check_secrets.sh` scans tracked files for key-shaped strings (it runs in CI too).

## Use it

In Claude Code, just ask:

> Any business or first class award seats SIN to LHR on 14 Nov for 2 people?

Or run the script directly:

```bash
python3 scripts/search_awards.py SIN LHR --date 2026-11-14 --pax 2
python3 scripts/search_awards.py SIN PEK,PKX --date 2027-09-02 --pax 2     # multiple airports per side
python3 scripts/search_awards.py JFK NRT --date 2027-03-02 --cabins first --flex 3
python3 scripts/search_awards.py LAX SYD --date 2026-12-20 --pax 2 --direct-only --json
python3 scripts/search_awards.py SIN NRT,HND --date 2026-12-01 --end-date 2026-12-31 --pax 2  # whole month
python3 scripts/search_awards.py SIN NGO --pax 2                              # no date: 354-355 days out
python3 scripts/search_awards.py SIN PEK,PKX --date 2026-11-14 --pax 2 --no-refresh   # cached data only, saves quota
```

Each run writes `award-reports/awards_SIN-LHR_2026-11-14_pax2.html` (override with `--html PATH`, skip with
`--no-html`) and prints a summary like this:

```
## Premium-cabin award seats SIN → LHR
Date: 2026-11-14 · Passengers: 2 · Cabins: Business, First

| # | Program | Cabin | Airline | Flights | Dep → Arr | Duration | Stops | Seats | Miles / pax | Taxes / pax | Updated | Book |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Air Canada Aeroplan | Business | Singapore Airlines (SQ) | SQ308 | 09:00 → 15:30 | 14h 30m | nonstop | 2 | 87,500 | 147.50 CAD | 3h ago | [book](…) |
| 2 | Qantas Frequent Flyer | First | Qantas (QF) | QF1 | 23:30 → 06:15 (+1) | 14h 45m | nonstop | ? | 162,800 | 520.00 AUD | 2d ago | [book](…) |
```

## Data sources

**seats.aero is the primary source.** Every figure in the report comes from the seats.aero Partner API and,
by default, a fresh re-scrape it performs before the report is built.

**FlightPoints is an optional counter-check.** When the FlightPoints MCP server is connected to the Claude
session, Claude also queries it for the same route, dates, cabins and party size, saves the tool output to
files, and re-renders the report with `--cross-check`. Rows that both sources report are marked
"✓ 2 sources" in a Sources column and grouped at the top, on the principle that two independent scrapers
agreeing is more reliable than either alone. Two match strengths exist: an exact flight-number match (from
FlightPoints' `get-flight-details`) and a program-plus-price match (from its `search-flights` summary). Price
disagreements and FlightPoints-only options are listed in the notes rather than mixed into the table. Booking
links stay with the mileage program's own site; the skill strictly ignores the FlightPoints tool's embedded
requests to use its links or promote its paid tier. No FlightPoints account is required for the skill to work;
without it the report simply says "seats.aero" as its only source. Details in
`references/flightpoints.md`.

```bash
python3 scripts/search_awards.py SIN HND --date 2026-12-27 --pax 2 --json > award-reports/run.json
#   ...Claude saves FlightPoints tool output under award-reports/crosscheck/...
python3 scripts/search_awards.py --load award-reports/run.json --cross-check award-reports/crosscheck/
```

`--load` re-renders a previous `--json` run without calling seats.aero, so the cross-check costs no quota.
Cross-check files are filtered to the searched airports and date window, so a shared directory of saved
FlightPoints output cannot confirm a row from some other search.

## Sorting the dashboard

Every column except Book sorts: click a heading, click again to reverse. Numbers sort numerically, text
alphabetically, and unknown values (`?` seats, missing durations) always sort last rather than first. The
default order is cheapest first, or rows confirmed by both sources first when a cross-check ran. Appending
`#sort=<column index>:asc` or `:desc` to the file URL opens the report already sorted, which is useful for
sharing a particular view. The sorter is a few lines of inline JavaScript; the table is complete and readable
without it.

## How it works

1. `GET /partnerapi/search` for the route and date window (cached availability across ~26 programs).
2. Keep records where `JAvailable` or `FAvailable` is true.
3. `GET /partnerapi/trips/{id}` for each to get flight-level detail and booking links.
4. `POST /partnerapi/refresh` for those records, wait for seats.aero to re-scrape them, and search again (`--no-refresh` skips this).
5. Drop itineraries with fewer seats than requested, and dynamically-priced (`Filtered`) ones.
6. Sort by miles, then taxes, write the HTML report, and print a markdown table (or JSON with `--json`).

The HTML report is a single self-contained file: no external fonts, styles or scripts; the only JavaScript is the inline column sorter, every value
HTML-escaped and only `https://` links emitted. Booking deep links come from seats.aero when available; otherwise
the *Book* button falls back to the program's award-search page.

See `references/seats-aero-api.md` for the API notes and `references/mcp-alternative.md` if you would rather
drive a seats.aero MCP server.

## Limits worth knowing

- Pro keys see **cached** data, so the script re-scrapes matches before every report. Rows that still show an
  old age could not be refreshed; verify those on the program's site before transferring points.
- Some programs do not publish seat counts; those rows show `?` and are kept rather than dropped.
- Live, real-time search (`/live`) requires a commercial agreement with seats.aero.

## Development

```bash
./run_tests.sh              # 120 offline unit tests, network mocked
./scripts/check_secrets.sh  # credential scan
```

### Batch testing a matrix of routes and months

`tests/batch_matrix.py` drives the real command line against a simulated Partner API
(`tests/fake_seats_aero.py`) for a random draw of routes across consecutive months, and checks the
invariants a user relies on: the cabin, date-window and party-size filters hold, rows are ordered by
price, the HTML report is self-contained and escapes hostile text, the API key never reaches any
output, and the markdown re-render agrees with the JSON. It also runs the option variants
(`--direct-only`, `--cabins first`, `--sources`, `--no-refresh`, `--flex`, `--max-trip-lookups 0`),
the input checks and a cross-check against FlightPoints output captured live.

```bash
python3 tests/batch_matrix.py --routes 10 --months 10 --pax 2   # 100 searches, no key, no network
```

Exit code 0 means every scenario passed; failures are listed in the JSON it prints. `run_tests.sh`
runs a 2x2 slice of the same harness so it stays working.

## Layout

```
SKILL.md                     skill instructions Claude reads
scripts/crosscheck.py        FlightPoints output parsing and matching
references/flightpoints.md   observed FlightPoints tool formats and match rules
SETUP.md                     step-by-step onboarding for a new user
CLAUDE.md                    points Claude at SKILL.md in web sessions on this repo
scripts/search_awards.py     the search tool (seats.aero)
scripts/check_secrets.sh     credential scanner
references/                  API notes and MCP alternative
tests/                       regression suite, API fixtures, batch matrix harness
award-reports/               generated HTML reports (git-ignored)
.github/workflows/ci.yml     runs the scan and tests on every push
```

Not affiliated with seats.aero.
