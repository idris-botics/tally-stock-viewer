"""The shop-side sales sync: reading sales vouchers and credit notes from Tally ONE CALENDAR MONTH at a time (a fake Tally here: a tiny
local HTTP server that answers like the real one, including its habits: it ignores SVFROMDATE/SVTODATE, it returns whole vouchers with
party names and amounts, an undeclared "UDF:" tag prefix and one stray <VOUCHER>0</VOUCHER> element) and pushing them to the website's
REAL /admin/intake/sales route (Flask test client). No real Tally, website, token or shop PC is ever contacted."""
import glob
import hashlib
import importlib.util
import inspect
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
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
FORBIDDEN_TEXT = ("ACME TRADERS", "SECRET NARRATION", "1500.00", "500/Nos", "SALES LEDGER", "RAW-GUID-", "SECRET UDF")
BASES = {"sales": ("Yes", "No"), "credit": ("No", "Yes"), "delivery": ("No", "No"), "purchase": ("No", "No")}


# ----------------------------------------------------------------------------------------------- fake Tally
def voucher(day, entries=(("ALTO 7D MAT BLACK", "3 Nos"),), vtype="SALES-HO", base="sales", guid=None, number="1", cancelled=False,
            optional=False, variant="ALL"):
    """base: sales | credit | delivery | purchase. Tally's own flags (not the name) say what a voucher type is."""
    return {"day": day, "entries": list(entries), "vtype": vtype, "base": base, "guid": guid, "number": number,
            "cancelled": cancelled, "optional": optional, "variant": variant}


def voucher_xml(v):
    """One voucher as the real Tally exports it, with plenty of private extras the sync must never read, store, log or send."""
    tag = "ALLINVENTORYENTRIES.LIST" if v["variant"] == "ALL" else "INVENTORYENTRIES.LIST"
    entries = "".join(
        f"<{tag}><STOCKITEMNAME>{name}</STOCKITEMNAME><BILLEDQTY> {qty}</BILLEDQTY><ACTUALQTY> {qty}</ACTUALQTY>"
        f"<RATE>500/Nos</RATE><AMOUNT>1500.00</AMOUNT></{tag}>" for name, qty in v["entries"])
    guid = f"<GUID>{v['guid']}</GUID>" if v["guid"] else ""
    sales_flag, credit_flag = BASES[v["base"]]
    return (f'<VOUCHER REMOTEID="r-{v["number"]}" VCHKEY="k-{v["number"]}" VCHTYPE="{v["vtype"]}" OBJVIEW="Invoice Voucher View">'
            f'<DATE TYPE="Date">{v["day"].strftime("%Y%m%d")}</DATE>{guid}<VOUCHERTYPENAME>{v["vtype"]}</VOUCHERTYPENAME>'
            f"<VOUCHERNUMBER>{v['number']}</VOUCHERNUMBER>"
            f'<ISCANCELLED TYPE="Logical">{"Yes" if v["cancelled"] else "No"}</ISCANCELLED>'
            f'<ISOPTIONAL TYPE="Logical">{"Yes" if v["optional"] else "No"}</ISOPTIONAL>'
            f"<ISSALESFLAG>{sales_flag}</ISSALESFLAG><ISCREDITNOTEFLAG>{credit_flag}</ISCREDITNOTEFLAG>"
            f"<PARTYLEDGERNAME>ACME TRADERS PVT</PARTYLEDGERNAME><NARRATION>SECRET NARRATION</NARRATION>"
            f"<LEDGERENTRIES.LIST><LEDGERNAME>SALES LEDGER</LEDGERNAME><AMOUNT>1500.00</AMOUNT></LEDGERENTRIES.LIST>{entries}"
            f'<UDF:USERDESCRIPTION.LIST DESC="x" ISLIST="Yes" TYPE="String" INDEX="1">SECRET UDF</UDF:USERDESCRIPTION.LIST></VOUCHER>')


def collection(vouchers, stray=True):
    """Tally's answer to the voucher request: whole vouchers and, like the real thing, one stray <VOUCHER>0</VOUCHER> element."""
    return ("<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DESC><STATICVARIABLES>"
            "<SVCURRENTCOMPANY>Super Seatings Test Co</SVCURRENTCOMPANY></STATICVARIABLES></DESC><DATA><COLLECTION>"
            + "".join(voucher_xml(v) for v in vouchers) + ("<VOUCHER>0</VOUCHER>" if stray else "") + "</COLLECTION></DATA></BODY></ENVELOPE>")


_MONTH_CONDITION = re.compile(r"\$\$MonthOfDate:\$Date = (\d+) AND \$\$YearOfDate:\$Date = (\d+)")


class FakeTally:
    """HTTP server answering the sales sync's two requests like the real Tally. `vouchers` can be changed while it runs. It honours
    the month condition and the sales/credit-note type filter ONLY; it ignores SVFROMDATE/SVTODATE like the real thing."""

    def __init__(self, company="Super Seatings Test Co"):
        self.company = company
        self.vouchers = []
        self.filter_supported = True
        self.ignore_month = False
        self.requests = []
        self.fail = False
        self.hang = 0.0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8", "replace")
                fake.requests.append(body)
                if fake.hang:
                    time.sleep(fake.hang)
                out = fake.reply(body).encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                except OSError:
                    pass

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
            if "<FILTER>" not in body or not self.filter_supported:
                return "<ENVELOPE><LINEERROR>Unknown formula</LINEERROR></ENVELOPE>"
            match = _MONTH_CONDITION.search(body)
            chosen = [v for v in self.vouchers if v["base"] in ("sales", "credit")
                      and (self.ignore_month or (match and (v["day"].month, v["day"].year) == (int(match.group(1)), int(match.group(2)))))]
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


def month_lines(year, month, qty=1, items=1):
    """A few lines in a month: the 1st, the 15th and the last day, `items` items each."""
    first, last = sales_sync.month_bounds(year, month)
    out = []
    for day in (first, first + timedelta(days=14), last):
        for n in range(items):
            out.append({"voucher_key": hashlib.sha256(f"{day.isoformat()}{n}".encode()).hexdigest()[:16], "day": day, "kind": "sale",
                        "item": f"ALTO 7D MAT BLACK {n}" if n else "ALTO 7D MAT BLACK", "qty": qty})
    return out


class StubReader:
    """A reader that answers from `lines` ({"YYYY-MM": [line, ...]}; months not listed get month_lines) and records its calls."""

    def __init__(self, clock=None, lines=None, company="Stub Company", read_seconds=0.0, error=None, default=True, size=0):
        self.clock, self.lines, self.company, self.read_seconds, self.error, self.default = clock, lines if lines is not None else {}, company, read_seconds, error, default
        self.size = size
        self.calls = []

    def company_name(self):
        return self.company

    def fetch_month(self, year, month):
        self.calls.append((year, month))
        if self.clock:
            self.clock.advance(self.read_seconds)
        if self.error:
            raise self.error
        key = sales_sync.month_key(year, month)
        lines = list(self.lines[key]) if key in self.lines else (month_lines(year, month) if self.default else [])
        return lines, {"seconds": self.read_seconds, "bytes": self.size, "sale_vouchers": len(lines)}


class Pusher:
    def __init__(self, answers=None):
        self.calls, self.answers = [], list(answers or [])

    def __call__(self, payload):
        self.calls.append(payload)
        return self.answers.pop(0) if self.answers else (200, '{"ok": true}')

    def ranges(self):
        return [(date.fromisoformat(c["from"]), date.fromisoformat(c["to"])) for c in self.calls]

    def months(self):
        return [c["from"][:7] for c in self.calls]


def make_sync(tmp, reader, pusher, clock, **settings):
    values = dict(mode="on", backfill_days=100, incremental_minutes=30, nightly_hour=3, shop_hours="")      # no shop-hours limit unless a test asks
    values.update(settings)
    return sales_sync.SalesSync(state_path=tmp, reader=reader, pusher=pusher, settings=sales_sync.SalesSettings(**values), clock=clock)


class LogCapture:
    """Everything logged by the feeder logger (the tests normally switch logging off)."""

    def __enter__(self):
        self.records, self.levels = [], []
        outer = self

        class Handler(logging.Handler):
            def emit(self, record):
                outer.records.append(record.getMessage())
                outer.levels.append(record.levelno)

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
        self.dir = tempfile.mkdtemp(prefix="sales_state_")
        self.state_path = os.path.join(self.dir, "sales_sync_state.json")
        self.clock = FakeClock()
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))

    def state(self):
        with open(self.state_path, encoding="utf-8") as handle:
            return json.load(handle)

    def run_cycles(self, sync, count, gap=180):
        labels = []
        for _ in range(count):
            labels.append(sync.step())
            self.clock.advance(gap)
        return labels


