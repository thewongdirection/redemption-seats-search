# seats.aero Partner API — working notes

Compiled from the public developer reference, the seats.aero knowledge base and two open-source clients
(`gavgrego/seats.aero-mcp-server`, `denverquane/seats-aero-go`). The official docs live at
https://developers.seats.aero/reference/ and win if anything here disagrees.

## Access

- Requires a **seats.aero Pro** membership. Generate the key in seats.aero → Settings.
- Pro keys get roughly **1,000 calls per day**. Cached Search, Bulk Availability, Get Trips, Get Routes and
  Get Destinations are available. **Live Search is not** (commercial agreement only).
- Base URL: `https://seats.aero/partnerapi`
- Auth header: `Partner-Authorization: <key>` (send `accept: application/json` too).
- All responses are JSON. Errors come back as HTTP status codes; 429 means slow down.

## Endpoints used by the skill

### GET `/search` — Cached Search

| Param | Notes |
|---|---|
| `origin_airport` | IATA code, comma list allowed (`SIN,KUL`) |
| `destination_airport` | IATA code, comma list allowed |
| `start_date`, `end_date` | `YYYY-MM-DD`, inclusive |
| `take` | page size, 10–1000 (script uses 500) |
| `cursor` | from the previous page when `hasMore` is true |
| `skip` | alternative offset pagination |
| `order_by` | `lowest_mileage` (orders by any cabin, so the script sorts client-side instead) |
| `sources` | comma list of program codes (below) |
| `carriers` | comma list of 2-letter airline codes |
| `only_direct` | boolean (some clients spell it `only_direct_flights`); the script filters stops client-side |
| `include_trips` | inline trip objects in each availability; the script calls `/trips/{id}` instead for booking links |
| `include_filtered` | include results normally hidden by dynamic-pricing filters |

Response envelope: `{ "data": [Availability...], "count": n, "hasMore": bool, "cursor": int }`.

### Availability object (one per route + date + program)

```
ID, RouteID, Route{ID, OriginAirport, OriginRegion, DestinationAirport, DestinationRegion, NumDaysOut, Distance, Source}
Date ("YYYY-MM-DD"), ParsedDate, Source, CreatedAt, UpdatedAt, TaxesCurrency
Per cabin, prefix Y (economy) / W (premium economy) / J (business) / F (first):
  {X}Available (bool)        {X}MileageCost (string)   {X}MileageCostRaw (int)
  {X}RemainingSeats (int)    {X}Airlines (string, "SQ, LH")   {X}TotalTaxes (int, minor units)
  {X}Direct (bool)           {X}DirectMileageCost / {X}DirectRemainingSeats / {X}DirectAirlines (newer fields)
```

The skill only reads the `J` and `F` families. `RemainingSeats == 0` means "not published", not "sold out",
because the object would not be `Available` otherwise.

### GET `/trips/{availability_id}` — Get Trips

Response: `{ "data": [Trip...], "origin_coordinates", "destination_coordinates", "booking_links": [{label, link, primary}], "revalidation_id" }`

Trip fields: `ID, RouteID, AvailabilityID, AvailabilitySegments[], TotalDuration (minutes), Stops, Carriers ("SQ" or "SQ, LH"),
RemainingSeats, MileageCost (int), TotalTaxes (int, minor units), TaxesCurrency, TaxesCurrencySymbol, AllianceCost,
TotalSegmentDistance, FlightNumbers ("SQ308" or "LH779, LH900"), DepartsAt, ArrivesAt, Cabin ("economy|premium|business|first"),
Source, Filtered (bool), CreatedAt, UpdatedAt`

Segment fields: `FlightNumber, Distance, FareClass, AircraftName, AircraftCode, OriginAirport, DestinationAirport, DepartsAt,
ArrivesAt, Cabin, Order`.

Times are airport-local wall-clock values serialised with a `Z` suffix; do not convert them to UTC.
`TotalTaxes` is in the currency's minor unit (14750 CAD → $147.50). If a program ever reports a zero-decimal
currency differently, adjust `format_taxes` in the script and add a test.

### POST `/refresh` — Refresh Cached Data (used by `--refresh`)

Body: `{"availability_ids": ["…", …]}` with 1–250 IDs. Pro keys only; commercial keys are refused.
Posting the same IDs again polls without re-queuing or spending quota. Observed live response:

```json
{"items":[{"availability_id":"…","status":"queued","updated_at":"2026-08-31T02:13:12Z"}],
 "queued":1,"refunded":0,"counts":{"processing":1,"succeeded":0,"failed":0},"complete":false,
 "quota":{"limit":1000,"used":61,"remaining":939,"reset_seconds":45069}}
```

Statuses seen: `queued` → `processing` → `succeeded` (about 15 s for Aeroplan), plus `failed` and
`skipped_outage` (seats.aero has that program's scraping paused; KrisFlyer returned this in September 2026).
`quota` is the shared daily API allowance; each queued ID counts as one call. An empty ID list returns
HTTP 400 `no_availability_ids`.

### Not used, for reference

- GET `/availability?source=…` — bulk dump for one program (region filters, pagination).
- GET `/routes?source=…` — routes a program is monitored on.
- GET `/destinations?origin_airport=…` — nonstop destinations with cheapest miles per cabin.
- POST `/live` — real-time search with `seat_count`; commercial keys only.

## Program (`Source`) codes

| Code | Program | Code | Program |
|---|---|---|---|
| aeromexico | Aeromexico Rewards | lufthansa | Lufthansa Miles & More |
| aeroplan | Air Canada Aeroplan | qantas | Qantas Frequent Flyer |
| alaska | Alaska Atmos Rewards | qatar | Qatar Privilege Club |
| american | American AAdvantage | saudia | Saudia AlFursan |
| azul | Azul Fidelidade | singapore | Singapore KrisFlyer |
| connectmiles | Copa ConnectMiles | smiles | GOL Smiles |
| delta | Delta SkyMiles | spirit | Spirit Free Spirit |
| emirates | Emirates Skywards | turkish | Turkish Miles&Smiles |
| ethiopian | Ethiopian ShebaMiles | united | United MileagePlus |
| etihad | Etihad Guest | velocity | Virgin Australia Velocity |
| eurobonus | SAS EuroBonus | virginatlantic | Virgin Atlantic Flying Club |
| finnair | Finnair Plus | jetblue | JetBlue TrueBlue |
| flyingblue | Air France-KLM Flying Blue | frontier | Frontier Miles |

seats.aero adds programs from time to time; an unknown code is shown verbatim by the script.
