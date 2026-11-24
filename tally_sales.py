"""Reads SALES vouchers and CREDIT NOTES from Tally (local HTTP/XML) for the shop-side sales sync, ONE CALENDAR MONTH at a time.

Why whole calendar months: on the real Tally a Voucher collection ignores SVFROMDATE/SVTODATE and "$Date" comparisons in a filter do
not work, but "$$MonthOfDate:$Date = M AND $$YearOfDate:$Date = Y" does. Only whole calendar months are ever requested.
Why the base type: the company renames its voucher types (SALES-HO and its children, Credit Note ...), so a voucher is a sale or a
return because Tally says so ($$IsSales / $$IsCreditNote on the voucher type), never because of its name.

Privacy: the request asks Tally only for the voucher date, its identity (GUID, number, type name), the cancelled and optional flags and,
for each stock line, the item name and quantities. Tally still hands over the whole voucher (party names, ledgers, amounts) in its
answer; the parser reads only the fields above and drops the rest at once, so nothing else is ever stored, logged or sent. A voucher is
reduced to anonymous "lines": a key that cannot be turned back into the voucher, the day, "sale" or "return", the item name and a
whole-number quantity.

Nothing here runs on import: no thread, no network call.
"""
import hashlib
import io
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal, InvalidOperation

logger = logging.getLogger("feeder")

VOUCHER_TIMEOUT_SECONDS = 120     # one month of vouchers; a timeout is never retried
COMPANY_TIMEOUT_SECONDS = 20
CONNECTION_RETRY_PAUSE_SECONDS = 5
MAX_ITEM_NAME = 200
MAX_QTY = 1000000

COMPANY_COLLECTION = "Sales Sync Company"
VOUCHER_COLLECTION = "Sales Sync Vouchers"
VOUCHER_FILTER = "SalesSyncOnlySalesAndCreditNotes"
TYPE_CONDITION = "$$IsSales:$VoucherTypeName OR $$IsCreditNote:$VoucherTypeName"


class TallyReadError(Exception):
    """Tally answered, but with an error or with something that is not readable."""


class TallyUnreachable(TallyReadError):
    """Tally did not answer at all (not running, wrong port, company closed)."""


class TallyTimeout(TallyReadError):
    """Tally accepted the request but did not answer in time."""


