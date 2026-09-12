"""Regression tests for scripts/search_awards.py.

The seats.aero API is mocked at the urllib "opener" boundary so these tests
run offline and never need a real API key.
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.error
from datetime import date
from email.message import Message
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import search_awards as sa  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeOpener:
    """Routes requests to canned responses by URL path; records every request."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.requests: list[str] = []

    def __call__(self, request, timeout):
        url = request.full_url
        self.requests.append(url)
        assert request.get_header("Partner-authorization"), "API key header missing"
        path = url.split("/partnerapi/", 1)[1].split("?", 1)[0]
        handler = self.routes.get(path)
        if handler is None:
            raise http_error(url, 404, "not found")
        if callable(handler):
            handler = handler()
        if isinstance(handler, Exception):
            raise handler
        return FakeResponse(json.dumps(handler).encode("utf-8"))


def http_error(url: str, code: int, body: str = "", headers: dict | None = None) -> urllib.error.HTTPError:
    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError(url, code, body, msg, io.BytesIO(body.encode("utf-8")))


def make_client(routes: dict[str, object]) -> tuple[sa.SeatsAeroClient, FakeOpener, list[float]]:
    opener = FakeOpener(routes)
    sleeps: list[float] = []
    client = sa.SeatsAeroClient("test-key", opener=opener, sleep=sleeps.append)
    return client, opener, sleeps


def default_routes() -> dict[str, object]:
    return {
        "search": load_fixture("search_response.json"),
        "trips/avail-aeroplan-1": load_fixture("trips_aeroplan.json"),
        "trips/avail-qantas-first": load_fixture("trips_qantas.json"),
    }


def query(**overrides) -> sa.SearchQuery:
    base = dict(origin="SIN", destination="LHR", travel_date=date(2026, 11, 14), pax=1)
    base.update(overrides)
    return sa.SearchQuery(**base)


# --------------------------------------------------------------------------- filtering


class PremiumCabinFilteringTests(unittest.TestCase):
    def test_only_business_and_first_itineraries_are_returned(self):
        client, opener, _ = make_client(default_routes())
        result = sa.run_search(client, query())
        cabins = {o.cabin for o in result.options}
        self.assertEqual(cabins, {"business", "first"})
        self.assertNotIn("trip-aeroplan-economy", [o.trip_id for o in result.options])

    def test_economy_only_availability_is_never_looked_up(self):
        client, opener, _ = make_client(default_routes())
        sa.run_search(client, query())
        self.assertFalse(any("avail-united-economy-only" in u for u in opener.requests))

    def test_dynamic_price_filtered_trips_are_dropped(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query())
        self.assertNotIn("trip-aeroplan-filtered", [o.trip_id for o in result.options])

    def test_first_only_request_excludes_business(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query(cabins=("first",)))
        self.assertEqual([o.cabin for o in result.options], ["first"])
        self.assertEqual(result.options[0].program, "Qantas Frequent Flyer")

    def test_pax_filter_drops_itineraries_with_too_few_seats_but_keeps_unknown(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query(pax=2))
        ids = [o.trip_id for o in result.options]
        self.assertIn("trip-aeroplan-direct", ids)        # 2 seats
        self.assertNotIn("trip-aeroplan-onestop", ids)    # 1 seat
        self.assertIn("trip-qantas-first", ids)           # 0 = unknown, kept and flagged
        self.assertTrue(any("does not publish a seat count" in n for n in result.notes))

    def test_direct_only_drops_connections(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query(direct_only=True))
        self.assertTrue(all(o.stops == 0 for o in result.options))
        self.assertNotIn("trip-aeroplan-onestop", [o.trip_id for o in result.options])

    def test_results_sorted_by_mileage_then_taxes(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query())
        costs = [(o.mileage_cost, o.taxes_minor_units) for o in result.options]
        self.assertEqual(costs, sorted(costs))
        self.assertEqual(result.options[0].trip_id, "trip-aeroplan-direct")

    def test_sources_restriction_is_passed_to_api(self):
        client, opener, _ = make_client(default_routes())
        sa.run_search(client, query(sources=("aeroplan", "united")))
        self.assertIn("sources=aeroplan%2Cunited", opener.requests[0])


