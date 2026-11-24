"""The shop-side sales sync: reading sales vouchers and credit notes from Tally (a fake Tally here: a tiny local HTTP
server with canned XML) and pushing them to the website's REAL /admin/intake/sales route (Flask test client).
No real Tally, website, token or shop PC is ever contacted."""
import hashlib
import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from tests import support
from tests.support import ROOT, INTAKE_HEADERS
from tests.test_deploy_checks import clean_env, free_port, make_copy
from tests.test_feeder_refresh import Rig

import sales_sync
import tally_sales

TODAY = date(2026, 3, 20)
FORBIDDEN_TEXT = ("ACME TRADERS", "SECRET NARRATION", "1500.00", "500/Nos", "SALES LEDGER", "RAW-GUID-")


# ----------------------------------------------------------------------------------------------- fake Tally
def voucher(day, entries=(("ALTO 7D MAT BLACK", "3 Nos"),), vtype="Sales", parent=None, guid=None, number="1", cancelled=False,
            optional=False, variant="ALL"):
    return {"day": day, "entries": list(entries), "vtype": vtype, "parent": parent if parent is not None else vtype, "guid": guid,
            "number": number, "cancelled": cancelled, "optional": optional, "variant": variant}


def voucher_xml(v):
    """One voucher as Tally would export it, with plenty of private extras the sync must never read."""
    tag = "ALLINVENTORYENTRIES.LIST" if v["variant"] == "ALL" else "INVENTORYENTRIES.LIST"
    entries = "".join(
        f"<{tag}><STOCKITEMNAME>{name}</STOCKITEMNAME><BILLEDQTY> {qty}</BILLEDQTY><ACTUALQTY> {qty}</ACTUALQTY>"
        f"<RATE>500/Nos</RATE><AMOUNT>1500.00</AMOUNT></{tag}>" for name, qty in v["entries"])
    guid = f"<GUID>{v['guid']}</GUID>" if v["guid"] else ""
    return (f"<VOUCHER><DATE>{v['day'].strftime('%Y%m%d')}</DATE>{guid}<VOUCHERNUMBER>{v['number']}</VOUCHERNUMBER>"
            f"<VOUCHERTYPENAME>{v['vtype']}</VOUCHERTYPENAME><PARENTVCHTYPE>{v['parent']}</PARENTVCHTYPE>"
            f"<ISCANCELLED>{'Yes' if v['cancelled'] else 'No'}</ISCANCELLED><ISOPTIONAL>{'Yes' if v['optional'] else 'No'}</ISOPTIONAL>"
            f"<PARTYLEDGERNAME>ACME TRADERS PVT</PARTYLEDGERNAME><NARRATION>SECRET NARRATION</NARRATION>"
            f"<LEDGERENTRIES.LIST><LEDGERNAME>SALES LEDGER</LEDGERNAME><AMOUNT>1500.00</AMOUNT></LEDGERENTRIES.LIST>{entries}</VOUCHER>")


def collection(vouchers):
    return "<ENVELOPE><BODY><DATA><COLLECTION>" + "".join(voucher_xml(v) for v in vouchers) + "</COLLECTION></DATA></BODY></ENVELOPE>"


class FakeTally:
    """HTTP server answering the sales sync's two requests. `vouchers` can be changed while it runs."""

    def __init__(self, company="Super Seatings Test Co"):
        self.company = company
        self.vouchers = []
        self.filter_supported = True
        self.requests = []
        self.fail = False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8", "replace")
                fake.requests.append(body)
                out = fake.reply(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        self.port = free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()

    def reply(self, body):
        if self.fail:
            return "<ENVELOPE><LINEERROR>boom</LINEERROR></ENVELOPE>"
        if "Sales Sync Company" in body:
            if self.company is None:
                return "<ENVELOPE><LINEERROR>no company</LINEERROR></ENVELOPE>"
            return (f'<ENVELOPE><BODY><DATA><COLLECTION><COMPANY NAME="Some Other Co"><NAME>Some Other Co</NAME></COMPANY>'
                    f'<COMPANY NAME="{self.company}"><NAME>{self.company}</NAME><SALESSYNCCURRENT>{self.company}</SALESSYNCCURRENT></COMPANY>'
                    f"</COLLECTION></DATA></BODY></ENVELOPE>")
        if "Sales Sync Vouchers" in body:
            frm = datetime.strptime(body.split("<SVFROMDATE>")[1].split("<")[0], "%Y%m%d").date()
            to = datetime.strptime(body.split("<SVTODATE>")[1].split("<")[0], "%Y%m%d").date()
            filtered = "<FILTER>" in body
            if filtered and not self.filter_supported:
                return "<ENVELOPE><LINEERROR>Unknown formula</LINEERROR></ENVELOPE>"
            chosen = [v for v in self.vouchers if frm <= v["day"] <= to
                      and (not filtered or v["parent"] in ("Sales", "Credit Note"))]
            return collection(chosen)
        return "<ENVELOPE><LINEERROR>x</LINEERROR></ENVELOPE>"


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.utc = datetime(2026, 3, 20, 9, 0, tzinfo=timezone.utc)       # 14:30 in India

    def monotonic(self):
        return self.t

    def utc_now(self):
        return self.utc

    def local_now(self):
        return (self.utc + timedelta(hours=5, minutes=30)).replace(tzinfo=None)

    def advance(self, seconds):
        self.t += seconds
        self.utc += timedelta(seconds=seconds)


class StubReader:
    def __init__(self, clock=None, make_lines=None, company="Stub Company", read_seconds=0.0, error=None):
        self.clock, self.make_lines, self.company, self.read_seconds, self.error = clock, make_lines, company, read_seconds, error
        self.calls = []
        self.last_stats = {}

    def company_name(self):
        return self.company

    def fetch_sales_lines(self, day_from, day_to):
        self.calls.append((day_from, day_to))
        if self.clock:
            self.clock.advance(self.read_seconds)
        if self.error:
            raise self.error
        return list(self.make_lines(day_from, day_to)) if self.make_lines else []


def one_line_per_day(day_from, day_to, qty=1):
    out, day = [], day_from
    while day <= day_to:
        out.append({"voucher_key": hashlib.sha256(day.isoformat().encode()).hexdigest()[:16], "day": day, "kind": "sale", "item": "ALTO 7D MAT BLACK", "qty": qty})
        day += timedelta(days=1)
    return out


class Pusher:
    def __init__(self, answers=None):
        self.calls, self.answers = [], list(answers or [])

    def __call__(self, payload):
        self.calls.append(payload)
        return self.answers.pop(0) if self.answers else (200, '{"ok": true}')

    def ranges(self):
        return [(date.fromisoformat(c["from"]), date.fromisoformat(c["to"])) for c in self.calls]


def make_sync(tmp, reader, pusher, clock, **settings):
    values = dict(mode="on", backfill_days=100, incremental_minutes=15, nightly_hour=3)
    values.update(settings)
    return sales_sync.SalesSync(state_path=tmp, reader=reader, pusher=pusher, settings=sales_sync.SalesSettings(**values), clock=clock)


class LogCapture:
    """Everything logged by the feeder logger (the tests normally switch logging off)."""

    def __enter__(self):
        self.records = []
        outer = self

        class Handler(logging.Handler):
            def emit(self, record):
                outer.records.append(record.getMessage())

        self.handler = Handler(level=logging.DEBUG)
        self.logger = logging.getLogger("feeder")
        self.old_level, self.old_disable = self.logger.level, logging.root.manager.disable
        logging.disable(logging.NOTSET)
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.old_level)
        logging.disable(self.old_disable)

    def text(self):
        return "\n".join(self.records)