# ----------------------------------------------------------------------------------------------- 1. parsing
class Parsing(unittest.TestCase):
    def parse(self, vouchers, year=2026, month=1, stray=True):
        return tally_sales.parse_vouchers(collection(vouchers, stray=stray), year, month)

    def test_both_xml_variants_and_the_plain_fields(self):
        lines, stats = self.parse([voucher(date(2026, 1, 10), [("ALTO 7D MAT BLACK", "3 Nos")], guid="RAW-GUID-1"),
                                   voucher(date(2026, 1, 11), [("SWIFT 7D MAT GREY", "1,250 Nos")], guid="RAW-GUID-2", variant="OLD")])
        self.assertEqual([(l["day"], l["kind"], l["item"], l["qty"]) for l in lines],
                         [(date(2026, 1, 10), "sale", "ALTO 7D MAT BLACK", 3), (date(2026, 1, 11), "sale", "SWIFT 7D MAT GREY", 1250)])
        self.assertEqual((stats["sale_vouchers"], stats["vouchers_read"], stats["lines"]), (2, 3, 2))

    def test_the_stray_voucher_element_is_skipped_and_counted_and_is_normal(self):
        lines, stats = self.parse([voucher(date(2026, 1, 10), guid="RAW-GUID-1")])
        self.assertEqual((len(lines), stats["skipped_no_date"]), (1, 1))
        self.assertEqual(self.parse([voucher(date(2026, 1, 10), guid="RAW-GUID-1")], stray=False)[1]["skipped_no_date"], 0)

    def test_cancelled_and_optional_vouchers_are_left_out_and_counted(self):
        lines, stats = self.parse([voucher(date(2026, 1, 10), guid="RAW-GUID-1", cancelled=True), voucher(date(2026, 1, 10), guid="RAW-GUID-2", optional=True),
                                   voucher(date(2026, 1, 10), guid="RAW-GUID-3")])
        self.assertEqual(len(lines), 1)
        self.assertEqual((stats["skipped_cancelled"], stats["skipped_optional"], stats["sale_vouchers"]), (1, 1, 1))

    def test_tallys_flags_decide_sale_or_return_never_the_voucher_name(self):
        lines, stats = self.parse([
            voucher(date(2026, 1, 10), vtype="SALES-HO", guid="RAW-GUID-1"), voucher(date(2026, 1, 10), vtype="Sales Gst", guid="RAW-GUID-2"),
            voucher(date(2026, 1, 10), vtype="MEESHO SALES", guid="RAW-GUID-3"), voucher(date(2026, 1, 10), vtype="ON SALES(Amz)", guid="RAW-GUID-4"),
            voucher(date(2026, 1, 10), vtype="Credit Note", base="credit", guid="RAW-GUID-5"),
            voucher(date(2026, 1, 10), vtype="DELIVERY NOTE-HO", base="delivery", guid="RAW-GUID-6"),
            voucher(date(2026, 1, 10), vtype="PURCHASE-HO", base="purchase", guid="RAW-GUID-7"),
            voucher(date(2026, 1, 10), vtype="Sales", base="delivery", guid="RAW-GUID-8")])          # called "Sales" but Tally says it is not
        self.assertEqual(sorted(l["kind"] for l in lines), ["return", "sale", "sale", "sale", "sale"])
        self.assertEqual((stats["sale_vouchers"], stats["credit_notes"], stats["skipped_other_type"]), (4, 1, 3))

    def test_a_voucher_without_the_flags_is_not_counted(self):
        xml = collection([voucher(date(2026, 1, 10), guid="RAW-GUID-1")]).replace("<ISSALESFLAG>Yes</ISSALESFLAG>", "")
        lines, stats = tally_sales.parse_vouchers(xml, 2026, 1)
        self.assertEqual((lines, stats["skipped_other_type"]), ([], 1))

    def test_quantity_text(self):
        for text, qty, fractional in (("3 Nos", 3, False), ("1,250 Nos", 1250, False), ("-4 Nos", 4, False), ("2.5 Mtr", 2, True),
                                      ("0 Nos", None, False), ("", None, False), ("Nos", None, False), ("10", 10, False), ("12.00 Nos", 12, False)):
            self.assertEqual(tally_sales.parse_quantity(text), (qty, fractional), text)

    def test_billed_quantity_is_used_and_actual_quantity_only_when_billed_is_empty(self):
        both = collection([voucher(date(2026, 1, 10), [("A", "3 Nos")], guid="RAW-GUID-1")]).replace("<ACTUALQTY> 3 Nos", "<ACTUALQTY> 9 Nos")
        self.assertEqual(tally_sales.parse_vouchers(both, 2026, 1)[0][0]["qty"], 3)
        empty = both.replace("<BILLEDQTY> 3 Nos</BILLEDQTY>", "<BILLEDQTY></BILLEDQTY>")
        self.assertEqual(tally_sales.parse_vouchers(empty, 2026, 1)[0][0]["qty"], 9)

    def test_fractional_quantities_use_the_integer_part_and_are_counted(self):
        lines, stats = self.parse([voucher(date(2026, 1, 10), [("A", "2.5 Mtr"), ("B", "3 Nos")], guid="RAW-GUID-1")])
        self.assertEqual([l["qty"] for l in lines], [2, 3])
        self.assertEqual(stats["fractional"], 1)

    def test_zero_and_negative_quantities(self):
        lines, _ = self.parse([voucher(date(2026, 1, 10), [("A", "0 Nos"), ("B", "-3 Nos")], guid="RAW-GUID-1")])
        self.assertEqual([(l["item"], l["qty"]) for l in lines], [("B", 3)])

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

    def test_a_voucher_with_an_unreadable_date_is_skipped_and_counted_and_the_rest_survive(self):
        xml = collection([voucher(date(2026, 1, 10), guid="RAW-GUID-1")]).replace("</COLLECTION>", "<VOUCHER><DATE>not a date</DATE></VOUCHER></COLLECTION>")
        lines, stats = tally_sales.parse_vouchers(xml, 2026, 1)
        self.assertEqual((len(lines), stats["skipped_no_date"]), (1, 2))

    def test_the_month_guard_drops_vouchers_from_other_months_and_counts_them(self):
        lines, stats = self.parse([voucher(date(2026, 1, 31), guid="RAW-GUID-1"), voucher(date(2026, 2, 1), guid="RAW-GUID-2"),
                                   voucher(date(2025, 1, 15), guid="RAW-GUID-3")])
        self.assertEqual([l["day"] for l in lines], [date(2026, 1, 31)])
        self.assertEqual((stats["dropped_out_of_range"], stats["sale_vouchers"]), (2, 1))

    def test_empty_and_odd_answers_give_no_lines_and_a_broken_answer_raises(self):
        for text in ("<ENVELOPE></ENVELOPE>", "<ENVELOPE><BODY><DATA><COLLECTION></COLLECTION></DATA></BODY></ENVELOPE>", "<ENVELOPE/>"):
            self.assertEqual(tally_sales.parse_vouchers(text, 2026, 1)[0], [])
        for broken in ("this is not xml at all <<<", "", "<ENVELOPE><LINEERROR>boom</LINEERROR></ENVELOPE>"):
            with self.assertRaises(tally_sales.TallyReadError):
                tally_sales.parse_vouchers(broken, 2026, 1)

    def test_illegal_characters_do_not_break_the_xml(self):
        xml = collection([voucher(date(2026, 1, 10), [("ALTO\x0b 7D", "3 Nos")], guid="RAW-GUID-1")])
        self.assertEqual(len(tally_sales.parse_vouchers(xml, 2026, 1)[0]), 1)

    def test_the_undeclared_udf_prefix_no_longer_breaks_the_xml(self):
        xml = collection([voucher(date(2026, 1, 10), guid="RAW-GUID-1")])
        self.assertIn("<UDF:USERDESCRIPTION.LIST", xml)
        import xml.etree.ElementTree as ET
        with self.assertRaises(ET.ParseError):                                                  # what the real Tally's answer did before
            ET.fromstring(xml)
        self.assertEqual(len(tally_sales.parse_vouchers(xml, 2026, 1)[0]), 1)

    def test_an_xml_declaration_in_the_decoded_text_is_tolerated(self):
        xml = '<?xml version="1.0" encoding="UTF-16"?>' + collection([voucher(date(2026, 1, 10), guid="RAW-GUID-1")])
        self.assertEqual(len(tally_sales.parse_vouchers(xml, 2026, 1)[0]), 1)

    def test_an_answer_with_20000_vouchers_is_read_with_iterparse(self):
        template = ('<VOUCHER><DATE>20260110</DATE><GUID>G{n}</GUID><VOUCHERTYPENAME>SALES-HO</VOUCHERTYPENAME><VOUCHERNUMBER>{n}</VOUCHERNUMBER>'
                    '<ISCANCELLED>No</ISCANCELLED><ISOPTIONAL>No</ISOPTIONAL><ISSALESFLAG>Yes</ISSALESFLAG><ISCREDITNOTEFLAG>No</ISCREDITNOTEFLAG>'
                    '<PARTYLEDGERNAME>ACME TRADERS PVT</PARTYLEDGERNAME><ALLINVENTORYENTRIES.LIST><STOCKITEMNAME>ITEM {m}</STOCKITEMNAME>'
                    '<BILLEDQTY> 2 Nos</BILLEDQTY><AMOUNT>1500.00</AMOUNT></ALLINVENTORYENTRIES.LIST></VOUCHER>')
        xml = "<ENVELOPE><DATA><COLLECTION>" + "".join(template.format(n=n, m=n % 500) for n in range(20000)) + "</COLLECTION></DATA></ENVELOPE>"
        started = time.perf_counter()
        lines, stats = tally_sales.parse_vouchers(xml, 2026, 1)
        elapsed = time.perf_counter() - started
        self.assertEqual((len(lines), stats["sale_vouchers"]), (20000, 20000))
        self.assertLess(elapsed, 30)
        source = inspect.getsource(tally_sales.parse_vouchers)
        self.assertIn("iterparse", source)
        self.assertIn("element.clear()", source)                                                # each voucher is cleared once it is handled


# ----------------------------------------------------------------------------------------------- 2. keys and hash
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

    def test_the_fingerprint_ignores_order_and_notices_any_change(self):
        lines = month_lines(2026, 1, items=3)
        self.assertEqual(sales_sync.fingerprint(lines), sales_sync.fingerprint(list(reversed(lines))))
        changed = [dict(l) for l in lines]
        changed[0]["qty"] += 1
        self.assertNotEqual(sales_sync.fingerprint(lines), sales_sync.fingerprint(changed))
        self.assertNotEqual(sales_sync.fingerprint(lines), sales_sync.fingerprint(lines[1:]))
        self.assertRegex(sales_sync.fingerprint([]), r"^[0-9a-f]{64}$")


# ----------------------------------------------------------------------------------------------- 3. the request text
class RequestText(unittest.TestCase):
    def test_the_month_request_is_exactly_the_one_proven_on_the_real_tally(self):
        expected = (
            '<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE><ID>Sales Sync Vouchers</ID></HEADER>'
            '<BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES><TDL><TDLMESSAGE>'
            '<COLLECTION NAME="Sales Sync Vouchers" ISMODIFY="No"><TYPE>Voucher</TYPE><FETCH>Date, GUID, VoucherNumber, VoucherTypeName, IsCancelled, '
            'IsOptional, AllInventoryEntries.StockItemName, AllInventoryEntries.BilledQty, AllInventoryEntries.ActualQty</FETCH>'
            '<COMPUTE>IsSalesFlag : $$IsSales:$VoucherTypeName</COMPUTE><COMPUTE>IsCreditNoteFlag : $$IsCreditNote:$VoucherTypeName</COMPUTE>'
            '<FILTER>SalesSyncOnlySalesAndCreditNotes</FILTER></COLLECTION><SYSTEM TYPE="Formulae" NAME="SalesSyncOnlySalesAndCreditNotes">'
            '($$IsSales:$VoucherTypeName OR $$IsCreditNote:$VoucherTypeName) AND $$MonthOfDate:$Date = 10 AND $$YearOfDate:$Date = 2026'
            '</SYSTEM></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>')
        self.assertEqual(tally_sales.build_voucher_request(2026, 10), expected)

    def test_month_and_year_are_plain_integers_never_padded(self):
        self.assertIn("$$MonthOfDate:$Date = 3 AND $$YearOfDate:$Date = 2026", tally_sales.build_voucher_request(2026, 3))
        self.assertIn("$$MonthOfDate:$Date = 12 AND $$YearOfDate:$Date = 2025", tally_sales.build_voucher_request(2025, 12))
        self.assertNotIn("= 03", tally_sales.build_voucher_request(2026, 3))

    def test_no_date_variables_no_unfiltered_variant_and_no_diag_names(self):
        request = tally_sales.build_voucher_request(2026, 1)
        for text in ("SVFROMDATE", "SVTODATE", "Sales Diag", "SalesDiagFilter"):
            self.assertNotIn(text, request)
        self.assertIn("<FILTER>", request)
        self.assertEqual(list(inspect.signature(tally_sales.build_voucher_request).parameters), ["year", "month"])      # no with_filter flag

    def test_the_requests_ask_for_no_party_amount_rate_or_narration(self):
        for body in (tally_sales.build_voucher_request(2026, 1), tally_sales.build_company_request()):
            for word in ("amount", "rate", "party", "ledger", "narration", "address", "gst"):
                self.assertNotIn(word, body.lower(), word)

    def test_the_company_request_is_unchanged(self):
        self.assertIn("SalesSyncCurrent : $$CurrentCompany", tally_sales.build_company_request())
        self.assertIn('NAME="Sales Sync Company"', tally_sales.build_company_request())


