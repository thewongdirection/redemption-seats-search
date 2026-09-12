# redemption-seats-search

A [Claude Code skill](https://docs.claude.com/en/docs/claude-code/skills) that finds **business and first class
award seats** between two airports on a date, for a given number of passengers, using the
[seats.aero](https://seats.aero) Partner API. It reports every option with the mileage program, airline, flight
numbers, times, seats remaining, miles, taxes and a booking link.

Every search writes a clean, dark-themed **HTML report** whose *Book* column takes you straight to the mileage
program's booking page for that itinerary, and prints a markdown summary in the terminal.

Economy and premium economy are deliberately out of scope.

## What you need

- A **seats.aero Pro** membership and a Partner API key (seats.aero → Settings). Pro keys get about 1,000
  API calls a day; a typical search uses 3–15.
- `python3` 3.9 or newer. The script uses only the standard library.

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
```

Each run writes `award-reports/awards_SIN-LHR_2026-11-14_pax2.html` (override with `--html PATH`, skip with
`--no-html`) and prints a summary like this:

```
## Premium-cabin award seats SIN → LHR
Date: 2026-11-14 · Passengers: 2 · Cabins: Business, First

| # | Program | Cabin | Airline | Flights | Date | Route | Dep → Arr | Duration | Stops | Seats | Miles / pax | Taxes / pax | Updated | Book |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Air Canada Aeroplan | Business | Singapore Airlines (SQ) | SQ308 | 2026-11-14 | SIN-LHR | 09:00 → 15:30 | 14h 30m | nonstop | 2 | 87,500 | 147.50 CAD | 3h ago | [book](…) |
| 2 | Qantas Frequent Flyer | First | Qantas (QF) | QF1 | 2026-11-14 | SIN-LHR | 23:30 → 06:15 (+1) | 14h 45m | nonstop | ? | 162,800 | 520.00 AUD | 2d ago | [book](…) |
```

## How it works

1. `GET /partnerapi/search` for the route and date window (cached availability across ~26 programs).
2. Keep records where `JAvailable` or `FAvailable` is true.
3. `GET /partnerapi/trips/{id}` for each to get flight-level detail and booking links.
4. Drop itineraries with fewer seats than requested, and dynamically-priced (`Filtered`) ones.
5. Sort by miles, then taxes, write the HTML report, and print a markdown table (or JSON with `--json`).

The HTML report is a single self-contained file: no JavaScript, no external fonts or scripts, every value
HTML-escaped and only `https://` links emitted. Booking deep links come from seats.aero when available; otherwise
the *Book* button falls back to the program's award-search page.

See `references/seats-aero-api.md` for the API notes and `references/mcp-alternative.md` if you would rather
drive a seats.aero MCP server.

## Limits worth knowing

- Pro keys see **cached** data only. The script shows cache age and flags anything over 24h old. Verify on the
  program's site before transferring points.
- Some programs do not publish seat counts; those rows show `?` and are kept rather than dropped.
- Live, real-time search (`/live`) requires a commercial agreement with seats.aero.

## Development

```bash
./run_tests.sh              # 53 offline unit tests, network mocked
./scripts/check_secrets.sh  # credential scan
```

## Layout

```
SKILL.md                     skill instructions Claude reads
scripts/search_awards.py     the search tool
scripts/check_secrets.sh     credential scanner
references/                  API notes and MCP alternative
tests/                       regression suite and API fixtures
award-reports/               generated HTML reports (git-ignored)
.github/workflows/ci.yml     runs the scan and tests on every push
```

Not affiliated with seats.aero.