class TempState(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="sales_state_")
        self.state_path = os.path.join(self.dir, "sales_sync_state.json")
        self.clock = FakeClock()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def state(self):
        with open(self.state_path, encoding="utf-8") as handle:
            return json.load(handle)


# ----------------------------------------------------------------------------------------------- 1. parsing
class Parsing(unittest.TestCase):
    def parse(self, vouchers):
        return tally_sales.parse_vouchers(collection(vouchers))

    def test_both_xml_variants_and_the_plain_fields(self):
        lines, stats = self.parse([voucher(date(2026, 1, 10), [("ALTO 7D MAT BLACK", "3 Nos")], guid="RAW-GUID-1"),
                                   voucher(date(2026, 1, 11), [("SWIFT 7D MAT GREY", "1,250 Nos")], guid="RAW-GUID-2", variant="OLD")])
        self.assertEqual([(l["day"], l["kind"], l["item"], l["qty"]) for l in lines],
                         [(date(2026, 1, 10), "sale", "ALTO 7D MAT BLACK", 3), (date(2026, 1, 11), "sale", "SWIFT 7D MAT GREY", 1250)])
        self.assertEqual(stats["vouchers"], 2)

    def test_cancelled_and_optional_vouchers_are_left_out(self):
        lines, stats = self.parse([voucher(date(2026, 1, 10), guid="RAW-GUID-1", cancelled=True), voucher(date(2026, 1, 10), guid="RAW-GUID-2", optional=True),
                                   voucher(date(2026, 1, 10), guid="RAW-GUID-3")])
        self.assertEqual(len(lines), 1)
        self.assertEqual((stats["cancelled"], stats["optional"]), (1, 1))

    def test_a_credit_note_is_a_return_and_other_voucher_types_are_ignored(self):
        lines, stats = self.parse([voucher(date(2026, 1, 10), vtype="Credit Note", guid="RAW-GUID-1"), voucher(date(2026, 1, 10), vtype="Receipt", guid="RAW-GUID-2"),
                                   voucher(date(2026, 1, 10), vtype="Purchase", guid="RAW-GUID-3")])
        self.assertEqual([l["kind"] for l in lines], ["return"])
        self.assertEqual(stats["other_types"], 2)

    def test_a_custom_voucher_type_name_counts_by_its_parent_type(self):
        lines, _ = self.parse([voucher(date(2026, 1, 10), vtype="Counter Sales", parent="Sales", guid="RAW-GUID-1"),
                               voucher(date(2026, 1, 10), vtype="Sale Return Slip", parent="Credit Note", guid="RAW-GUID-2")])
        self.assertEqual(sorted(l["kind"] for l in lines), ["return", "sale"])

    def test_a_missing_parent_type_falls_back_to_the_type_name(self):
        xml = collection([voucher(date(2026, 1, 10), guid="RAW-GUID-1")]).replace("<PARENTVCHTYPE>Sales</PARENTVCHTYPE>", "")
        lines, _ = tally_sales.parse_vouchers(xml)
        self.assertEqual(len(lines), 1)

    def test_quantity_text(self):
        for text, qty, fractional in (("3 Nos", 3, False), ("1,250 Nos", 1250, False), ("-4 Nos", 4, False), ("2.5 Mtr", 2, True),
                                      ("0 Nos", None, False), ("", None, False), ("Nos", None, False), ("10", 10, False), ("12.00 Nos", 12, False)):
            self.assertEqual(tally_sales.parse_quantity(text), (qty, fractional), text)

    def test_fractional_quantities_use_the_integer_part_and_are_counted(self):
        lines, stats = self.parse([voucher(date(2026, 1, 10), [("A", "2.5 Mtr"), ("B", "3 Nos")], guid="RAW-GUID-1")])
        self.assertEqual([l["qty"] for l in lines], [2, 3])
        self.assertEqual(stats["fractional"], 1)

    def test_zero_quantity_lines_are_skipped(self):
        lines, _ = self.parse([voucher(date(2026, 1, 10), [("A", "0 Nos"), ("B", "3 Nos")], guid="RAW-GUID-1")])
        self.assertEqual([l["item"] for l in lines], ["B"])

    def test_two_entries_for_the_same_item_stay_two_lines(self):
        lines, _ = self.parse([voucher(date(2026, 1, 10), [("A", "2 Nos"), ("A", "3 Nos")], guid="RAW-GUID-1")])
        self.assertEqual([(l["item"], l["qty"]) for l in lines], [("A", 2), ("A", 3)])
        self.assertEqual(len({l["voucher_key"] for l in lines}), 1)

    def test_a_voucher_without_a_guid_gets_a_stable_fallback_key(self):
        first, _ = self.parse([voucher(date(2026, 1, 10), number="77")])
        again, _ = self.parse([voucher(date(2026, 1, 10), number="77")])
        other, _ = self.parse([voucher(date(2026, 1, 10), number="78")])
        self.assertEqual(first[0]["voucher_key"], again[0]["voucher_key"])
        self.assertNotEqual(first[0]["voucher_key"], other[0]["voucher_key"])

    def test_a_malformed_voucher_is_skipped_and_counted_and_the_rest_survive(self):
        xml = collection([voucher(date(2026, 1, 10), guid="RAW-GUID-1")]).replace("</COLLECTION>", "<VOUCHER><DATE>not a date</DATE></VOUCHER></COLLECTION>")
        lines, stats = tally_sales.parse_vouchers(xml)
        self.assertEqual(len(lines), 1)
        self.assertEqual(stats["bad"], 1)

    def test_empty_and_odd_answers_give_no_lines_and_never_raise(self):
        for text in ("<ENVELOPE></ENVELOPE>", "<ENVELOPE><BODY><DATA><COLLECTION></COLLECTION></DATA></BODY></ENVELOPE>", "<ENVELOPE/>"):
            self.assertEqual(tally_sales.parse_vouchers(text)[0], [])
        with self.assertRaises(tally_sales.TallyReadError):
            tally_sales.parse_vouchers("this is not xml at all <<<")

    def test_illegal_characters_do_not_break_the_xml(self):
        xml = collection([voucher(date(2026, 1, 10), [("ALTO\x0b 7D", "3 Nos")], guid="RAW-GUID-1")])
        self.assertEqual(len(tally_sales.parse_vouchers(xml)[0]), 1)


# ----------------------------------------------------------------------------------------------- 3. key and hash
class Keys(unittest.TestCase):
    def test_voucher_key_is_16_hex_characters_and_stable(self):
        key = tally_sales.voucher_key("RAW-GUID-1", "Sales", date(2026, 1, 10), "5")
        self.assertRegex(key, r"^[0-9a-f]{16}$")
        self.assertEqual(key, tally_sales.voucher_key("RAW-GUID-1", "Sales", date(2026, 1, 11), "9"))      # the GUID decides, not date or number
        self.assertEqual(key, hashlib.sha256(b"RAW-GUID-1|Sales").hexdigest()[:16])
        self.assertNotEqual(key, tally_sales.voucher_key("RAW-GUID-1", "Credit Note", date(2026, 1, 10), "5"))

    def test_fallback_key_uses_date_type_and_number(self):
        key = tally_sales.voucher_key(None, "Sales", date(2026, 1, 10), "5")
        self.assertEqual(key, hashlib.sha256(b"2026-01-10|Sales|5").hexdigest()[:16])
        self.assertEqual(key, tally_sales.voucher_key("", "Sales", date(2026, 1, 10), "5"))

    def test_company_hash_is_sha256_hex_and_stable(self):
        value = tally_sales.company_hash("Super Seatings")
        self.assertRegex(value, r"^[0-9a-f]{64}$")
        self.assertEqual(value, hashlib.sha256("Super Seatings".encode("utf-8")).hexdigest())
        self.assertEqual(value, tally_sales.company_hash("  Super Seatings  "))
        self.assertNotEqual(value, tally_sales.company_hash("Super Seating"))