# ----------------------------------------------------------------------------------------------- 4. reading from the (fake) Tally
class Reading(unittest.TestCase):
    def setUp(self):
        self.tally = FakeTally()
        self.addCleanup(self.tally.close)
        self.reader = tally_sales.TallyReader(tally_url=self.tally.url, retry_pause=0)

    def test_one_calendar_month_only_and_no_date_variables_are_sent(self):
        self.tally.vouchers = [voucher(date(2025, 12, 31), guid="RAW-GUID-1"), voucher(date(2026, 1, 1), guid="RAW-GUID-2"),
                               voucher(date(2026, 1, 31), guid="RAW-GUID-3"), voucher(date(2026, 2, 1), guid="RAW-GUID-4")]
        lines, stats = self.reader.fetch_month(2026, 1)
        self.assertEqual(sorted(l["day"] for l in lines), [date(2026, 1, 1), date(2026, 1, 31)])
        self.assertEqual((stats["sale_vouchers"], stats["dropped_out_of_range"], stats["skipped_no_date"]), (2, 0, 1))
        self.assertIn("$$MonthOfDate:$Date = 1 AND $$YearOfDate:$Date = 2026", self.tally.requests[0])
        self.assertNotIn("SVFROMDATE", self.tally.requests[0])

    def test_stats_carry_the_size_and_the_seconds(self):
        self.tally.vouchers = [voucher(date(2026, 1, 10), guid="RAW-GUID-1")]
        _lines, stats = self.reader.fetch_month(2026, 1)
        self.assertGreater(stats["bytes"], 1000)
        self.assertGreaterEqual(stats["seconds"], 0)
        self.assertEqual(self.reader.last_stats, stats)

    def test_delivery_notes_and_purchases_are_not_returned_and_renamed_types_are(self):
        self.tally.vouchers = [voucher(date(2026, 1, 10), vtype="DELIVERY NOTE-HO", base="delivery", guid="RAW-GUID-1"),
                               voucher(date(2026, 1, 10), vtype="PURCHASE-HO", base="purchase", guid="RAW-GUID-2"),
                               voucher(date(2026, 1, 10), vtype="MEESHO SALES", guid="RAW-GUID-3"),
                               voucher(date(2026, 1, 10), vtype="Credit Note", base="credit", guid="RAW-GUID-4")]
        self.assertEqual(sorted(l["kind"] for l in self.reader.fetch_month(2026, 1)[0]), ["return", "sale"])

    def test_a_tally_that_ignores_the_month_is_caught_by_the_guard(self):
        self.tally.ignore_month = True
        self.tally.vouchers = [voucher(date(2026, 1, 10), guid="RAW-GUID-1"), voucher(date(2026, 2, 10), guid="RAW-GUID-2")]
        with LogCapture() as log:
            lines, stats = self.reader.fetch_month(2026, 1)
        self.assertEqual(len(lines), 1)
        self.assertEqual(stats["dropped_out_of_range"], 1)
        self.assertIn("outside the requested month", log.text())

    def test_nothing_in_the_month_gives_an_empty_list(self):
        self.assertEqual(self.reader.fetch_month(2026, 1)[0], [])

    def test_the_company_name_comes_from_tallys_current_company(self):
        self.assertEqual(self.reader.company_name(), "Super Seatings Test Co")

    def test_an_unreadable_company_gives_none(self):
        self.tally.company = None
        self.assertIsNone(self.reader.company_name())

    def test_a_tally_error_answer_raises_and_there_is_no_unfiltered_fallback(self):
        self.tally.filter_supported = False
        with self.assertRaises(tally_sales.TallyReadError):
            self.reader.fetch_month(2026, 1)
        self.assertEqual(len(self.tally.requests), 1)                                              # asked once, never again without the filter
        self.assertTrue(all("<FILTER>" in body for body in self.tally.requests))

    def test_tally_not_running_raises_the_unreachable_error_after_one_retry_with_a_5_second_pause(self):
        dead = tally_sales.TallyReader(tally_url=f"http://127.0.0.1:{free_port()}")
        with mock.patch("tally_sales.time.sleep") as sleep:
            with self.assertRaises(tally_sales.TallyUnreachable):
                dead.fetch_month(2026, 1)
        self.assertEqual(sleep.call_args_list, [mock.call(5)])

    def test_the_timeouts_are_120_seconds_for_a_month_and_20_for_the_company(self):
        with mock.patch.object(tally_sales, "_session_post", return_value=("<ENVELOPE/>", 11)) as post:
            reader = tally_sales.TallyReader(tally_url="http://x")
            reader.fetch_month(2026, 1)
            self.assertEqual(post.call_args.kwargs["timeout"], 120)
            reader.company_name()
            self.assertEqual(post.call_args.kwargs["timeout"], 20)

    def test_a_hung_tally_is_not_asked_again(self):
        self.tally.hang = 3.0
        slow = tally_sales.TallyReader(tally_url=self.tally.url, voucher_timeout=0.4, retry_pause=0)
        with self.assertRaises(tally_sales.TallyTimeout):
            slow.fetch_month(2026, 1)
        time.sleep(0.2)
        self.assertEqual(len(self.tally.requests), 1)                                              # no retry after a timeout

    def test_fractional_quantities_are_logged_as_a_count_only(self):
        self.tally.vouchers = [voucher(date(2026, 1, 10), [("SECRET DESIGN", "2.5 Mtr")], guid="RAW-GUID-1")]
        with LogCapture() as log:
            self.reader.fetch_month(2026, 1)
        self.assertIn("1 fractional", log.text())
        self.assertNotIn("SECRET DESIGN", log.text())

    def test_nothing_private_survives_the_reading(self):
        self.tally.vouchers = [voucher(date(2026, 1, 10), [("ALTO 7D MAT BLACK", "3 Nos")], guid="RAW-GUID-1")]
        lines, stats = self.reader.fetch_month(2026, 1)
        self.assertEqual(set(lines[0]), {"voucher_key", "day", "kind", "item", "qty"})
        for text in FORBIDDEN_TEXT:
            self.assertNotIn(text, repr(lines) + repr(stats))


# ----------------------------------------------------------------------------------------------- 5. settings and state
class SettingsAndUrl(unittest.TestCase):
    def test_defaults_and_limits(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ("SALES_SYNC", "SALES_BACKFILL_DAYS", "SALES_INCREMENTAL_MINUTES", "SALES_NIGHTLY_HOUR"):
                os.environ.pop(name, None)
            s = sales_sync.SalesSettings.from_environment()
        self.assertEqual((s.mode, s.backfill_days, s.incremental_minutes, s.nightly_hour), ("on", 365, 30, 3))
        for env, expected in (({"SALES_BACKFILL_DAYS": "9999"}, 400), ({"SALES_BACKFILL_DAYS": "0"}, 1), ({"SALES_BACKFILL_DAYS": "abc"}, 365),
                              ({"SALES_NIGHTLY_HOUR": "30"}, 3), ({"SALES_INCREMENTAL_MINUTES": "0"}, 30), ({"SALES_INCREMENTAL_MINUTES": "45"}, 45)):
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

    def test_month_helpers(self):
        self.assertEqual(sales_sync.previous_month(2026, 1), (2025, 12))
        self.assertEqual(sales_sync.previous_month(2026, 3), (2026, 2))
        self.assertEqual(sales_sync.month_bounds(2026, 2), (date(2026, 2, 1), date(2026, 2, 28)))
        self.assertEqual(sales_sync.month_bounds(2028, 2)[1], date(2028, 2, 29))
        self.assertEqual(sales_sync.month_bounds(2026, 12)[1], date(2026, 12, 31))
        self.assertEqual(sales_sync.parse_month(sales_sync.month_key(2026, 3)), (2026, 3))


class StateFile(TempState):
    def test_state_survives_a_restart_is_version_2_and_is_written_atomically(self):
        reader, pusher = StubReader(self.clock), Pusher()
        first = make_sync(self.state_path, reader, pusher, self.clock)
        first.step(); self.clock.advance(180); first.step(); self.clock.advance(180); first.step()
        saved = self.state()
        self.assertEqual(sorted(saved), sorted(sales_sync.STATE_KEYS))
        self.assertEqual(saved["version"], 2)
        self.assertEqual(os.listdir(self.dir), ["sales_sync_state.json"])                  # no temp file left behind
        self.assertEqual(sorted(saved["months"]), ["2026-02", "2026-03"])
        for entry in saved["months"].values():
            self.assertRegex(entry["fingerprint"], r"^[0-9a-f]{64}$")
            self.assertTrue(entry["pushed_utc"])
        again = make_sync(self.state_path, StubReader(self.clock), Pusher(), self.clock)
        self.assertEqual(again.state["backfill_cursor"], saved["backfill_cursor"])
        self.assertEqual(again.state["months"], saved["months"])

    def test_a_fresh_state_starts_at_last_month_with_a_fixed_floor_month(self):
        sync = make_sync(self.state_path, StubReader(self.clock), Pusher(), self.clock, backfill_days=100)
        self.assertEqual(sync.state["backfill_cursor"], "2026-02")
        self.assertEqual(sync.state["backfill_floor"], "2025-12")                              # the month of 2026-03-20 minus 100 days
        self.assertFalse(sync.state["backfill_done"])
        sync.step(); self.clock.advance(180); sync.step()                                       # saved
        self.clock.advance(10 * 86400)                                                         # the floor does not drift as days pass
        later = make_sync(self.state_path, StubReader(self.clock), Pusher(), self.clock, backfill_days=100)
        self.assertEqual(later.state["backfill_floor"], "2025-12")

    def test_a_missing_or_corrupt_state_file_starts_fresh_and_logs_once(self):
        with LogCapture() as log:
            make_sync(self.state_path, StubReader(self.clock), Pusher(), self.clock)
            self.assertEqual(log.records, [])                                                  # a missing file is silent
            with open(self.state_path, "w", encoding="utf-8") as handle:
                handle.write("{ this is not json")
            sync = make_sync(self.state_path, StubReader(self.clock), Pusher(), self.clock)
        self.assertFalse(sync.state["backfill_done"])
        self.assertEqual(sum("starting fresh" in m for m in log.records), 1)

    def test_a_state_with_wrong_types_or_another_version_starts_fresh(self):
        for content in ({"version": 2, "backfill_cursor": 5, "months": "many"}, {"version": 1, "backfill_cursor": "2026-02"},
                        {"version": 3}, {"backfill_cursor": "2026-02"}, {"version": 2, "months": {"2026-13": {"fingerprint": "x", "pushed_utc": "2026-01-01T00:00:00+00:00"}}},
                        {"version": 2, "backfill_cursor": "2025-06", "backfill_floor": "2025-01", "backfill_done": False, "fail_streak": -1}, [1, 2]):
            with open(self.state_path, "w", encoding="utf-8") as handle:
                json.dump(content, handle)
            with LogCapture() as log:
                sync = make_sync(self.state_path, StubReader(self.clock), Pusher(), self.clock)
            self.assertEqual(sync.state["backfill_cursor"], "2026-02", content)
            self.assertIn("starting fresh", log.text())

    def test_startup_does_nothing_but_read_the_state(self):
        reader, pusher = StubReader(self.clock), Pusher()
        make_sync(self.state_path, reader, pusher, self.clock)
        self.assertEqual((reader.calls, pusher.calls), ([], []))


# ----------------------------------------------------------------------------------------------- 6. the order of steps
class Scheduling(TempState):
    def test_the_first_cycle_runs_nothing_then_incremental_pushes_the_whole_current_month(self):
        reader, pusher = StubReader(self.clock), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock)
        self.assertIsNone(sync.step())
        self.assertEqual(reader.calls, [])
        self.assertEqual(sync.step(), "incremental")
        self.assertEqual(reader.calls, [(2026, 3)])
        self.assertEqual(pusher.ranges(), [(date(2026, 3, 1), date(2026, 3, 31))])              # the whole calendar month, also the current one

    def test_at_most_one_step_per_cycle_and_the_order_is_incremental_backfill_nightly(self):
        reader, pusher = StubReader(self.clock), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock)
        labels = self.run_cycles(sync, 6)
        self.assertEqual(labels[:5], [None, "incremental", "backfill", "nightly", "backfill"])
        self.assertEqual(len([c for c in reader.calls]), sum({"incremental": 1, "backfill": 1, "nightly": 3}.get(l, 0) for l in labels))

    def test_history_goes_newest_month_first_down_to_the_floor_month_and_stops(self):
        reader, pusher = StubReader(self.clock), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, incremental_minutes=100000)
        sync.state["last_nightly_day"] = TODAY.isoformat()                                   # keep the nightly out of this picture
        with LogCapture() as log:
            labels = self.run_cycles(sync, 30)
        self.assertEqual(reader.calls, [(2026, 3), (2026, 2), (2026, 1), (2025, 12)])           # the incremental, then newest to oldest
        self.assertEqual(labels.count("backfill"), 3)
        self.assertTrue(sync.state["backfill_done"])
        self.assertEqual(pusher.months(), ["2026-03", "2026-02", "2026-01", "2025-12"])        # one unbroken stretch that ends with this month
        self.assertEqual(pusher.ranges()[-1], (date(2025, 12, 1), date(2025, 12, 31)))
        self.assertEqual(sum("backfill complete (100 days)" in m for m in log.records), 1)

    def test_backfill_steps_are_at_least_two_cycles_apart(self):
        reader, pusher = StubReader(self.clock), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, incremental_minutes=100000)
        sync.state["last_nightly_day"] = TODAY.isoformat()
        labels = self.run_cycles(sync, 12)
        positions = [i for i, l in enumerate(labels) if l == "backfill"]
        self.assertTrue(all(b - a >= 2 for a, b in zip(positions, positions[1:])), positions)

    def test_a_month_with_no_vouchers_is_a_valid_answer_and_history_continues(self):
        reader, pusher = StubReader(self.clock, lines={"2026-02": []}), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, incremental_minutes=100000)
        sync.state["last_nightly_day"] = TODAY.isoformat()
        self.run_cycles(sync, 12)
        self.assertEqual(reader.calls[:3], [(2026, 3), (2026, 2), (2026, 1)])
        empty = next(c for c in pusher.calls if c["from"] == "2026-02-01")
        self.assertEqual((empty["to"], empty["lines"]), ("2026-02-28", []))                   # the empty month is still pushed (deleted slips vanish)
        self.assertTrue(sync.state["backfill_done"])

    def test_a_failed_month_leaves_the_cursor_so_the_same_month_is_retried(self):
        reader, pusher = StubReader(self.clock), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, incremental_minutes=100000)
        sync.state["last_nightly_day"] = TODAY.isoformat()
        sync.step(); sync.step()                                                              # the incremental
        pusher.answers = [(503, "busy")]
        self.clock.advance(180); self.assertEqual(sync.step(), "backfill")
        self.assertEqual(sync.state["backfill_cursor"], "2026-02")
        pusher.answers = []
        for _ in range(8):
            self.clock.advance(180)
            sync.step()
        self.assertEqual(reader.calls.count((2026, 2)), 2)                                      # the failed month read again, never skipped

    def test_the_incremental_interval_is_30_minutes_and_the_nightly_runs_once_a_day(self):
        reader, pusher = StubReader(self.clock), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, backfill_days=1)
        labels = self.run_cycles(sync, 12)                                                    # 36 minutes of 3-minute cycles
        self.assertEqual(labels.count("incremental"), 2)                                      # at minute 3 and 33
        self.assertEqual(labels.count("nightly"), 1)
        self.clock.advance(86400)                                                             # the next day
        self.assertEqual(self.run_cycles(sync, 4).count("nightly"), 1)

    def test_the_nightly_waits_for_its_hour(self):
        reader, pusher = StubReader(self.clock), Pusher()
        self.clock.utc = datetime(2026, 3, 19, 20, 0, tzinfo=timezone.utc)                    # 01:30 in India, before 03:00
        sync = make_sync(self.state_path, reader, pusher, self.clock, backfill_days=1)
        self.assertNotIn("nightly", self.run_cycles(sync, 6))
        self.clock.advance(3 * 3600)                                                          # now 04:30
        self.assertIn("nightly", self.run_cycles(sync, 3))

    def test_nothing_runs_when_the_stock_cycle_failed(self):
        reader, pusher = StubReader(self.clock), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock)
        sync.step()
        self.assertIsNone(sync.step(stock_ok=False))
        self.assertEqual(reader.calls, [])

    def test_the_nightly_reads_the_current_month_and_the_two_before_it(self):
        reader, pusher = StubReader(self.clock), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, backfill_days=1)
        labels = self.run_cycles(sync, 4)
        self.assertIn("nightly", labels)
        self.assertEqual(reader.calls[-3:], [(2026, 3), (2026, 2), (2026, 1)])

    def test_a_tiny_history_needs_no_backfill(self):
        sync = make_sync(self.state_path, StubReader(self.clock), Pusher(), self.clock, backfill_days=2)
        self.assertTrue(sync.state["backfill_done"])

    def test_the_year_boundary(self):
        self.clock.utc = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
        reader, pusher = StubReader(self.clock), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, backfill_days=60, incremental_minutes=100000)
        sync.state["last_nightly_day"] = "2026-01-15"
        self.run_cycles(sync, 10)
        self.assertEqual(reader.calls, [(2026, 1), (2025, 12), (2025, 11)])


