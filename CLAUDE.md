# redemption-seats-search

This repository is a Claude Code skill for finding business and first class award seats via seats.aero.

- For any request about award availability, redemption seats, points or miles flights, or seats.aero,
  follow `SKILL.md` in this directory. It explains what to collect from the user, how to run
  `scripts/search_awards.py`, and how to present the HTML report it writes.
- Human setup instructions live in `SETUP.md`; the option reference is in `README.md`.
- Run `./run_tests.sh` (offline, no key needed) and `./scripts/check_secrets.sh` before any commit.
- Never write the seats.aero API key into a file in this repository or echo it in chat. It is read only
  from `SEATS_AERO_API_KEY` or `~/.config/seats-aero/api_key`.
