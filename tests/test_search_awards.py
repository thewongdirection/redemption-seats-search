"""Regression tests for scripts/search_awards.py.

The seats.aero API is mocked at the urllib "opener" boundary so these tests
run offline and never need a real API key.
"""
from __future__ import annotations

import contextlib
import fnmatch
import io
import json
import os
import shutil
import subprocess
import time
import sys
import tempfile
import unittest
import urllib.error
from datetime import date
from email.message import Message
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import crosscheck  # noqa: E402
import preflight as pf  # noqa: E402
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
        self.bodies: list[str] = []

    def __call__(self, request, timeout):
        url = request.full_url
        self.requests.append(url)
        if request.data:
            self.bodies.append(request.data.decode("utf-8"))
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
        "refresh": lambda: refresh_response({}, complete=True),
        "search": load_fixture("search_response.json"),
        "trips/avail-aeroplan-1": load_fixture("trips_aeroplan.json"),
        "trips/avail-qantas-first": load_fixture("trips_qantas.json"),
    }


def query(**overrides) -> sa.SearchQuery:
    base = dict(origin="SIN", destination="LHR", start_date=date(2026, 11, 14), end_date=date(2026, 11, 14), pax=1, refresh=False)
    base.update(overrides)
    return sa.SearchQuery(**base)


def refresh_response(items: dict[str, str], complete: bool, remaining: int = 900) -> dict:
    return {
        "items": [{"availability_id": k, "status": v, "updated_at": "2026-09-12T00:00:00Z"} for k, v in items.items()],
        "queued": sum(1 for v in items.values() if v == "queued"),
        "refunded": 0,
        "counts": {"processing": sum(1 for v in items.values() if v in ("queued", "processing")),
                   "succeeded": sum(1 for v in items.values() if v == "succeeded"),
                   "failed": sum(1 for v in items.values() if v == "failed")},
        "complete": complete,
        "quota": {"limit": 1000, "used": 1000 - remaining, "remaining": remaining, "reset_seconds": 40000},
    }


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


# --------------------------------------------------------------------------- refresh


class RefreshTests(unittest.TestCase):
    def test_refresh_posts_json_and_polls_until_complete(self):
        responses = iter([
            refresh_response({"a": "queued", "b": "queued"}, complete=False),
            refresh_response({"a": "processing", "b": "succeeded"}, complete=False),
            refresh_response({"a": "succeeded", "b": "succeeded"}, complete=True, remaining=850),
        ])
        client, opener, sleeps = make_client({"refresh": lambda: next(responses)})
        outcome = client.refresh_and_wait(["a", "b", "a"], timeout_seconds=60, poll_seconds=5)
        self.assertEqual(outcome.requested, 2)
        self.assertEqual(outcome.succeeded, 2)
        self.assertFalse(outcome.timed_out)
        self.assertEqual(sleeps, [5, 5])
        self.assertEqual(client.calls_made, 2, "one quota call per record refreshed; polls are free")
        self.assertEqual(outcome.quota["remaining"], 850)
        self.assertIn("2 refreshed", outcome.summary())
        self.assertIn("850/1000", outcome.summary(), "the quota line lives in the notes, which both report formats carry")

    def test_refresh_request_body_shape(self):
        captured = {}
        def handler():
            return refresh_response({"x": "succeeded"}, complete=True)
        opener = FakeOpener({"refresh": handler})
        original = opener.__call__
        def spy(request, timeout):
            captured["method"] = request.get_method()
            captured["body"] = json.loads(request.data.decode())
            captured["ctype"] = request.get_header("Content-type")
            return original(request, timeout)
        client = sa.SeatsAeroClient("k", opener=spy, sleep=lambda _: None)
        client.refresh(["x"])
        self.assertEqual(captured, {"method": "POST", "body": {"availability_ids": ["x"]}, "ctype": "application/json"})

    def test_refresh_times_out_and_reports_processing(self):
        client, _, sleeps = make_client({"refresh": lambda: refresh_response({"a": "processing"}, complete=False)})
        outcome = client.refresh_and_wait(["a"], timeout_seconds=12, poll_seconds=5)
        self.assertTrue(outcome.timed_out)
        self.assertEqual(outcome.processing, 1)
        self.assertEqual(len(sleeps), 3)
        self.assertIn("still processing", outcome.summary())

    def test_refresh_batches_by_250(self):
        calls = []
        def handler():
            calls.append(1)
            return refresh_response({}, complete=True)
        client, _, _ = make_client({"refresh": handler})
        client.refresh_and_wait([f"id{i}" for i in range(501)], timeout_seconds=0)
        self.assertEqual(len(calls), 3)

    def test_skipped_outage_is_explained(self):
        client, _, _ = make_client({"refresh": lambda: refresh_response({"a": "skipped_outage", "b": "succeeded"}, complete=True)})
        outcome = client.refresh_and_wait(["a", "b"])
        self.assertEqual(outcome.skipped, 1)
        self.assertIn("scraping paused", outcome.summary())

    def test_run_search_refreshes_only_stale_records_and_researches(self):
        search_calls = []
        refreshed = []
        def search_handler():
            search_calls.append(1)
            return load_fixture("search_response.json")
        opener_routes = default_routes()
        opener_routes["search"] = search_handler
        def refresh_handler():
            return refresh_response({"avail-qantas-first": "succeeded"}, complete=True)
        opener_routes["refresh"] = refresh_handler
        opener = FakeOpener(opener_routes)
        client = sa.SeatsAeroClient("k", opener=opener, sleep=lambda _: None)
        result = sa.run_search(client, query(refresh=True))
        refresh_bodies = [u for u in opener.requests if u.endswith("/refresh")]
        self.assertEqual(len(refresh_bodies), 1)          # aeroplan/united records are "fresh" (2099) so only qantas (2020) is stale
        self.assertEqual(len(search_calls), 2)            # searched again after the refresh
        self.assertIsNotNone(result.refresh)
        self.assertEqual(result.refresh.requested, 1)
        self.assertTrue(any("1 refreshed" in n for n in result.notes))
        payload = json.loads(sa.render_json(result))
        self.assertEqual(payload["refresh"]["succeeded"], 1)
        self.assertIn("Refreshed before reporting", sa.render_markdown(result))
        self.assertIn("Cached data, no refresh", sa.render_markdown(sa.run_search(make_client(default_routes())[0], query())))

    def test_default_threshold_refreshes_every_match_oldest_first(self):
        posted = []
        def refresh_handler():
            return refresh_response({}, complete=True)
        routes = default_routes(); routes["refresh"] = refresh_handler
        opener = FakeOpener(routes)
        original = opener.__call__
        def spy(request, timeout):
            if request.full_url.endswith("/refresh"):
                posted.append(json.loads(request.data.decode())["availability_ids"])
            return original(request, timeout)
        client = sa.SeatsAeroClient("k", opener=spy, sleep=lambda _: None)
        sa.run_search(client, query(refresh=True, refresh_older_than_hours=0.0))
        # qantas (2020) is the only premium match with a past UpdatedAt; the aeroplan fixture is dated 2099 and united is economy-only
        self.assertEqual(posted, [["avail-qantas-first"]])

    def test_refresh_cap_protects_quota(self):
        many = load_fixture("search_response.json")
        template = many["data"][0]
        many["data"] = [{**template, "ID": f"a{i}", "UpdatedAt": f"2026-01-{(i % 28) + 1:02d}T00:00:00Z"} for i in range(150)]
        posted = []
        routes = {"search": many, "refresh": lambda: refresh_response({}, complete=True)}
        opener = FakeOpener(routes)
        original = opener.__call__
        def spy(request, timeout):
            if request.full_url.endswith("/refresh"):
                posted.append(json.loads(request.data.decode())["availability_ids"])
            return original(request, timeout)
        client = sa.SeatsAeroClient("k", opener=spy, sleep=lambda _: None)
        with mock.patch.object(sa, "MAX_REFRESH_RECORDS", 100):
            result = sa.run_search(client, query(refresh=True, max_trip_lookups=0))
        self.assertEqual(sum(len(b) for b in posted), 100)
        self.assertEqual(result.refresh.capped_from, 150)
        self.assertTrue(any("100 oldest of 150" in n for n in result.notes))

    def test_run_search_refresh_with_nothing_stale_adds_note_only(self):
        routes = default_routes()
        client, opener, _ = make_client(routes)
        result = sa.run_search(client, query(refresh=True, refresh_older_than_hours=10**7))
        self.assertFalse(any(u.endswith("/refresh") for u in opener.requests))
        self.assertTrue(any("No refresh needed" in n for n in result.notes))

    def test_refresh_on_with_no_matches_does_not_crash(self):
        routes = default_routes()
        routes["search"] = {"data": [], "hasMore": False, "cursor": 0}
        client, opener, _ = make_client(routes)
        result = sa.run_search(client, query(refresh=True))
        self.assertEqual(result.options, [])
        self.assertFalse(any(u.endswith("/refresh") for u in opener.requests))
        self.assertFalse(any("refresh" in n.lower() for n in result.notes))

    def test_polling_log_is_quiet_until_progress_or_30s(self):
        responses = iter([refresh_response({"a": "queued"}, complete=False)] + [refresh_response({"a": "processing"}, complete=False)] * 20)
        client, _, _ = make_client({"refresh": lambda: next(responses)})
        lines = []
        client.refresh_and_wait(["a"], timeout_seconds=60, poll_seconds=5, log=lines.append)
        self.assertLessEqual(len(lines), 3)   # first change (queued->processing), then every 30s

    def test_run_search_survives_refresh_failure(self):
        routes = default_routes()
        routes["refresh"] = lambda: http_error("x", 403, "not allowed")
        client, _, _ = make_client(routes)
        result = sa.run_search(client, query(refresh=True))
        self.assertTrue(any("Refresh was not possible" in n for n in result.notes))
        self.assertGreater(len(result.options), 0)

    def test_schedule_opening_note(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query(date_mode="schedule-opening", start_date=date(2027, 9, 1), end_date=date(2027, 9, 2)))
        self.assertTrue(any("354-355 days out" in n for n in result.notes))


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
        self.assertEqual(q.date_mode, "flex")
        self.assertTrue(q.refresh, "fresh data is the default")
        self.assertEqual(q.refresh_older_than_hours, 0.0)

    def test_no_refresh_opts_out(self):
        q, _ = self.parse("SIN", "LHR", "--date", "2026-11-14", "--no-refresh")
        self.assertFalse(q.refresh)

    def test_no_date_scans_schedule_opening_window(self):
        q, _ = self.parse("SIN", "LHR", "--pax", "2")
        self.assertEqual((q.start_date, q.end_date), (date(2027, 9, 1), date(2027, 9, 2)))
        self.assertEqual(q.date_mode, "schedule-opening")
        self.assertEqual(q.window_label, "2027-09-01 to 2027-09-02")
        self.assertEqual(sa.default_report_path(q).name, "awards_SIN-LHR_2027-09-01_to_2027-09-02_pax2.html")

    def test_end_date_gives_a_range(self):
        q, _ = self.parse("SIN", "NRT,HND", "--date", "2026-12-01", "--end-date", "2026-12-31", "--pax", "2")
        self.assertEqual((q.start_date, q.end_date, q.date_mode), (date(2026, 12, 1), date(2026, 12, 31), "range"))
        self.assertEqual(sa.default_report_path(q).name, "awards_SIN-NRT+HND_2026-12-01_to_2026-12-31_pax2.html")
        self.assertNotIn("--flex", sa._next_steps_hint(q))

    def test_end_date_validation(self):
        for extra in (["--end-date", "2026-11-30"], ["--end-date", "2027-02-15"], ["--end-date", "12/31/2026"], ["--end-date", "2026-12-31", "--flex", "2"]):
            with self.assertRaises(sa.UsageError, msg=extra):
                self.parse("SIN", "LHR", "--date", "2026-12-01", *extra)
        with self.assertRaises(sa.UsageError):
            self.parse("SIN", "LHR", "--end-date", "2026-12-31")

    def test_flex_without_date_is_rejected(self):
        with self.assertRaises(sa.UsageError):
            self.parse("SIN", "LHR", "--flex", "2")

    def test_refresh_flags_are_parsed(self):
        q, _ = self.parse("SIN", "LHR", "--date", "2026-11-14", "--refresh-older-than", "6", "--refresh-timeout", "30")
        self.assertTrue(q.refresh)
        self.assertEqual((q.refresh_older_than_hours, q.refresh_timeout_seconds), (6.0, 30.0))
        with self.assertRaises(sa.UsageError):
            self.parse("SIN", "LHR", "--date", "2026-11-14", "--refresh-older-than", "-1")

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
        self.assertEqual(q.date_mode, "exact")
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
        self.assertIn("| # | Program | Cabin | Airline | Flights | Dep → Arr |", text)   # single day, single pair: no Date/Route
        self.assertNotIn("| SIN-LHR |", text)
        self.assertIn("Air Canada Aeroplan", text)
        self.assertIn("Singapore Airlines (SQ)", text)
        self.assertIn("87,500", text)
        self.assertIn("147.50 CAD", text)
        self.assertIn("[book](https://www.aircanada.com/", text)
        self.assertIn("Passengers: 2", text)

    def test_date_and_route_columns_appear_when_they_carry_information(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query(destination="LHR,LGW", start_date=date(2026, 11, 11), end_date=date(2026, 11, 17)))
        text = sa.render_markdown(result)
        self.assertIn("| Flights | Date | Route | Dep → Arr |", text)
        self.assertIn("| 2026-11-14 | SIN-LHR |", text)
        html = sa.render_html(result)
        self.assertIn(">Date</button></th>", html)
        self.assertIn(">Route</button></th>", html)

    def test_markdown_overnight_arrival_marker(self):
        text = sa.render_markdown(self.result)
        self.assertIn("23:30 → 06:15 (+1)", text)

    def test_json_output_is_valid_and_complete(self):
        payload = json.loads(sa.render_json(self.result))
        self.assertEqual(payload["query"]["origin"], "SIN")
        self.assertEqual(payload["query"]["start_date"], "2026-11-14")
        self.assertIsNone(payload["refresh"])
        self.assertGreater(len(payload["options"]), 0)
        option = payload["options"][0]
        for key in ("program", "cabin", "airlines", "flight_numbers", "mileage_cost", "taxes_display", "booking_link", "seats_known"):
            self.assertIn(key, option)

    def empty(self, q=None, **kw):
        base = dict(query=q or query(), options=[], notes=[], api_calls=1, availabilities_seen=0, generated_at="", searched_on=date(2026, 9, 12))
        base.update(kw)
        return sa.SearchResult(**base)

    def test_no_results_when_only_economy_is_cached(self):
        text = sa.render_markdown(self.empty(availabilities_seen=5))
        self.assertIn("No business or first class award space found", text)
        self.assertIn("5 cached records", text)
        self.assertIn("none with business or first class space", text)
        self.assertIn("--flex 3", text)

    def test_no_results_when_premium_space_exists_but_party_too_big(self):
        r = self.empty(query(pax=2, direct_only=True), availabilities_seen=3, premium_matches=1)
        text = sa.render_markdown(r)
        self.assertIn("1 cached record show business or first class space", text)
        self.assertIn("room for 2 passengers and a nonstop routing", text)
        self.assertIn("--pax 1", text)
        self.assertIn("dropping `--direct-only`", text)
        self.assertNotIn("none with business", text)

    def test_no_results_wording_respects_cabins_and_sources(self):
        first_only = self.empty(query(cabins=("first",)), availabilities_seen=2)
        text = sa.render_markdown(first_only)
        self.assertIn("No first class award space found", text)
        self.assertIn("none with first class space", text)
        scoped = self.empty(query(sources=("aeroplan", "united")))
        self.assertIn("The requested programs (aeroplan, united) has no award space", sa.render_markdown(scoped))
        self.assertIn("any cabin", sa.render_html(self.empty()))
        self.assertIn("First class award availability", sa.render_html(first_only))

    def test_no_results_horizon_uses_search_date_not_render_date(self):
        far = self.empty(query(start_date=date(2027, 10, 1), end_date=date(2027, 10, 1)))
        self.assertIn("nothing cached this far ahead", far.explain_no_results())
        near = self.empty(query(start_date=date(2027, 10, 1), end_date=date(2027, 10, 1)), searched_on=date(2027, 6, 1))
        self.assertIn("any cabin", near.explain_no_results())

    def test_no_results_in_schedule_opening_mode_does_not_contradict_itself(self):
        r = self.empty(query(date_mode="schedule-opening", start_date=date(2027, 9, 1), end_date=date(2027, 9, 2)),
                       notes=["No date was given, so this scanned 354-355 days out, where airlines first release award inventory."])
        text = sa.render_markdown(r)
        self.assertIn("has not cached SIN → LHR this far ahead yet", text)
        self.assertNotIn("re-run once the date is closer", text)
        self.assertIn("--flex 3", text)

    def test_run_search_records_premium_matches_and_search_date(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query())
        self.assertEqual(result.premium_matches, 2)
        self.assertEqual(result.searched_on, date.today())
        payload = json.loads(sa.render_json(result))
        self.assertEqual(payload["premium_matches"], 2)
        self.assertEqual(payload["searched_on"], date.today().isoformat())

    def test_markdown_rows_have_exactly_as_many_cells_as_headers(self):
        client, _, _ = make_client(default_routes())
        for q in (query(), query(destination="LHR,LGW", start_date=date(2026, 11, 11), end_date=date(2026, 11, 17))):
            result = sa.run_search(client, q)
            table = [l for l in sa.render_markdown(result).splitlines() if l.startswith("|")]
            widths = {len(l.strip("|").split("|")) for l in table}
            self.assertEqual(len(widths), 1, table[:3])
            html = sa.render_html(result)
            self.assertEqual(html.count("<th") - html.count("<thead"), len(sa.html_columns(q)))
            self.assertEqual(len(table[0].strip("|").split("|")), len(sa.markdown_columns(q)))
            self.assertEqual(html.count("<tr>") - 1, len(result.options))

    def test_formatters(self):
        self.assertEqual(sa.format_duration(870), "14h 30m")
        self.assertEqual(sa.format_miles(0), "n/a")
        self.assertEqual(sa.format_taxes(14750, "CAD"), "147.50 CAD")
        self.assertEqual(sa.format_stops(0), "nonstop")
        self.assertEqual(sa.format_stops(2), "2 stops")
        self.assertEqual(sa.format_stops(-1), "?")
        self.assertEqual(sa.format_airlines(["QF", "ZZ"]), "Qantas (QF), ZZ")