# ----------------------------------------------------------------------------------------------- 7. only what changed is pushed
class Unchanged(TempState):
    def started(self, **kwargs):
        reader, pusher = StubReader(self.clock, **kwargs), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, backfill_days=1)
        sync.step(); self.clock.advance(180); sync.step()                                       # the first incremental: pushed
        return sync, reader, pusher

    def test_the_same_month_again_is_read_but_not_pushed(self):
        sync, reader, pusher = self.started()
        self.assertEqual(len(pusher.calls), 1)
        self.clock.advance(31 * 60)
        with LogCapture() as log:
            self.assertEqual(sync.step(), "incremental")
        self.assertEqual((len(reader.calls), len(pusher.calls)), (2, 1))                      # read again, not pushed again
        self.assertIn("Sales sync: incremental 2026-03 unchanged, no push", log.text())

    def test_a_changed_month_is_pushed_again(self):
        sync, reader, pusher = self.started()
        reader.lines["2026-03"] = month_lines(2026, 3, qty=7)
        self.clock.advance(31 * 60)
        sync.step()
        self.assertEqual(len(pusher.calls), 2)

    def test_an_unchanged_month_is_still_pushed_once_in_24_hours(self):
        sync, reader, pusher = self.started()
        self.clock.advance(23 * 3600)
        sync.step()
        self.assertEqual(len(pusher.calls), 1)
        self.clock.advance(2 * 3600)                                                           # now more than 24 hours since the push
        sync.step()
        self.assertEqual(len(pusher.calls), 2)

    def test_when_the_website_does_not_take_a_month_its_fingerprint_is_forgotten(self):
        sync, reader, pusher = self.started()
        reader.lines["2026-03"] = month_lines(2026, 3, qty=7)
        pusher.answers = [(503, "busy")]
        self.clock.advance(31 * 60)
        sync.step()
        self.assertNotIn("2026-03", sync.state["months"])
        self.assertNotIn("2026-03", self.state()["months"])
        reader.lines["2026-03"] = month_lines(2026, 3)                                         # back to what the website already has: still pushed
        self.clock.advance(180)
        sync.step()
        self.assertEqual(len(pusher.calls), 3)
        self.assertIn("2026-03", sync.state["months"])

    def test_every_non_success_answer_forgets_the_fingerprint(self):
        for status in (404, 409, 413, 422, 500, 401):
            self.setUp()
            sync, reader, pusher = self.started()
            reader.lines["2026-03"] = month_lines(2026, 3, qty=7)
            pusher.answers = [(status, "{}")]
            self.clock.advance(31 * 60)
            sync.step()
            self.assertNotIn("2026-03", sync.state["months"], status)

    def test_the_nightly_pushes_an_older_month_whose_slips_were_deleted(self):
        reader, pusher = StubReader(self.clock, lines={"2026-01": month_lines(2026, 1, items=3)}), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, incremental_minutes=100000)
        self.run_cycles(sync, 14)
        self.assertTrue(sync.state["backfill_done"])
        self.assertIn("2026-01", pusher.months())
        pushed = len(pusher.calls)
        reader.lines["2026-01"] = month_lines(2026, 1, items=3)[:-3]                           # slips of the month's last day were deleted in Tally
        sync.state["last_nightly_day"] = None
        self.run_cycles(sync, 4)
        again = [c for c in pusher.calls[pushed:]]
        self.assertEqual([c["from"] for c in again], ["2026-01-01"])                          # only the month that changed was pushed
        self.assertEqual(len(again[0]["lines"]), 6)

    def test_a_nightly_with_nothing_changed_pushes_nothing(self):
        reader, pusher = StubReader(self.clock), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, incremental_minutes=100000)
        self.run_cycles(sync, 14)
        pushed = len(pusher.calls)
        sync.state["last_nightly_day"] = None
        self.run_cycles(sync, 4)
        self.assertEqual(len(pusher.calls), pushed)


# ----------------------------------------------------------------------------------------------- 8. the slow guard
class SlowGuard(TempState):
    def first_incremental(self, read_seconds, error=None, incremental_minutes=30):
        reader, pusher = StubReader(self.clock, read_seconds=read_seconds, error=error), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, backfill_days=1, incremental_minutes=incremental_minutes)
        sync.state["last_nightly_day"] = TODAY.isoformat()                                   # keep the nightly out of this picture
        sync.step(); self.clock.advance(180); sync.step()
        return sync, reader, pusher

    def test_a_read_slower_than_40_seconds_doubles_the_incremental_interval(self):
        sync, reader, _pusher = self.first_incremental(41)
        self.assertEqual(sync.interval_minutes(), 60)
        reader.read_seconds = 0
        calls = len(reader.calls)
        self.clock.advance(45 * 60)                                                           # past the configured 30 minutes, not past 60
        sync.step()
        self.assertEqual(len(reader.calls), calls)
        self.clock.advance(20 * 60)
        self.assertEqual(sync.step(), "incremental")

    def test_the_slow_line_is_15_seconds(self):
        self.assertEqual(sales_sync.SLOW_READ_SECONDS, 15.0)
        sync, _reader, _pusher = self.first_incremental(14)
        self.assertEqual(sync.interval_minutes(), 30)                                          # 14 s is still fine

    def test_a_16_second_read_is_slow(self):
        with LogCapture() as log:
            sync, _reader, _pusher = self.first_incremental(16)
        self.assertEqual(sync.interval_minutes(), 60)
        self.assertTrue(any("Tally needed 16 s to read a month" in m for m in log.records))

    def test_the_interval_goes_on_doubling_up_to_4_hours_and_not_beyond(self):
        sync, reader, _pusher = self.first_incremental(41)
        for expected in (120, 240, 240):
            self.clock.advance(sync.interval_minutes() * 60 + 60)
            sync.step()
            self.assertEqual(sync.interval_minutes(), expected)

    def test_a_fast_read_brings_the_configured_interval_back_and_a_middling_one_keeps_it(self):
        sync, reader, _pusher = self.first_incremental(41)
        reader.read_seconds = 12
        self.clock.advance(sync.interval_minutes() * 60 + 60)
        sync.step()
        self.assertEqual(sync.interval_minutes(), 60)                                          # 10 to 15 seconds: neither faster nor slower
        reader.read_seconds = 5
        self.clock.advance(sync.interval_minutes() * 60 + 60)
        sync.step()
        self.assertEqual(sync.interval_minutes(), 30)

    def test_a_configured_interval_longer_than_4_hours_is_never_shortened(self):
        sync, _reader, _pusher = self.first_incremental(41, incremental_minutes=600)
        self.assertEqual(sync.interval_minutes(), 600)

    def test_the_warning_is_logged_once_an_hour(self):
        sync = make_sync(self.state_path, StubReader(self.clock), Pusher(), self.clock)
        with LogCapture() as log:
            for _ in range(5):
                sync._note_read_time(50)
            self.clock.advance(3601)
            sync._note_read_time(50)
        slow = [(level, m) for level, m in zip(log.levels, log.records) if "Tally needed" in m]
        self.assertEqual(len(slow), 2)                                                        # once, and again after an hour
        self.assertTrue(all(level == logging.WARNING for level, _m in slow))

    def test_a_timeout_is_a_failed_step_with_backoff_and_the_month_is_tried_again(self):
        sync, reader, pusher = self.first_incremental(0, error=tally_sales.TallyTimeout("slow"))
        self.assertEqual(pusher.calls, [])
        self.assertEqual(sync.state["fail_streak"], 1)
        self.assertIsNone(sync.state["last_incremental_utc"])
        self.assertGreater(sync._skip_cycles, 0)

    def test_a_timeout_on_a_backfill_month_leaves_the_cursor(self):
        reader, pusher = StubReader(self.clock), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, incremental_minutes=100000)
        sync.step(); sync.step()
        reader.error = tally_sales.TallyTimeout("slow")
        self.clock.advance(180)
        self.assertEqual(sync.step(), "backfill")
        self.assertEqual(sync.state["backfill_cursor"], "2026-02")

    def test_only_the_read_time_counts_not_the_push(self):
        reader = StubReader(self.clock, read_seconds=5)

        class SlowPusher(Pusher):
            def __call__(inner, payload):
                self.clock.advance(100)
                return super().__call__(payload)
        sync = make_sync(self.state_path, reader, SlowPusher(), self.clock, backfill_days=1)
        sync.step(); self.clock.advance(180); sync.step()
        self.assertEqual(sync.interval_minutes(), 30)


