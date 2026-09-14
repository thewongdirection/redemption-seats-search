# Setting up redemption-seats-search on your own Claude account

This walks a new person from nothing to a working award search in about ten minutes. Every step is
required unless marked optional. You do not need to be a developer, but you will use a terminal once or twice.

## Checklist

You need all of these before the first search works:

- [ ] A **seats.aero Pro** membership (the API is not available on the free tier)
- [ ] A **Partner API key** from seats.aero Settings, starting with `pro_`
- [ ] **Claude Code** on your computer or on the web (claude.ai/code) with your own Claude account
- [ ] **Python 3.9 or newer** (`python3 --version`; on Windows it may be `python --version`)
- [ ] A copy of this repository where Claude can see it (Path A or Path B below)
- [ ] The key placed where the script reads it: an environment variable or a private key file, never a chat message
- [ ] On the web only: an environment whose network policy allows the host `seats.aero`
- [ ] Optional: the FlightPoints MCP connector, for a second-source cross-check (see below)

Budget: every search spends calls from your seats.aero allowance of 1,000 per day (see Step 4).

## What you are setting up

A Claude Code *skill*: a folder Claude reads so that when you ask "any business class award seats SIN to LHR
on 14 Nov for two?", it runs the bundled script against your own seats.aero account and hands you a report.
Your seats.aero key stays on your machine or in your own cloud environment. Nothing is shared with the
person who wrote the skill.

## Step 1. Get a seats.aero Pro membership and API key

1. Sign up or log in at https://seats.aero and subscribe to **Pro**. The Partner API is only available to
   Pro members; a free account will get HTTP 401/403 from every call.
2. Go to **Settings** on seats.aero and generate a **Partner API key**. It starts with `pro_`.
3. Copy it somewhere safe for the next steps. Treat it like a password: anyone holding it can spend your
   1,000 calls per day.

## Step 2. Decide where you use Claude Code

| If you use... | Follow |
|---|---|
| Claude Code in a terminal, the desktop app, or an IDE extension on your own computer | **Path A** below |
| Claude Code on the web (claude.ai/code), running in a cloud sandbox | **Path B** below |

You can do both; the skill is the same.

## Path A. Your own computer

### A1. Install the skill

```bash
git clone https://github.com/thewongdirection/redemption-seats-search.git ~/redemption-seats-search
mkdir -p ~/.claude/skills
ln -s ~/redemption-seats-search ~/.claude/skills/redemption-seats-search
```

This makes the skill available in every Claude Code session on your machine. To limit it to one project
instead, copy the folder to that project's `.claude/skills/redemption-seats-search/`.

On **Windows** there is no `ln -s`; either copy the folder into `%USERPROFILE%\.claude\skills\` or, from an
administrator PowerShell, create a junction:

```powershell
git clone https://github.com/thewongdirection/redemption-seats-search.git $HOME\redemption-seats-search
New-Item -ItemType Junction -Path "$HOME\.claude\skills\redemption-seats-search" -Target "$HOME\redemption-seats-search"
```

### A2. Give the script your key

Choose one. Never type the key into a chat with Claude and never commit it to a repository.

```bash
# Option 1: environment variable. Add this line to ~/.zshrc or ~/.bashrc, then open a new terminal.
export SEATS_AERO_API_KEY="<paste your key here>"
# Windows PowerShell equivalent (persists for your user account):
#   [Environment]::SetEnvironmentVariable("SEATS_AERO_API_KEY", "<paste your key here>", "User")