# --------------------------------------------------------------------------- FlightPoints cross-check

import crosscheck as cc  # noqa: E402


class CrossCheckParsingTests(unittest.TestCase):
    def test_search_output_yields_program_level_entries(self):
        entries = cc.parse_text((FIXTURES / "flightpoints_search.txt").read_text())
        self.assertEqual(len(entries), 6)
        aa = next(e for e in entries if e.source == "american")
        self.assertEqual((aa.date, aa.route, aa.cabin, aa.miles, aa.flight_numbers), ("2026-12-27", "SIN-HND", "first", 40000, ()))
        qf = [e for e in entries if e.source == "qantas"]
        self.assertEqual(sorted((e.cabin, e.miles) for e in qf), [("business", 73400), ("first", 107800)])
        self.assertEqual(next(e for e in entries if e.source == "united").taxes_usd, 51.0)

    def test_details_output_yields_flight_level_entries(self):
        entries = cc.parse_text((FIXTURES / "flightpoints_details_ac.txt").read_text())
        self.assertEqual([e.flight_numbers for e in entries], [("SQ968", "NH872"), ("SQ634",)])   # "SQ 634" normalised
        self.assertEqual([e.seats for e in entries], [1, 2])
        self.assertTrue(all(e.source == "aeroplan" and e.cabin == "business" and e.miles == 52500 for e in entries))

    def test_normalised_json_is_accepted(self):
        entries = cc.parse_text(json.dumps([{"date": "2026-11-14", "origin": "sin", "destination": "lhr", "cabin": "Biz", "program": "KrisFlyer", "miles": "87,500", "flight_numbers": "SQ 308"}]))
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0].source, entries[0].cabin, entries[0].miles, entries[0].flight_numbers), ("singapore", "business", 87500, ("SQ308",)))

    def test_unrecognised_text_yields_nothing(self):
        self.assertEqual(cc.parse_text("Error: FlightPoints API 400"), [])
        self.assertEqual(cc.parse_text("{not json"), [])

    def test_load_files_walks_directories(self):
        entries, files, empty, _skipped = cc.load_files([FIXTURES])
        self.assertGreaterEqual(files, 3)
        self.assertGreaterEqual(len(entries), 9)
        self.assertEqual(empty, 0)

    def test_explicit_empty_results_are_counted_separately(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "a.txt").write_text("Award Flight Search: SIN → HKG\nDate: 2026-11-06  |  Cabin: Business  |  Passengers: 2\n\nSearch complete. Found 0 award flight option(s).\n\nNo flights found matching your criteria.\n")
        (tmp / "b.txt").write_text("No detailed flight information found.\n")
        (tmp / "c.txt").write_text("Error: FlightPoints API 400 for /search/key/\n")
        entries, files, empty, _skipped = cc.load_files([tmp])
        self.assertEqual((len(entries), files, empty), (0, 3, 2))
        self.assertTrue(cc.is_empty_result("Search complete. Found 0 award flight option(s)."))
        self.assertFalse(cc.is_empty_result("Error: FlightPoints API 400"))


class CrossCheckMatchingTests(unittest.TestCase):
    def make(self, **kw):
        base = dict(program="Air Canada Aeroplan", source="aeroplan", cabin="business", travel_date="2026-12-27", route="SIN-HND",
                    airlines=["SQ"], flight_numbers="SQ968, NH872", departs_at="", arrives_at="", duration_minutes=0, stops=1,
                    remaining_seats=2, mileage_cost=52500, taxes_minor_units=0, taxes_currency="CAD", booking_link="")
        base.update(kw)
        return sa.AwardOption(**base)

    def entries(self):
        return cc.parse_text((FIXTURES / "flightpoints_search.txt").read_text()) + \
               cc.parse_text((FIXTURES / "flightpoints_details_ac.txt").read_text()) + \
               cc.parse_text((FIXTURES / "flightpoints_details_aa.txt").read_text())

    def test_flight_match_outranks_program_match_and_unmatched(self):
        by_flight = self.make()                                                      # SQ968+NH872 exists in AC details
        by_program = self.make(flight_numbers="SQ638", stops=0, mileage_cost=52500)  # aeroplan, same price, unknown flight
        unmatched = self.make(program="United MileagePlus", source="united", flight_numbers="ZH228, ZH651", mileage_cost=95000)
        cheaper_unconfirmed = self.make(program="Virgin Atlantic Flying Club", source="virginatlantic", flight_numbers="VN650, VN516", mileage_cost=50500)
        options = [cheaper_unconfirmed, unmatched, by_program, by_flight]
        summary = cc.match_options(options, self.entries())
        sa.sort_options(options)
        self.assertEqual([o.confirmation for o in options], ["flight", "program", "", ""])
        self.assertEqual(options[2].mileage_cost, 50500)   # unconfirmed rows still sorted by price among themselves
        self.assertEqual((summary.flight_matches, summary.program_matches), (1, 1))
        self.assertEqual(options[0].sources, ["seats.aero", "flightpoints"])
        self.assertEqual(unmatched.sources, ["seats.aero"])
        self.assertIn("seats.aero 95,000 vs FlightPoints 90,000", summary.price_disagreements[0])
        self.assertTrue(any("qantas" in u.lower() or "Frequent Flyer" in u for u in summary.unmatched))

    def test_unmatched_list_is_deduplicated_and_excludes_confirmed_twins(self):
        aa_first = self.make(program="American AAdvantage", source="american", cabin="first", flight_numbers="JL36", stops=0, mileage_cost=40000)
        summary = cc.match_options([aa_first], self.entries())
        self.assertEqual(aa_first.confirmation, "flight")
        self.assertFalse(any("AAdvantage" in u and "first" in u for u in summary.unmatched), summary.unmatched)   # program twin retired
        aeroplan = [u for u in summary.unmatched if "aeroplan" in u.lower()]
        self.assertEqual(len(aeroplan), 1, summary.unmatched)          # detail + summary entries collapse to one line
        self.assertIn("(SQ968, NH872)", aeroplan[0])                     # flight-level detail preferred

    def test_flight_match_prefers_same_program_and_never_compares_prices_across_programs(self):
        # Aeroplan sells SQ968+NH872 at 52,500 (fixture). A United row on the same flights at 90,000 must not be
        # told "FlightPoints quotes 52,500"; it is confirmed as seen, and Aeroplan's entry stays available.
        united = self.make(program="United MileagePlus", source="united", mileage_cost=90000)
        aeroplan = self.make()
        summary = cc.match_options([united, aeroplan], self.entries())
        self.assertEqual(united.confirmation, "flight")
        # The other programme's price is named on the row - retiring the entry is what keeps it out
        # of "FlightPoints also lists", so this is the only place it would otherwise appear - but it
        # is never compared with this row's price, which is what price_disagreements is for.
        self.assertIn("via", united.crosscheck_note)
        self.assertIn("52,500 miles there", united.crosscheck_note)
        self.assertEqual(aeroplan.confirmation, "flight")
        self.assertEqual(aeroplan.crosscheck_note, "")
        self.assertEqual(summary.price_disagreements, [])

    def test_price_disagreement_on_flight_match_is_noted_but_still_confirmed(self):
        o = self.make(mileage_cost=55000)
        cc.match_options([o], self.entries())
        self.assertEqual(o.confirmation, "flight")
        self.assertIn("52,500", o.crosscheck_note)

    def test_different_date_or_cabin_does_not_match(self):
        wrong_date = self.make(travel_date="2026-12-28")
        wrong_cabin = self.make(cabin="first")
        cc.match_options([wrong_date, wrong_cabin], self.entries())
        self.assertEqual((wrong_date.confirmation, wrong_cabin.confirmation), ("", ""))