# ----------------------------------------------------------------------------------------------- 9. splitting
class Splitting(TempState):
    def test_a_month_of_more_than_25000_lines_is_split_into_in_month_requests(self):
        def many():
            out = []
            for day in range(1, 32):
                out += [{"voucher_key": f"{n:016x}", "day": date(2026, 3, day), "kind": "sale", "item": f"I{n}", "qty": 1} for n in range(2000)]
            return out
        reader, pusher = StubReader(self.clock, lines={"2026-03": many()}), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, backfill_days=1)
        sync.step(); sync.step()
        self.assertGreater(len(pusher.calls), 2)
        for call in pusher.calls:
            self.assertLessEqual(len(call["lines"]), 25000)
            self.assertTrue(all(call["from"] <= l["d"] <= call["to"] for l in call["lines"]))
        ranges = sorted(pusher.ranges())
        self.assertEqual((ranges[0][0], ranges[-1][1]), (date(2026, 3, 1), date(2026, 3, 31)))
        for a, b in zip(ranges, ranges[1:]):
            self.assertEqual(b[0], a[1] + timedelta(days=1))                                     # the pieces tile the month exactly
        self.assertEqual(sum(len(c["lines"]) for c in pusher.calls), 62000)

    def test_a_small_month_is_one_push_with_the_contract_keys(self):
        reader, pusher = StubReader(self.clock), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, backfill_days=1)
        sync.step(); sync.step()
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(set(pusher.calls[0]), {"company", "from", "to", "lines"})
        self.assertEqual(set(pusher.calls[0]["lines"][0]), {"v", "d", "k", "i", "q"})


# ----------------------------------------------------------------------------------------------- 10. website answers
class WebsiteAnswers(TempState):
    def started(self, answers, **kwargs):
        reader, pusher = StubReader(self.clock, **kwargs), Pusher(answers)
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
        sync, _reader, _pusher = self.started([(409, "x")])
        self.cycles(sync, 1)
        again = make_sync(self.state_path, StubReader(self.clock), Pusher(), self.clock)
        again.step(); self.clock.advance(180)
        self.assertIsNone(again.step())

    def test_422_is_logged_once_an_hour_with_the_reason_and_counts_as_a_failure(self):
        with LogCapture() as log:
            sync, _reader, pusher = self.started([(422, '{"error": "this push would remove most of the stored sales lines", "refused": true}')] * 200)
            self.cycles(sync, 60)                                                               # three hours of cycles
        lines = [m for m in log.records if "refused" in m.lower() and "Sales sync" in m]
        self.assertLessEqual(len(lines), 4)
        self.assertGreaterEqual(len(lines), 1)
        self.assertIn("(422)", lines[0])
        self.assertIn("would remove most of the stored sales lines", lines[0])
        self.assertGreaterEqual(sync.state["fail_streak"], 1)

    def test_413_is_a_failure_that_backs_off_and_the_month_is_retried(self):
        sync, _reader, pusher = self.started([(413, "too large")])
        with LogCapture() as log:
            self.cycles(sync, 1)
        self.assertEqual(sync.state["fail_streak"], 1)
        self.assertNotIn("2026-03", sync.state["months"])
        self.assertIn("too large", log.text())

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

    def test_a_pusher_that_raises_is_a_quiet_failure(self):
        class Raising:
            def __call__(inner, payload):
                raise ConnectionError("secret address 10.0.0.1")
        sync = make_sync(self.state_path, StubReader(self.clock), Raising(), self.clock)
        with LogCapture() as log:
            sync.step()
            self.cycles(sync, 5)
        self.assertGreaterEqual(sync.state["fail_streak"], 1)
        self.assertNotIn("10.0.0.1", log.text())

    def test_failures_back_off_instead_of_hammering(self):
        sync, reader, pusher = self.started([], error=tally_sales.TallyReadError("boom"))
        self.cycles(sync, 60)
        self.assertLess(len(reader.calls), 12)                                                  # 60 cycles, far fewer attempts
        self.assertGreaterEqual(sync.state["fail_streak"], 3)

    def test_the_backoff_is_capped_at_60_cycles_and_resets_on_success(self):
        sync, reader, pusher = self.started([])
        sync.state["fail_streak"] = 30
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
        self.assertNotIn("force", inspect.getsource(sales_sync).lower().replace("never sends a force header", ""))

    def test_an_unreadable_company_means_nothing_is_pushed_and_one_log_line(self):
        reader, pusher = StubReader(self.clock, company=None), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock)
        with LogCapture() as log:
            sync.step()
            for _ in range(20):
                self.clock.advance(180)
                sync.step()
        self.assertEqual(pusher.calls, [])
        self.assertEqual(sum("company" in m.lower() and "Sales sync" in m for m in log.records), 1)

    def test_a_different_company_resets_the_history_and_skips_that_step(self):
        reader, pusher = StubReader(self.clock, company="First Co"), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, incremental_minutes=100000)
        self.run_cycles(sync, 6)
        self.assertGreater(len(sync.state["months"]), 0)
        reader.company = "Second Co"
        sync.state["last_nightly_day"] = None
        with LogCapture() as log:
            self.clock.advance(180)
            self.assertIsNone(sync.step())
        self.assertEqual(sync.state["months"], {})
        self.assertEqual(sync.state["company"], tally_sales.company_hash("Second Co"))
        self.assertIn("different company", log.text())


# ----------------------------------------------------------------------------------------------- 11. off and dry run
class Modes(TempState):
    def test_off_does_nothing_at_all(self):
        reader, pusher = StubReader(self.clock), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, mode="off")
        with LogCapture() as log:
            for _ in range(10):
                self.assertIsNone(sync.step())
                self.clock.advance(180)
        self.assertEqual((reader.calls, pusher.calls), ([], []))
        self.assertEqual(sum("Sales sync: off" in m for m in log.records), 1)
        self.assertFalse(os.path.exists(self.state_path))

    def test_dryrun_reads_months_and_logs_counts_seconds_and_a_fingerprint_but_never_pushes_or_saves(self):
        reader, pusher = StubReader(self.clock, read_seconds=2), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, mode="dryrun")
        with LogCapture() as log:
            self.run_cycles(sync, 6)
        self.assertGreater(len(reader.calls), 0)
        self.assertEqual(pusher.calls, [])
        self.assertFalse(os.path.exists(self.state_path))
        dry = [m for m in log.records if "dry run" in m.lower()]
        self.assertTrue(dry and all("would push" in m for m in dry))
        self.assertRegex(dry[0], r"incremental 2026-03 read 3 lines in 2\.0 s \(fingerprint [0-9a-f]{8}\)")


class DryRunLeavesNoTrace(TempState):
    """A dry run only reads and logs. Whatever it 'does' must not move the saved progress, so that switching to "on" loads the whole
    history and runs today's nightly step as if the dry run had never happened."""

    def dry_run(self, steps=14, walked=4, **settings):
        reader, pusher = StubReader(self.clock), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, mode="dryrun", **settings)
        before = dict(sync.state)
        for _ in range(steps):                                                                # enough to walk the whole backfill and the nightly
            sync.step()
            self.clock.advance(180)
        self.assertEqual(pusher.calls, [])
        self.assertGreaterEqual(len(set(reader.calls)), walked, "the dry run really walked the months")
        return sync, before

    def assert_a_full_start_after_the_switch(self, sync, pusher, log, steps=16):
        for _ in range(steps):
            sync.step()
            self.clock.advance(180)
        self.assertEqual(pusher.months()[:4], ["2026-03", "2026-02", "2026-01", "2025-12"])   # the current month, then the history newest first
        self.assertEqual(sorted(set(pusher.months())), ["2025-12", "2026-01", "2026-02", "2026-03"])
        self.assertTrue(sync.state["backfill_done"])
        self.assertEqual(sync.state["last_nightly_day"], TODAY.isoformat())                  # the nightly step ran that day
        self.assertTrue(any("Sales sync: nightly" in m for m in log.records))
        self.assertTrue(any("backfill complete" in m for m in log.records))

    def test_switching_from_dryrun_to_on_on_the_same_object_starts_from_the_real_state(self):
        sync, before = self.dry_run()
        self.assertEqual(sync.state, before)                                                  # the real state did not move
        pusher = Pusher()
        sync._pusher = pusher
        sync.settings.mode = "on"
        with LogCapture() as log:
            self.assert_a_full_start_after_the_switch(sync, pusher, log)

    def test_switching_from_dryrun_to_on_in_a_new_object_starts_from_the_real_state(self):
        self.dry_run()
        self.assertFalse(os.path.exists(self.state_path))
        pusher = Pusher()
        sync = make_sync(self.state_path, StubReader(self.clock), pusher, self.clock, mode="on")
        with LogCapture() as log:
            self.assert_a_full_start_after_the_switch(sync, pusher, log)

    def test_a_dry_run_leaves_a_missing_state_file_missing(self):
        self.dry_run()
        self.assertFalse(os.path.exists(self.state_path))
        self.assertEqual(os.listdir(self.dir), [])

    def test_a_dry_run_leaves_an_existing_state_file_byte_identical(self):
        reader, pusher = StubReader(self.clock), Pusher()
        live = make_sync(self.state_path, reader, pusher, self.clock, mode="on")
        for _ in range(4):
            live.step()
            self.clock.advance(180)
        with open(self.state_path, "rb") as handle:
            saved = handle.read()
        self.dry_run(steps=20, walked=2)
        with open(self.state_path, "rb") as handle:
            self.assertEqual(handle.read(), saved)
        self.assertEqual(os.listdir(self.dir), ["sales_sync_state.json"])                    # and no temp file beside it
        restarted = make_sync(self.state_path, StubReader(self.clock), Pusher(), self.clock, mode="dryrun")
        restarted.step(); restarted.step()
        with open(self.state_path, "rb") as handle:
            self.assertEqual(handle.read(), saved)

    def test_a_dry_run_that_meets_another_company_or_a_failure_still_changes_nothing(self):
        live = make_sync(self.state_path, StubReader(self.clock, company="First Co"), Pusher(), self.clock, mode="on")
        for _ in range(4):
            live.step()
            self.clock.advance(180)
        with open(self.state_path, "rb") as handle:
            saved = handle.read()
        sync = make_sync(self.state_path, StubReader(self.clock, company="Second Co"), Pusher(), self.clock, mode="dryrun")
        for _ in range(6):
            sync.step()
            self.clock.advance(180)
        failing = make_sync(self.state_path, StubReader(self.clock, error=tally_sales.TallyReadError("boom")), Pusher(), self.clock, mode="dryrun")
        for _ in range(6):
            failing.step()
            self.clock.advance(180)
        with open(self.state_path, "rb") as handle:
            self.assertEqual(handle.read(), saved)
        self.assertEqual(failing.state["fail_streak"], 0)
        again = make_sync(self.state_path, StubReader(self.clock, company="First Co"), Pusher(), self.clock, mode="on")
        self.assertEqual(again.state["company"], tally_sales.company_hash("First Co"))

    def test_a_failure_during_a_dry_run_does_not_delay_the_first_real_step(self):
        reader = StubReader(self.clock, error=tally_sales.TallyReadError("boom"))
        pusher = Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, mode="dryrun")
        for _ in range(6):
            sync.step()
            self.clock.advance(180)
        reader.error = None
        sync.settings.mode = "on"
        sync._pusher = pusher
        self.assertEqual(sync.step(), "incremental")                                          # no back-off left over from the dry run
        self.assertEqual(pusher.months(), ["2026-03"])