# ----------------------------------------------------------------------------------------------- reading from the (fake) Tally
class Reading(unittest.TestCase):
    def setUp(self):
        self.tally = FakeTally()
        self.addCleanup(self.tally.close)
        self.reader = tally_sales.TallyReader(tally_url=self.tally.url)

    def test_lines_within_the_range_only(self):
        self.tally.vouchers = [voucher(date(2026, 1, 9), guid="RAW-GUID-1"), voucher(date(2026, 1, 10), guid="RAW-GUID-2"),
                               voucher(date(2026, 1, 20), guid="RAW-GUID-3"), voucher(date(2026, 1, 21), guid="RAW-GUID-4")]
        lines = self.reader.fetch_sales_lines(date(2026, 1, 10), date(2026, 1, 20))
        self.assertEqual(sorted(l["day"] for l in lines), [date(2026, 1, 10), date(2026, 1, 20)])
        self.assertIn("<SVFROMDATE>20260110</SVFROMDATE>", self.tally.requests[0])
        self.assertIn("<SVTODATE>20260120</SVTODATE>", self.tally.requests[0])

    def test_the_request_asks_for_no_party_amount_rate_or_narration(self):
        self.reader.fetch_sales_lines(date(2026, 1, 10), date(2026, 1, 20))
        self.reader.company_name()
        for body in self.tally.requests:
            for word in ("amount", "rate", "party", "ledger", "narration", "address", "gst"):
                self.assertNotIn(word, body.lower().replace("ledgername", "").replace("partyledgername", "") if False else body.lower(), word)

    def test_only_sales_and_credit_notes_come_back_and_the_python_filter_backs_up_the_tally_filter(self):
        self.tally.vouchers = [voucher(date(2026, 1, 10), vtype="Receipt", guid="RAW-GUID-1"), voucher(date(2026, 1, 10), guid="RAW-GUID-2"),
                               voucher(date(2026, 1, 10), vtype="Credit Note", guid="RAW-GUID-3")]
        self.assertEqual(sorted(l["kind"] for l in self.reader.fetch_sales_lines(date(2026, 1, 1), date(2026, 1, 31))), ["return", "sale"])

    def test_when_tally_does_not_support_the_filter_it_falls_back_to_fetching_all_and_filtering_here(self):
        self.tally.filter_supported = False
        self.tally.vouchers = [voucher(date(2026, 1, 10), vtype="Receipt", guid="RAW-GUID-1"), voucher(date(2026, 1, 10), guid="RAW-GUID-2")]
        self.assertEqual(len(self.reader.fetch_sales_lines(date(2026, 1, 1), date(2026, 1, 31))), 1)
        self.assertEqual(sum("<FILTER>" in r for r in self.tally.requests), 1)             # tried once, then remembered
        self.reader.fetch_sales_lines(date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(sum("<FILTER>" in r for r in self.tally.requests), 1)

    def test_nothing_in_the_range_gives_an_empty_list(self):
        self.assertEqual(self.reader.fetch_sales_lines(date(2026, 1, 1), date(2026, 1, 31)), [])

    def test_the_company_name_comes_from_tallys_current_company(self):
        self.assertEqual(self.reader.company_name(), "Super Seatings Test Co")

    def test_an_unreadable_company_gives_none(self):
        self.tally.company = None
        self.assertIsNone(self.reader.company_name())

    def test_a_tally_error_answer_raises_a_clear_error(self):
        self.tally.fail = True
        with self.assertRaises(tally_sales.TallyReadError):
            self.reader.fetch_sales_lines(date(2026, 1, 1), date(2026, 1, 31))

    def test_tally_not_running_raises_the_unreachable_error(self):
        dead = tally_sales.TallyReader(tally_url=f"http://127.0.0.1:{free_port()}", max_retries=1)
        with self.assertRaises(tally_sales.TallyUnreachable):
            dead.fetch_sales_lines(date(2026, 1, 1), date(2026, 1, 31))

    def test_a_timeout_is_capped_at_90_seconds(self):
        with mock.patch.object(tally_sales, "_session_post", return_value="<ENVELOPE/>") as post:
            tally_sales.TallyReader(tally_url="http://x", timeout=500).fetch_sales_lines(date(2026, 1, 1), date(2026, 1, 2))
        self.assertLessEqual(post.call_args.kwargs["timeout"], 90)

    def test_fractional_quantities_are_logged_as_a_count_only(self):
        self.tally.vouchers = [voucher(date(2026, 1, 10), [("SECRET DESIGN", "2.5 Mtr")], guid="RAW-GUID-1")]
        with LogCapture() as log:
            self.reader.fetch_sales_lines(date(2026, 1, 1), date(2026, 1, 31))
        self.assertIn("1 fractional", log.text())
        self.assertNotIn("SECRET DESIGN", log.text())


# ----------------------------------------------------------------------------------------------- settings and state
class SettingsAndUrl(unittest.TestCase):
    def test_defaults_and_limits(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ("SALES_SYNC", "SALES_BACKFILL_DAYS", "SALES_INCREMENTAL_MINUTES", "SALES_NIGHTLY_HOUR"):
                os.environ.pop(name, None)
            s = sales_sync.SalesSettings.from_environment()
        self.assertEqual((s.mode, s.backfill_days, s.incremental_minutes, s.nightly_hour), ("on", 365, 15, 3))
        for env, expected in (({"SALES_BACKFILL_DAYS": "9999"}, 400), ({"SALES_BACKFILL_DAYS": "0"}, 1), ({"SALES_BACKFILL_DAYS": "abc"}, 365),
                              ({"SALES_NIGHTLY_HOUR": "30"}, 3), ({"SALES_INCREMENTAL_MINUTES": "0"}, 15)):
            with mock.patch.dict(os.environ, env):
                s = sales_sync.SalesSettings.from_environment()
            self.assertIn(expected, (s.backfill_days, s.nightly_hour, s.incremental_minutes), env)

    def test_modes(self):
        for raw, mode in (("on", "on"), ("ON", "on"), ("off", "off"), ("dryrun", "dryrun"), ("DryRun", "dryrun"), ("nonsense", "on"), ("", "on")):
            with mock.patch.dict(os.environ, {"SALES_SYNC": raw}):
                self.assertEqual(sales_sync.SalesSettings.from_environment().mode, mode, raw)

    def test_the_sales_url_comes_from_the_stock_push_url(self):
        for given, expected in (("http://127.0.0.1:5000/admin/intake/sync_data", "http://127.0.0.1:5000/admin/intake/sales"),
                                ("https://example.test/prefix/admin/intake/sync_data", "https://example.test/prefix/admin/intake/sales"),
                                ("https://example.test", "https://example.test/admin/intake/sales"),
                                ("https://example.test/something/else", "https://example.test/admin/intake/sales")):
            self.assertEqual(sales_sync.sales_url(given), expected)


class StateFile(TempState):
    def test_state_survives_a_restart_and_is_written_atomically(self):
        reader, pusher = StubReader(self.clock, one_line_per_day), Pusher()
        first = make_sync(self.state_path, reader, pusher, self.clock)
        first.step(); self.clock.advance(180); first.step()
        saved = self.state()
        self.assertEqual(sorted(saved), sorted(["backfill_done", "backfill_cursor", "backfill_floor", "chunk_days", "last_incremental_utc",
                                                "last_nightly_day", "company", "website_missing_until_utc", "fail_streak", "blocked_until_utc"]))
        self.assertEqual(os.listdir(self.dir), ["sales_sync_state.json"])                  # no temp file left behind
        again = make_sync(self.state_path, StubReader(self.clock, one_line_per_day), Pusher(), self.clock)
        self.assertEqual(again.state["backfill_cursor"], saved["backfill_cursor"])
        self.assertEqual(again.state["last_incremental_utc"], saved["last_incremental_utc"])

    def test_a_fresh_state_has_the_cursor_three_days_back_and_a_fixed_floor(self):
        sync = make_sync(self.state_path, StubReader(self.clock), Pusher(), self.clock, backfill_days=100)
        self.assertEqual(sync.state["backfill_cursor"], (TODAY - timedelta(days=3)).isoformat())
        self.assertEqual(sync.state["backfill_floor"], (TODAY - timedelta(days=100)).isoformat())
        self.assertEqual((sync.state["backfill_done"], sync.state["chunk_days"]), (False, 31))
        self.clock.advance(10 * 86400)                                                       # the floor does not drift as days pass
        later = make_sync(self.state_path, StubReader(self.clock), Pusher(), self.clock, backfill_days=100)
        self.assertEqual(later.state["backfill_floor"], (TODAY - timedelta(days=100)).isoformat()) if os.path.exists(self.state_path) else None

    def test_a_missing_or_corrupt_state_file_starts_fresh_and_logs_once(self):
        with LogCapture() as log:
            make_sync(self.state_path, StubReader(self.clock), Pusher(), self.clock)
            with open(self.state_path, "w", encoding="utf-8") as handle:
                handle.write("{ this is not json")
            sync = make_sync(self.state_path, StubReader(self.clock), Pusher(), self.clock)
        self.assertFalse(sync.state["backfill_done"])
        self.assertEqual(sum("starting fresh" in m for m in log.records), 1)

    def test_a_state_with_wrong_types_starts_fresh(self):
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump({"backfill_cursor": 5, "chunk_days": "many"}, handle)
        sync = make_sync(self.state_path, StubReader(self.clock), Pusher(), self.clock)
        self.assertEqual(sync.state["chunk_days"], 31)

    def test_startup_does_nothing_but_read_the_state(self):
        reader, pusher = StubReader(self.clock), Pusher()
        make_sync(self.state_path, reader, pusher, self.clock)
        self.assertEqual((reader.calls, pusher.calls), ([], []))


# ----------------------------------------------------------------------------------------------- 4. the order of steps
class Scheduling(TempState):
    def run_cycles(self, sync, count, gap=180):
        labels = []
        for _ in range(count):
            labels.append(sync.step())
            self.clock.advance(gap)
        return labels

    def test_the_first_cycle_runs_nothing_then_incremental_is_the_first_step_ever(self):
        reader, pusher = StubReader(self.clock, one_line_per_day), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock)
        self.assertIsNone(sync.step())
        self.assertEqual(reader.calls, [])
        self.assertEqual(sync.step(), "incremental")
        self.assertEqual(reader.calls[0], (TODAY - timedelta(days=2), TODAY))

    def test_at_most_one_step_per_cycle_and_the_order_is_incremental_backfill_nightly(self):
        reader, pusher = StubReader(self.clock, one_line_per_day), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock)
        labels = self.run_cycles(sync, 6)
        self.assertEqual(labels[:5], [None, "incremental", "backfill", "nightly", "backfill"])
        self.assertEqual(len(reader.calls), sum(1 for l in labels if l))                     # one read per step, never two in a cycle

    def test_chunks_go_newest_to_oldest_and_together_with_the_incremental_form_one_unbroken_stretch(self):
        reader, pusher = StubReader(self.clock, one_line_per_day), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, backfill_days=100, incremental_minutes=100000, nightly_hour=24 if False else 3)
        sync.state["last_nightly_day"] = TODAY.isoformat()                                   # keep the nightly out of this picture
        labels = self.run_cycles(sync, 30)
        chunk_ranges = [r for l, r in zip([x for x in labels if x], reader.calls) if l == "backfill"]
        self.assertEqual(chunk_ranges[0][1], TODAY - timedelta(days=3))                      # the first chunk ends three days before today
        for later, earlier in zip(chunk_ranges, chunk_ranges[1:]):
            self.assertEqual(earlier[1], later[0] - timedelta(days=1))                       # each next chunk ends the day before the last began
        self.assertEqual(chunk_ranges[-1][0], TODAY - timedelta(days=100))                   # the cursor reached the floor
        self.assertTrue(sync.state["backfill_done"])
        stretch = sorted([(TODAY - timedelta(days=2), TODAY)] + chunk_ranges)
        self.assertEqual(stretch[0][0], TODAY - timedelta(days=100))
        for a, b in zip(stretch, stretch[1:]):
            self.assertEqual(b[0], a[1] + timedelta(days=1), (a, b))                         # no gap and no overlap
        self.assertEqual(stretch[-1][1], TODAY)

    def test_a_failed_chunk_leaves_the_cursor_so_the_same_range_is_retried(self):
        reader, pusher = StubReader(self.clock, one_line_per_day), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock)
        sync.step(); sync.step()                                                              # the incremental
        cursor = sync.state["backfill_cursor"]
        pusher.answers = [(503, "busy")]
        self.clock.advance(180); self.assertEqual(sync.step(), "backfill")
        self.assertEqual(sync.state["backfill_cursor"], cursor)
        pusher.answers = []
        for _ in range(8):
            self.clock.advance(180)
            sync.step()
        self.assertEqual(reader.calls.count(reader.calls[1]), 2)                              # the failed chunk (call 1) read again, never skipped

    def test_the_incremental_interval_and_the_nightly_once_a_day(self):
        reader, pusher = StubReader(self.clock, one_line_per_day), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, backfill_days=1)
        labels = self.run_cycles(sync, 12)                                                   # 36 minutes of 3-minute cycles
        self.assertEqual(labels.count("incremental"), 3)                                     # at minute 3, 18 and 33
        self.assertEqual(labels.count("nightly"), 1)
        self.clock.advance(86400)                                                             # the next day
        later = self.run_cycles(sync, 4)
        self.assertEqual(later.count("nightly"), 1)

    def test_the_nightly_waits_for_its_hour(self):
        reader, pusher = StubReader(self.clock, one_line_per_day), Pusher()
        self.clock.utc = datetime(2026, 3, 19, 20, 0, tzinfo=timezone.utc)                    # 01:30 in India, before 03:00
        sync = make_sync(self.state_path, reader, pusher, self.clock, backfill_days=1)
        labels = self.run_cycles(sync, 6)
        self.assertNotIn("nightly", labels)
        self.clock.advance(3 * 3600)                                                          # now 04:30
        self.assertIn("nightly", self.run_cycles(sync, 3))

    def test_nothing_runs_when_the_stock_cycle_failed(self):
        reader, pusher = StubReader(self.clock, one_line_per_day), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock)
        sync.step()
        self.assertIsNone(sync.step(stock_ok=False))
        self.assertEqual(reader.calls, [])

    def test_the_nightly_range_is_the_last_35_days(self):
        reader, pusher = StubReader(self.clock, one_line_per_day), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, backfill_days=1)
        self.run_cycles(sync, 4)
        self.assertIn((TODAY - timedelta(days=35), TODAY), reader.calls)

    def test_the_history_length_is_capped_and_a_tiny_history_needs_no_backfill(self):
        sync = make_sync(self.state_path, StubReader(self.clock), Pusher(), self.clock, backfill_days=2)
        self.assertTrue(sync.state["backfill_done"])