class CrossCheckRenderingTests(unittest.TestCase):
    def test_sources_column_only_when_cross_check_ran(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query())
        self.assertNotIn("| Sources |", sa.render_markdown(result))
        self.assertNotIn('<th class="src"', sa.render_html(result))
        sa.apply_cross_check(result, [FIXTURES / "flightpoints_search_sin_lhr.txt"])
        md = sa.render_markdown(result)
        self.assertIn("| Sources |", md)
        self.assertIn("Cross-checked with FlightPoints", md)
        self.assertTrue(any("Cross-checked against FlightPoints" in n for n in result.notes))
        html = sa.render_html(result)
        self.assertIn("Confirmed by FlightPoints", html)
        self.assertIn("seats.aero + FlightPoints cross-check", html)
        payload = json.loads(sa.render_json(result))
        self.assertEqual(payload["crosscheck"]["provider"], "flightpoints")

    def test_confirmed_rows_render_badge_and_come_first(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query())
        entries_file = Path(tempfile.mkdtemp()) / "fp.json"
        entries_file.write_text(json.dumps([{"date": "2026-11-14", "origin": "SIN", "destination": "LHR", "cabin": "first", "program": "Frequent Flyer", "miles": 162800, "flight_numbers": "QF1"}]))
        sa.apply_cross_check(result, [entries_file])
        self.assertEqual(result.options[0].cabin, "first")          # the confirmed (more expensive) row now leads
        self.assertEqual(result.options[0].confirmation, "flight")
        html = sa.render_html(result)
        self.assertIn('<tr class="confirmed">', html)
        self.assertIn("✓ 2 sources", html)
        self.assertIn("✓ seats.aero + FlightPoints", sa.render_markdown(result))

    def test_entries_for_other_routes_or_dates_are_ignored(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query())   # SIN-LHR on 2026-11-14
        # The SIN-HND 2026-12-27 fixture must confirm nothing here: it describes a different search.
        sa.apply_cross_check(result, [FIXTURES / "flightpoints_search.txt"])
        self.assertFalse(any(o.confirmed for o in result.options))
        self.assertEqual(result.crosscheck.out_of_scope, 6)
        self.assertEqual(result.crosscheck.unmatched, [])
        note = next(n for n in result.notes if "FlightPoints" in n)
        self.assertIn("describe other routes or dates", note)
        self.assertNotIn("could be read", note)

    def test_in_scope_program_entries_confirm_on_matching_price(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query())
        sa.apply_cross_check(result, [FIXTURES / "flightpoints_search_sin_lhr.txt"])
        confirmed = [o for o in result.options if o.confirmed]
        self.assertEqual({(o.source, o.cabin, o.mileage_cost) for o in confirmed},
                         {("aeroplan", "business", 87500), ("qantas", "first", 162800)})
        self.assertTrue(all(o.confirmation == "program" for o in confirmed))
        self.assertEqual(result.crosscheck.out_of_scope, 0)
        self.assertTrue(result.options[0].confirmed)   # confirmed rows lead

    def test_reapplying_a_cross_check_replaces_the_previous_note(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query())
        sa.apply_cross_check(result, [FIXTURES / "flightpoints_search_sin_lhr.txt"])
        sa.apply_cross_check(result, [FIXTURES / "flightpoints_search_sin_lhr.txt"])
        self.assertEqual(sum(1 for n in result.notes if "Cross-checked against FlightPoints" in n), 1)

    def test_load_restores_the_cross_check_summary(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query())
        sa.apply_cross_check(result, [FIXTURES / "flightpoints_search_sin_lhr.txt"])
        path = Path(tempfile.mkdtemp()) / "run.json"
        path.write_text(sa.render_json(result))
        loaded = sa.load_result(path)
        self.assertIsNotNone(loaded.crosscheck)
        self.assertEqual(loaded.crosscheck.program_matches, result.crosscheck.program_matches)
        html = sa.render_html(loaded)
        self.assertIn("✓ 2 sources", html)
        self.assertIn(">Sources</button></th>", html)   # the badge and green rows are explained
        self.assertIn("Confirmed by FlightPoints", html)

    def test_load_without_a_summary_still_explains_confirmed_rows(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query())
        sa.apply_cross_check(result, [FIXTURES / "flightpoints_search_sin_lhr.txt"])
        payload = json.loads(sa.render_json(result))
        payload.pop("crosscheck")
        path = Path(tempfile.mkdtemp()) / "old.json"
        path.write_text(json.dumps(payload))
        loaded = sa.load_result(path)
        self.assertIsNotNone(loaded.crosscheck)
        self.assertIn(">Sources</button></th>", sa.render_html(loaded))

    def test_unreadable_cross_check_files_add_a_note_not_a_crash(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query())
        broken = Path(tempfile.mkdtemp()) / "broken.txt"; broken.write_text("Error: FlightPoints API 400")
        sa.apply_cross_check(result, [broken])
        self.assertTrue(any("no FlightPoints entries could be read" in n for n in result.notes))
        self.assertFalse(result.crosscheck.answered)

    def test_flightpoints_finding_nothing_is_reported_as_a_real_answer(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query())
        tmp = Path(tempfile.mkdtemp())
        (tmp / "none.txt").write_text("Award Flight Search: SIN → LHR\nDate: 2026-11-14  |  Cabin: Business  |  Passengers: 1\n\nSearch complete. Found 0 award flight option(s).\n\nNo flights found matching your criteria.\n")
        sa.apply_cross_check(result, [tmp])
        self.assertTrue(result.crosscheck.answered)
        note = next(n for n in result.notes if "FlightPoints" in n)
        self.assertIn("reported no business or first space", note)
        self.assertNotIn("could be read", note)
        self.assertEqual(json.loads(sa.render_json(result))["crosscheck"]["empty_files"], 1)


class LoadPreviousRunTests(unittest.TestCase):
    def test_json_round_trip_preserves_rows_and_query(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query(pax=2, refresh=True))
        path = Path(tempfile.mkdtemp()) / "run.json"
        path.write_text(sa.render_json(result))
        loaded = sa.load_result(path)
        self.assertEqual(loaded.query, result.query)
        self.assertEqual([o.trip_id for o in loaded.options], [o.trip_id for o in result.options])
        self.assertEqual(loaded.options[0].mileage_cost, result.options[0].mileage_cost)
        self.assertEqual(loaded.premium_matches, result.premium_matches)
        self.assertEqual(sa.render_markdown(loaded).splitlines()[3:], sa.render_markdown(result).splitlines()[3:])

    def test_main_load_with_cross_check_makes_no_api_calls(self):
        client, _, _ = make_client(default_routes())
        result = sa.run_search(client, query())
        tmp = Path(tempfile.mkdtemp())
        (tmp / "run.json").write_text(sa.render_json(result))
        stdout = io.StringIO()
        def no_network(*a, **k):
            raise AssertionError("network must not be used with --load")
        with mock.patch("urllib.request.urlopen", no_network), mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", io.StringIO()):
            code = sa.main(["--load", str(tmp / "run.json"), "--cross-check", str(FIXTURES / "flightpoints_search.txt"), "--html", str(tmp / "out.html")])
        self.assertEqual(code, 0)
        self.assertIn("| Sources |", stdout.getvalue())
        self.assertTrue((tmp / "out.html").is_file())

    def test_cli_requires_airports_unless_loading(self):
        with self.assertRaises(sa.UsageError):
            sa.parse_args([], today=date(2026, 9, 12))
        with self.assertRaises(sa.UsageError):
            sa.parse_args(["--load", "/nonexistent.json"], today=date(2026, 9, 12))
        with self.assertRaises(sa.UsageError):
            sa.parse_args(["SIN", "LHR", "--cross-check", "/nonexistent.txt"], today=date(2026, 9, 12))


# --------------------------------------------------------------------------- sortable dashboard