class StockMovement(TempState):
    """The current month is only read again when the stock moved since the last read and the interval has passed, or after two quiet
    hours. The very first read after a start happens at once. History and the nightly step do not depend on stock movement."""

    def started(self, **settings):
        reader, pusher = StubReader(self.clock), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, backfill_days=1, **settings)
        sync.state["last_nightly_day"] = TODAY.isoformat()                                   # keep the nightly out of this picture
        return sync, reader, pusher

    def seconds_until_the_next_read(self, sync, changed_at=(), limit=12 * 3600):
        """Steps every 3 minutes ('changed_at': the minute numbers at which the stock moved) until a step reads; returns the seconds."""
        waited = 0
        while waited < limit:
            self.clock.advance(180)
            waited += 180
            if sync.step(True, (waited // 60) in changed_at):
                return waited
        return None

    def test_the_first_step_after_a_start_reads_at_once_even_when_the_stock_did_not_move(self):
        sync, reader, _pusher = self.started()
        self.assertIsNone(sync.step(True, False))                                           # the first cycle after a start never reads
        self.clock.advance(180)
        self.assertEqual(sync.step(True, False), "incremental")
        self.assertEqual(reader.calls, [(2026, 3)])

    def test_no_change_means_no_read_until_two_hours_have_passed(self):
        sync, reader, _pusher = self.started()
        sync.step(True, False); self.clock.advance(180); sync.step(True, False)              # the first read
        waited = self.seconds_until_the_next_read(sync)
        self.assertGreaterEqual(waited, 2 * 3600)
        self.assertLess(waited, 2 * 3600 + 180)
        self.assertEqual(len(reader.calls), 2)

    def test_a_change_makes_the_next_read_happen_when_the_interval_has_passed(self):
        sync, reader, _pusher = self.started()
        sync.step(True, False); self.clock.advance(180); sync.step(True, False)
        waited = self.seconds_until_the_next_read(sync, changed_at=(6,))                    # the stock moved at minute 6, long before the 30 minutes
        self.assertGreaterEqual(waited, 30 * 60)                                            # remembered until the interval has passed
        self.assertLess(waited, 30 * 60 + 180)

    def test_a_change_right_before_the_interval_is_read_at_the_interval(self):
        sync, _reader, _pusher = self.started()
        sync.step(True, False); self.clock.advance(180); sync.step(True, False)
        waited = self.seconds_until_the_next_read(sync, changed_at=(27,))
        self.assertGreaterEqual(waited, 30 * 60)
        self.assertLess(waited, 30 * 60 + 180)

    def test_a_read_uses_up_the_change_so_the_next_quiet_stretch_waits_two_hours_again(self):
        sync, reader, _pusher = self.started()
        sync.step(True, False); self.clock.advance(180); sync.step(True, False)
        first = self.seconds_until_the_next_read(sync, changed_at=(3,))
        self.assertLess(first, 31 * 60)
        second = self.seconds_until_the_next_read(sync)
        self.assertGreaterEqual(second, 2 * 3600)
        self.assertEqual(len(reader.calls), 3)

    def test_an_interval_longer_than_two_hours_is_never_undercut(self):
        sync, _reader, _pusher = self.started(incremental_minutes=300)
        sync.step(True, False); self.clock.advance(180); sync.step(True, True)
        waited = self.seconds_until_the_next_read(sync, changed_at=tuple(range(1, 400)))
        self.assertGreaterEqual(waited, 300 * 60)

    def test_history_and_the_nightly_step_ignore_stock_movement(self):
        reader, pusher = StubReader(self.clock), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock)                       # 100 days of history, nightly due
        sync.step(True, False); self.clock.advance(180)
        labels = []
        for _ in range(12):
            labels.append(sync.step(True, False))                                           # the stock never moves
            self.clock.advance(180)
        self.assertEqual(labels[0], "incremental")
        self.assertEqual(labels.count("backfill"), 3)
        self.assertEqual(labels.count("nightly"), 1)

    def test_the_stock_signal_is_remembered_across_steps_that_cannot_run(self):
        sync, reader, _pusher = self.started()
        sync.step(True, False); self.clock.advance(180); sync.step(True, False)
        self.clock.advance(31 * 60)
        self.assertIsNone(sync.step(False, True))                                           # the stock cycle failed, but it said the stock moved
        self.clock.advance(180)
        self.assertEqual(sync.step(True, False), "incremental")


class ShopHours(TempState):
    """History and the nightly re-check wait while the shop is open (default 10:00-19:30, Monday to Saturday). Sunday counts as closed;
    an empty setting means no limit. The current month is still read during shop hours."""

    FRIDAY = (2026, 3, 20)
    SATURDAY = (2026, 3, 21)
    SUNDAY = (2026, 3, 22)

    def set_local(self, day, hour, minute):
        self.clock.utc = datetime(*day, hour, minute, tzinfo=timezone.utc) - timedelta(hours=5, minutes=30)

    def runs(self, label, day, hour, minute, hours="10:00-19:30"):
        if os.path.exists(self.state_path):
            os.remove(self.state_path)                                                         # a state saved by the call before would change the picture
        self.set_local(day, hour, minute)
        reader, pusher = StubReader(self.clock), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, incremental_minutes=100000, shop_hours=hours,
                         backfill_days=100 if label == "backfill" else 1)
        sync.state["last_incremental_utc"] = self.clock.utc.isoformat()                    # the current month is not due
        sync.state["last_nightly_day"] = self.clock.local_now().date().isoformat() if label == "backfill" else None
        self.assertIsNone(sync.step())                                                       # the first cycle after a start
        return sync.step() == label

    def test_the_boundaries_of_a_weekday(self):
        for label in ("backfill", "nightly"):
            self.assertTrue(self.runs(label, self.FRIDAY, 9, 59), (label, "09:59"))
            self.assertFalse(self.runs(label, self.FRIDAY, 10, 0), (label, "10:00"))
            self.assertFalse(self.runs(label, self.FRIDAY, 14, 30), (label, "14:30"))
            self.assertFalse(self.runs(label, self.FRIDAY, 19, 29), (label, "19:29"))
            self.assertTrue(self.runs(label, self.FRIDAY, 19, 30), (label, "19:30"))
            self.assertTrue(self.runs(label, self.FRIDAY, 23, 0), (label, "23:00"))

    def test_saturday_counts_as_a_shop_day_and_sunday_does_not(self):
        for label in ("backfill", "nightly"):
            self.assertFalse(self.runs(label, self.SATURDAY, 14, 30), (label, "Saturday"))
            self.assertTrue(self.runs(label, self.SUNDAY, 14, 30), (label, "Sunday"))

    def test_an_empty_setting_means_no_limit(self):
        for label in ("backfill", "nightly"):
            self.assertTrue(self.runs(label, self.FRIDAY, 14, 30, hours=""), label)

    def test_another_window_can_be_set(self):
        for label in ("backfill", "nightly"):
            self.assertFalse(self.runs(label, self.FRIDAY, 7, 30, hours="07:00-21:00"), label)
            self.assertTrue(self.runs(label, self.FRIDAY, 21, 0, hours="07:00-21:00"), label)

    def test_the_current_month_is_still_read_during_shop_hours(self):
        self.set_local(self.FRIDAY, 14, 30)
        reader, pusher = StubReader(self.clock), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, shop_hours="10:00-19:30")
        sync.step()
        self.assertEqual(sync.step(), "incremental")

    def test_history_waits_for_the_evening_and_then_loads(self):
        self.set_local(self.FRIDAY, 18, 0)
        reader, pusher = StubReader(self.clock), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, incremental_minutes=100000, shop_hours="10:00-19:30")
        sync.state["last_nightly_day"] = "2026-03-20"
        labels = []
        for _ in range(40):                                                                   # from 18:00, 3 minutes at a time
            labels.append(sync.step())
            self.clock.advance(180)
        self.assertEqual(labels[:30].count("backfill"), 0)                                  # nothing before 19:30 (30 cycles x 3 minutes)
        self.assertGreater(labels[30:].count("backfill"), 0)

    def test_the_setting_is_read_from_the_environment(self):
        for env, expected in (({}, "10:00-19:30"), ({"SALES_SHOP_HOURS": ""}, ""), ({"SALES_SHOP_HOURS": " 08:00-20:00 "}, "08:00-20:00"),
                              ({"SALES_SHOP_HOURS": "garbage"}, "10:00-19:30"), ({"SALES_SHOP_HOURS": "20:00-08:00"}, "10:00-19:30"),
                              ({"SALES_SHOP_HOURS": "10:00"}, "10:00-19:30")):
            with mock.patch.dict(os.environ, env):
                if not env:
                    os.environ.pop("SALES_SHOP_HOURS", None)
                self.assertEqual(sales_sync.SalesSettings.from_environment().shop_hours, expected, env)

    def test_parse_shop_hours(self):
        self.assertIsNone(sales_sync.parse_shop_hours(""))
        opens, closes = sales_sync.parse_shop_hours("10:00-19:30")
        self.assertEqual((opens.hour, opens.minute, closes.hour, closes.minute), (10, 0, 19, 30))
        for bad in ("10:00", "x-y", "19:30-10:00", "10:00-10:00", "25:00-26:00"):
            with self.assertRaises(ValueError):
                sales_sync.parse_shop_hours(bad)


class ReadTimingInTheLog(TempState):
    def test_pushed_and_unchanged_lines_carry_the_read_seconds_and_the_size(self):
        reader, pusher = StubReader(self.clock, read_seconds=3.1, size=524288), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, backfill_days=1)
        sync.state["last_nightly_day"] = TODAY.isoformat()
        with LogCapture() as log:
            sync.step(); self.clock.advance(180); sync.step()
            self.clock.advance(31 * 60); sync.step()
        self.assertIn("Sales sync: incremental 2026-03 pushed 3 lines (read 3.1 s, 0.5 MB)", log.text())
        self.assertIn("Sales sync: incremental 2026-03 unchanged, no push (3 lines, read 3.1 s, 0.5 MB)", log.text())

    def test_the_log_never_holds_an_item_name(self):
        reader, pusher = StubReader(self.clock, read_seconds=1, size=1000), Pusher()
        sync = make_sync(self.state_path, reader, pusher, self.clock, backfill_days=1)
        with LogCapture() as log:
            sync.step(); self.clock.advance(180); sync.step()
        self.assertNotIn("ALTO", log.text())