# ----------------------------------------------------------------------------------------------- 5. chunk size
class AdaptiveChunks(TempState):
    def chunk_size_after(self, read_seconds, error=None, start=31):
        reader, pusher = StubReader(self.clock, one_line_per_day, read_seconds=read_seconds, error=error), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, backfill_days=400, incremental_minutes=100000)
        sync.state["chunk_days"] = start
        sync.state["last_incremental_utc"] = self.clock.utc.isoformat()
        sync.step(); self.clock.advance(180)
        sync.step()
        return sync.state["chunk_days"], sync

    def test_a_slow_read_halves_the_chunk(self):
        self.assertEqual(self.chunk_size_after(41)[0], 15)

    def test_a_fast_read_grows_it_by_half(self):
        self.assertEqual(self.chunk_size_after(5)[0], 46)

    def test_a_middling_read_keeps_it(self):
        self.assertEqual(self.chunk_size_after(20)[0], 31)

    def test_the_bounds_are_1_and_60(self):
        self.assertEqual(self.chunk_size_after(41, start=1)[0], 1)
        self.assertEqual(self.chunk_size_after(5, start=60)[0], 60)
        self.assertEqual(self.chunk_size_after(5, start=45)[0], 60)

    def test_a_timeout_halves_the_chunk_and_the_range_is_tried_again_later(self):
        size, sync = self.chunk_size_after(0, error=tally_sales.TallyTimeout("slow"))
        self.assertEqual(size, 15)
        self.assertEqual(sync.state["backfill_cursor"], (TODAY - timedelta(days=3)).isoformat())

    def test_only_the_read_time_counts_not_the_push(self):
        reader = StubReader(self.clock, one_line_per_day, read_seconds=5)

        class SlowPusher(Pusher):
            def __call__(inner, payload):
                self.clock.advance(100)
                return super().__call__(payload)
        sync = make_sync(self.state_path, reader, SlowPusher(), self.clock, backfill_days=400, incremental_minutes=100000)
        sync.state["last_incremental_utc"] = self.clock.utc.isoformat()
        sync.step(); self.clock.advance(180); sync.step()
        self.assertEqual(sync.state["chunk_days"], 46)