# --------------------------------------------------------------------------- enrichment


class TripEnrichmentTests(unittest.TestCase):
    def test_option_carries_flight_detail_and_booking_link(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query())
        best = result.options[0]
        self.assertEqual(best.flight_numbers, "SQ308")
        self.assertEqual(best.airlines, ["SQ"])
        self.assertEqual(best.aircraft, ["Airbus A380-800"])
        self.assertEqual(best.duration_minutes, 870)
        self.assertEqual(best.taxes_currency, "CAD")
        self.assertTrue(best.booking_link.startswith("https://www.aircanada.com/"))
        self.assertEqual(best.detail_level, "trip")

    def test_repeated_carrier_codes_are_collapsed(self):
        routes = default_routes()
        trips = load_fixture("trips_aeroplan.json")
        trips["data"][1]["Carriers"] = "VN, VN"
        routes["trips/avail-aeroplan-1"] = trips
        client, _, _ = make_client(routes)
        result = sa.run_search(client, query())
        onestop = next(o for o in result.options if o.trip_id == "trip-aeroplan-onestop")
        self.assertEqual(onestop.airlines, ["VN"])
        self.assertEqual(sa.format_airlines(onestop.airlines), "Vietnam Airlines (VN)")

    def test_segments_are_ordered_by_order_field(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query())
        onestop = next(o for o in result.options if o.trip_id == "trip-aeroplan-onestop")
        self.assertEqual(onestop.aircraft, ["Airbus A350-900", "Airbus A320neo"])

    def test_falls_back_to_summary_when_trip_lookup_fails(self):
        routes = default_routes()
        routes["trips/avail-qantas-first"] = http_error("x", 404, "gone")
        client, _, _ = make_client(routes)
        result = sa.run_search(client, query())
        summaries = [o for o in result.options if o.detail_level == "summary"]
        self.assertEqual({o.cabin for o in summaries}, {"business", "first"})
        first = next(o for o in summaries if o.cabin == "first")
        self.assertEqual(first.mileage_cost, 162800)
        self.assertEqual(first.airlines, ["QF"])
        self.assertEqual(first.program, "Qantas Frequent Flyer")

    def test_summary_when_no_itinerary_survives_filters(self):
        routes = default_routes()
        search = load_fixture("search_response.json")
        search["data"] = [a for a in search["data"] if a["Source"] == "aeroplan"]
        routes["search"] = search
        routes["trips/avail-aeroplan-1"] = {"data": [], "booking_links": []}
        client, _, _ = make_client(routes)
        result = sa.run_search(client, query())
        self.assertEqual(len(result.options), 1)
        self.assertEqual(result.options[0].detail_level, "summary")
        self.assertIn("no itinerary matched", result.options[0].flight_numbers)

    def test_trip_lookup_cap_is_respected(self):
        client, opener, _ = make_client(default_routes())
        result = sa.run_search(client, query(max_trip_lookups=1))
        trip_calls = [u for u in opener.requests if "/trips/" in u]
        self.assertEqual(len(trip_calls), 1)
        self.assertTrue(any("max-trip-lookups" in n for n in result.notes))
        self.assertTrue(any(o.detail_level == "summary" for o in result.options))

    def test_stale_cache_is_flagged(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query())
        self.assertTrue(any("older than" in n for n in result.notes))


# --------------------------------------------------------------------------- http client