class SortableDashboardTests(unittest.TestCase):
    def setUp(self):
        client, _, _ = make_client(default_routes())
        self.result = sa.run_search(client, query(pax=1, destination="LHR,LGW", start_date=date(2026, 11, 11), end_date=date(2026, 11, 17)))
        self.html = sa.render_html(self.result)
        self.columns = sa.html_columns(self.result.query)

    def test_every_column_but_book_is_sortable(self):
        sortable = [c.header for c in self.columns if c.sort is not None]
        self.assertEqual(sortable, [c.header for c in self.columns if c.header != "Book"])
        for index, column in enumerate(self.columns):
            if column.sort is not None:
                # aria-sort only means something on a columnheader, so the th keeps that role
                # and the control is a button inside it.
                self.assertIn(f'data-col="{index}" aria-sort="none"', self.html)
                self.assertIn(f'<button type="button" title="Sort by {column.header}">{column.header}</button>', self.html)
        self.assertNotIn('role="button"', self.html)
        self.assertEqual(self.html.count('aria-sort="none"'), len(sortable))
        self.assertNotIn('title="Sort by Book"', self.html)

    def test_numeric_columns_carry_numeric_sort_keys(self):
        row = self.result.options[0]
        keys = {c.header: c.sort(1, row) for c in self.columns if c.sort}
        self.assertEqual(keys["Miles / pax"], row.mileage_cost)
        self.assertEqual(keys["Duration"], row.duration_minutes)
        self.assertEqual(keys["Seats"], -row.remaining_seats)       # most seats first under ascending
        self.assertEqual(keys["Dep → Arr"], 9 * 60)                 # 09:00 departure
        self.assertEqual(keys["Date"], "2026-11-14")
        self.assertEqual(keys["Cabin"], 1)                           # first (0) sorts above business (1)
        self.assertIn('data-sort="87500"', self.html)
        self.assertIn('data-sort="870"', self.html)

    def test_unknown_values_sort_last_not_first(self):
        unknown = sa.AwardOption(program="X", source="x", cabin="business", travel_date="2026-11-14", route="SIN-LHR",
                                 airlines=[], flight_numbers="-", departs_at="", arrives_at="", duration_minutes=0,
                                 stops=-1, remaining_seats=0, mileage_cost=0, taxes_minor_units=0, taxes_currency="", booking_link="")
        keys = {c.header: c.sort(1, unknown) for c in self.columns if c.sort}
        self.assertGreater(keys["Miles / pax"], 10**8)
        self.assertGreater(keys["Duration"], 10**5)
        self.assertGreater(keys["Dep → Arr"], 10**5)
        self.assertEqual(keys["Seats"], 1)      # after every row with a known count (negative keys)
        self.assertGreater(keys["Updated"], 10**5)

    def test_text_sort_keys_are_escaped_and_lowercased(self):
        evil = sa.AwardOption(program='<b>"x"</b>', source="x", cabin="business", travel_date="2026-11-14", route="SIN-LHR",
                              airlines=[], flight_numbers="SQ1", departs_at="", arrives_at="", duration_minutes=1,
                              stops=0, remaining_seats=1, mileage_cost=1, taxes_minor_units=0, taxes_currency="", booking_link="")
        result = sa.SearchResult(query=self.result.query, options=[evil], notes=[], api_calls=0, availabilities_seen=1, generated_at="")
        html = sa.render_html(result)
        self.assertIn('data-sort="&lt;b&gt;&quot;x&quot;&lt;/b&gt;"', html)
        self.assertNotIn('data-sort="<b>', html)

    def test_script_is_inline_and_table_is_addressable(self):
        self.assertIn('<table id="awards">', self.html)
        self.assertIn("getElementById('awards')", self.html)
        self.assertIn("Click a column heading to sort", self.html)
        self.assertNotIn("<script src", self.html)     # self-contained: no external dependency
        # Keyboard support now comes from the native <button> in each sortable header,
        # so the script no longer needs its own Enter/Space handling.
        self.assertIn('<button type="button" title="Sort by Program">', self.html)
        self.assertNotIn("e.key === 'Enter'", self.html)

    def test_default_order_hint_mentions_cross_check_only_when_present(self):
        self.assertIn("the cheapest first", self.html)
        self.assertNotIn("confirmed by both sources first", self.html)
        sa.apply_cross_check(self.result, [FIXTURES / "flightpoints_search_sin_lhr.txt"])
        self.assertIn("confirmed by both sources first", sa.render_html(self.result))


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
        self.assertIn(">Book</th>", self.html)          # not sortable, so no button
        self.assertNotIn("position:sticky;right:0", self.html)   # table is narrow enough not to need a pinned column

    def test_rows_link_to_program_booking_page_and_airline_site(self):
        self.assertIn('class="book" href="https://www.aircanada.com/aeroplan/redeem/availability/outbound?org0=SIN&amp;dest0=LHR', self.html)
        self.assertIn(">Book</a>", self.html)
        self.assertIn('href="https://www.singaporeair.com/"', self.html)
        self.assertIn(">Singapore Airlines</a>", self.html)        # no "(SQ)" suffix in the dashboard
        self.assertNotIn("Singapore Airlines (SQ)", self.html)

    def test_dashboard_is_narrow_taxes_only_in_markdown(self):
        self.assertNotIn("<th>Taxes / pax</th>", self.html)
        self.assertNotIn("<th>Stops</th>", self.html)
        self.assertIn('<th class="wrap" data-col="1"', self.html)
        self.assertIn('title="Air Canada Aeroplan">Aeroplan<', self.html)
        self.assertIn('<div class="muted">nonstop</div>', self.html)
        self.assertNotIn("min-width:960px", self.html)
        md = sa.render_markdown(self.result)
        self.assertIn("| Taxes / pax |", md)
        self.assertIn("| Stops |", md)
        self.assertIn("147.50 CAD", md)
        self.assertIn('rel="noopener noreferrer"', self.html)

    def test_falls_back_to_program_site_when_api_has_no_deep_link(self):
        # Qantas fixture returns no booking_links
        self.assertIn('class="book book-fallback" href="https://www.qantas.com/', self.html)
        self.assertIn(">Site</a>", self.html)

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
        self.assertIn("4 cached records", html)

    def test_write_report_creates_file_at_default_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = sa.default_report_path(query(pax=2, start_date=date(2026, 11, 11), end_date=date(2026, 11, 17)), Path(tmp))
            self.assertEqual(path.name, "awards_SIN-LHR_2026-11-11_to_2026-11-17_pax2.html")
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
        self.assertEqual(set(sa.PROGRAM_SHORT_NAMES), set(sa.PROGRAM_NAMES))
        self.assertIn("british", sa.PROGRAM_NAMES)   # seen live in September 2026
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


# --------------------------------------------------------------------------- nonstop-only, program-level rows


def summary_only_availability(direct: bool | None, source: str = "aeroplan", **extra) -> dict:
    """One availability record with business space and no trip detail behind it.

    direct=None leaves JDirect out, the way seats.aero omits it on some records.
    """
    record = {
        "ID": f"avail-{source}-{'direct' if direct else 'connecting'}",
        "Date": "2026-11-14",
        "Source": source,
        "Route": {"OriginAirport": "SIN", "DestinationAirport": "LHR"},
        "JAvailable": True,
        "JMileageCost": "60000",
        "JRemainingSeats": 2,
        "JAirlines": "SQ, LH",
        "JTotalTaxes": 5000,
        "TaxesCurrency": "SGD",
        "UpdatedAt": "2026-11-01T00:00:00Z",
    }
    if direct is not None:
        record["JDirect"] = direct
    record.update(extra)
    return record


def run_summary_search(records: list[dict], **overrides):
    """A search that returns program-level rows only, with no trip detail fetched."""
    client, _, _ = make_client({"search": {"data": records, "hasMore": False, "cursor": 0}})
    return sa.run_search(client, query(max_trip_lookups=0, **overrides))


class DirectOnlySummaryRowTests(unittest.TestCase):
    """--direct-only must hold for program-level rows too, not just itineraries with flight numbers."""

    def run_with(self, records: list[dict], **overrides):
        return run_summary_search(records, **overrides)

    def test_connecting_program_row_is_dropped_when_nonstop_only(self):
        result = self.run_with([summary_only_availability(direct=False)], direct_only=True)
        self.assertEqual(result.options, [])
        self.assertEqual(result.premium_matches, 1)
        self.assertIn("nonstop routing", result.explain_no_results())

    def test_direct_program_row_survives_and_reports_nonstop(self):
        result = self.run_with([summary_only_availability(direct=True)], direct_only=True)
        self.assertEqual([(o.detail_level, o.stops) for o in result.options], [("summary", 0)])

    def test_without_the_flag_a_connecting_program_row_is_kept_with_an_unknown_stop_count(self):
        result = self.run_with([summary_only_availability(direct=False)])
        self.assertEqual([(o.detail_level, o.stops) for o in result.options], [("summary", -1)])
        self.assertEqual(sa.format_stops(result.options[0].stops), "?")

    def test_mixed_records_keep_only_the_nonstop_one(self):
        result = self.run_with(
            [summary_only_availability(direct=False, source="united"), summary_only_availability(direct=True)],
            direct_only=True,
        )
        self.assertEqual([o.source for o in result.options], ["aeroplan"])

    def test_a_record_with_no_direct_flag_is_kept_and_marked_unknown(self):
        # seats.aero omits {X}Direct on some records; that is "it did not say", not "connecting".
        result = self.run_with([summary_only_availability(direct=None)], direct_only=True)
        self.assertEqual([(o.detail_level, o.stops) for o in result.options], [("summary", -1)])
        self.assertEqual(sa.format_stops(result.options[0].stops), "?")
        self.assertTrue(any("says nothing either way" in n for n in result.notes))

    def test_nonstop_search_quotes_the_nonstop_price_seats_and_airlines(self):
        record = summary_only_availability(
            direct=True,
            JDirectMileageCost="82000", JDirectRemainingSeats=4, JDirectAirlines="SQ",
        )
        result = self.run_with([record], direct_only=True)
        row = result.options[0]
        self.assertEqual((row.mileage_cost, row.remaining_seats, row.airlines), (82000, 4, ["SQ"]))

    def test_without_the_flag_the_cabin_wide_figures_are_kept(self):
        record = summary_only_availability(
            direct=True,
            JDirectMileageCost="82000", JDirectRemainingSeats=4, JDirectAirlines="SQ",
        )
        result = self.run_with([record])
        row = result.options[0]
        self.assertEqual((row.mileage_cost, row.remaining_seats, row.airlines), (60000, 2, ["SQ", "LH"]))

    def test_party_size_is_judged_on_the_nonstop_seat_count(self):
        # The cabin as a whole has one seat, but its nonstop space has four.
        record = summary_only_availability(direct=True, JRemainingSeats=1, JDirectRemainingSeats=4)
        self.assertEqual(len(self.run_with([record], direct_only=True, pax=2).options), 1)
        self.assertEqual(self.run_with([record], pax=2).options, [])

    def test_the_nonstop_filter_is_explained_in_the_notes(self):
        result = self.run_with([summary_only_availability(direct=True)], direct_only=True)
        self.assertTrue(any("Filtered to nonstop itineraries" in n for n in result.notes))


# --------------------------------------------------------------------------- FlightPoints output captured live


LIVE_FIXTURES = FIXTURES / "live"


class LiveFlightPointsFormatTests(unittest.TestCase):
    """Parsing checked against tool output captured from FlightPoints in September 2026.

    The search capture is verbatim; the details capture keeps 4 of 48 itineraries and the
    CDG-LAX capture keeps 2 of 15 results-table rows, so the files stay a readable size.
    """

    def test_live_search_output_parses_into_premium_entries(self):
        entries = cc.parse_text((LIVE_FIXTURES / "search_jfk_ams_2026-10-15.txt").read_text(encoding="utf-8"))
        premium = [e for e in entries if e.cabin in ("business", "first")]
        self.assertEqual(len(premium), 12)
        self.assertTrue(all(e.date == "2026-10-15" and e.route == "JFK-AMS" for e in premium))
        aeroplan = next(e for e in premium if e.source == "aeroplan")
        self.assertEqual((aeroplan.cabin, aeroplan.miles, aeroplan.taxes_usd), ("business", 58800, 141.0))
        # A program FlightPoints lists that seats.aero does not track stays verbatim and simply never matches.
        self.assertIn("miles&go", {e.source for e in entries})

    def test_live_details_output_yields_flight_numbers_and_ignores_premium_economy(self):
        entries = cc.parse_text((LIVE_FIXTURES / "details_ac_jfk_ams_2026-10-15.txt").read_text(encoding="utf-8"))
        business = [e for e in entries if e.cabin == "business"]
        self.assertEqual([e.flight_numbers for e in business], [("LO27", "LO267"), ("EK206", "LH257", "LH986")])
        self.assertEqual([e.miles for e in business], [75000, 58800])
        self.assertTrue(all(e.source == "aeroplan" for e in business))
        self.assertNotIn("premium", {e.cabin for e in entries})   # "Prem. Eco." lines are out of scope

    def test_live_empty_search_is_an_answer_not_an_unreadable_file(self):
        text = (LIVE_FIXTURES / "search_eze_hkg_2027-05-14_empty.txt").read_text(encoding="utf-8")
        self.assertEqual(cc.parse_text(text), [])
        self.assertTrue(cc.is_empty_result(text))

    def test_flightpoints_display_names_map_onto_seats_aero_programs(self):
        seen_live = {
            "AAdvantage": "american", "Aeroplan": "aeroplan", "Atmos Rewards": "alaska", "Flying Blue": "flyingblue",
            "SkyMiles": "delta", "TrueBlue": "jetblue", "Etihad Guest": "etihad", "Miles & More": "lufthansa",
            "Frequent Flyer": "qantas", "Privilege Club / Avios": "qatar", "MileagePlus": "united",
            "Miles&Smiles": "turkish", "Aeromexico Rewards": "aeromexico", "Smiles": "smiles",
            "Flying Club": "virginatlantic", "Velocity Frequent Flyer": "velocity",
        }
        for label, source in seen_live.items():
            with self.subTest(label=label):
                self.assertEqual(cc.source_for_program(label), source)
                self.assertIn(source, sa.PROGRAM_NAMES)

    def test_velocity_rows_confirm_instead_of_reading_as_missing_from_seats_aero(self):
        entry = cc.parse_text(
            "Award Flight Search: SYD → LAX\nDate: 2026-11-14  |  Cabin: Business\n\n"
            "Premium cabins (points):\n- Velocity Frequent Flyer: Biz 95,000+$289\n"
        )
        self.assertEqual([e.source for e in entry], ["velocity"])
        option = sa.AwardOption(
            program="Virgin Australia Velocity", source="velocity", cabin="business", travel_date="2026-11-14",
            route="SYD-LAX", airlines=["VA"], flight_numbers="see program site", departs_at="", arrives_at="",
            duration_minutes=0, stops=0, remaining_seats=2, mileage_cost=95000, taxes_minor_units=0,
            taxes_currency="AUD", booking_link="",
        )
        summary = cc.match_options([option], entry)
        self.assertEqual((summary.program_matches, summary.unmatched), (1, []))
        self.assertEqual(option.confirmation, "program")