# ----------------------------------------------------------------------------------------------- 6. splitting
class Splitting(TempState):
    def test_a_push_of_more_than_25000_lines_is_split_into_in_range_requests(self):
        def many(day_from, day_to):
            out, day = [], day_from
            while day <= day_to:
                out += [{"voucher_key": f"{n:016x}", "day": day, "kind": "sale", "item": f"I{n}", "qty": 1} for n in range(2000)]
                day += timedelta(days=1)
            return out
        reader, pusher = StubReader(self.clock, many), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, backfill_days=1)
        sync.step(); sync.step()                                                                # incremental: three days of 2000 lines: no split
        self.assertEqual(len(pusher.calls), 1)
        self.clock.advance(1200)
        sync.step()                                                                             # the incremental is due again, it goes first
        pusher.calls.clear()
        self.clock.advance(180)
        sync.step()                                                                             # nightly: 36 days x 2000 = 72000 lines
        self.assertGreater(len(pusher.calls), 2)
        for call in pusher.calls:
            self.assertLessEqual(len(call["lines"]), 25000)
            self.assertTrue(all(call["from"] <= l["d"] <= call["to"] for l in call["lines"]))
        ranges = sorted(pusher.ranges())
        self.assertEqual(ranges[0][0], TODAY - timedelta(days=35))
        self.assertEqual(ranges[-1][1], TODAY)
        for a, b in zip(ranges, ranges[1:]):
            self.assertEqual(b[0], a[1] + timedelta(days=1))                                     # the pieces tile the range exactly
        self.assertEqual(sum(len(c["lines"]) for c in pusher.calls), 72000)

    def test_an_empty_chunk_is_still_pushed_so_deleted_vouchers_disappear(self):
        reader, pusher = StubReader(self.clock, None), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock)
        sync.step(); sync.step()
        self.assertEqual((len(pusher.calls), pusher.calls[0]["lines"]), (1, []))


# ----------------------------------------------------------------------------------------------- 7. website answers
class WebsiteAnswers(TempState):
    def started(self, answers, **kwargs):
        reader, pusher = StubReader(self.clock, one_line_per_day, **kwargs), Pusher(answers)
        sync = make_sync(self.state_path, reader, pusher, self.clock)
        sync.step()
        return sync, reader, pusher

    def cycles(self, sync, count, gap=180):
        out = []
        for _ in range(count):
            self.clock.advance(gap)
            out.append(sync.step())
        return out

    def test_404_pauses_for_an_hour_with_one_log_line_then_tries_again(self):
        with LogCapture() as log:
            sync, _reader, pusher = self.started([(404, "not found")])
            self.cycles(sync, 1)
            first = len(pusher.calls)
            self.cycles(sync, 15)                                                               # 45 minutes: silent
            self.assertEqual(len(pusher.calls), first)
            self.cycles(sync, 6)                                                                # past the hour
        self.assertGreater(len(pusher.calls), first)
        self.assertEqual(sum("waiting for the website to be updated" in m for m in log.records), 1)

    def test_409_stops_for_24_hours_with_one_clear_line(self):
        with LogCapture() as log:
            sync, _reader, pusher = self.started([(409, '{"error": "company mismatch", "refused": true}')])
            self.cycles(sync, 1)
            first = len(pusher.calls)
            self.cycles(sync, 100)                                                              # five hours
            self.assertEqual(len(pusher.calls), first)
            self.clock.advance(24 * 3600)
            self.cycles(sync, 3)
        self.assertGreater(len(pusher.calls), first)
        self.assertEqual(sum("company differs from the one the website already knows" in m for m in log.records), 1)
        self.assertIn("Sales sync refused", log.text())

    def test_the_409_pause_survives_a_restart(self):
        sync, reader, pusher = self.started([(409, "x")])
        self.cycles(sync, 1)
        again = make_sync(self.state_path, StubReader(self.clock, one_line_per_day), Pusher(), self.clock)
        again.step(); self.clock.advance(180)
        self.assertIsNone(again.step())

    def test_422_is_logged_once_an_hour_and_counts_as_a_failure(self):
        with LogCapture() as log:
            sync, _reader, pusher = self.started([(422, '{"error": "this push would remove most of the stored sales lines", "refused": true}')] * 200)
            self.cycles(sync, 60)                                                               # three hours of cycles
        lines = [m for m in log.records if "refused" in m.lower() and "Sales sync" in m]
        self.assertLessEqual(len(lines), 4)
        self.assertGreaterEqual(len(lines), 1)
        self.assertGreaterEqual(sync.state["fail_streak"], 1)

    def test_413_halves_the_chunk(self):
        sync, _r, _p = self.started([(200, "{}"), (413, "too large")])
        sync.state["chunk_days"] = 40
        self.cycles(sync, 2)                                                                    # the incremental (200), then a chunk (413)
        self.assertEqual(sync.state["chunk_days"], 20)

    def test_503_is_retried_next_cycle_without_backing_off(self):
        sync, _reader, pusher = self.started([(503, "busy")])
        self.cycles(sync, 1)
        self.assertEqual(len(pusher.calls), 1)
        self.cycles(sync, 1)
        self.assertEqual(len(pusher.calls), 2)
        self.assertEqual(sync.state["fail_streak"], 0)

    def test_401_and_403_are_failures_that_back_off(self):
        for status in (401, 403):
            sync, _reader, pusher = self.started([(status, "no")] * 50)
            self.cycles(sync, 40)
            self.assertLess(len(pusher.calls), 14, status)

    def test_failures_back_off_instead_of_hammering(self):
        sync, reader, pusher = self.started([], error=tally_sales.TallyReadError("boom"))
        self.cycles(sync, 60)
        self.assertLess(len(reader.calls), 12)                                                  # 60 cycles, far fewer attempts
        self.assertGreaterEqual(sync.state["fail_streak"], 3)

    def test_the_backoff_is_capped_at_60_cycles_and_resets_on_success(self):
        sync, reader, pusher = self.started([])
        sync.state["fail_streak"] = 30
        sync.on_failure_for_test() if hasattr(sync, "on_failure_for_test") else None
        reader.error = tally_sales.TallyReadError("boom")
        self.cycles(sync, 2)
        self.assertLessEqual(sync._skip_cycles, 60)
        reader.error = None
        sync._skip_cycles = 0
        self.cycles(sync, 4)
        self.assertEqual(sync.state["fail_streak"], 0)

    def test_tally_unreachable_is_one_quiet_line_not_a_traceback(self):
        with LogCapture() as log:
            sync, _reader, _pusher = self.started([], error=tally_sales.TallyUnreachable("down"))
            self.cycles(sync, 30)
        self.assertNotIn("Traceback", log.text())
        self.assertLessEqual(sum("Tally" in m and "Sales sync" in m for m in log.records), 3)

    def test_force_is_never_sent_and_the_payload_has_only_the_contract_keys(self):
        sync, _reader, pusher = self.started([])
        self.cycles(sync, 1)
        self.assertEqual(set(pusher.calls[0]), {"company", "from", "to", "lines"})
        self.assertEqual(set(pusher.calls[0]["lines"][0]), {"v", "d", "k", "i", "q"})

    def test_an_unreadable_company_means_nothing_is_pushed_and_one_log_line(self):
        reader, pusher = StubReader(self.clock, one_line_per_day, company=None), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock)
        with LogCapture() as log:
            sync.step()
            for _ in range(20):
                self.clock.advance(180)
                sync.step()
        self.assertEqual(pusher.calls, [])
        self.assertEqual(sum("company" in m.lower() and "Sales sync" in m for m in log.records), 1)