class ClientBehaviourTests(unittest.TestCase):
    def test_search_request_shape(self):
        client, opener, _ = make_client(default_routes())
        list(client.search("SIN", "LHR", date(2026, 11, 14), date(2026, 11, 16)))
        url = opener.requests[0]
        self.assertTrue(url.startswith("https://seats.aero/partnerapi/search?"))
        for expected in ("origin_airport=SIN", "destination_airport=LHR", "start_date=2026-11-14", "end_date=2026-11-16", "take=500"):
            self.assertIn(expected, url)

    def test_pagination_follows_cursor(self):
        page1 = {"data": [{"ID": "a"}], "hasMore": True, "cursor": 1700000000}
        page2 = {"data": [{"ID": "b"}], "hasMore": False, "cursor": 0}
        pages = iter([page1, page2])
        client, opener, _ = make_client({"search": lambda: next(pages)})
        ids = [a["ID"] for a in client.search("SIN", "LHR", date(2026, 11, 14), date(2026, 11, 14))]
        self.assertEqual(ids, ["a", "b"])
        self.assertIn("cursor=1700000000", opener.requests[1])

    def test_retries_on_429_then_succeeds(self):
        attempts = iter([http_error("x", 429, "slow down", {"Retry-After": "3"}), {"data": [], "hasMore": False}])
        client, _, sleeps = make_client({"search": lambda: next(attempts)})
        self.assertEqual(list(client.search("SIN", "LHR", date(2026, 11, 14), date(2026, 11, 14))), [])
        self.assertEqual(sleeps, [3.0])

    def test_gives_up_after_max_retries_on_5xx(self):
        client, _, sleeps = make_client({"search": lambda: http_error("x", 503, "down")})
        with self.assertRaises(sa.SeatsAeroError):
            list(client.search("SIN", "LHR", date(2026, 11, 14), date(2026, 11, 14)))
        self.assertEqual(sleeps, [1.0, 2.0, 4.0, 8.0])

    def test_auth_failure_is_clear_and_not_retried(self):
        client, _, sleeps = make_client({"search": lambda: http_error("x", 401, "bad key")})
        with self.assertRaises(sa.SeatsAeroError) as ctx:
            list(client.search("SIN", "LHR", date(2026, 11, 14), date(2026, 11, 14)))
        self.assertIn("API key", str(ctx.exception))
        self.assertNotIn("test-key", str(ctx.exception))
        self.assertEqual(sleeps, [])

    def test_trip_id_is_url_encoded(self):
        client, opener, _ = make_client({"trips/weird%2Fid": {"data": []}})
        client.trips("weird/id")
        self.assertTrue(opener.requests[0].endswith("/trips/weird%2Fid"))


# --------------------------------------------------------------------------- auth resolution


