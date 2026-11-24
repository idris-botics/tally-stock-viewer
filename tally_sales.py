"""Reads SALES vouchers and CREDIT NOTES from Tally (local HTTP/XML) for the shop-side sales sync.

Privacy: the request asks Tally only for the voucher date, its identity (GUID, number, type name, parent type), the cancelled and
optional flags, and for each stock line the item name and the billed quantity. No customer, ledger, amount, rate or narration is
requested, and nothing else is ever read from the answer. A voucher is reduced to anonymous "lines": a key that cannot be turned
back into the voucher, the day, "sale" or "return", the item name and a whole-number quantity.

Nothing here runs on import: no thread, no network call.
"""
import hashlib
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

logger = logging.getLogger("feeder")

MAX_TIMEOUT_SECONDS = 90          # the site's own Tally timeout is longer; one sales read never holds Tally for more than this
MAX_ITEM_NAME = 200
MAX_QTY = 1000000

COMPANY_COLLECTION = "Sales Sync Company"
VOUCHER_COLLECTION = "Sales Sync Vouchers"


class TallyReadError(Exception):
    """Tally answered, but with an error or with something that is not readable."""


class TallyUnreachable(TallyReadError):
    """Tally did not answer at all (not running, wrong port, company closed)."""


class TallyTimeout(TallyReadError):
    """Tally accepted the request but did not answer in time (the range was probably too big)."""


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
def _envelope(collection_id, static_vars, collection_xml, system_xml=""):
    return (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE>"
        f"<ID>{collection_id}</ID></HEADER><BODY><DESC><STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>{static_vars}"
        f"</STATICVARIABLES><TDL><TDLMESSAGE>{collection_xml}{system_xml}</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>")


def build_company_request():
    """Asks for the name of the company Tally has open (the one the stock export also reads)."""
    collection = (
        f'<COLLECTION NAME="{COMPANY_COLLECTION}" ISMODIFY="No"><TYPE>Company</TYPE><NATIVEMETHOD>Name</NATIVEMETHOD>'
        "<COMPUTE>SalesSyncCurrent : $$CurrentCompany</COMPUTE></COLLECTION>")
    return _envelope(COMPANY_COLLECTION, "", collection)