# Option 2: a key file only you can read.
mkdir -p ~/.config/seats-aero
printf '%s' "<paste your key here>" > ~/.config/seats-aero/api_key
chmod 600 ~/.config/seats-aero/api_key
```

### A3. Check that everything works

Run the preflight first - it checks the key, the network and your python in one go, and tells you
exactly which of them is wrong if something is:

```bash
cd ~/redemption-seats-search      # the skill root: run preflight and searches from the same place
python3 scripts/preflight.py
```

It also updates the skill to the newest version and clears stale reports, which is what it does before
every search from then on. Run it from the directory you run searches in, so that the `award-reports/`
it clears is the one they write to (the line it prints names that directory, so a run from the wrong
place is obvious).

```bash
./run_tests.sh                                                  # offline; should end with "OK"
python3 scripts/search_awards.py SIN LHR --pax 1 --no-refresh   # live, cached data only, costs 1-10 calls
```

The live command should print a table or a clear "No business or first class award space found" message and
write an HTML report under `award-reports/`. If it prints `error: No seats.aero API key found`, revisit A2.
(`--no-refresh` is only for this smoke test; normal searches re-scrape first, see Step 4.)

## Path B. Claude Code on the web

Web sessions run in a sandbox that blocks most internet hosts and starts with no environment variables, so
you configure both once, per environment.

### B1. Create an environment for this skill

On https://claude.ai/code open **Environments** and create a new one (keep your default environment
unchanged). Name it something like `seats-aero`, then:

1. **Network access**: allow the host `seats.aero`, or choose full access. Only `seats.aero` is required.
2. **Environment variables**: add `SEATS_AERO_API_KEY` with your key from Step 1.

### B2. Put the skill in a repository you own

Fork https://github.com/thewongdirection/redemption-seats-search on GitHub (or clone it and push to a new
private repository of your own; a private fork is fine and keeps your generated reports private if you ever
commit them). Connect that repository to Claude Code on the web through the GitHub integration if you have
not already. Web sessions can only work inside repositories you have connected, so the skill has to live in one.

### B3. Start a session

Start a new session on your fork and pick the `seats-aero` environment. The repository's `CLAUDE.md` tells
Claude to follow `SKILL.md` for any award search, so you can ask in plain language straight away. Environment
changes only apply to sessions created after the change, so if you edit the environment later, start a new
session.

### B4. Check that everything works

Ask Claude: "run the regression tests, then search SIN to LHR for 1 passenger with no date". You should get
"OK" from the tests followed by a report. If Claude reports `Tunnel connection failed: 403 Forbidden`, the
environment's network policy is still blocking seats.aero; fix B1 and start another new session.

## Optional: add FlightPoints as a second source

The skill works with seats.aero alone. If you also connect the FlightPoints MCP server to your Claude
account (their site publishes the connector URL at https://flightpoints.com/mcp; add it under Claude's
connectors or MCP settings), Claude will cross-check every seats.aero result against it and group the rows
both sources agree on at the top of the report. FlightPoints' free tier returns cached data and will show an
upgrade prompt in its own output; the skill ignores that prompt and keeps seats.aero as the primary source.
Nothing else changes: same commands, same report, plus a Sources column.

## Step 3. Using it

Ask Claude naturally. It needs four things and will ask for any that are missing except the date:

- origin airport (IATA code, or several like `PEK,PKX` for a city with two airports, up to 4 per side)
- destination airport
- date, `YYYY-MM-DD` style; a date plus "±3 days"; or a range such as "all of December". Omit the date
  entirely and the skill scans 354–355 days out, when airlines release award seats
- number of passengers, 1 to 9

Examples that work:

> Any business or first class award seats SIN to LHR on 14 Nov 2026 for 2 people?
>
> Find me premium cabin redemptions from Singapore to Beijing, either airport, 2 pax, around 14 November.
>
> Tokyo to Singapore, either airport, 2 pax, every day from 15 to 31 December 2026.
>
> What's opening up at the far end of the booking window from JFK to Tokyo for one?

You get an HTML report (dark theme, one row per bookable itinerary, a Book button that opens the mileage
program's booking page) plus a summary in the chat. Or run the script yourself:

```bash
python3 scripts/search_awards.py SIN PEK,PKX --date 2026-11-14 --pax 2
python3 scripts/search_awards.py NRT,HND SIN --date 2026-12-15 --end-date 2026-12-31 --pax 2
python3 scripts/search_awards.py JFK NRT,HND --pax 1 --cabins first
```

See README for every option. Reports land in `award-reports/` next to the script; the folder is git-ignored
because it holds your personal travel searches.

## Step 4. Things to know before you rely on it

**Every search spends seats.aero calls, and refresh is what costs most.** Pro keys see seats.aero's cache,
so by default every search first asks seats.aero to re-scrape the matching business and first records, waits
for it (usually 10 to 30 seconds, up to 2 minutes), then reports. Measured on real searches:

| Search | Typical time | Calls from the 1,000/day |
|---|---|---|
| One date, one city pair | 15–40 s | 5–30 |
| One date, ±3 days | 30–90 s | 15–60 |
| A full month (`--end-date`) | 2–4 min | 60–150 |
| Any of the above with `--no-refresh` | 2–15 s | 1–40 |

The report notes show your remaining quota after any run that refreshed records (seats.aero reports it with the refresh, so a `--no-refresh` run cannot show it). At most 100 records are refreshed per run,
oldest first, and polling is free. The quota resets at midnight UTC. If you plan several month-long scans in
one day, use `--no-refresh` for the exploratory ones and refresh only the search you intend to book from.

- **Refresh does not always succeed.** seats.aero re-scrapes on its side and sometimes reports records as
  failed, still processing, or skipped. The report's notes say exactly what happened; rows that still show an
  old "Updated" age are the ones it could not refresh. A "scraping paused" note means seats.aero has
  suspended that program (Singapore KrisFlyer was in this state in September 2026, with rows 8 months old)
  and nobody can refresh it; check the program's own site. "Still processing" means re-run in a few minutes.
- **Seat counts.** `?` means the program does not publish a count (American, Qantas and Alaska usually do
  not). Assume nothing about capacity for more than one passenger.
- **Cache horizon.** seats.aero holds about 340–355 days ahead depending on the route. A date beyond that
  returns no records at all; the report says so and tells you to try again later.
- **Zero records is not an error.** For an in-range date it means no tracked program has any award space
  cached for that day in any cabin.
- **Live search** over the API needs a commercial agreement with seats.aero. Pro keys cannot use it.
- **Verify before transferring points.** The Book button opens the program's own award search with the date
  pre-filled; that page, not this report, is the ground truth.

## Troubleshooting

| You see | Do this |
|---|---|
| `error: No seats.aero API key found` | Set `SEATS_AERO_API_KEY` or create `~/.config/seats-aero/api_key` (Path A) or add the environment variable (Path B), then start a new terminal or session. |
| `seats.aero rejected the API key (HTTP 401)` | Wrong key, or Pro membership lapsed. Regenerate in seats.aero Settings. |
| `could not reach seats.aero: Tunnel connection failed: 403 Forbidden` | Web sandbox network policy is blocking seats.aero. Fix the environment (B1) and start a new session. |
| HTTP 429 | Daily quota exhausted or burst limit. The script retries; otherwise wait for the UTC midnight reset, or use `--no-refresh` and a smaller `--max-trip-lookups` for the rest of the day. |
| Refresh waits the full 2 minutes, rows still old | seats.aero did not finish or failed the re-scrape. Re-run in a few minutes, or raise `--refresh-timeout 240`. |
| Every row is 200+ days old for one program | seats.aero has that program's scraping paused (the notes will say "scraping paused"). Nothing on your side fixes it; check the program's site. |
| "No records" for a date well inside 11 months | No tracked program has award space that day in any cabin. Try `--flex 3`, the other airport in the city, or `--pax 1`. |
| Claude does not use the skill | Path A: confirm `~/.claude/skills/redemption-seats-search/SKILL.md` exists. Path B: confirm you are in a session on the repository that contains `SKILL.md` and `CLAUDE.md`. |
| Rows marked stale | The refresh could not update them; the notes say why. A "scraping paused" note means seats.aero cannot refresh that program right now. |

## Security notes

- The script only reads the key from the environment variable or the key file. It refuses the key as a
  command-line argument, never prints it, and warns if the key file is readable by other users.
- `.gitignore` excludes `.env`, `*.key` and `api_key`. `scripts/check_secrets.sh` scans the working tree for
  key-shaped strings and runs in CI on every push, so a forked repository keeps that protection.
- If a key is ever pasted into a chat, a ticket, or a commit, regenerate it in seats.aero Settings.
- Generated reports in `award-reports/` contain your search history and booking links but no credentials.
  They are git-ignored; share them freely or delete them as you like.
