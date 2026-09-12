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
| A **Partner API key** | Authenticates every request | seats.aero → Settings → API. Pro keys allow about 1,000 calls a day; one search uses 3–30 depending on how many programs have space |
| `python3` 3.9 or newer | Runs the script; no packages to install | Already present on macOS and most Linux; https://python.org for Windows |
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

seats.aero caches roughly 11 months ahead. A date beyond that returns no records at all rather than an error;
re-run once the date falls inside the window.

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

## How it works

1. `GET /partnerapi/search` for the route and date window (cached availability across ~26 programs).
2. Keep records where `JAvailable` or `FAvailable` is true.
3. `GET /partnerapi/trips/{id}` for each to get flight-level detail and booking links.
4. `POST /partnerapi/refresh` for those records, wait for seats.aero to re-scrape them, and search again (`--no-refresh` skips this).
5. Drop itineraries with fewer seats than requested, and dynamically-priced (`Filtered`) ones.
6. Sort by miles, then taxes, write the HTML report, and print a markdown table (or JSON with `--json`).

The HTML report is a single self-contained file: no JavaScript, no external fonts or scripts, every value
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
./run_tests.sh              # 81 offline unit tests, network mocked
./scripts/check_secrets.sh  # credential scan
```

## Layout

```
SKILL.md                     skill instructions Claude reads
SETUP.md                     step-by-step onboarding for a new user
CLAUDE.md                    points Claude at SKILL.md in web sessions on this repo
scripts/search_awards.py     the search tool
scripts/check_secrets.sh     credential scanner
references/                  API notes and MCP alternative
tests/                       regression suite and API fixtures
award-reports/               generated HTML reports (git-ignored)
.github/workflows/ci.yml     runs the scan and tests on every push
```

Not affiliated with seats.aero.