def build_voucher_request(day_from, day_to, with_filter=True):
    """Vouchers dated day_from..day_to (inclusive). With the filter Tally itself leaves out everything but sales and credit notes;
    without it every voucher type is returned and the leaving-out is done here (the fallback when a Tally does not take the filter)."""
    parent = "$Parent:VoucherType:$VoucherTypeName"
    filter_tag = "<FILTER>SalesSyncOnlySalesAndCreditNotes</FILTER>" if with_filter else ""
    collection = (
        f'<COLLECTION NAME="{VOUCHER_COLLECTION}" ISMODIFY="No"><TYPE>Voucher</TYPE>'
        "<FETCH>Date, GUID, VoucherNumber, VoucherTypeName, IsCancelled, IsOptional, "
        "AllInventoryEntries.StockItemName, AllInventoryEntries.BilledQty</FETCH>"
        f"<COMPUTE>ParentVchType : {parent}</COMPUTE>{filter_tag}</COLLECTION>")
    system = (
        f'<SYSTEM TYPE="Formulae" NAME="SalesSyncOnlySalesAndCreditNotes">{parent} = "Sales" OR {parent} = "Credit Note"</SYSTEM>'
        if with_filter else "")
    static = f"<SVFROMDATE>{day_from.strftime('%Y%m%d')}</SVFROMDATE><SVTODATE>{day_to.strftime('%Y%m%d')}</SVTODATE>"
    return _envelope(VOUCHER_COLLECTION, static, collection, system)


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
    """Drops the characters XML cannot carry (Tally sometimes emits a few) so one stray byte does not lose a whole chunk."""
    text = re.sub("[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)

    def keep(match):
        try:
            code = int(match.group(1))
        except ValueError:
            return ""
        return match.group(0) if code in (9, 10, 13) or 0x20 <= code <= 0xD7FF or 0xE000 <= code <= 0xFFFD else ""
    return re.sub(r"&#(\d+);", keep, text)


def _parse_root(xml_text):
    try:
        return ET.fromstring(_sanitize(xml_text or "").strip())
    except ET.ParseError as exc:
        raise TallyReadError("Tally's answer is not readable XML") from exc


def new_stats():
    return {"vouchers": 0, "lines": 0, "cancelled": 0, "optional": 0, "other_types": 0, "fractional": 0, "bad": 0}


def parse_vouchers(xml_text):
    """(lines, stats) from a Tally voucher export. A line is {"voucher_key", "day", "kind", "item", "qty"}. Cancelled and optional
    vouchers and every type that is not (a child of) Sales or Credit Note are left out; a voucher that cannot be read is skipped and
    counted, never fatal."""
    root = _parse_root(xml_text)
    if root.find(".//LINEERROR") is not None:
        raise TallyReadError("Tally answered with an error")
    lines, stats = [], new_stats()
    for voucher in root.iter("VOUCHER"):
        try:
            day = datetime.strptime(_text(voucher, "DATE"), "%Y%m%d").date()
        except ValueError:
            stats["bad"] += 1
            continue
        type_name = _text(voucher, "VOUCHERTYPENAME")
        parent = (_text(voucher, "PARENTVCHTYPE") or type_name).lower()
        if parent == "sales":
            kind = "sale"
        elif parent == "credit note":
            kind = "return"
        else:
            stats["other_types"] += 1
            continue
        if _yes(_text(voucher, "ISCANCELLED")):
            stats["cancelled"] += 1
            continue
        if _yes(_text(voucher, "ISOPTIONAL")):
            stats["optional"] += 1
            continue
        key = voucher_key(_text(voucher, "GUID"), type_name, day, _text(voucher, "VOUCHERNUMBER"))
        stats["vouchers"] += 1
        for entry in list(voucher.findall("ALLINVENTORYENTRIES.LIST")) + list(voucher.findall("INVENTORYENTRIES.LIST")):
            item = _text(entry, "STOCKITEMNAME")[:MAX_ITEM_NAME]
            qty, fractional = parse_quantity(_text(entry, "BILLEDQTY", "ACTUALQTY"))
            if fractional:
                stats["fractional"] += 1
            if not item or qty is None:
                continue
            lines.append({"voucher_key": key, "day": day, "kind": kind, "item": item, "qty": qty})
    stats["lines"] = len(lines)
    return lines, stats


def parse_company(xml_text):
    """The open company's name from the company answer, or None when it is not there."""
    root = _parse_root(xml_text)
    if root.find(".//LINEERROR") is not None:
        return None
    for element in root.iter("SALESSYNCCURRENT"):
        if element.text and element.text.strip():
            return element.text.strip()
    return None


# ------------------------------------------------------------------------------------------------ talking to Tally
def _session_post(session, url, xml, timeout, max_retries):
    """POSTs the request and returns Tally's answer as text. Sleeps only between attempts (never after the last one)."""
    import requests
    last = None
    for attempt in range(max(1, max_retries)):
        try:
            response = session.post(url, data=xml.encode("utf-8"), timeout=timeout, headers={"Content-Type": "text/xml; charset=utf-8"})
            response.raise_for_status()
            raw = response.content
            if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
                return raw.decode("utf-16", "replace")
            return raw.decode("utf-8-sig", "replace")
        except requests.Timeout as exc:
            last = TallyTimeout("Tally did not answer in time")
            last.__cause__ = exc
        except requests.ConnectionError as exc:
            last = TallyUnreachable("Tally is not reachable")
            last.__cause__ = exc
        except requests.RequestException as exc:
            last = TallyReadError("Tally answered with an HTTP error")
            last.__cause__ = exc
        if attempt + 1 < max(1, max_retries):
            time.sleep(2 ** attempt)
    raise last


class TallyReader:
    """Reads sales lines and the company name from Tally's local HTTP server."""

    def __init__(self, tally_url=None, timeout=None, max_retries=2):
        self._url = tally_url
        self._timeout = timeout
        self.max_retries = max_retries
        self._session = None
        self._filter_ok = True
        self.last_stats = new_stats()

    @property
    def url(self):
        if self._url:
            return self._url
        from config import Config
        return Config.TALLY_URL

    def _post(self, xml):
        if self._session is None:
            import requests
            self._session = requests.Session()
        timeout = min(MAX_TIMEOUT_SECONDS, self._timeout or MAX_TIMEOUT_SECONDS)
        return _session_post(self._session, self.url, xml, timeout=timeout, max_retries=self.max_retries)

    def company_name(self):
        """The name of the company Tally has open, or None when Tally cannot say. Tally being unreachable still raises."""
        try:
            return parse_company(self._post(build_company_request()))
        except TallyUnreachable:
            raise
        except TallyReadError:
            return None

    def fetch_sales_lines(self, day_from, day_to):
        """All sales and return lines dated day_from..day_to. Raises TallyUnreachable, TallyTimeout or TallyReadError."""
        if self._filter_ok:
            try:
                lines, stats = parse_vouchers(self._post(build_voucher_request(day_from, day_to, True)))
            except (TallyUnreachable, TallyTimeout):
                raise
            except TallyReadError:
                lines, stats = parse_vouchers(self._post(build_voucher_request(day_from, day_to, False)))
                self._filter_ok = False                    # this Tally does not take the filter: asked without it from now on
        else:
            lines, stats = parse_vouchers(self._post(build_voucher_request(day_from, day_to, False)))
        self.last_stats = stats
        if stats["fractional"]:
            logger.info("Sales sync: %d fractional quantities were rounded down to whole units", stats["fractional"])
        return lines


_default_reader = None


def default_reader():
    global _default_reader
    if _default_reader is None:
        _default_reader = TallyReader()
    return _default_reader


def fetch_sales_lines(day_from, day_to):
    """Module-level convenience: the default reader (the TALLY_URL the stock feeder uses)."""
    return default_reader().fetch_sales_lines(day_from, day_to)