# --------------------------------------------------------------------------- batch matrix


class BatchMatrixTests(unittest.TestCase):
    """A small slice of tests/batch_matrix.py, so the batch harness itself stays working.

    The full sweep (10 routes x 10 months) is run from the command line:
        python3 tests/batch_matrix.py --routes 10 --months 10 --pax 2
    """

    def test_small_matrix_passes_every_invariant(self):
        import batch_matrix

        with tempfile.TemporaryDirectory() as tmp:
            failures, coverage = batch_matrix.run_matrix(
                routes=2, months=2, pax=2, seed="unit-test", workdir=Path(tmp) / "reports")
        self.assertEqual(failures, [])
        self.assertEqual(coverage["scenarios"], 4)
        self.assertGreater(coverage["options"], 0)


# --------------------------------------------------------------------------- review follow-ups


class UnknownFieldTests(unittest.TestCase):
    """seats.aero omits fields on some records; absent must never be read as a convenient default."""

    def trip(self, **overrides) -> dict:
        base = {
            "ID": "trip-x", "Cabin": "business", "MileageCost": 60000, "TotalTaxes": 1000,
            "TaxesCurrency": "USD", "RemainingSeats": 2, "Carriers": "LH, LH",
            "FlightNumbers": "LH405, LH996", "DepartsAt": "2026-11-14T10:00:00Z",
            "ArrivesAt": "2026-11-14T18:00:00Z", "TotalDuration": 480,
            "AvailabilitySegments": [{"Order": 0}, {"Order": 1}],
        }
        base.update(overrides)
        return base

    def availability(self) -> dict:
        return {"ID": "avail-1", "Date": "2026-11-14", "Source": "aeroplan", "JAvailable": True,
                "Route": {"OriginAirport": "SIN", "DestinationAirport": "LHR"}, "UpdatedAt": "2026-11-13T00:00:00Z"}

    def test_a_trip_without_a_stops_field_is_unknown_not_nonstop(self):
        options = sa._trip_options(self.availability(), {"data": [self.trip()]}, query())
        self.assertEqual([o.stops for o in options], [1])            # two segments means one stop
        self.assertEqual(sa.format_stops(options[0].stops), "1 stop")

    def test_a_trip_with_neither_stops_nor_segments_reports_an_unknown_stop_count(self):
        trip = self.trip(AvailabilitySegments=[], FlightNumbers="")
        options = sa._trip_options(self.availability(), {"data": [trip]}, query())
        self.assertEqual([o.stops for o in options], [-1])
        self.assertEqual(sa.format_stops(options[0].stops), "?")

    def test_a_connecting_trip_without_a_stops_field_is_dropped_by_direct_only(self):
        options = sa._trip_options(self.availability(), {"data": [self.trip()]}, query(direct_only=True))
        self.assertEqual(options, [])

    def test_a_nonstop_trip_without_a_stops_field_survives_direct_only(self):
        trip = self.trip(AvailabilitySegments=[{"Order": 0}], FlightNumbers="SQ308", Carriers="SQ")
        options = sa._trip_options(self.availability(), {"data": [trip]}, query(direct_only=True))
        self.assertEqual([o.stops for o in options], [0])

    def test_an_unreadable_timestamp_is_infinitely_old_not_brand_new(self):
        self.assertEqual(sa._hours_since(""), float("inf"))
        self.assertEqual(sa._hours_since("not a date"), float("inf"))
        self.assertEqual(sa.format_age("not a date"), "unknown")

    def test_records_of_unknown_age_are_the_first_ones_refreshed(self):
        records = [
            dict(self.availability(), ID="fresh", UpdatedAt=sa._iso_now() if hasattr(sa, "_iso_now") else "2026-11-13T00:00:00Z"),
            dict(self.availability(), ID="ageless", UpdatedAt=""),
        ]
        routes = {"search": {"data": records, "hasMore": False, "cursor": 0},
                  "refresh": lambda: refresh_response({"ageless": "succeeded", "fresh": "succeeded"}, complete=True),
                  "trips/fresh": {"data": []}, "trips/ageless": {"data": []}}
        client, opener, _ = make_client(routes)
        sa.run_search(client, query(refresh=True, refresh_older_than_hours=1.0))
        posted = json.loads([r for r in opener.bodies if "availability_ids" in r][0])["availability_ids"]
        self.assertEqual(posted[0], "ageless", "a record with no usable timestamp must be refreshed first")

    def test_a_record_without_an_id_cannot_be_refreshed_and_does_not_crash(self):
        records = [{k: v for k, v in self.availability().items() if k != "ID"}]
        routes = {"search": {"data": records, "hasMore": False, "cursor": 0}}
        client, opener, _ = make_client(routes)
        result = sa.run_search(client, query(refresh=True))
        self.assertFalse(any("refresh" in url for url in opener.requests))
        self.assertEqual(result.premium_matches, 1)

    def test_a_published_zero_seat_count_on_nonstop_space_is_not_overwritten(self):
        record = summary_only_availability(direct=True, JRemainingSeats=4, JDirectRemainingSeats=0)
        client, _, _ = make_client({"search": {"data": [record], "hasMore": False, "cursor": 0}})
        result = sa.run_search(client, query(max_trip_lookups=0, direct_only=True, pax=4))
        self.assertEqual([o.remaining_seats for o in result.options], [0])   # "not published", not four
        self.assertTrue(any("does not publish a seat count" in n for n in result.notes))


class QuotaAccountingTests(unittest.TestCase):
    def test_a_refresh_costs_one_call_per_record(self):
        client, _, _ = make_client({"refresh": lambda: refresh_response({"a": "succeeded", "b": "succeeded", "c": "succeeded"}, complete=True)})
        client.refresh_and_wait(["a", "b", "c"])
        self.assertEqual(client.calls_made, 3)

    def test_a_retried_call_is_charged_once_not_once_per_attempt(self):
        """A 429 or 5xx was never served, so re-counting it would report a spent quota that is not."""
        attempts = [http_error("https://seats.aero/partnerapi/refresh", 429, "slow down"),
                    http_error("https://seats.aero/partnerapi/refresh", 503, "busy"),
                    refresh_response({"a": "succeeded"}, complete=True)]
        client, _, _ = make_client({"refresh": lambda: attempts.pop(0)})
        client.refresh_and_wait(["a"])
        self.assertEqual(client.calls_made, 1, "one record refreshed is one call, however many attempts it took")

    def test_the_report_states_the_quota_seats_aero_reported_exactly_once(self):
        """One owner: the refresh note, which both the HTML and the markdown report carry."""
        outcome = sa.RefreshOutcome(requested=2, statuses={"a": "succeeded", "b": "succeeded"}, quota={"limit": 1000, "remaining": 880})
        result = sa.SearchResult(query=query(), options=[], notes=[outcome.summary()], api_calls=4,
                                 availabilities_seen=0, generated_at="2026-09-13T00:00:00+00:00", refresh=outcome)
        html = sa.render_html(result)
        self.assertIn("880/1000", html)
        self.assertEqual(html.count("880/1000"), 1, "the footer must not restate what the note already says")
        self.assertIn("880/1000", sa.render_markdown(result))

    def test_the_report_stays_quiet_when_no_quota_was_reported(self):
        outcome = sa.RefreshOutcome(requested=2, statuses={"a": "succeeded", "b": "succeeded"})
        self.assertNotIn("quota", outcome.summary())

    def test_a_quota_missing_its_remaining_count_is_not_rendered_as_none(self):
        outcome = sa.RefreshOutcome(requested=2, statuses={"a": "succeeded", "b": "succeeded"}, quota={"limit": 1000})
        self.assertNotIn("None", outcome.summary())


class FlexWindowTests(unittest.TestCase):
    def test_flex_never_reaches_back_before_today(self):
        today = date(2026, 9, 13)
        q, _ = sa.parse_args(["SIN", "LHR", "--date", "2026-09-13", "--flex", "3"], today=today)
        self.assertEqual((q.start_date, q.end_date), (today, date(2026, 9, 16)))

    def test_flex_is_symmetric_when_the_whole_window_is_ahead(self):
        q, _ = sa.parse_args(["SIN", "LHR", "--date", "2026-10-13", "--flex", "3"], today=date(2026, 9, 13))
        self.assertEqual((q.start_date, q.end_date), (date(2026, 10, 10), date(2026, 10, 16)))


class CrossCheckAccountingTests(unittest.TestCase):
    def entry(self, **kw):
        base = dict(date="2026-10-15", origin="JFK", destination="AMS", cabin="business", source="united",
                    miles=110000, flight_numbers=("LO27", "LO267"), program_label="MileagePlus")
        base.update(kw)
        return cc.CrossCheckEntry(**base)

    def option(self, **kw):
        base = dict(program="Air Canada Aeroplan", source="aeroplan", cabin="business", travel_date="2026-10-15",
                    route="JFK-AMS", airlines=["LO"], flight_numbers="LO27, LO267", departs_at="", arrives_at="",
                    duration_minutes=0, stops=1, remaining_seats=2, mileage_cost=75000, taxes_minor_units=0,
                    taxes_currency="USD", booking_link="")
        base.update(kw)
        return sa.AwardOption(**base)

    def test_a_match_through_another_program_is_not_also_reported_as_missing(self):
        option = self.option()
        summary = cc.match_options([option], [self.entry()])
        self.assertEqual(summary.flight_matches, 1)
        self.assertEqual(option.confirmation, "flight")
        self.assertIn("also on FlightPoints via MileagePlus", option.crosscheck_note)
        self.assertEqual(summary.unmatched, [], "the entry that confirmed the row is not also missing from seats.aero")

    def test_a_genuinely_extra_flightpoints_option_is_still_reported(self):
        extra = self.entry(source="delta", miles=99000, flight_numbers=("DL40",), program_label="SkyMiles")
        summary = cc.match_options([self.option()], [self.entry(), extra])
        self.assertEqual(len(summary.unmatched), 1)
        self.assertIn("SkyMiles", summary.unmatched[0])


class EmptyFlightPointsResultTests(unittest.TestCase):
    """"FlightPoints found no premium space" must not read as "the file was unreadable"."""

    def files(self, **named: str) -> Path:
        tmp = Path(tempfile.mkdtemp())
        for name, text in named.items():
            (tmp / f"{name}.txt").write_text(text, encoding="utf-8")
        return tmp

    def test_a_search_with_results_but_no_premium_section_counts_as_an_answer(self):
        text = ("Award Flight Search: SIN → HKG\nDate: 2026-11-06  |  Cabin: Business  |  Passengers: 2\n\n"
                "Search complete. Found 14 award flight option(s).\n\n"
                "| # | Program | Airline | Economy | Seats | Stops | Book |\n"
                "| 1 | [AAdvantage](https://x) | American Airlines | 35,000 + $6 | 1 | Direct | [Book](https://x) |\n")
        entries, files, empty, _skipped = cc.load_files([self.files(economy_only=text)])
        self.assertEqual((len(entries), files, empty), (0, 1, 1))
        self.assertTrue(cc.CrossCheckSummary(entries=0, files=1, empty_files=1).answered)

    def test_a_details_result_with_no_business_or_first_line_counts_as_an_answer(self):
        # Premium economy is out of scope, so this file says "nothing for you here", not "unreadable".
        text = ("Found 2 detailed flight option(s).\n\n"
                "1. [AC](https://x): SIN → HKG\n   Departs: 2026-11-06 10:00:00\n"
                "   Prem. Eco.: 85,000 pts + 22.00 taxes  |  2 seat(s)\n")
        entries, files, empty, _skipped = cc.load_files([self.files(economy_details=text)])
        self.assertEqual((len(entries), files, empty), (0, 1, 1))

    def test_a_changed_premium_block_still_reads_as_unreadable(self):
        # The section is there but nothing parses: that is a format change, not "no space".
        text = ("Award Flight Search: SIN → HKG\nDate: 2026-11-06  |  Cabin: Business\n\n"
                "Premium cabins (points):\n- Aeroplan -- business class for 52500 points\n")
        entries, files, empty, _skipped = cc.load_files([self.files(new_format=text)])
        self.assertEqual((len(entries), files, empty), (0, 1, 0))
        self.assertFalse(cc.CrossCheckSummary(entries=0, files=1, empty_files=0).answered)