# ----------------------------------------------------------------------------------------------- 12. end to end
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

    def fake_history(self, days=100):
        """One sale per day of ALTO 7D MAT BLACK (day number + 1 units), a few returns, and noise vouchers that must not count."""
        out = []
        for n in range(days):
            day = TODAY - timedelta(days=n)
            out.append(voucher(day, [("ALTO 7D MAT BLACK", f"{1 + n % 4} Nos"), ("SWIFT 7D MAT GREY", "2 Nos")], guid=f"RAW-GUID-S{n}", number=str(n)))
            if n % 10 == 0:
                out.append(voucher(day, [("ALTO 7D MAT BLACK", "1 Nos")], vtype="Credit Note", base="credit", guid=f"RAW-GUID-C{n}", number=f"C{n}"))
            if n % 7 == 0:
                out.append(voucher(day, [("ALTO 7D MAT BLACK", "9 Nos")], vtype="DELIVERY NOTE-HO", base="delivery", guid=f"RAW-GUID-R{n}", number=f"R{n}"))
            if n % 13 == 0:
                out.append(voucher(day, [("ALTO 7D MAT BLACK", "5 Nos")], guid=f"RAW-GUID-X{n}", number=f"X{n}", cancelled=True))
            if n % 11 == 0:
                out.append(voucher(day, [("SWIFT 7D MAT GREY", "4 Nos")], vtype="MEESHO SALES", guid=f"RAW-GUID-M{n}", number=f"M{n}"))
        return out

    def expected(self, vouchers):
        net = {}
        for v in vouchers:
            if v["cancelled"] or v["optional"] or v["base"] not in ("sales", "credit"):
                continue
            sign = 1 if v["base"] == "sales" else -1
            for name, text in v["entries"]:
                net[name.upper()] = net.get(name.upper(), 0) + sign * tally_sales.parse_quantity(text)[0]
        return net

    def site_net(self):
        with self.db.get_connection() as conn:
            rows = conn.execute("SELECT item_key, SUM(CASE WHEN kind = 'sale' THEN qty ELSE -qty END) FROM sales_lines GROUP BY item_key").fetchall()
        return {r[0]: r[1] for r in rows}

    def make(self, **settings):
        reader = tally_sales.TallyReader(tally_url=self.tally.url, retry_pause=0)
        return make_sync(self.state_path, reader, self.pusher, self.clock, **settings)

    def run_until_done(self, sync, limit=60):
        for _ in range(limit):
            sync.step()
            self.clock.advance(180)
            yield
            if sync.state["backfill_done"]:
                return

    def test_a_backfill_loads_newest_first_in_one_unbroken_stretch_ending_with_the_current_month_and_is_exact(self):
        self.tally.vouchers = self.fake_history(100)
        sync = self.make(incremental_minutes=100000)
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
                self.assertEqual(len(merged), 1, merged)                                    # one unbroken stretch after every month
                self.assertEqual(merged[0][1], date(2026, 3, 31))                           # ... that ends with the current month
        self.assertTrue(sync.state["backfill_done"])
        self.assertEqual(self.pushed[0], (date(2026, 3, 1), date(2026, 3, 31)))             # the incremental went first
        self.assertEqual([r[0].strftime("%Y-%m") for r in self.pushed], ["2026-03", "2026-02", "2026-01", "2025-12"])
        self.assertEqual(self.site_net(), self.expected(self.tally.vouchers))
        for request in self.tally.requests:
            self.assertNotIn("SVFROMDATE", request)

    def test_an_unchanged_month_is_not_pushed_again_and_changes_and_deletions_heal(self):
        self.tally.vouchers = self.fake_history(100)
        sync = self.make(incremental_minutes=30)
        for _ in self.run_until_done(sync):
            pass
        base, pushes = self.site_net(), len(self.pushed)
        self.clock.advance(31 * 60)
        sync.step()                                                                           # nothing changed in Tally: nothing pushed
        self.assertEqual(len(self.pushed), pushes)
        recent = next(v for v in self.tally.vouchers if v["guid"] == "RAW-GUID-S1")
        old = next(v for v in self.tally.vouchers if v["guid"] == "RAW-GUID-S50")
        recent["entries"] = [("ALTO 7D MAT BLACK", "50 Nos")]                                  # a recent voucher altered
        self.tally.vouchers.remove(old)                                                       # an older voucher deleted
        self.clock.advance(31 * 60)
        sync.step()                                                                           # the incremental pushes the current month
        self.assertEqual(self.pushed[-1], (date(2026, 3, 1), date(2026, 3, 31)))
        self.assertNotEqual(self.site_net()["ALTO 7D MAT BLACK"], base["ALTO 7D MAT BLACK"])   # the recent change reached the site
        sync.state["last_nightly_day"] = None
        for _ in range(6):
            self.clock.advance(180)
            sync.step()
        self.assertEqual(self.site_net(), self.expected(self.tally.vouchers))                  # the old deletion healed too

    def test_the_nightly_with_the_same_data_changes_nothing(self):
        self.tally.vouchers = self.fake_history(60)
        sync = self.make(backfill_days=59)
        for _ in self.run_until_done(sync):
            pass
        with self.db.get_connection() as conn:
            before = [tuple(r) for r in conn.execute("SELECT * FROM sales_lines ORDER BY voucher_key, kind, item_key").fetchall()]
        pushes = len(self.pushed)
        sync.state["last_nightly_day"] = None
        for _ in range(4):
            self.clock.advance(180)
            sync.step()
        with self.db.get_connection() as conn:
            after = [tuple(r) for r in conn.execute("SELECT * FROM sales_lines ORDER BY voucher_key, kind, item_key").fetchall()]
        self.assertEqual(before, after)
        self.assertEqual(len(self.pushed), pushes)

    def test_the_company_is_pinned_on_the_website_by_the_first_push(self):
        self.tally.vouchers = self.fake_history(5)
        sync = self.make(backfill_days=4)
        sync.step(); sync.step()
        with self.db.get_connection() as conn:
            pin = conn.execute("SELECT value FROM capture_meta WHERE key = 'sales_company_hash'").fetchone()[0]
        self.assertEqual(pin, tally_sales.company_hash(self.tally.company))
        self.tally.company = "A Different Company"
        other = make_sync(os.path.join(self.dir, "second.json"), tally_sales.TallyReader(tally_url=self.tally.url, retry_pause=0), self.pusher, self.clock)
        other.step(); self.clock.advance(180)
        with LogCapture() as log:
            other.step()
        self.assertIn("company differs from the one the website already knows", log.text())

    def test_privacy_nothing_private_reaches_the_website_or_the_log(self):
        self.tally.vouchers = self.fake_history(40)
        sync = self.make(backfill_days=39)
        with LogCapture() as log:
            for _ in self.run_until_done(sync):
                pass
        everything = "\n".join(self.bodies)
        self.assertTrue(self.bodies)
        for text in FORBIDDEN_TEXT:
            self.assertNotIn(text, everything)
            self.assertNotIn(text, log.text())
        for body in self.bodies:
            payload = json.loads(body)
            self.assertEqual(set(payload), {"company", "from", "to", "lines"})
            for line in payload["lines"]:
                self.assertEqual(set(line), {"v", "d", "k", "i", "q"})
                self.assertRegex(line["v"], r"^[0-9a-f]{16}$")
        self.assertNotIn("ALTO 7D MAT BLACK", log.text())                                  # counts and months only: never item names
        self.assertNotIn("SWIFT 7D MAT GREY", log.text())

    def test_the_log_lines_the_guide_describes(self):
        self.tally.vouchers = self.fake_history(40)
        sync = self.make(backfill_days=39, incremental_minutes=30)
        with LogCapture() as log:
            for _ in self.run_until_done(sync):
                pass
            sync.state["last_nightly_day"] = None
            self.clock.advance(1900)
            for _ in range(3):
                sync.step(); self.clock.advance(180)
        text = log.text()
        for expected in ("Sales sync: incremental 2026-03 pushed", "Sales sync: backfill 2026-02 pushed", "backfill complete (39 days)",
                         "Sales sync: nightly ", "unchanged, no push", " lines"):
            self.assertIn(expected, text)

    def test_a_month_with_3000_vouchers_is_read_and_parsed_quickly(self):
        self.tally.vouchers = [voucher(date(2026, 1, 1 + n % 28), [("ALTO 7D MAT BLACK", "2 Nos")], guid=f"RAW-GUID-{n}", number=str(n)) for n in range(3000)]
        reader = tally_sales.TallyReader(tally_url=self.tally.url)
        start = time.perf_counter()
        lines, stats = reader.fetch_month(2026, 1)
        elapsed = time.perf_counter() - start
        print(f"\n  fake Tally, one month with 3000 vouchers: read and parse {elapsed * 1000:.0f} ms ({len(lines)} lines, {stats['bytes']} bytes)")
        self.assertEqual(len(lines), 3000)
        self.assertLess(elapsed, 10.0)