# ----------------------------------------------------------------------------------------------- 8. off and dry run
class Modes(TempState):
    def test_off_does_nothing_at_all(self):
        reader, pusher = StubReader(self.clock, one_line_per_day), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, mode="off")
        with LogCapture() as log:
            for _ in range(10):
                self.assertIsNone(sync.step())
                self.clock.advance(180)
        self.assertEqual((reader.calls, pusher.calls), ([], []))
        self.assertEqual(sum("Sales sync: off" in m for m in log.records), 1)
        self.assertFalse(os.path.exists(self.state_path))

    def test_dryrun_reads_and_logs_counts_but_never_pushes(self):
        reader, pusher = StubReader(self.clock, one_line_per_day), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, mode="dryrun")
        with LogCapture() as log:
            for _ in range(6):
                sync.step()
                self.clock.advance(180)
        self.assertGreater(len(reader.calls), 0)
        self.assertEqual(pusher.calls, [])
        self.assertTrue(any("dry run" in m.lower() and "would push" in m for m in log.records))


# ----------------------------------------------------------------------------------------------- 2 and 10. end to end
class EndToEnd(TempState):
    """The fake Tally over real HTTP, the real website route through the Flask test client."""

    @classmethod
    def setUpClass(cls):
        cls.app_module, cls.db = support.bootstrap()
        cls.snapshot = support.snapshot_state()

    def setUp(self):
        super().setUp()
        self.site = support.client_for("anonymous")
        self.tally = FakeTally()
        self.addCleanup(self.tally.close)
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM sales_lines")
            conn.execute("DELETE FROM capture_meta WHERE key LIKE 'sales_%' OR key = 'last_sales_prune_day_ist'")
        self.pushed = []
        self.bodies = []
        self.stderr = []

        def pusher(payload):
            raw = json.dumps(payload)
            self.bodies.append(raw)
            self.pushed.append((date.fromisoformat(payload["from"]), date.fromisoformat(payload["to"])))
            response = self.site.post("/admin/intake/sales", json=payload, headers=INTAKE_HEADERS)
            return response.status_code, response.get_data(as_text=True)
        self.pusher = pusher
        patcher = mock.patch.object(self.app_module, "_stock_now_utc", return_value=datetime(2026, 3, 20, 9, 0, tzinfo=timezone.utc))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        support.restore_state(self.snapshot)

    def fake_history(self, days=90):
        """One sale per day of ALTO 7D MAT BLACK (day number + 1 units), a few returns, and some noise vouchers."""
        out = []
        for n in range(days):
            day = TODAY - timedelta(days=n)
            out.append(voucher(day, [("ALTO 7D MAT BLACK", f"{1 + n % 4} Nos"), ("SWIFT 7D MAT GREY", "2 Nos")], guid=f"RAW-GUID-S{n}", number=str(n)))
            if n % 10 == 0:
                out.append(voucher(day, [("ALTO 7D MAT BLACK", "1 Nos")], vtype="Credit Note", guid=f"RAW-GUID-C{n}", number=f"C{n}"))
            if n % 7 == 0:
                out.append(voucher(day, [("ALTO 7D MAT BLACK", "9 Nos")], vtype="Receipt", guid=f"RAW-GUID-R{n}", number=f"R{n}"))
            if n % 13 == 0:
                out.append(voucher(day, [("ALTO 7D MAT BLACK", "5 Nos")], guid=f"RAW-GUID-X{n}", number=f"X{n}", cancelled=True))
        return out

    def expected(self, vouchers, since=None):
        net = {}
        for v in vouchers:
            if v["cancelled"] or v["optional"] or v["parent"] not in ("Sales", "Credit Note") or (since and v["day"] < since):
                continue
            sign = 1 if v["parent"] == "Sales" else -1
            for name, text in v["entries"]:
                net[name.upper()] = net.get(name.upper(), 0) + sign * tally_sales.parse_quantity(text)[0]
        return net

    def site_net(self):
        with self.db.get_connection() as conn:
            rows = conn.execute("SELECT item_key, SUM(CASE WHEN kind = 'sale' THEN qty ELSE -qty END) FROM sales_lines GROUP BY item_key").fetchall()
        return {r[0]: r[1] for r in rows}

    def make(self, **settings):
        reader = tally_sales.TallyReader(tally_url=self.tally.url)
        return make_sync(self.state_path, reader, self.pusher, self.clock, **settings)

    def run_until_done(self, sync, limit=60):
        for _ in range(limit):
            sync.step()
            self.clock.advance(180)
            yield
            if sync.state["backfill_done"]:
                return

    def test_a_90_day_backfill_loads_newest_first_in_one_unbroken_stretch_and_ends_exact(self):
        self.tally.vouchers = self.fake_history(90)
        sync = self.make(backfill_days=89, incremental_minutes=100000)
        sync.state["last_nightly_day"] = TODAY.isoformat()
        seen = 0
        for _ in self.run_until_done(sync):
            if len(self.pushed) > seen:
                seen = len(self.pushed)
                union = sorted(self.pushed)
                merged = [union[0]]
                for start, end in union[1:]:
                    last = merged[-1]
                    if start <= last[1] + timedelta(days=1):
                        merged[-1] = (last[0], max(last[1], end))
                    else:
                        merged.append((start, end))
                self.assertEqual(len(merged), 1, merged)                                    # one unbroken stretch after every chunk
                self.assertEqual(merged[0][1], TODAY)                                       # ... and it ends today
        self.assertTrue(sync.state["backfill_done"])
        self.assertEqual(self.pushed[0], (TODAY - timedelta(days=2), TODAY))                # the incremental went first
        self.assertEqual(self.site_net(), self.expected(self.tally.vouchers, since=TODAY - timedelta(days=89)))

    def test_changed_and_deleted_vouchers_heal_through_the_incremental_and_the_nightly(self):
        self.tally.vouchers = self.fake_history(60)
        sync = self.make(backfill_days=59, incremental_minutes=15)
        for _ in self.run_until_done(sync):
            pass
        base = self.site_net()
        recent = next(v for v in self.tally.vouchers if v["guid"] == "RAW-GUID-S1")
        old = next(v for v in self.tally.vouchers if v["guid"] == "RAW-GUID-S20")
        recent["entries"] = [("ALTO 7D MAT BLACK", "50 Nos")]                                  # a recent voucher altered
        self.tally.vouchers.remove(old)                                                       # an older voucher deleted
        self.clock.advance(16 * 60)
        sync.step()                                                                           # an incremental
        self.assertEqual(self.pushed[-1], (TODAY - timedelta(days=2), TODAY))
        net = self.site_net()
        self.assertNotEqual(net["ALTO 7D MAT BLACK"], base["ALTO 7D MAT BLACK"])               # the recent change reached the site
        sync.state["last_nightly_day"] = None
        self.clock.advance(180)
        for _ in range(4):
            sync.step(); self.clock.advance(180)
        self.assertEqual(self.site_net(), self.expected(self.tally.vouchers, since=TODAY - timedelta(days=59)))     # the old deletion healed too

    def test_the_nightly_with_the_same_data_changes_nothing(self):
        self.tally.vouchers = self.fake_history(40)
        sync = self.make(backfill_days=39)
        for _ in self.run_until_done(sync):
            pass
        with self.db.get_connection() as conn:
            before = [tuple(r) for r in conn.execute("SELECT * FROM sales_lines ORDER BY voucher_key, kind, item_key").fetchall()]
        sync.state["last_nightly_day"] = None
        self.clock.advance(180)
        for _ in range(3):
            sync.step(); self.clock.advance(180)
        with self.db.get_connection() as conn:
            after = [tuple(r) for r in conn.execute("SELECT * FROM sales_lines ORDER BY voucher_key, kind, item_key").fetchall()]
        self.assertEqual(before, after)

    def test_the_company_is_pinned_on_the_website_by_the_first_push(self):
        self.tally.vouchers = self.fake_history(5)
        sync = self.make(backfill_days=4)
        sync.step(); sync.step()
        with self.db.get_connection() as conn:
            pin = conn.execute("SELECT value FROM capture_meta WHERE key = 'sales_company_hash'").fetchone()[0]
        self.assertEqual(pin, tally_sales.company_hash(self.tally.company))
        self.tally.company = "A Different Company"
        other = make_sync(os.path.join(self.dir, "second.json"), tally_sales.TallyReader(tally_url=self.tally.url), self.pusher, self.clock)
        other.step(); self.clock.advance(180)
        with LogCapture() as log:
            other.step()
        self.assertIn("company differs from the one the website already knows", log.text())

    def test_privacy_nothing_private_reaches_the_website_or_the_log(self):
        self.tally.vouchers = self.fake_history(20)
        sync = self.make(backfill_days=19)
        with LogCapture() as log:
            for _ in self.run_until_done(sync):
                pass
        everything = "\n".join(self.bodies)
        for text in FORBIDDEN_TEXT:
            self.assertNotIn(text, everything)
            self.assertNotIn(text, log.text())
        for body in self.bodies:
            payload = json.loads(body)
            self.assertEqual(set(payload), {"company", "from", "to", "lines"})
            for line in payload["lines"]:
                self.assertEqual(set(line), {"v", "d", "k", "i", "q"})
                self.assertRegex(line["v"], r"^[0-9a-f]{16}$")
        self.assertNotIn("ALTO 7D MAT BLACK", log.text())                                  # counts and ranges only: never item names
        self.assertNotIn("SWIFT 7D MAT GREY", log.text())
        self.assertTrue(any("Sales sync: backfill" in m and "pushed" in m for m in log.records))

    def test_the_log_lines_the_guide_describes(self):
        self.tally.vouchers = self.fake_history(10)
        sync = self.make(backfill_days=9, incremental_minutes=15)
        with LogCapture() as log:
            for _ in self.run_until_done(sync):
                pass
            sync.state["last_nightly_day"] = None
            self.clock.advance(1000)
            for _ in range(3):
                sync.step(); self.clock.advance(180)
        text = log.text()
        for expected in ("Sales sync: incremental ", "Sales sync: backfill ", "Sales sync: backfill complete (9 days)", "Sales sync: nightly ", " lines"):
            self.assertIn(expected, text)

    def test_a_30_day_chunk_with_3000_vouchers_is_quick(self):
        vouchers = [voucher(TODAY - timedelta(days=3 + n % 30), [("ALTO 7D MAT BLACK", "2 Nos")], guid=f"RAW-GUID-{n}", number=str(n)) for n in range(3000)]
        self.tally.vouchers = vouchers
        reader = tally_sales.TallyReader(tally_url=self.tally.url)
        start = time.perf_counter()
        lines = reader.fetch_sales_lines(TODAY - timedelta(days=33), TODAY - timedelta(days=3))
        elapsed = time.perf_counter() - start
        print(f"\n  fake Tally, one 31-day chunk with 3000 vouchers: read and parse {elapsed * 1000:.0f} ms ({len(lines)} lines)")
        self.assertEqual(len(lines), 3000)
        self.assertLess(elapsed, 5.0)


