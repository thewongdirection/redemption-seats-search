# Setting up redemption-seats-search on your own Claude account

This walks a new person from nothing to a working award search in about ten minutes. Every step is
required unless marked optional. You do not need to be a developer, but you will use a terminal once or twice.

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

### A2. Give the script your key

Choose one. Never type the key into a chat with Claude and never commit it to a repository.

```bash
# Option 1: environment variable. Add this line to ~/.zshrc or ~/.bashrc, then open a new terminal.
export SEATS_AERO_API_KEY="<paste your key here>"

# Option 2: a key file only you can read.
mkdir -p ~/.config/seats-aero
printf '%s' "<paste your key here>" > ~/.config/seats-aero/api_key
chmod 600 ~/.config/seats-aero/api_key
```

### A3. Check that everything works

```bash
cd ~/redemption-seats-search
./run_tests.sh                                     # offline; should end with "OK"
python3 scripts/search_awards.py SIN LHR --pax 1   # live; scans 354-355 days out
```

The live command should print a table or a clear "No business or first class award space found" message and
write an HTML report under `award-reports/`. If it prints `error: No seats.aero API key found`, revisit A2.

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
private repository of your own). Connect that repository to Claude Code on the web through the GitHub
integration if you have not already.

### B3. Start a session

Start a new session on your fork and pick the `seats-aero` environment. The repository's `CLAUDE.md` tells
Claude to follow `SKILL.md` for any award search, so you can ask in plain language straight away. Environment
changes only apply to sessions created after the change, so if you edit the environment later, start a new
session.

### B4. Check that everything works

Ask Claude: "run the regression tests, then search SIN to LHR for 1 passenger with no date". You should get
"OK" from the tests followed by a report.

## Step 3. Using it

Ask Claude naturally. It needs four things and will ask for any that are missing except the date:

- origin airport (IATA code, or several like `PEK,PKX` for a city with two airports)
- destination airport
- date, `YYYY-MM-DD` style (omit it and the skill scans 354–355 days out, when airlines release award seats)
- number of passengers

Examples that work:

> Any business or first class award seats SIN to LHR on 14 Nov 2026 for 2 people?
>
> Find me premium cabin redemptions from Singapore to Beijing, either airport, 2 pax, around 14 November, and refresh anything stale.
>
> What's opening up at the far end of the booking window from JFK to Tokyo for one?

You get an HTML report (dark theme, one row per bookable itinerary, a Book button that opens the mileage
program's booking page) plus a summary in the chat. Or run the script yourself:

```bash
python3 scripts/search_awards.py SIN PEK,PKX --date 2026-11-14 --pax 2
python3 scripts/search_awards.py JFK NRT,HND --pax 1 --cabins first
```

See README for every option.

## Step 4. Things to know before you rely on it

- **Fresh by default.** Pro keys see seats.aero's cache, so every search first asks seats.aero to re-scrape
  the matching records (10 to 30 seconds) and then reports. Each refreshed record costs one call from your
  1,000 per day; a typical search spends 5 to 30. Add `--no-refresh` for a quota-free look at the cache as-is.
- **Some programs cannot be refreshed** while seats.aero has them paused; the report says so when it happens.
  Check that program's own site.
- **Seat counts.** `?` means the program does not publish a count. Assume nothing about capacity.
- **Cache horizon.** seats.aero holds roughly 11 months ahead. A date beyond that returns no records at all.
- **Live search** over the API needs a commercial agreement with seats.aero. Pro keys cannot use it.

## Troubleshooting

| You see | Do this |
|---|---|
| `error: No seats.aero API key found` | Set `SEATS_AERO_API_KEY` or create `~/.config/seats-aero/api_key` (Path A) or add the environment variable (Path B), then start a new terminal or session. |
| `seats.aero rejected the API key (HTTP 401)` | Wrong key, or Pro membership lapsed. Regenerate in seats.aero Settings. |
| `could not reach seats.aero: Tunnel connection failed: 403 Forbidden` | Web sandbox network policy is blocking seats.aero. Fix the environment (B1) and start a new session. |
| HTTP 429 | Daily quota exhausted or burst limit. The script retries; otherwise wait for the UTC midnight reset or reduce `--flex` and `--max-trip-lookups`. |
| Claude does not use the skill | Path A: confirm `~/.claude/skills/redemption-seats-search/SKILL.md` exists. Path B: confirm you are in a session on the repository that contains `SKILL.md` and `CLAUDE.md`. |
| Rows marked stale | The refresh could not update them; the notes say why. A "scraping paused" note means seats.aero cannot refresh that program right now. |

## Security notes

- The script only reads the key from the environment variable or the key file. It refuses the key as a
  command-line argument, never prints it, and warns if the key file is readable by other users.
- `.gitignore` excludes `.env`, `*.key` and `api_key`. `scripts/check_secrets.sh` scans the working tree for
  key-shaped strings and runs in CI on every push, so a forked repository keeps that protection.
- If a key is ever pasted into a chat, a ticket, or a commit, regenerate it in seats.aero Settings.