# ----------------------------------------------------------------------------------------------- 13. the feeder itself
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
                         "    def step(self, stock_ok=True, stock_changed=True):\n        raise RuntimeError('sales exploded')\n")
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

    def test_the_stock_changed_signal_counts_any_doubt_as_a_change(self):
        copy = make_copy("sales_signal_")
        self.addCleanup(lambda: shutil.rmtree(copy, ignore_errors=True))
        code = (
            "import feeder\n"
            "values = iter(['a', 'a', 'b', RuntimeError('boom'), 'b'])\n"
            "def fake():\n    value = next(values)\n    if isinstance(value, Exception):\n        raise value\n    return value\n"
            "feeder.site._compute_feeder_fingerprint = fake\n"
            "print('RESULT', [feeder.stock_changed_since_last_cycle() for _ in range(5)])\n")
        result = subprocess.run([sys.executable, "-c", code], cwd=copy, env=clean_env(LOG_FILE="logs/signal_check.log"), capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        self.assertIn("RESULT [True, False, True, True, False]", result.stdout)          # first look, the same, moved, an error, the same as before the error

    def test_the_real_fingerprint_changes_only_when_the_stock_changes(self):
        copy = make_copy("sales_signal_real_")
        self.addCleanup(lambda: shutil.rmtree(copy, ignore_errors=True))
        code = (
            "import json, feeder\n"
            "site = feeder.site\n"
            "def put(rows):\n    site._atomic_json_write(site.ITEM_STOCK_CACHE_JSON, rows)\n"
            "rows = [{'item_name': 'A', 'qty': 1}, {'item_name': 'B', 'qty': 2}]\n"
            "out = []\n"
            "put(rows); out.append(feeder.stock_changed_since_last_cycle())\n"
            "put(rows); out.append(feeder.stock_changed_since_last_cycle())\n"
            "put(list(reversed(rows))); out.append(feeder.stock_changed_since_last_cycle())\n"
            "put([{'item_name': 'A', 'qty': 1}, {'item_name': 'B', 'qty': 1}]); out.append(feeder.stock_changed_since_last_cycle())\n"
            "print('RESULT', out)\n")
        result = subprocess.run([sys.executable, "-c", code], cwd=copy, env=clean_env(LOG_FILE="logs/signal_real.log"), capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        self.assertIn("RESULT [True, False, False, True]", result.stdout)                 # first look, a rewrite, Tally's order, a real change

    def test_importing_feeder_starts_no_thread_and_makes_no_network_call(self):
        copy = make_copy("sales_import_")
        self.addCleanup(lambda: shutil.rmtree(copy, ignore_errors=True))
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


# ----------------------------------------------------------------------------------------------- 14. the probe
def load_module(name, *parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, *parts))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Probe(unittest.TestCase):
    def setUp(self):
        self.tally = FakeTally()
        self.addCleanup(self.tally.close)
        self.tally.vouchers = [
            voucher(date(2026, 1, 10), [("ALTO 7D MAT BLACK", "3 Nos")], guid="RAW-GUID-1"), voucher(date(2026, 1, 20), [("SWIFT 7D MAT GREY", "2 Nos")], guid="RAW-GUID-2"),
            voucher(date(2026, 1, 21), [("ALTO 7D MAT BLACK", "1 Nos")], vtype="Credit Note", base="credit", guid="RAW-GUID-3"),
            voucher(date(2026, 2, 5), [("ALTO 7D MAT BLACK", "4 Nos"), ("SWIFT 7D MAT GREY", "1 Nos")], guid="RAW-GUID-4"),
            voucher(date(2026, 2, 6), [("X", "9 Nos")], vtype="DELIVERY NOTE-HO", base="delivery", guid="RAW-GUID-5")]
        self.out = []

    def run_probe(self, *args):
        probe = load_module("sales_probe", "tools", "shop", "sales_probe.py")
        code = probe.main(list(args), reader=tally_sales.TallyReader(tally_url=self.tally.url, retry_pause=0), out=self.out.append, today=date(2026, 3, 20), pause=0)
        return code, "\n".join(self.out)

    def test_it_prints_one_row_per_month_for_the_last_three_months_and_the_first_lines(self):
        code, text = self.run_probe()
        self.assertEqual(code, 0)
        self.assertIn("request mode: calendar months", text)
        self.assertIn(tally_sales.company_hash("Super Seatings Test Co")[:8], text)
        self.assertNotIn(tally_sales.company_hash("Super Seatings Test Co")[:9], text)             # only the first 8 characters
        # month, sale vouchers, credit notes, sold, returned, net, items
        self.assertRegex(text, r"2026-01\s+2\s+1\s+5\s+1\s+4\s+2\s")
        self.assertRegex(text, r"2026-02\s+1\s+0\s+5\s+0\s+5\s+2\s")
        self.assertRegex(text, r"2026-03\s+0\s+0\s+0\s+0\s+0\s+0\s")
        self.assertIn("first 2 lines of 2026-02", text)                                          # the newest month that has any
        self.assertIn("ALTO 7D MAT BLACK", text)
        self.assertNotIn("X  x9", text)                                                           # the delivery note never shows

    def test_it_prints_at_most_15_lines_and_nothing_private(self):
        self.tally.vouchers = [voucher(date(2026, 2, 1) + timedelta(days=n % 28), [("ALTO 7D MAT BLACK", "1 Nos")], guid=f"RAW-GUID-{n}") for n in range(40)]
        _code, text = self.run_probe()
        sample = [l for l in text.splitlines() if l.startswith("  2026-") and "ALTO 7D MAT BLACK" in l]
        self.assertEqual(len(sample), 15)
        for private in FORBIDDEN_TEXT:
            self.assertNotIn(private, text)

    def test_it_never_pushes(self):
        with mock.patch("requests.post") as post:
            self.run_probe()
        post.assert_not_called()

    def test_the_today_argument(self):
        probe = load_module("sales_probe", "tools", "shop", "sales_probe.py")
        self.assertEqual(probe.parse_today(["--today", "2026-11-24"]), date(2026, 11, 24))
        self.assertEqual(probe.parse_today(["--today=2026-01-05"]), date(2026, 1, 5))
        self.assertIsNone(probe.parse_today([]))
        self.assertIsNone(probe.parse_today(["--today", "not-a-date"]))
        _code, text = self.run_probe("--today", "2026-02-15")                                    # Feb, Jan and Dec are read
        self.assertIn("2026-02", text)
        self.assertNotIn("2026-03", text)

    def test_a_friendly_message_when_tally_is_not_reachable(self):
        probe = load_module("sales_probe", "tools", "shop", "sales_probe.py")
        dead = tally_sales.TallyReader(tally_url=f"http://127.0.0.1:{free_port()}", retry_pause=0)
        out = []
        code = probe.main([], reader=dead, out=out.append, today=date(2026, 3, 20), pause=0)
        self.assertNotEqual(code, 0)
        self.assertIn("Tally", "\n".join(out))
        self.assertNotIn("Traceback", "\n".join(out))


# ----------------------------------------------------------------------------------------------- 15. the one-click local check
def tree_state(path):
    """{relative path: (size, mtime)} for a file or a whole folder (empty when it does not exist)."""
    if os.path.isfile(path):
        stat = os.stat(path)
        return {".": (stat.st_size, stat.st_mtime)}
    out = {}
    for folder, _dirs, names in os.walk(path):
        for name in names:
            full = os.path.join(folder, name)
            stat = os.stat(full)
            out[os.path.relpath(full, path)] = (stat.st_size, stat.st_mtime)
    return out


def check_folders():
    return {p for p in glob.glob(os.path.join(tempfile.gettempdir(), "sales_local_check_*")) if os.path.isdir(p)}


class LocalCheck(unittest.TestCase):
    """tools/dev/sales_local_check.py run against the fake Tally: real throw-away website in a temporary folder, real SalesSync."""

    def setUp(self):
        self.checker = load_module("sales_local_check", "tools", "dev", "sales_local_check.py")
        self.tally = FakeTally()
        self.addCleanup(self.tally.close)

    def history(self):
        out = []
        for n in range(60):
            day = TODAY - timedelta(days=n)
            out.append(voucher(day, [("ALTO 7D MAT BLACK", f"{1 + n % 3} Nos"), ("SWIFT 7D MAT GREY", "2 Nos")], guid=f"RAW-GUID-S{n}", number=str(n)))
            if n % 9 == 0:
                out.append(voucher(day, [("ALTO 7D MAT BLACK", "1 Nos")], vtype="Credit Note", base="credit", guid=f"RAW-GUID-C{n}", number=f"C{n}"))
            if n % 5 == 0:
                out.append(voucher(day, [("ALTO 7D MAT BLACK", "6 Nos")], vtype="DELIVERY NOTE-HO", base="delivery", guid=f"RAW-GUID-D{n}", number=f"D{n}"))
        return out

    def test_the_whole_chain_runs_and_cleans_up_after_itself(self):
        self.tally.vouchers = self.history()
        before_folders = check_folders()
        watched = {name: tree_state(os.path.join(ROOT, name)) for name in ("data", "mappings.db", ".env")}
        connects = []
        original = socket.socket.connect

        def recording(sock, address, *a, **k):
            connects.append(address[0] if isinstance(address, tuple) else address)
            return original(sock, address, *a, **k)
        out = []
        with mock.patch.object(socket.socket, "connect", recording):
            code = self.checker.main(["--today", "2026-03-20", "--days", "60", "--tally-url", self.tally.url, "--expect", "none"], out=out.append)
        text = "\n".join(out)
        self.assertEqual(code, 0, text[-1500:])
        self.assertIn("OVERALL: PASS", text)
        self.assertIn("Nothing was sent to any real website.", text)
        self.assertIn("unchanged, no push: yes", text)
        # the month table equals the fake data (sale vouchers read from Tally, sold, returned, net, items as stored on the website)
        for month in ("2026-03", "2026-02", "2026-01"):
            first, last = sales_sync.month_bounds(int(month[:4]), int(month[5:]))
            mine = [v for v in self.tally.vouchers if first <= v["day"] <= last and v["base"] in ("sales", "credit")]
            vouchers = sum(1 for v in mine if v["base"] == "sales")
            sold = sum(tally_sales.parse_quantity(t)[0] for v in mine if v["base"] == "sales" for _n, t in v["entries"])
            returned = sum(tally_sales.parse_quantity(t)[0] for v in mine if v["base"] == "credit" for _n, t in v["entries"])
            items = len({n for v in mine for n, _t in v["entries"]})
            self.assertRegex(text, rf"{month}\s+{vouchers}\s+{sold}\s+{returned}\s+{sold - returned}\s+{items}\s+yes\s")
        # the per-item list of the last full month was written outside the repo
        path = re.search(r"written to: (.+)", text).group(1).strip()
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        self.assertTrue(os.path.isfile(path))
        self.assertFalse(inside_repo(path))
        rows = [line.split(" | ") for line in open(path, encoding="utf-8").read().splitlines()]
        self.assertEqual([r[0] for r in rows], sorted(r[0] for r in rows))
        self.assertEqual({r[0] for r in rows}, {"ALTO 7D MAT BLACK", "SWIFT 7D MAT GREY"})
        # clean: the temporary folder is gone, the repo's data and files were not touched, nothing but 127.0.0.1 was contacted
        self.assertEqual(check_folders(), before_folders)
        self.assertEqual({name: tree_state(os.path.join(ROOT, name)) for name in watched}, watched)
        self.assertTrue(connects)
        self.assertEqual({host for host in connects if host not in ("127.0.0.1", "::1")}, set())

    def test_it_only_talks_to_a_tally_on_this_laptop(self):
        out = []
        self.assertEqual(self.checker.main(["--today", "2026-03-20", "--tally-url", "http://example.test:9000"], out=out.append), 2)
        self.assertIn("Nothing was sent to any real website.", "\n".join(out))
        self.assertEqual(self.tally.requests, [])

    def test_a_tally_that_is_not_running_gives_a_clear_message_and_no_leftovers(self):
        before = check_folders()
        out = []
        with mock.patch("tally_sales.time.sleep"):
            code = self.checker.main(["--today", "2026-03-20", "--tally-url", f"http://127.0.0.1:{free_port()}"], out=out.append)
        self.assertEqual(code, 2)
        self.assertIn("Tally is not reachable", "\n".join(out))
        self.assertEqual(check_folders(), before)

    def test_the_pusher_refuses_any_host_but_127_0_0_1(self):
        push = self.checker.make_pusher("http://127.0.0.1:1")
        with mock.patch.object(self.checker, "urlsplit", return_value=mock.Mock(hostname="example.test")):
            with self.assertRaises(AssertionError):
                push({"company": "x"})

    def test_the_clone_numbers_are_the_ones_the_owner_gave(self):
        self.assertEqual(self.checker.EXPECTED_CLONE["2026-10"], (371, 796, 2, 794))
        self.assertEqual(sum(v[0] for v in self.checker.EXPECTED_CLONE.values()), 3151)
        self.assertEqual(sum(v[3] for v in self.checker.EXPECTED_CLONE.values()), 6851)
        self.assertEqual(self.checker.EXPECTED_CLONE_LIST, {"month": "2026-10", "total": 794, "items": 463})


def inside_repo(path):
    return os.path.commonpath([os.path.realpath(path), os.path.realpath(ROOT)]) == os.path.realpath(ROOT)


# ----------------------------------------------------------------------------------------------- 16. files
class Files(unittest.TestCase):
    def check_bat(self, *parts, needs):
        data = open(os.path.join(ROOT, *parts), "rb").read()
        self.assertGreater(len(data.split(b"\r\n")), 3)
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))                                        # every line ends with CRLF
        self.assertNotIn(b"\r", data.replace(b"\r\n", b""))
        text = data.decode("utf-8").lower()
        for needed in needs:
            self.assertIn(needed, text)
        self.assertTrue(text.rstrip().endswith("pause"))
        return text

    def test_the_probe_bat_has_crlf_line_endings_and_pauses(self):
        self.check_bat("tools", "shop", "SALES_PROBE.bat", needs=(r".venv\scripts\python.exe", "sales_probe.py", "%*"))

    def test_the_local_check_bat_has_crlf_line_endings_a_fixed_test_date_and_pauses(self):
        text = self.check_bat("tools", "dev", "SALES_LOCAL_CHECK.bat", needs=(r".venv\scripts\python.exe", "sales_local_check.py", "--today 2026-11-24", "%*", "rem "))
        self.assertIn("fixed test date", text)

    def test_the_update_script_still_finds_its_log_lines(self):
        update = open(os.path.join(ROOT, "tools", "shop", "SHOP_UPDATE.bat"), encoding="utf-8", errors="replace").read()
        sources = open(os.path.join(ROOT, "feeder.py"), encoding="utf-8").read() + open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
        for needed in ("machine role=", "reason=", "Cloud sync push succeeded"):
            self.assertIn(needed, update)
            self.assertIn(needed.replace("reason=", "Full refresh starting: reason="), sources) if needed == "reason=" else self.assertIn(needed, sources)

    def test_the_guide_exists_and_covers_what_the_owner_needs(self):
        text = open(os.path.join(ROOT, "docs", "SALES_SYNC.md"), encoding="utf-8").read()
        for needed in ("SALES_PROBE.bat", "SALES_LOCAL_CHECK.bat", "SALES_SYNC=off", "SALES_SYNC=dryrun", "NEVER", "409", "404", "422", "newest",
                       "sales_sync_state.json", "SALES_BACKFILL_DAYS", "SALES_INCREMENTAL_MINUTES", "SALES_NIGHTLY_HOUR", "9000", "calendar month",
                       "Stock Summary", "Outwards", "unchanged, no push", "SHOP_UPDATE.bat", "company pin", "Educational"):
            self.assertIn(needed, text)
        for gone in ("31-day", "31 days", "date range", "SVFROMDATE", "test-token-local"):
            self.assertNotIn(gone, text)
        self.assertNotIn("edit the feeder", text.lower().split("check it on your laptop")[1].split("go live")[0])      # the laptop part has no .env step

    def test_no_real_address_or_token_in_the_new_files(self):
        for name in ("tally_sales.py", "sales_sync.py", os.path.join("tools", "shop", "sales_probe.py"), os.path.join("tools", "dev", "sales_local_check.py"),
                     os.path.join("tools", "dev", "SALES_LOCAL_CHECK.bat"), os.path.join("docs", "SALES_SYNC.md")):
            text = open(os.path.join(ROOT, name), encoding="utf-8").read().lower()
            for bad in (".onrender.com", "idris900323", "idris-botics", "x-intake-force"):
                self.assertNotIn(bad, text, (name, bad))

    def test_the_checker_never_reads_the_owners_env_and_never_imports_config_or_the_app(self):
        text = open(os.path.join(ROOT, "tools", "dev", "sales_local_check.py"), encoding="utf-8").read()
        for bad in ("import config", "from config", "load_dotenv", "make_default_pusher", "Config.", "CLOUD_SYNC_URL", "CLOUD_SYNC_TOKEN", "open(\".env\""):
            self.assertNotIn(bad, text, bad)
        self.assertIn("sys.dont_write_bytecode = True", text)                                  # nothing is left behind in the repo

    def test_old_range_reading_is_gone(self):
        for name in ("tally_sales.py", "sales_sync.py"):
            text = open(os.path.join(ROOT, name), encoding="utf-8").read()
            for gone in ("with_filter", "fetch_sales_lines", "chunk_days", "ParentVchType", "PARENTVCHTYPE", "MAX_TIMEOUT_SECONDS", "_halve_chunk"):
                self.assertNotIn(gone, text, (name, gone))


if __name__ == "__main__":
    unittest.main()