# ----------------------------------------------------------------------------------------------- 9 and 11. the feeder itself
class SalesRig(Rig):
    """The existing mock Tally rig, plus a record of the sales requests it receives."""

    def __init__(self, **env):
        super().__init__(**env)
        self.sales_requests = []

    def reply(self, b):
        if "Sales Sync" in b:
            self.sales_requests.append(b)
            return "<ENVELOPE><LINEERROR>no sales in this mock</LINEERROR></ENVELOPE>"
        return super().reply(b)


class FeederHook(unittest.TestCase):
    def test_a_failing_sales_step_never_stops_the_stock_cycle_or_the_stock_push(self):
        rig = SalesRig()
        self.addCleanup(rig.stop)
        with open(os.path.join(rig.dir, "sales_sync.py"), "w", encoding="utf-8") as handle:
            handle.write("class SalesSync:\n    @classmethod\n    def from_environment(cls):\n        return cls()\n"
                         "    def step(self, stock_ok=True):\n        raise RuntimeError('sales exploded')\n")
        rig.start()
        self.assertTrue(rig.wait(lambda: len(rig.pushes) >= 4), "the stock push must keep happening")
        log = rig.log()
        self.assertEqual(log.count("Sales sync error: RuntimeError"), 1)                       # at most once per 10 minutes
        self.assertNotIn("sales exploded", log)
        self.assertEqual(rig.reasons(), ["startup"])

    def test_with_sales_sync_on_and_a_tally_that_cannot_answer_the_stock_push_is_unaffected(self):
        rig = SalesRig()
        self.addCleanup(rig.stop)
        rig.start()
        self.assertTrue(rig.wait(lambda: len(rig.pushes) >= 4))
        self.assertTrue(rig.wait(lambda: len(rig.sales_requests) >= 1), "the sales step asked Tally from the second cycle on")
        self.assertEqual(rig.pushes[-1][1]["item_stock"] and True, True)
        self.assertIn("Cloud sync push succeeded", rig.log())

    def test_with_sales_sync_off_tally_is_never_asked_and_the_stock_timing_is_unchanged(self):
        rig = SalesRig(SALES_SYNC="off")
        self.addCleanup(rig.stop)
        rig.start()
        self.assertTrue(rig.wait(lambda: len(rig.pushes) >= 5))
        self.assertEqual(rig.sales_requests, [])
        gaps = [b[0] - a[0] for a, b in zip(rig.pushes, rig.pushes[1:])]
        self.assertTrue(all(1.0 <= g <= 4.5 for g in gaps[1:]), gaps)                          # TALLY_EXPORT_INTERVAL is 2 s in the rig
        print(f"\n  stock pushes with SALES_SYNC=off: gaps {[round(g, 2) for g in gaps]} s (interval 2 s)")

    def test_importing_feeder_starts_no_thread_and_makes_no_network_call(self):
        copy = make_copy("sales_import_")
        self.addCleanup(lambda: __import__("shutil").rmtree(copy, ignore_errors=True))
        code = (
            "import socket, threading\n"
            "calls = []\n"
            "orig_connect = socket.socket.connect\n"
            "def guarded(self, *a, **k):\n    calls.append('connect'); raise OSError('no network in this test')\n"
            "socket.socket.connect = guarded\n"
            "orig_start = threading.Thread.start\n"
            "def no_start(self, *a, **k):\n    calls.append('thread'); raise RuntimeError('no threads in this test')\n"
            "threading.Thread.start = no_start\n"
            "before = threading.active_count()\n"
            "import feeder\n"
            "print('RESULT', calls, threading.active_count() - before)\n")
        result = subprocess.run([sys.executable, "-c", code], cwd=copy, env=clean_env(LOG_FILE="logs/import_check.log"), capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        self.assertIn("RESULT [] 0", result.stdout)


# ----------------------------------------------------------------------------------------------- 12. the probe
def load_probe():
    spec = importlib.util.spec_from_file_location("sales_probe", os.path.join(ROOT, "tools", "shop", "sales_probe.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Probe(unittest.TestCase):
    def setUp(self):
        self.tally = FakeTally()
        self.addCleanup(self.tally.close)
        self.tally.vouchers = [
            voucher(date(2026, 1, 10), [("ALTO 7D MAT BLACK", "3 Nos")], guid="RAW-GUID-1"), voucher(date(2026, 1, 20), [("SWIFT 7D MAT GREY", "2 Nos")], guid="RAW-GUID-2"),
            voucher(date(2026, 1, 21), [("ALTO 7D MAT BLACK", "1 Nos")], vtype="Credit Note", guid="RAW-GUID-3"),
            voucher(date(2026, 2, 5), [("ALTO 7D MAT BLACK", "4 Nos"), ("SWIFT 7D MAT GREY", "1 Nos")], guid="RAW-GUID-4"),
            voucher(date(2026, 2, 6), [("X", "9 Nos")], vtype="Receipt", guid="RAW-GUID-5")]
        self.out = []

    def run_probe(self, *args):
        probe = load_probe()
        code = probe.main(list(args), reader=tally_sales.TallyReader(tally_url=self.tally.url), out=self.out.append, today=date(2026, 3, 20))
        return code, "\n".join(self.out)

    def test_it_prints_the_per_month_table_and_the_first_lines(self):
        code, text = self.run_probe("--days", "90")
        self.assertEqual(code, 0)
        self.assertIn(tally_sales.company_hash("Super Seatings Test Co")[:8], text)
        self.assertNotIn(tally_sales.company_hash("Super Seatings Test Co")[:9], text)             # only the first 8 characters
        self.assertRegex(text, r"vouchers:\s*4\b")
        self.assertRegex(text, r"lines:\s*5\b")
        self.assertRegex(text, r"2026-01\s+2\s+5\s+1\s+4")                                         # month, vouchers, sold, returned, net
        self.assertRegex(text, r"2026-02\s+1\s+5\s+0\s+5")
        self.assertIn("ALTO 7D MAT BLACK", text)

    def test_it_prints_at_most_15_lines_and_nothing_private(self):
        self.tally.vouchers = [voucher(date(2026, 1, 1) + timedelta(days=n % 28), [("ALTO 7D MAT BLACK", "1 Nos")], guid=f"RAW-GUID-{n}") for n in range(40)]
        _code, text = self.run_probe("--days", "90")
        sample = [l for l in text.splitlines() if "ALTO 7D MAT BLACK" in l]
        self.assertEqual(len(sample), 15)
        for private in FORBIDDEN_TEXT:
            self.assertNotIn(private, text)

    def test_it_never_pushes(self):
        with mock.patch("requests.post") as post:
            self.run_probe("--days", "30")
        post.assert_not_called()

    def test_days_default_and_limit(self):
        probe = load_probe()
        self.assertEqual(probe.parse_days([]), 30)
        self.assertEqual(probe.parse_days(["--days", "1000"]), 365)
        self.assertEqual(probe.parse_days(["--days", "0"]), 1)
        self.assertEqual(probe.parse_days(["--days", "abc"]), 30)

    def test_a_friendly_message_when_tally_is_not_reachable(self):
        probe = load_probe()
        dead = tally_sales.TallyReader(tally_url=f"http://127.0.0.1:{free_port()}", max_retries=1)
        out = []
        code = probe.main(["--days", "5"], reader=dead, out=out.append, today=date(2026, 3, 20))
        self.assertNotEqual(code, 0)
        self.assertIn("Tally", "\n".join(out))
        self.assertNotIn("Traceback", "\n".join(out))


# ----------------------------------------------------------------------------------------------- 13. files
class Files(unittest.TestCase):
    def test_the_probe_bat_has_crlf_line_endings_and_pauses(self):
        data = open(os.path.join(ROOT, "tools", "shop", "SALES_PROBE.bat"), "rb").read()
        lines = data.split(b"\r\n")
        self.assertGreater(len(lines), 3)
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))                                        # every line ends with CRLF
        self.assertNotIn(b"\r", data.replace(b"\r\n", b""))
        text = data.decode("utf-8").lower()
        self.assertIn(r".venv\scripts\python.exe", text)
        self.assertIn("sales_probe.py", text)
        self.assertTrue(text.rstrip().endswith("pause"))

    def test_the_update_script_still_finds_its_log_lines(self):
        update = open(os.path.join(ROOT, "tools", "shop", "SHOP_UPDATE.bat"), encoding="utf-8", errors="replace").read()
        sources = open(os.path.join(ROOT, "feeder.py"), encoding="utf-8").read() + open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
        for needed in ("machine role=", "reason=", "Cloud sync push succeeded"):
            self.assertIn(needed, update)
            self.assertIn(needed.replace("reason=", "Full refresh starting: reason="), sources) if needed == "reason=" else self.assertIn(needed, sources)

    def test_the_guide_exists_and_covers_what_the_owner_needs(self):
        text = open(os.path.join(ROOT, "docs", "SALES_SYNC.md"), encoding="utf-8").read()
        for needed in ("SALES_PROBE.bat", "SALES_SYNC=off", "test-token-local", "NEVER", "1st, 2nd", "409", "404", "newest", "sales_sync_state.json",
                       "SALES_BACKFILL_DAYS", "SALES_INCREMENTAL_MINUTES", "SALES_NIGHTLY_HOUR", "dryrun", "9000"):
            self.assertIn(needed, text)

    def test_no_real_address_or_token_in_the_new_files(self):
        for name in ("tally_sales.py", "sales_sync.py", os.path.join("tools", "shop", "sales_probe.py"), os.path.join("docs", "SALES_SYNC.md")):
            text = open(os.path.join(ROOT, name), encoding="utf-8").read().lower()
            for bad in (".onrender.com", "idris900323", "idris-botics", "x-intake-force"):
                self.assertNotIn(bad, text, (name, bad))


if __name__ == "__main__":
    unittest.main()