class ReportHonestyTests(unittest.TestCase):
    """What a row claims must match the figures it actually quotes."""

    def test_an_unreadable_timestamp_does_not_break_the_whole_updated_column(self):
        """A non-numeric sort key drops the column back to sorting its text, for every row."""
        key = sa._age_sort_key("not-a-timestamp")
        self.assertNotEqual(key, float("inf"))
        self.assertRegex(str(key), r"^-?\d+(\.\d+)?$", "the sorter's numeric test must accept it")
        self.assertGreater(key, sa._age_sort_key("2020-01-01T00:00:00Z"), "unknown ages sort last")

    def test_a_cabin_wide_row_never_claims_to_be_nonstop(self):
        """{X}Direct means the cabin has some nonstop space, not that the cheapest space is."""
        record = summary_only_availability(
            direct=True, JMileageCost="60000", JAirlines="SQ, LH",
            JDirectMileageCost="82000", JDirectAirlines="SQ",
        )
        row = run_summary_search([record]).options[0]
        self.assertEqual((row.mileage_cost, row.airlines), (60000, ["SQ", "LH"]))
        self.assertLess(row.stops, 0, "a connecting price must not be rendered as nonstop")

    def test_a_nonstop_row_quotes_nonstop_taxes(self):
        record = summary_only_availability(
            direct=True, JTotalTaxes=45000, JDirectMileageCost="82000", JDirectTotalTaxes=12000,
        )
        row = run_summary_search([record], direct_only=True).options[0]
        self.assertEqual((row.mileage_cost, row.taxes_minor_units), (82000, 12000),
                         "miles and taxes must come from the same itinerary")