# ------------------------------------------------------------------------------------------------ keys and hashes
def voucher_key(guid, type_name, day, number):
    """The anonymous voucher key: the first 16 hex characters of sha256("<GUID>|<voucher type name>"); a voucher without a GUID
    falls back to "<date>|<type>|<number>". It is stable (the same voucher always gives the same key) and one-way."""
    guid = (guid or "").strip()
    type_name = (type_name or "").strip()
    if guid:
        text = f"{guid}|{type_name}"
    else:
        text = f"{day.isoformat()}|{type_name}|{(number or '').strip()}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def company_hash(name):
    """sha256 (64 hex characters) of the Tally company name: the website pins the first one it sees and refuses a different one."""
    return hashlib.sha256((name or "").strip().encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------------------ requests
def _envelope(collection_id, collection_xml, system_xml=""):
    return (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE>"
        f"<ID>{collection_id}</ID></HEADER><BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"
        f"</STATICVARIABLES><TDL><TDLMESSAGE>{collection_xml}{system_xml}</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>")


def build_company_request():
    """Asks for the name of the company Tally has open (the one the stock export also reads)."""
    collection = (
        f'<COLLECTION NAME="{COMPANY_COLLECTION}" ISMODIFY="No"><TYPE>Company</TYPE><NATIVEMETHOD>Name</NATIVEMETHOD>'
        "<COMPUTE>SalesSyncCurrent : $$CurrentCompany</COMPUTE></COLLECTION>")
    return _envelope(COMPANY_COLLECTION, collection)


def month_formula(year, month):
    """The filter: sales and credit notes (by base type) of ONE calendar month. Month and year are written as plain integers."""
    return f"({TYPE_CONDITION}) AND $$MonthOfDate:$Date = {int(month)} AND $$YearOfDate:$Date = {int(year)}"


def build_voucher_request(year, month):
    """Sales vouchers and credit notes dated in the calendar month year/month. No date variables are sent (Tally ignores them)."""
    collection = (
        f'<COLLECTION NAME="{VOUCHER_COLLECTION}" ISMODIFY="No"><TYPE>Voucher</TYPE>'
        "<FETCH>Date, GUID, VoucherNumber, VoucherTypeName, IsCancelled, IsOptional, AllInventoryEntries.StockItemName, "
        "AllInventoryEntries.BilledQty, AllInventoryEntries.ActualQty</FETCH>"
        "<COMPUTE>IsSalesFlag : $$IsSales:$VoucherTypeName</COMPUTE><COMPUTE>IsCreditNoteFlag : $$IsCreditNote:$VoucherTypeName</COMPUTE>"
        f"<FILTER>{VOUCHER_FILTER}</FILTER></COLLECTION>")
    system = f'<SYSTEM TYPE="Formulae" NAME="{VOUCHER_FILTER}">{month_formula(year, month)}</SYSTEM>'
    return _envelope(VOUCHER_COLLECTION, collection, system)


# ------------------------------------------------------------------------------------------------ parsing
_NUMBER = re.compile(r"-?\s*[\d,]*\.?\d+")


def parse_quantity(text):
    """(whole units, was_fractional) from Tally quantity text: "3 Nos" -> (3, False), "1,250 Nos" -> (1250, False),
    "-4 Nos" -> (4, False), "2.5 Mtr" -> (2, True). No readable quantity, or none left after dropping the fraction -> (None, ...)."""
    match = _NUMBER.search(text or "")
    if not match:
        return None, False
    raw = match.group(0).replace(",", "").replace(" ", "")
    try:
        value = abs(Decimal(raw))
    except InvalidOperation:
        return None, False
    whole = int(value)
    fractional = value != whole
    if whole < 1:
        return None, fractional
    return min(whole, MAX_QTY), fractional


def _text(element, *names):
    for name in names:
        found = element.find(name)
        if found is not None and found.text and found.text.strip():
            return found.text.strip()
    return ""


def _yes(value):
    return (value or "").strip().lower() in ("yes", "true", "1")


def _sanitize(text):
    """Drops the characters XML cannot carry (Tally sometimes emits a few) so one stray byte does not lose a whole month."""
    text = re.sub("[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)

    def keep(match):
        try:
            code = int(match.group(1))
        except ValueError:
            return ""
        return match.group(0) if code in (9, 10, 13) or 0x20 <= code <= 0xD7FF or 0xE000 <= code <= 0xFFFD else ""
    return re.sub(r"&#(\d+);", keep, text)


def _prepare(xml_text):
    """Makes Tally's answer parseable: illegal characters out, the undeclared "UDF:" tag prefix turned into a plain name
    ("unbound prefix" otherwise), and the XML declaration dropped (the text is already decoded)."""
    text = _sanitize(xml_text or "")
    text = re.sub(r"(</?)UDF:", r"\1UDF_", text)
    return re.sub(r"^\s*<\?xml[^>]*\?>", "", text).strip()


def new_stats():
    return {"vouchers_read": 0, "sale_vouchers": 0, "credit_notes": 0, "skipped_other_type": 0, "skipped_cancelled": 0,
            "skipped_optional": 0, "skipped_no_date": 0, "dropped_out_of_range": 0, "fractional": 0, "lines": 0,
            "bytes": 0, "seconds": 0.0}


def _handle_voucher(voucher, year, month, lines, stats):
    stats["vouchers_read"] += 1
    try:
        day = datetime.strptime(_text(voucher, "DATE"), "%Y%m%d").date()
    except ValueError:
        stats["skipped_no_date"] += 1          # Tally returns one stray <VOUCHER>0</VOUCHER> element: normal
        return
    if (day.year, day.month) != (year, month):
        stats["dropped_out_of_range"] += 1     # expected to stay 0: the month condition is Tally's job
        return
    is_sale, is_return = _yes(_text(voucher, "ISSALESFLAG")), _yes(_text(voucher, "ISCREDITNOTEFLAG"))
    if not (is_sale or is_return):
        stats["skipped_other_type"] += 1
        return
    if _yes(_text(voucher, "ISCANCELLED")):
        stats["skipped_cancelled"] += 1
        return
    if _yes(_text(voucher, "ISOPTIONAL")):
        stats["skipped_optional"] += 1
        return
    kind = "sale" if is_sale else "return"
    type_name = _text(voucher, "VOUCHERTYPENAME") or voucher.attrib.get("VCHTYPE", "")
    key = voucher_key(_text(voucher, "GUID"), type_name, day, _text(voucher, "VOUCHERNUMBER"))
    stats["sale_vouchers" if is_sale else "credit_notes"] += 1
    for entry in list(voucher.findall("ALLINVENTORYENTRIES.LIST")) + list(voucher.findall("INVENTORYENTRIES.LIST")):
        item = _text(entry, "STOCKITEMNAME")[:MAX_ITEM_NAME]
        qty, fractional = parse_quantity(_text(entry, "BILLEDQTY", "ACTUALQTY"))      # ACTUALQTY only when BILLEDQTY is empty
        if fractional:
            stats["fractional"] += 1
        if not item or qty is None:
            continue
        lines.append({"voucher_key": key, "day": day, "kind": kind, "item": item, "qty": qty})


def parse_vouchers(xml_text, year, month):
    """(lines, stats) from Tally's answer for the calendar month year/month. A line is {"voucher_key", "day", "kind", "item", "qty"}.
    Everything is read with iterparse and each <VOUCHER> is cleared as soon as it is handled, so a big answer stays small in memory.
    Cancelled and optional vouchers, vouchers that are neither a sale nor a credit note, vouchers without a date and vouchers dated
    outside the month are left out and counted."""
    data = _prepare(xml_text).encode("utf-8")
    lines, stats = [], new_stats()
    try:
        for _event, element in ET.iterparse(io.BytesIO(data), events=("end",)):
            if element.tag == "LINEERROR":
                raise TallyReadError("Tally answered with an error")
            if element.tag == "VOUCHER":
                _handle_voucher(element, year, month, lines, stats)
                element.clear()
    except ET.ParseError as exc:
        raise TallyReadError("Tally's answer is not readable XML") from exc
    stats["lines"] = len(lines)
    return lines, stats


def parse_company(xml_text):
    """The open company's name from the company answer, or None when it is not there."""
    try:
        root = ET.fromstring(_prepare(xml_text))
    except ET.ParseError as exc:
        raise TallyReadError("Tally's answer is not readable XML") from exc
    if root.find(".//LINEERROR") is not None:
        return None
    for element in root.iter("SALESSYNCCURRENT"):
        if element.text and element.text.strip():
            return element.text.strip()
    return None


# ------------------------------------------------------------------------------------------------ talking to Tally
def _session_post(session, url, xml, timeout, retry_pause=CONNECTION_RETRY_PAUSE_SECONDS):
    """POSTs the request and returns (Tally's answer as text, its size in bytes). A timeout raises at once and is never retried; a
    refused connection is retried once after retry_pause seconds."""
    import requests
    for attempt in (0, 1):
        try:
            response = session.post(url, data=xml.encode("utf-8"), timeout=timeout, headers={"Content-Type": "text/xml; charset=utf-8"})
            response.raise_for_status()
            raw = response.content
            if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
                return raw.decode("utf-16", "replace"), len(raw)
            return raw.decode("utf-8-sig", "replace"), len(raw)
        except requests.Timeout as exc:
            raise TallyTimeout("Tally did not answer in time") from exc
        except requests.ConnectionError as exc:
            if attempt == 0:
                time.sleep(retry_pause)
                continue
            raise TallyUnreachable("Tally is not reachable") from exc
        except requests.RequestException as exc:
            raise TallyReadError("Tally answered with an HTTP error") from exc


class TallyReader:
    """Reads one calendar month of sales lines, and the company name, from Tally's local HTTP server."""

    def __init__(self, tally_url=None, voucher_timeout=VOUCHER_TIMEOUT_SECONDS, company_timeout=COMPANY_TIMEOUT_SECONDS,
                 retry_pause=CONNECTION_RETRY_PAUSE_SECONDS):
        self._url = tally_url
        self.voucher_timeout = voucher_timeout
        self.company_timeout = company_timeout
        self.retry_pause = retry_pause
        self._session = None
        self.last_stats = new_stats()

    @property
    def url(self):
        if self._url:
            return self._url
        from config import Config
        return Config.TALLY_URL

    def _post(self, xml, timeout):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return _session_post(self._session, self.url, xml, timeout=timeout, retry_pause=self.retry_pause)

    def company_name(self):
        """The name of the company Tally has open, or None when Tally cannot say. Tally being unreachable still raises."""
        try:
            return parse_company(self._post(build_company_request(), self.company_timeout)[0])
        except TallyUnreachable:
            raise
        except TallyReadError:
            return None

    def fetch_month(self, year, month):
        """(lines, stats) for the sales and credit notes dated in the calendar month year/month. Raises TallyUnreachable,
        TallyTimeout or TallyReadError. Only the filtered request exists: the unfiltered one (every voucher type) is far too big."""
        started = time.monotonic()
        text, size = self._post(build_voucher_request(year, month), self.voucher_timeout)
        lines, stats = parse_vouchers(text, year, month)
        stats["bytes"], stats["seconds"] = size, time.monotonic() - started
        self.last_stats = stats
        if stats["fractional"]:
            logger.info("Sales sync: %d fractional quantities were rounded down to whole units", stats["fractional"])
        if stats["dropped_out_of_range"]:
            logger.warning("Sales sync: Tally returned %d vouchers outside the requested month", stats["dropped_out_of_range"])
        return lines, stats


_default_reader = None


def default_reader():
    global _default_reader
    if _default_reader is None:
        _default_reader = TallyReader()
    return _default_reader