class ApiKeyResolutionTests(unittest.TestCase):
    def test_env_var_preferred(self):
        self.assertEqual(sa.resolve_api_key({"SEATS_AERO_API_KEY": " abc "}, Path("/nonexistent")), "abc")

    def test_legacy_env_var_supported(self):
        self.assertEqual(sa.resolve_api_key({"SEATS_API_KEY": "legacy"}, Path("/nonexistent")), "legacy")

    def test_key_file_fallback(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "api_key"
            key_file.write_text("from-file\n")
            os.chmod(key_file, 0o600)
            self.assertEqual(sa.resolve_api_key({}, key_file), "from-file")

    def test_missing_key_raises_usage_error_with_instructions(self):
        with self.assertRaises(sa.UsageError) as ctx:
            sa.resolve_api_key({}, Path("/nonexistent"))
        self.assertIn("SEATS_AERO_API_KEY", str(ctx.exception))


# --------------------------------------------------------------------------- cli validation


class ArgumentValidationTests(unittest.TestCase):
    TODAY = date(2026, 9, 12)

    def parse(self, *argv):
        return sa.parse_args(list(argv), today=self.TODAY)

    def test_happy_path_uppercases_airports(self):
        q, _ = self.parse("sin", "lhr", "--date", "2026-11-14", "--pax", "2", "--flex", "2")
        self.assertEqual((q.origin, q.destination, q.pax), ("SIN", "LHR", 2))
        self.assertEqual((q.start_date, q.end_date), (date(2026, 11, 12), date(2026, 11, 16)))
        self.assertEqual(q.cabins, ("business", "first"))

    def test_rejects_bad_airport(self):
        with self.assertRaises(sa.UsageError):
            self.parse("SING", "LHR", "--date", "2026-11-14")

    def test_rejects_same_airports(self):
        with self.assertRaises(sa.UsageError):
            self.parse("SIN", "SIN", "--date", "2026-11-14")

    def test_multi_airport_lists_are_accepted_and_normalised(self):
        q, _ = self.parse("sin", "pek, pkx,PEK", "--date", "2027-09-02", "--pax", "2")
        self.assertEqual((q.origin, q.destination), ("SIN", "PEK,PKX"))
        self.assertEqual(q.destination_label, "PEK/PKX")
        self.assertEqual(sa.default_report_path(q).name, "awards_SIN-PEK+PKX_2027-09-02_pax2.html")

    def test_rejects_overlapping_or_too_many_airports(self):
        with self.assertRaises(sa.UsageError):
            self.parse("LHR,LGW", "LGW", "--date", "2026-11-14")
        with self.assertRaises(sa.UsageError):
            self.parse("SIN", "PEK,PKX,TSN,SJW,NAY", "--date", "2026-11-14")

    def test_rejects_past_date(self):
        with self.assertRaises(sa.UsageError):
            self.parse("SIN", "LHR", "--date", "2026-01-01")

    def test_rejects_invalid_calendar_date(self):
        with self.assertRaises(sa.UsageError):
            self.parse("SIN", "LHR", "--date", "2026-02-30")

    def test_rejects_economy_cabin(self):
        with self.assertRaises(sa.UsageError) as ctx:
            self.parse("SIN", "LHR", "--date", "2026-11-14", "--cabins", "economy")
        self.assertIn("economy is out of scope", str(ctx.exception))

    def test_rejects_pax_out_of_range(self):
        for pax in ("0", "10"):
            with self.assertRaises(sa.UsageError):
                self.parse("SIN", "LHR", "--date", "2026-11-14", "--pax", pax)

    def test_rejects_unknown_source(self):
        with self.assertRaises(sa.UsageError):
            self.parse("SIN", "LHR", "--date", "2026-11-14", "--sources", "bonvoy")


# --------------------------------------------------------------------------- rendering


class RenderingTests(unittest.TestCase):
    def setUp(self):
        client, _, _ = make_client(default_routes())
        self.result = sa.run_search(client, query(pax=2))

    def test_markdown_table_contains_key_columns(self):
        text = sa.render_markdown(self.result)
        self.assertIn("| Program | Cabin | Airline | Flights | Date | Route |", text)
        self.assertIn("| SIN-LHR |", text)
        self.assertIn("Air Canada Aeroplan", text)
        self.assertIn("Singapore Airlines (SQ)", text)
        self.assertIn("87,500", text)
        self.assertIn("147.50 CAD", text)
        self.assertIn("[book](https://www.aircanada.com/", text)
        self.assertIn("Passengers: 2", text)

    def test_markdown_overnight_arrival_marker(self):
        text = sa.render_markdown(self.result)
        self.assertIn("23:30 → 06:15 (+1)", text)

    def test_json_output_is_valid_and_complete(self):
        payload = json.loads(sa.render_json(self.result))
        self.assertEqual(payload["query"]["origin"], "SIN")
        self.assertEqual(payload["query"]["travel_date"], "2026-11-14")
        self.assertGreater(len(payload["options"]), 0)
        option = payload["options"][0]
        for key in ("program", "cabin", "airlines", "flight_numbers", "mileage_cost", "taxes_display", "booking_link", "seats_known"):
            self.assertIn(key, option)

    def test_no_results_message(self):
        empty = sa.SearchResult(query=query(), options=[], notes=[], api_calls=1, availabilities_seen=5, generated_at="")
        text = sa.render_markdown(empty)
        self.assertIn("No business or first class award space", text)
        self.assertIn("--flex 3", text)

    def test_formatters(self):
        self.assertEqual(sa.format_duration(870), "14h 30m")
        self.assertEqual(sa.format_miles(0), "n/a")
        self.assertEqual(sa.format_taxes(14750, "CAD"), "147.50 CAD")
        self.assertEqual(sa.format_stops(0), "nonstop")
        self.assertEqual(sa.format_stops(2), "2 stops")
        self.assertEqual(sa.format_stops(-1), "?")
        self.assertEqual(sa.format_airlines(["QF", "ZZ"]), "Qantas (QF), ZZ")


# --------------------------------------------------------------------------- html report


class HtmlReportTests(unittest.TestCase):
    def setUp(self):
        client, _, _ = make_client(default_routes())
        self.result = sa.run_search(client, query(pax=2))
        self.html = sa.render_html(self.result)

    def test_is_complete_dark_themed_document(self):
        self.assertTrue(self.html.startswith("<!doctype html>"))
        self.assertIn("color-scheme:dark", self.html)
        self.assertIn("<title>Award seats SIN → LHR · 2026-11-14</title>", self.html)
        self.assertIn("<th>Book</th>", self.html)
        self.assertIn("td:last-child{position:sticky;right:0", self.html)

    def test_rows_link_to_program_booking_page_and_airline_site(self):
        self.assertIn('class="book" href="https://www.aircanada.com/aeroplan/redeem/availability/outbound?org0=SIN&amp;dest0=LHR', self.html)
        self.assertIn('href="https://www.singaporeair.com/"', self.html)
        self.assertIn('rel="noopener noreferrer"', self.html)

    def test_falls_back_to_program_site_when_api_has_no_deep_link(self):
        # Qantas fixture returns no booking_links
        self.assertIn('class="book book-fallback" href="https://www.qantas.com/', self.html)
        self.assertIn("Program site →", self.html)

    def test_summary_cards_show_best_per_cabin(self):
        self.assertIn("Best business", self.html)
        self.assertIn("87,500 miles", self.html)
        self.assertIn("Best first", self.html)
        self.assertIn("162,800 miles", self.html)

    def test_untrusted_values_are_escaped_and_unsafe_urls_dropped(self):
        evil = sa.AwardOption(
            program="<script>alert(1)</script>", source="x", cabin="business", travel_date="2026-11-14",
            route="SIN-LHR", airlines=["ZZ"], flight_numbers='ZZ1 "quoted"', departs_at="", arrives_at="",
            duration_minutes=0, stops=0, remaining_seats=1, mileage_cost=1000, taxes_minor_units=0,
            taxes_currency="", booking_link="javascript:alert(1)",
        )
        result = sa.SearchResult(query=query(), options=[evil], notes=["<b>note</b>"], api_calls=0, availabilities_seen=0, generated_at="now")
        html = sa.render_html(result)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("javascript:", html)
        self.assertIn("&lt;b&gt;note&lt;/b&gt;", html)
        self.assertIn("&quot;quoted&quot;", html)

    def test_empty_result_renders_helpful_message(self):
        empty = sa.SearchResult(query=query(), options=[], notes=[], api_calls=1, availabilities_seen=4, generated_at="")
        html = sa.render_html(empty)
        self.assertIn("No business or first class award space", html)
        self.assertIn("4 availability records", html)

    def test_write_report_creates_file_at_default_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = sa.default_report_path(query(pax=2, flex_days=3), Path(tmp))
            self.assertEqual(path.name, "awards_SIN-LHR_2026-11-14_pm3d_pax2.html")
            sa.write_report(self.result, path)
            self.assertTrue(path.is_file())
            self.assertIn("Air Canada Aeroplan", path.read_text(encoding="utf-8"))

    def test_safe_url_and_booking_url_helpers(self):
        self.assertEqual(sa.safe_url("https://a.b/c"), "https://a.b/c")
        self.assertEqual(sa.safe_url("javascript:alert(1)"), "")
        self.assertEqual(sa.safe_url("ftp://a.b"), "")
        self.assertEqual(sa.safe_url(""), "")
        option = self.result.options[0]
        self.assertTrue(sa.booking_url(option).startswith("https://www.aircanada.com/"))
        option.booking_link = ""
        self.assertEqual(sa.booking_url(option), sa.PROGRAM_BOOKING_URLS["aeroplan"])

    def test_every_program_has_booking_fallback_and_https_urls(self):
        self.assertEqual(set(sa.PROGRAM_BOOKING_URLS), set(sa.PROGRAM_NAMES))
        for url in list(sa.PROGRAM_BOOKING_URLS.values()) + list(sa.AIRLINE_URLS.values()):
            self.assertTrue(url.startswith("https://"), url)
        self.assertEqual(set(sa.AIRLINE_URLS), set(sa.AIRLINE_NAMES))

    def test_format_age_never_negative(self):
        self.assertEqual(sa.format_age("2099-01-01T00:00:00Z"), "just now")


# --------------------------------------------------------------------------- end to end


class MainEntryPointTests(unittest.TestCase):
    def test_main_runs_end_to_end_with_mocked_network(self):
        opener = FakeOpener(default_routes())
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"SEATS_AERO_API_KEY": "k"}), \
             mock.patch("urllib.request.urlopen", lambda req, timeout=None: opener(req, timeout)), \
             mock.patch("search_awards.date") as fake_date, \
             mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            fake_date.today.return_value = date(2026, 9, 12)
            fake_date.fromisoformat = date.fromisoformat
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                report = Path(tmp) / "out.html"
                code = sa.main(["SIN", "LHR", "--date", "2026-11-14", "--quiet", "--html", str(report)])
                self.assertEqual(code, 0, stderr.getvalue())
                self.assertTrue(report.is_file())
                self.assertIn(f"_HTML report: {report}_", stdout.getvalue())
        self.assertIn("Air Canada Aeroplan", stdout.getvalue())
        self.assertNotIn("k", stderr.getvalue())

    def test_main_no_html_and_json(self):
        opener = FakeOpener(default_routes())
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, {"SEATS_AERO_API_KEY": "k"}), \
             mock.patch("urllib.request.urlopen", lambda req, timeout=None: opener(req, timeout)), \
             mock.patch("search_awards.date") as fake_date, \
             mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", io.StringIO()):
            fake_date.today.return_value = date(2026, 9, 12)
            fake_date.fromisoformat = date.fromisoformat
            code = sa.main(["SIN", "LHR", "--date", "2026-11-14", "--quiet", "--no-html", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertIsNone(payload["report_path"])
        self.assertTrue(payload["options"][0]["book_url"].startswith("https://"))

    def test_main_usage_error_exit_code(self):
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(sa.main(["SIN", "LHR", "--date", "not-a-date"]), 2)
            self.assertIn("YYYY-MM-DD", err.getvalue())

    def test_main_missing_key_exit_code(self):
        env = {k: v for k, v in os.environ.items() if k not in sa.API_KEY_ENV_VARS}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(sa, "API_KEY_FILE", Path("/nonexistent")), \
             mock.patch("search_awards.date") as fake_date, \
             mock.patch("sys.stderr", io.StringIO()) as err:
            fake_date.today.return_value = date(2026, 9, 12)
            fake_date.fromisoformat = date.fromisoformat
            self.assertEqual(sa.main(["SIN", "LHR", "--date", "2026-11-14"]), 2)
            self.assertIn("No seats.aero API key", err.getvalue())


if __name__ == "__main__":
    unittest.main()