class SortScriptBehaviourTests(unittest.TestCase):
    """Run the dashboard's own sorter against a stub DOM, so the comparator is tested, not just its text."""

    HARNESS = """
    function cell(key) { return { hasAttribute: function () { return key !== null; },
                                  getAttribute: function () { return key; } }; }
    function row(keys) { return { cells: keys.map(cell) }; }
    var ROWS = INPUT_ROWS.map(row);
    var order = [];
    var body = { rows: ROWS, appendChild: function (r) { order.push(ROWS.indexOf(r)); } };
    var heads = [{ hasAttribute: function () { return true; }, getAttribute: function () { return '0'; },
                   setAttribute: function () {}, classList: { toggle: function () {} },
                   querySelector: function () { return null; }, addEventListener: function () {} }];
    var document = { getElementById: function () { return { tBodies: [body], tHead: { rows: [{ cells: heads }] } }; } };
    var location = { hash: '' };
    SCRIPT
    heads[0].addEventListener = null;
    console.log(JSON.stringify(order));
    """

    def order_after_sorting(self, keys: list[str]) -> list[int]:
        script = sa.SORT_SCRIPT.replace("})();", "sortBy(0, 'ascending');\n})();")
        harness = self.HARNESS.replace("SCRIPT", script).replace("INPUT_ROWS", json.dumps([[k] for k in keys]))
        out = subprocess.run([shutil.which("node"), "-e", harness], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_a_date_column_really_reorders(self):
        # parseFloat("2026-11-20") is 2026 for every row, which used to leave the order untouched.
        self.assertEqual(self.order_after_sorting(["2026-11-20", "2026-11-03", "2026-11-11"]), [1, 2, 0])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_numeric_columns_still_sort_numerically(self):
        self.assertEqual(self.order_after_sorting(["108400", "87500", "162800"]), [1, 0, 2])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_codes_that_start_with_digits_sort_as_text(self):
        self.assertEqual(self.order_after_sorting(["3u", "aa", "1x"]), [2, 0, 1])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_unknown_values_still_sort_last(self):
        self.assertEqual(self.order_after_sorting(["500", "", "100"]), [2, 0, 1])


# --------------------------------------------------------------------------- preflight


def git(args: list[str], root: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@e", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, env=env, check=False)


def make_checkout(tmp: Path) -> tuple[Path, Path]:
    """An origin repository with one commit, and a clone of it that tracks its branch."""
    origin, clone = tmp / "origin", tmp / "clone"
    origin.mkdir()
    git(["init", "--quiet", "-b", "main", "."], origin)
    (origin / "SKILL.md").write_text("v1\n")
    git(["add", "-A"], origin)
    git(["commit", "--quiet", "-m", "first"], origin)
    subprocess.run(["git", "clone", "--quiet", str(origin), str(clone)], check=True, capture_output=True)
    return origin, clone


def publish(origin: Path, text: str) -> None:
    (origin / "SKILL.md").write_text(text)
    git(["add", "-A"], origin)
    git(["commit", "--quiet", "-m", "newer"], origin)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class PreflightUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin, self.clone = make_checkout(self.tmp)

    def run_update(self, root: Path | None = None) -> pf.Report:
        report = pf.Report()
        pf.update_skill(root or self.clone, report)
        return report

    def step(self, report: pf.Report) -> dict:
        return next(s for s in report.steps if s["step"] == "update")

    def test_a_newer_version_is_pulled_in(self):
        publish(self.origin, "v2\n")
        step = self.step(self.run_update())
        self.assertEqual(step["status"], "updated")
        self.assertEqual(step["commits"], 1)
        self.assertEqual((self.clone / "SKILL.md").read_text(), "v2\n")

    def test_an_up_to_date_checkout_is_left_alone(self):
        self.assertEqual(self.step(self.run_update())["status"], "ok")
        self.assertEqual((self.clone / "SKILL.md").read_text(), "v1\n")

    def test_uncommitted_work_is_never_overwritten(self):
        publish(self.origin, "v2\n")
        (self.clone / "SKILL.md").write_text("mine\n")
        step = self.step(self.run_update())
        self.assertEqual(step["status"], "warning")
        self.assertIn("uncommitted changes", step["detail"])
        self.assertEqual((self.clone / "SKILL.md").read_text(), "mine\n")

    def test_local_commits_are_never_discarded(self):
        publish(self.origin, "v2\n")
        (self.clone / "local.md").write_text("local work\n")
        git(["add", "-A"], self.clone)
        git(["commit", "--quiet", "-m", "local"], self.clone)
        head = git(["rev-parse", "HEAD"], self.clone).stdout
        step = self.step(self.run_update())
        self.assertEqual(step["status"], "warning")
        self.assertIn("its own", step["detail"])
        self.assertEqual(git(["rev-parse", "HEAD"], self.clone).stdout, head)

    def test_an_unreachable_remote_is_a_warning_not_a_failure(self):
        publish(self.origin, "v2\n")
        git(["remote", "set-url", "origin", str(self.tmp / "gone")], self.clone)
        report = self.run_update()
        self.assertEqual(self.step(report)["status"], "warning")
        self.assertEqual(report.exit_code, 0, "a failed update must never stop a search")

    def test_a_plain_directory_is_not_treated_as_a_checkout(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertEqual(self.step(self.run_update(plain))["status"], "skipped")

    def test_offline_does_not_touch_the_network(self):
        publish(self.origin, "v2\n")
        report = pf.Report()
        pf.update_skill(self.clone, report, offline=True)
        self.assertEqual(self.step(report)["status"], "skipped")
        self.assertEqual((self.clone / "SKILL.md").read_text(), "v1\n")


class PreflightFlushTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()) / "award-reports"
        (self.dir / "crosscheck").mkdir(parents=True)

    def write(self, name: str, age_hours: float) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * 100)
        stamp = time.time() - age_hours * 3600
        os.utime(path, (stamp, stamp))
        return path

    def flush(self, **kw) -> pf.Report:
        report = pf.Report()
        pf.flush_stale_data(self.dir, report, kw.get("max_age_hours", pf.DEFAULT_MAX_AGE_HOURS),
                            kw.get("crosscheck_max_age_minutes", pf.DEFAULT_CROSSCHECK_MAX_AGE_MINUTES),
                            kw.get("flush_all", False))
        return report

    def test_stale_reports_and_runs_go_and_fresh_ones_stay(self):
        old_html, old_json = self.write("awards_SIN-LHR_2026-11-14_pax2.html", 30), self.write("run.json", 30)
        fresh = self.write("awards_SIN-HND_2026-12-01_pax2.html", 1)
        self.flush()
        self.assertFalse(old_html.exists())
        self.assertFalse(old_json.exists())
        self.assertTrue(fresh.exists(), "a report from an hour ago is still this session's work")

    def test_cross_check_dumps_expire_within_the_hour(self):
        stale = self.write("crosscheck/search-SIN-HND.txt", 2)
        fresh = self.write("crosscheck/search-JFK-AMS.txt", 0.2)
        self.flush()
        self.assertFalse(stale.exists(), "an old cross-check file describes another search")
        self.assertTrue(fresh.exists())

    def test_flush_all_clears_everything_whatever_its_age(self):
        paths = [self.write("awards_a_2026-11-14_pax2.html", 0.1), self.write("crosscheck/x.txt", 0.1)]
        self.flush(flush_all=True)
        self.assertEqual([p for p in paths if p.exists()], [])

    def test_nothing_outside_the_report_directory_is_touched(self):
        outside = self.dir.parent / "awards_keepme.html"
        outside.write_text("not ours")
        os.utime(outside, (0, 0))
        nested = self.dir / "archive"
        nested.mkdir()
        buried = self.write("archive/awards_old.html", 99)
        self.flush()
        self.assertTrue(outside.exists(), "only the report directory is ever cleared")
        self.assertTrue(buried.exists(), "flush does not recurse into other directories")

    def test_a_missing_report_directory_is_fine(self):
        report = pf.Report()
        pf.flush_stale_data(self.dir.parent / "never-made", report, 12, 60)
        self.assertEqual(report.steps[0]["status"], "ok")
        self.assertEqual(report.exit_code, 0)

    def test_the_summary_counts_what_it_removed(self):
        for n in range(3):
            self.write(f"awards_r{n}_2026-11-14_pax2.html", 40)
        report = self.flush()
        self.assertEqual(report.steps[0]["removed"], 3)
        self.assertIn("cleared 3 stale file(s)", report.steps[0]["detail"])


class PreflightCheckTests(unittest.TestCase):
    def client_factory(self, opener: FakeOpener, **overrides):
        """Hand preflight a client wired to a fake opener, without the patched name recursing.

        Whatever preflight passes (timeout, max_retries) is forwarded, so the tests exercise the
        real call; `overrides` only wins where a test deliberately sets something else.
        """
        real = sa.SeatsAeroClient
        def build(key, **kw):
            return real(key, opener=opener, sleep=lambda _seconds: None, **{**kw, **overrides})
        return build

    def key_file(self, value: str = "test-key") -> Path:
        path = Path(tempfile.mkdtemp()) / "api_key"
        path.write_text(value)
        path.chmod(0o600)
        return path

    def test_a_missing_key_blocks_the_search_with_exit_2(self):
        report = pf.Report()
        with mock.patch.dict(os.environ, {"SEATS_AERO_API_KEY": "", "SEATS_API_KEY": ""}, clear=False), \
             mock.patch.object(sa, "API_KEY_FILE", Path("/nonexistent/api_key")):
            pf.check_api_key(report)
        self.assertEqual(report.exit_code, 2)
        self.assertIn("No seats.aero API key", report.blocked)

    def test_a_key_the_api_rejects_blocks_the_search_with_exit_3(self):
        report = pf.Report()
        opener = FakeOpener({"search": http_error("https://seats.aero/partnerapi/search", 403, "forbidden")})
        with mock.patch.object(sa, "SeatsAeroClient", self.client_factory(opener)):
            pf.check_seats_aero("stale-key", report)
        self.assertEqual(report.exit_code, 3)
        self.assertIn("rejected the API key", report.blocked)

    def test_an_unreachable_api_blocks_the_search_with_exit_3(self):
        report = pf.Report()
        opener = FakeOpener({"search": urllib.error.URLError("no route to host")})
        with mock.patch.object(sa, "SeatsAeroClient", self.client_factory(opener, max_retries=0)):
            pf.check_seats_aero("test-key", report)
        self.assertEqual(report.exit_code, 3)
        self.assertIn("could not reach seats.aero", report.blocked)

    def test_a_working_key_costs_exactly_one_call(self):
        report = pf.Report()
        opener = FakeOpener({"search": {"data": [], "hasMore": False, "cursor": 0}})
        build, clients = self.client_factory(opener), []
        def factory(key, **kw):
            clients.append(build(key, **kw))
            return clients[-1]
        with mock.patch.object(sa, "SeatsAeroClient", factory):
            pf.check_seats_aero("test-key", report)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(clients[0].calls_made, 1)
        self.assertEqual(len(opener.requests), 1)
        self.assertIn("take=1", opener.requests[0])

    def test_python_older_than_the_floor_is_refused(self):
        report = pf.Report()
        with mock.patch.object(sys, "version_info", (3, 8, 0, "final", 0)):
            pf.check_python(report)
        self.assertEqual(report.exit_code, 2)
        self.assertIn("too old", report.blocked)

    def test_connector_and_freshness_reminders_are_always_present(self):
        report = pf.Report()
        pf.note_checks_this_script_cannot_make(report)
        details = " ".join(s["detail"] for s in report.steps)
        self.assertIn("FlightPoints", details)
        self.assertIn("--no-refresh", details)

    def test_the_reminders_are_notes_not_checks_that_passed(self):
        report = pf.Report()
        pf.note_checks_this_script_cannot_make(report)
        self.assertEqual({s["status"] for s in report.steps}, {"note"},
                         "nothing was verified here, so these must not read as passing checks")


class PreflightCommandTests(unittest.TestCase):
    def test_offline_run_reports_ready_without_touching_the_network(self):
        out = io.StringIO()
        key = Path(tempfile.mkdtemp()) / "api_key"
        key.write_text("test-key")
        key.chmod(0o600)
        with mock.patch.object(sa, "API_KEY_FILE", key), mock.patch.dict(os.environ, {"SEATS_AERO_API_KEY": ""}, clear=False), \
             contextlib.redirect_stdout(out):
            code = pf.main(["--offline", "--no-flush", "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["ready"])
        self.assertFalse(payload["verified"], "--offline never put the key to seats.aero")
        self.assertEqual({s["step"] for s in payload["steps"]},
                         {"update", "flush", "python", "api key", "seats.aero", "connectors", "freshness"})

    def test_an_offline_run_says_the_key_was_never_checked(self):
        out = io.StringIO()
        key = Path(tempfile.mkdtemp()) / "api_key"
        key.write_text("test-key")
        key.chmod(0o600)
        with mock.patch.object(sa, "API_KEY_FILE", key), mock.patch.dict(os.environ, {"SEATS_AERO_API_KEY": ""}, clear=False), \
             contextlib.redirect_stdout(out):
            code = pf.main(["--offline", "--no-flush"])
        self.assertEqual(code, 0)
        self.assertIn("never checked against seats.aero", out.getvalue())

    def test_a_blocked_run_says_so_and_exits_nonzero(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"SEATS_AERO_API_KEY": "", "SEATS_API_KEY": ""}, clear=False), \
             mock.patch.object(sa, "API_KEY_FILE", Path("/nonexistent/api_key")), contextlib.redirect_stdout(out):
            code = pf.main(["--offline", "--no-flush"])
        self.assertEqual(code, 2)
        self.assertIn("cannot search:", out.getvalue())


class PreflightSafetyTests(unittest.TestCase):
    """Every case a review found in the preflight; each one stays fixed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_a_skill_vendored_in_another_repo_never_updates_that_repo(self):
        project = self.tmp / "project"
        (project / ".claude" / "skills" / "rss").mkdir(parents=True)
        git(["init", "--quiet", "-b", "main", "."], project)
        (project / "app.py").write_text("the user's own code\n")
        git(["add", "-A"], project)
        git(["commit", "--quiet", "-m", "project"], project)
        head = git(["rev-parse", "HEAD"], project).stdout

        report = pf.Report()
        pf.update_skill(project / ".claude" / "skills" / "rss", report)
        step = next(s for s in report.steps if s["step"] == "update")
        self.assertEqual(step["status"], "skipped")
        self.assertIn("not its own", step["detail"])
        self.assertEqual(git(["rev-parse", "HEAD"], project).stdout, head, "the host repository must not move")
        self.assertEqual((project / "app.py").read_text(), "the user's own code\n")

    def test_a_machine_without_git_reports_a_skipped_update_not_a_traceback(self):
        report = pf.Report()
        with mock.patch.object(pf.subprocess, "run", side_effect=FileNotFoundError("git")):
            pf.update_skill(self.tmp, report)
        step = next(s for s in report.steps if s["step"] == "update")
        self.assertEqual(step["status"], "skipped")
        self.assertIn("git is not installed", step["detail"])
        self.assertEqual(report.exit_code, 0)

    def test_rate_limiting_is_a_warning_because_the_key_is_still_good(self):
        report = pf.Report()
        opener = FakeOpener({"search": http_error("https://seats.aero/partnerapi/search", 429, "slow down", {"Retry-After": "3600"})})
        real = sa.SeatsAeroClient
        sleeps = []
        with mock.patch.object(sa, "SeatsAeroClient", lambda key, **kw: real(key, opener=opener, sleep=sleeps.append, **kw)):
            pf.check_seats_aero("test-key", report)
        step = next(s for s in report.steps if s["step"] == "seats.aero")
        self.assertEqual(step["status"], "warning")
        self.assertEqual(report.exit_code, 0, "a busy API is not an invalid key")
        self.assertEqual(sleeps, [], "the preflight must not wait out a Retry-After of an hour")
        self.assertEqual(len(opener.requests), 1)

    def test_a_server_error_is_a_warning_too(self):
        report = pf.Report()
        opener = FakeOpener({"search": http_error("https://seats.aero/partnerapi/search", 503, "maintenance")})
        real = sa.SeatsAeroClient
        with mock.patch.object(sa, "SeatsAeroClient", lambda key, **kw: real(key, opener=opener, sleep=lambda _s: None, **kw)):
            pf.check_seats_aero("test-key", report)
        self.assertEqual(next(s for s in report.steps if s["step"] == "seats.aero")["status"], "warning")
        self.assertEqual(report.exit_code, 0)

    def test_an_unreadable_response_blocks_rather_than_crashing(self):
        report = pf.Report()
        class Garbage(FakeResponse):
            def read(self):
                return b"<html>not json</html>"
        real = sa.SeatsAeroClient
        with mock.patch.object(sa, "SeatsAeroClient",
                               lambda key, **kw: real(key, opener=lambda req, timeout: Garbage(b""), sleep=lambda _s: None, **kw)):
            pf.check_seats_aero("test-key", report)
        self.assertEqual(report.exit_code, 3)
        self.assertIn("could not read", report.blocked)

    def test_only_the_files_this_skill_writes_are_deleted(self):
        reports = self.tmp / "award-reports"
        reports.mkdir()
        keep = reports / "my-notes.json"           # someone else's file parked in the same folder
        keep.write_text("{}")
        os.utime(keep, (0, 0))
        ours = reports / "awards_SIN-LHR_2026-11-14_pax2.html"
        ours.write_text("<html></html>")
        os.utime(ours, (0, 0))
        run_dump = reports / "run.json"
        run_dump.write_text("{}")
        os.utime(run_dump, (0, 0))

        report = pf.Report()
        pf.flush_stale_data(reports, report, 12, 60, flush_all=True)
        self.assertTrue(keep.exists(), "an unrelated JSON file is not this skill's to delete")
        self.assertFalse(ours.exists())
        self.assertFalse(run_dump.exists())

    def test_the_flush_line_names_the_directory_it_cleared(self):
        reports = self.tmp / "award-reports"
        reports.mkdir()
        stale = reports / "awards_a_2026-11-14_pax2.html"
        stale.write_text("x")
        os.utime(stale, (0, 0))
        report = pf.Report()
        pf.flush_stale_data(reports, report, 12, 60)
        self.assertEqual(report.steps[0]["directory"], str(reports.resolve()))
        self.assertIn(str(reports.resolve()), report.steps[0]["detail"])

    def test_a_symlinked_crosscheck_directory_is_left_alone(self):
        reports = self.tmp / "award-reports"
        reports.mkdir()
        elsewhere = self.tmp / "documents"
        elsewhere.mkdir()
        theirs = elsewhere / "taxes.txt"           # nothing to do with this skill
        theirs.write_text("keep me")
        os.utime(theirs, (0, 0))
        (reports / pf.CROSSCHECK_DIR).symlink_to(elsewhere, target_is_directory=True)

        report = pf.Report()
        pf.flush_stale_data(reports, report, 12, 60, flush_all=True)
        self.assertTrue(theirs.exists(), "a symlinked crosscheck folder must not widen the blast radius")
        self.assertEqual(report.exit_code, 0)

    def test_the_crosscheck_flush_only_takes_the_dumps_this_skill_writes(self):
        crosscheck = self.tmp / "award-reports" / pf.CROSSCHECK_DIR
        crosscheck.mkdir(parents=True)
        theirs = crosscheck / "my-personal-notes.md"
        theirs.write_text("keep me")
        os.utime(theirs, (0, 0))
        ours = crosscheck / "search-SIN-HND-business.txt"
        ours.write_text("FlightPoints output")
        os.utime(ours, (0, 0))

        report = pf.Report()
        pf.flush_stale_data(self.tmp / "award-reports", report, 12, 60)
        self.assertTrue(theirs.exists(), "only .txt dumps in this folder are this skill's")
        self.assertFalse(ours.exists())

    def test_a_file_that_cannot_be_deleted_is_the_users_to_fix_not_a_dead_api(self):
        out = io.StringIO()
        with mock.patch.object(pf, "flush_stale_data", side_effect=PermissionError("[Errno 13] Permission denied")), \
             contextlib.redirect_stdout(out):
            code = pf.main(["--offline", "--json"])
        payload = json.loads(out.getvalue())     # must stay parseable: --json promises JSON
        self.assertEqual(code, 2, "exit 3 would tell the user their key expired")
        self.assertFalse(payload["ready"])
        self.assertIn("Permission denied", payload["blocked"])

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_a_hanging_fetch_is_a_warning_not_a_dead_search(self):
        """A blackholed network makes fetch hang rather than fail; the search must still go ahead."""
        _origin, clone = make_checkout(self.tmp)
        report = pf.Report()
        real = pf.subprocess.run

        def hang(cmd, *args, **kw):
            if "fetch" in cmd:
                raise subprocess.TimeoutExpired(cmd, pf.GIT_TIMEOUT_SECONDS)
            return real(cmd, *args, **kw)

        with mock.patch.object(pf.subprocess, "run", side_effect=hang):
            pf.update_skill(clone, report)
        step = next(s for s in report.steps if s["step"] == "update")
        self.assertEqual(step["status"], "warning")
        self.assertIn("did not answer", step["detail"])
        self.assertEqual(report.exit_code, 0, "a failed update must never stop a search")

    def test_git_is_never_allowed_to_prompt(self):
        seen = {}

        def record(cmd, *args, **kw):
            seen.update(kw)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(pf.subprocess, "run", side_effect=record):
            pf.git(["status"], self.tmp)
        self.assertEqual(seen["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(seen["stdin"], subprocess.DEVNULL, "an unattended run must not wait on a password")

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_a_status_that_fails_is_not_read_as_a_clean_tree(self):
        origin, clone = make_checkout(self.tmp)
        publish(origin, "v2\n")
        real = pf.git

        def fail_status(args, root):
            if args[0] == "status":
                return subprocess.CompletedProcess(args, 128, "", "fatal: unable to read index.lock")
            return real(args, root)

        report = pf.Report()
        with mock.patch.object(pf, "git", side_effect=fail_status):
            pf.update_skill(clone, report)
        step = next(s for s in report.steps if s["step"] == "update")
        self.assertEqual(step["status"], "warning")
        self.assertEqual((clone / "SKILL.md").read_text(), "v1\n", "an unknown tree state must not be merged over")

    def test_unreadable_rev_list_output_warns_instead_of_raising(self):
        self.assertIsNone(pf._two_counts("warning: refname is ambiguous\n0\t2\n"))
        self.assertIsNone(pf._two_counts(""))
        self.assertEqual(pf._two_counts("0\t2\n"), (0, 2))

    def test_the_probe_asks_for_no_retries(self):
        seen = {}
        opener = FakeOpener({"search": {"data": [], "hasMore": False, "cursor": 0}})
        real = sa.SeatsAeroClient

        def factory(key, **kw):
            seen.update(kw)
            return real(key, opener=opener, sleep=lambda _s: None, **kw)

        with mock.patch.object(sa, "SeatsAeroClient", factory):
            pf.check_seats_aero("test-key", pf.Report())
        self.assertEqual(seen["max_retries"], 0, "a preflight must answer now, not wait out a Retry-After")

    def test_the_flush_and_the_cross_check_agree_on_what_a_dump_is(self):
        """Anything a search reads out of crosscheck/ must be something a flush can clear.

        A file on one side only is the bug: read-but-never-flushed lets yesterday's availability
        confirm today's rows forever, and flushed-but-never-read deletes evidence in use.
        """
        crosscheck_dir = self.tmp / "award-reports" / pf.CROSSCHECK_DIR
        crosscheck_dir.mkdir(parents=True)
        outside = self.tmp / "elsewhere.txt"
        outside.write_text("Premium cabins\n")
        for name in ("search-SIN-HND.txt", "SEARCH-SIN-NRT.TXT", "notes.md", "dump.json", "plain"):
            (crosscheck_dir / name).write_text("Premium cabins\n")
        (crosscheck_dir / "linked.txt").symlink_to(outside)

        flushed = {path.name for path in pf._expired(crosscheck_dir, crosscheck.is_dump, time.time(), 0, True)}
        read = {path.name for path in sorted(crosscheck_dir.iterdir()) if crosscheck.is_dump(path)}
        self.assertEqual(flushed, read, "a search must read exactly the set a flush can clear")
        self.assertEqual(read, {"search-SIN-HND.txt", "SEARCH-SIN-NRT.TXT", "dump.json"},
                         "a dump is a .txt or .json in any case, and never a symlink out of the folder")

    def test_files_a_cross_check_directory_skips_are_reported_not_swallowed(self):
        crosscheck_dir = self.tmp / "crosscheck"
        crosscheck_dir.mkdir()
        (crosscheck_dir / "search-SIN-HND.md").write_text("Premium cabins\n")   # neither .txt nor .json
        _entries, files, _empty, skipped = crosscheck.load_files([crosscheck_dir])
        self.assertEqual((files, skipped), (0, 1))

        result = sa.SearchResult(query=query(), options=[], notes=[], api_calls=0, availabilities_seen=0, generated_at="now")
        sa.apply_cross_check(result, [crosscheck_dir])
        self.assertIn("were not read", " ".join(result.notes), "a directory of unread files must say so")

    def test_a_half_read_cross_check_directory_still_says_what_it_skipped(self):
        """The common case: some dumps read, some passed over - previously reported as a clean success."""
        crosscheck_dir = self.tmp / "half"
        crosscheck_dir.mkdir()
        (crosscheck_dir / "search.txt").write_text(
            (FIXTURES / "live" / "search_jfk_ams_2026-10-15.txt").read_text(encoding="utf-8"), encoding="utf-8")
        (crosscheck_dir / "details.md").write_text("Premium cabins\n")      # the wrong extension
        result = sa.SearchResult(query=query(origin="JFK", destination="AMS", start_date=date(2026, 10, 15),
                                             end_date=date(2026, 10, 15)),
                                 options=[], notes=[], api_calls=0, availabilities_seen=0, generated_at="now")
        sa.apply_cross_check(result, [crosscheck_dir])
        notes = " ".join(result.notes)
        self.assertIn("Cross-checked against FlightPoints", notes, "entries were read, so this is a success")
        self.assertIn("were not read", notes, "and it must still say one file was passed over")

    def test_a_file_that_vanishes_mid_flush_is_a_warning_not_a_dead_search(self):
        reports = self.tmp / "award-reports"
        reports.mkdir()
        stale = reports / "awards_SIN-LHR_2026-11-14_pax2.html"
        stale.write_text("x")
        os.utime(stale, (0, 0))
        report = pf.Report()
        with mock.patch.object(Path, "unlink", side_effect=FileNotFoundError("gone")):
            pf.flush_stale_data(reports, report, 12, 60)
        self.assertEqual(report.steps[0]["status"], "warning")
        self.assertEqual(report.exit_code, 0, "another session tidying up must not block this search")

    def test_a_broken_filesystem_keeps_the_steps_already_taken(self):
        out = io.StringIO()
        with mock.patch.object(pf, "check_python", side_effect=PermissionError("[Errno 13] Permission denied")), \
             contextlib.redirect_stdout(out):
            code = pf.main(["--offline", "--no-update", "--no-flush", "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 2)
        self.assertIn("update", [s["step"] for s in payload["steps"]], "earlier steps and their warnings survive")
        self.assertEqual(payload["steps"][-1]["status"], "blocked")

    def test_a_server_error_does_not_blame_offline_for_the_unchecked_key(self):
        report = pf.Report()
        opener = FakeOpener({"search": http_error("https://seats.aero/partnerapi/search", 503, "maintenance")})
        real = sa.SeatsAeroClient
        with mock.patch.object(sa, "SeatsAeroClient", lambda key, **kw: real(key, opener=opener, sleep=lambda _s: None, **kw)):
            pf.check_seats_aero("test-key", report)
        self.assertFalse(report.verified)
        self.assertIn("503", report.unverified)
        self.assertNotIn("--offline", report.to_text())

    def test_the_users_own_ssh_command_survives_batch_mode(self):
        with mock.patch.dict(os.environ, {"GIT_SSH_COMMAND": "ssh -i ~/.ssh/work_key"}, clear=False):
            env = pf.git_env()
        self.assertEqual(env["GIT_SSH_COMMAND"], f"ssh {pf.BATCH_MODE} -i ~/.ssh/work_key",
                         "the identity must survive, unquoted, with batch mode asked for first")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_batch_mode_wins_over_a_batch_mode_the_user_turned_off(self):
        """ssh honours the first value for an option, so ours has to come before theirs."""
        with mock.patch.dict(os.environ, {"GIT_SSH_COMMAND": "ssh -oBatchMode=no"}, clear=False):
            self.assertTrue(pf.git_env()["GIT_SSH_COMMAND"].startswith(f"ssh {pf.BATCH_MODE}"))

    def test_an_ssh_command_we_do_not_know_is_left_alone(self):
        with mock.patch.dict(os.environ, {"GIT_SSH_COMMAND": "plink -batch"}, clear=False):
            self.assertEqual(pf.git_env()["GIT_SSH_COMMAND"], "plink -batch",
                             "an ssh flag handed to a non-OpenSSH client would break a working fetch")

    def test_a_symlinked_cross_check_directory_is_not_read_either(self):
        """Reading through one a flush cannot clear leaves dumps that confirm rows forever."""
        reports = self.tmp / "award-reports"
        (reports / "real").mkdir(parents=True)
        (reports / "real" / "search-SIN-HND.txt").write_text("Premium cabins\nBusiness: 60,000 miles\n")
        (reports / pf.CROSSCHECK_DIR).symlink_to(reports / "real", target_is_directory=True)

        flushed = pf._expired(reports / pf.CROSSCHECK_DIR, crosscheck.is_dump, time.time(), 0, True)
        _entries, files, _empty, skipped = crosscheck.load_files([reports / pf.CROSSCHECK_DIR])
        self.assertEqual((flushed, files), ([], 0), "neither side may go through a symlinked directory")
        self.assertEqual(skipped, 1, "and the search must say it passed something over")

    def test_a_normalised_json_dump_is_read_and_flushed(self):
        """parse_normalised_json exists, so .json dumps must survive the directory filter."""
        crosscheck_dir = self.tmp / "crosscheck"
        crosscheck_dir.mkdir()
        dump = crosscheck_dir / "entries.json"
        dump.write_text('[{"date": "2026-11-14", "route": "SIN-LHR", "cabin": "business", '
                        '"program": "aeroplan", "miles": 87500}]')
        entries, files, _empty, skipped = crosscheck.load_files([crosscheck_dir])
        self.assertEqual((len(entries), files, skipped), (1, 1, 0))
        os.utime(dump, (0, 0))
        report = pf.Report()
        pf.flush_stale_data(self.tmp, report, 12, 60)
        self.assertFalse(dump.exists(), "what a search reads, a flush must be able to clear")

    def test_the_users_own_core_sshcommand_is_never_overridden(self):
        """GIT_SSH_COMMAND beats core.sshCommand, so setting it would drop the identity they configured."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GIT_SSH_COMMAND", None)
            self.assertNotIn("GIT_SSH_COMMAND", pf.git_env())

    def test_a_negative_age_does_not_silently_mean_flush_everything(self):
        for argv in (["--max-age-hours", "-1"], ["--crosscheck-max-age-minutes", "nan"], ["--timeout", "-5"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    pf.parse_args(argv)

    def test_a_file_that_vanishes_before_its_age_is_read_is_not_a_dead_search(self):
        """The listing and the mtime question are two syscalls; another session can act in between."""
        reports = self.tmp / "award-reports"
        reports.mkdir()
        stale = reports / "awards_SIN-LHR_2026-11-14_pax2.html"
        stale.write_text("x")
        os.utime(stale, (0, 0))
        real_stat = Path.stat

        def vanish(self_path, *a, **kw):
            if self_path.name.startswith("awards_"):
                raise FileNotFoundError("gone")
            return real_stat(self_path, *a, **kw)

        report = pf.Report()
        with mock.patch.object(Path, "stat", vanish):
            pf.flush_stale_data(reports, report, 12, 60)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.steps[0]["status"], "ok")

    def test_a_key_file_in_an_encoding_we_cannot_read_still_reports_json(self):
        """The contract is no tracebacks and always JSON, whatever the exception type."""
        out = io.StringIO()
        key = Path(tempfile.mkdtemp()) / "api_key"
        key.write_bytes("test-key".encode("utf-16"))      # a Windows editor's default
        key.chmod(0o600)
        with mock.patch.object(sa, "API_KEY_FILE", key), \
             mock.patch.dict(os.environ, {"SEATS_AERO_API_KEY": "", "SEATS_API_KEY": ""}, clear=False), \
             contextlib.redirect_stdout(out):
            code = pf.main(["--offline", "--no-update", "--no-flush", "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 2)
        self.assertFalse(payload["ready"])

    def test_rate_limiting_does_not_count_as_proof_the_key_is_good(self):
        """A 429 can come from an edge limiter that never looked at the key."""
        report = pf.Report()
        opener = FakeOpener({"search": http_error("https://seats.aero/partnerapi/search", 429, "slow down")})
        real = sa.SeatsAeroClient
        with mock.patch.object(sa, "SeatsAeroClient", lambda key, **kw: real(key, opener=opener, sleep=lambda _s: None, **kw)):
            pf.check_seats_aero("revoked-key", report)
        self.assertEqual(report.exit_code, 0, "a busy API is not a refused key either")
        self.assertFalse(report.verified, "429 says nothing about whether the key still works")
        self.assertIn("429", report.unverified)

    def test_a_saved_run_named_per_route_is_still_flushed(self):
        reports = self.tmp / "award-reports"
        reports.mkdir()
        ours = [reports / "run.json", reports / "run-SIN-HND.json", reports / "run_SIN_NRT.json"]
        theirs = reports / "my-notes.json"
        for path in [*ours, theirs]:
            path.write_text("{}")
            os.utime(path, (0, 0))
        report = pf.Report()
        pf.flush_stale_data(reports, report, 12, 60)
        self.assertTrue(theirs.exists(), "an unrelated JSON file is not this skill's to delete")
        for path in ours:
            self.assertFalse(path.exists(), f"{path.name} would be re-rendered later as if it were current")

    def test_report_patterns_track_the_name_the_search_tool_writes(self):
        written = sa.default_report_path(query(origin="SIN", destination="LHR")).name
        self.assertTrue(any(fnmatch.fnmatch(written, pattern) for pattern in pf.REPORT_PATTERNS),
                        f"{written} matches none of {pf.REPORT_PATTERNS}, so stale reports would survive")


if __name__ == "__main__":
    unittest.main()
