"""Montana Distressed Rental Finder (GM Labs).
Parcel-centric motivated-seller leads for 2+ unit rental properties in Montana counties.
Baked point-in-time extract, rebuilt by hand (MT DOR ORION CAMA: units, year built, last deed) + live light public sources at run time
(MT Cadastral, county treasurer delinquency lists, Billings code cases, sheriff / trustee sale lists).
Never outputs owner names or mailing addresses."""
import collections, asyncio, datetime as dt, gzip, html as htmlmod, json, os, re, subprocess, tempfile, time
from urllib.parse import urljoin
import requests
from apify import Actor
from common import (COUNTIES, MT_CAD, CAD_FIELDS, UA, owner_flags, absentee_flags, situs_city, tax_id, addr_key,
                    DEATH_DEED_RE, HARD_SIGNALS, is_unit_address, MIN_DELINQUENT_USD, score, tax_tier, clean_address, street_key, owner_key)

HERE = os.path.dirname(os.path.abspath(__file__)); DATA = os.path.join(os.path.dirname(HERE), "data")
TODAY = dt.date.today()
def _mt_today():
    """today's date in Montana (America/Denver: MST UTC-7, MDT UTC-6 from the 2nd Sunday of March to the 1st Sunday of November, 2 am local)"""
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/Denver")).date()
    except Exception:
        u = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None); y = u.year
        sun = lambda m, n: (lambda d: d + dt.timedelta(days=(6 - d.weekday()) % 7 + 7 * (n - 1)))(dt.datetime(y, m, 1))
        dst = sun(3, 2) + dt.timedelta(hours=9) <= u < sun(11, 1) + dt.timedelta(hours=8)
        return (u - dt.timedelta(hours=6 if dst else 7)).date()
SALE_TODAY = _mt_today()   # sale dates are local (Montana) dates
EXCLUDE_PTYPES = {"Mobile/RV Parks", "Non-Valued Property", "Non-Valued with Specials", "Exempt Property", "CN - Centrally Assessed Non-Valued Property", "CA - Centrally Assessed"}
YC = "https://www.yellowstonecountymt.gov"
HWM = {st: f"https://www.halliday-watkins.com/live/{st}/bids.htm" for st in ("MT", "WY", "ID")}   # one sale list per state, same table layout
HWM_MT = HWM["MT"]
BIL_CODE = "https://billingsgis.com/arcgis_public/rest/services/ArcOnline_Public/BuildingPermits_CodeViolations_EXT/MapServer/{}/query"
# NOT linked (Boss rule 2026-10-19: no links to pages that show owner names, mailing addresses or document parties):
# Montana Cadastral parcel pages, Gallatin / Park iTax pages, Yellowstone recorder documents, Billings code-case records.
# Each row carries the plain-text id instead (geocode, tax id, document number) plus a note on where to look it up.
TAX_SITE = {"Gallatin": "the Gallatin County Treasurer's online tax search (iTax)", "Park": "the Park County Treasurer's online tax search (iTax)", "Missoula": "the Missoula County Treasurer's online tax search (iTax)",
            "Cascade": "the Cascade County Treasurer's online tax search (iTax)", "Lewis and Clark": "the Lewis and Clark County Treasurer's office",
            "Silver Bow": "the Butte-Silver Bow Treasurer's online tax search (iTax)"}
ITAX_DETAIL = {"Gallatin": "https://itax.gallatin.mt.gov/detail.aspx?taxid={}", "Park": "https://itax.tylertech.com/ParkMT/detail.aspx?taxid={}"}
MIXED_NA = "n/a (only assessed for 5+ units)"
ASSIGN_ASOF = None # date of the tax-lien assignment snapshot (Yellowstone), set by delinq_yellowstone()
SOURCES = {}       # source name -> {"ok": bool, "rows": n, "url": .., "as_of": ..}

# ---------------------------------------------------------------- polite HTTP (per-host spacing, retries, hard timeouts)
_last = {}
HOST_GAP = {"www.yellowstonecountymt.gov": 10.0,   # robots.txt Crawl-delay: 10
            "itax.tylertech.com": 2.0, "itax.gallatin.mt.gov": 2.0}   # no robots.txt; stay gentle
def get(url, method="GET", data=None, timeout=30, binary=False, tries=2):
    """One request with a HARD wall-clock limit per attempt (connect 10 s; the whole body must arrive within `timeout`),
    at most `tries` attempts, so a hung or trickling server cannot push the run toward its platform timeout."""
    host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
    for a in range(tries):
        gap = HOST_GAP.get(host, 1.0); wait = _last.get(host, 0) + gap - time.time()
        if wait > 0: time.sleep(wait)
        try:
            t0 = time.time()
            with requests.request(method, url, data=data, headers=UA, timeout=(10, min(timeout, 30)), stream=True) as r:
                r.raise_for_status(); buf = bytearray()
                for chunk in r.iter_content(65536):
                    buf += chunk
                    if time.time() - t0 > timeout: raise TimeoutError(f"body not complete after {timeout} s")
                _last[host] = time.time()
                b = bytes(buf)
                return b if binary else b.decode(r.encoding or "utf-8", errors="replace")
        except Exception as e:
            _last[host] = time.time()
            if a == tries - 1: raise
            Actor.log.warning(f"retry {a+1} {scrub(url)}: {scrub(e)}"); time.sleep(3)

# Customer-visible output (dataset, STATS, log, status message) may only carry URLs of pages that show no owner names.
# Anything else (county delinquent-list PDFs and exports, iTax detail pages, recorder documents, case records, ...) is
# reduced to its host name before it is logged or stored.
SAFE_URL_PREFIXES = ()   # filled in below, once the page constants exist
def scrub(x):
    def one(m):
        u = m.group(0)
        if any(u.startswith(p) for p in SAFE_URL_PREFIXES): return u
        return re.sub(r"^https?://([^/]+).*$", r"\1 (address withheld)", u.rstrip(":.,;")) + u[len(u.rstrip(":.,;")):]
    return re.sub(r"https?://[^\s'\"<>)]+", one, str(x))

def arcgis(url, where, fields):
    out, off = [], 0
    while True:
        r = json.loads(get(url, "POST", dict(where=where, outFields=fields, returnGeometry="false", f="json", resultOffset=off, resultRecordCount=2000)))
        if "error" in r: raise RuntimeError(r["error"])
        f = r.get("features", []); out += [x["attributes"] for x in f]; off += len(f)
        if not f or not r.get("exceededTransferLimit"): return out

def strip_tags(s): return htmlmod.unescape(re.sub(r"<[^>]+>", " ", s)).strip()
def money(s):
    try: return float(str(s).replace(",", "").replace("$", ""))
    except ValueError: return 0.0

def pdf_text(b):
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(b); f.flush()
        return subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, text=True, timeout=120, check=True).stdout

def find_link(page_url, pattern):
    h = get(page_url)
    for href, txt in re.findall(r'href="([^"]+)"[^>]*>(.*?)</a>', h, flags=re.S | re.I):
        if re.search(pattern, href + " " + strip_tags(txt), re.I): return urljoin(page_url, htmlmod.unescape(href)), strip_tags(txt)
    return None, None

# ---------------------------------------------------------------- treasurer delinquency (tax id -> {years, due})
def delinq_yellowstone():
    """Each TaxDel_Export page carries TWO tax years (year=2024 -> 2023 + 2024, year=2025 -> 2024 + 2025), so the same
    (tax id, year) rows appear on two pages. Rows are summed within one page section, and each (tax id, year) is taken
    from ONE page only (the newest export), which matches the treasurer's csadelin totals. Tax ids keep their suffix
    letter (C02514B). Tax-lien ASSIGNMENTS are not on the exports; they come from a point-in-time box-side snapshot
    (data/yellowstone_assignments.json, as_of = the date it was taken; it is NOT refreshed automatically)."""
    base = YC + "/treasurer/"
    try:
        h = get(base + "TaxSales.asp")
        links = sorted(set(re.findall(r'href="(TaxDel(?:Special)?_Export\.asp\?[^"]*wv=True)"', h)))
    except Exception: links = []
    links = links or ["TaxDel_Export.asp?year=2025&wv=True", "TaxDel_Export.asp?year=2024&wv=True", "TaxDelSpecial_Export.asp?wv=True"]
    def page_rank(q):                           # newest regular export first, specials separately
        m = re.search(r"year=(\d{4})", q); return -(int(m.group(1)) if m else 0)
    taken, asof = {}, None                      # (tid, year, kind) -> amount
    for q in sorted(links, key=page_rank):
        s = get(base + q.replace("&amp;", "&"))
        m = re.search(r"Current as of (\d\d/\d\d/\d{4})", s); asof = asof or (m.group(1) if m else None)
        kind = "special" if "Special" in q else "regular"
        page, yr = {}, None
        for tok in re.finditer(r"Tax Year - (\d{4})|<tr>\s*<td[^>]*>\s*([A-Z]\d{5}[A-Z]?)\s*(?:\((\d{4})\))?\s*</td>\s*<td>.*?</td>\s*<td[^>]*>([^<]*)</td>\s*<td[^>]*>([^<]*)</td>\s*<td[^>]*>([^<]*)</td>", s, re.S):
            if tok.group(1): yr = int(tok.group(1)); continue
            y = int(tok.group(3)) if tok.group(3) else yr
            if y is None: continue
            k = (tok.group(2), y, kind); page[k] = page.get(k, 0.0) + money(tok.group(6))
        for k, v in page.items(): taken.setdefault(k, v)       # first (newest) page wins; no double count
    recs = {}
    for (tid, y, kind), v in taken.items():
        e = recs.setdefault(tid, {"years": set(), "due": 0.0, "assigned_due": 0.0, "assigned_years": set(), "by_year": {}}); e["due"] += v; e["years"].add(y)
        e["by_year"][y] = e["by_year"].get(y, 0.0) + v
    n_assign, a_asof = 0, None
    global ASSIGN_ASOF
    try:
        snap = json.load(open(f"{DATA}/yellowstone_assignments.json")); a_asof = snap.get("as_of"); ASSIGN_ASOF = a_asof
        for tid, yrs in snap["assigned"].items():
            for y, v in yrs.items():
                y = int(y)
                if (tid, y, "regular") in taken: continue              # still on the live list: already counted
                e = recs.setdefault(tid, {"years": set(), "due": 0.0, "assigned_due": 0.0, "assigned_years": set(), "by_year": {}})
                e["by_year"][y] = e["by_year"].get(y, 0.0) + v; e["due"] += v; e["years"].add(y); e["assigned_due"] += v; e["assigned_years"].add(y); n_assign += 1
    except FileNotFoundError: pass
    SOURCES["treasurer_delinquent"] = {"ok": True, "rows": len(recs), "url": base + "TaxSales.asp", "as_of": asof,
                                       "tax_lien_assignments": n_assign, "assignment_snapshot_as_of": a_asof}
    return recs

GALLATIN_DLQ_PAGE = "https://www.gallatinmt.gov/679/Delinquent-Taxes-Tax-Liens-and-Assignmen"
PARK_TREAS_PAGE = "https://www.parkcounty.org/Search-Index/Treasurer/department/35/"
def delinq_gallatin():
    page = GALLATIN_DLQ_PAGE
    url, title = find_link(page, r"DocumentCenter/View/.*DLQ")
    if not url: raise RuntimeError("Gallatin delinquent list link not found on " + page)
    t = pdf_text(get(url, binary=True, timeout=120))
    rx = re.compile(r"^\s*(\d{4})\s+([A-Z]{1,4}\d+[A-Z]?)\s+(\S+)\s+([A-Z]{2,3})\s+.*?\s+(-?[\d.,]+)\s+(-?[\d.,]+)\s+(-?[\d.,]+)\s+(-?[\d.,]+)\s+(-?[\d.,]+)\s*$")
    recs = {}
    for line in t.splitlines():
        m = rx.match(line)
        if m and m.group(4) == "RE":
            e = recs.setdefault(m.group(2), {"years": set(), "due": 0.0, "by_year": {}}); y = int(m.group(1)); e["years"].add(y); e["due"] += money(m.group(7))  # totalBill (excl. P&I)
            e["by_year"][y] = e["by_year"].get(y, 0.0) + money(m.group(7))
    m = re.search(r"as[- ]+of[- ]+(\d{1,2})[- ](\d{1,2})[- ](\d{2,4})", (title or "") + " " + url, re.I)
    # the list PDF itself prints owner names: only the department page (no names) and the list's date are recorded
    SOURCES["treasurer_delinquent"] = {"ok": True, "rows": len(recs), "source": "Gallatin County Treasurer: Delinquent Taxes, Tax Liens and Assignments", "list": "Gallatin County delinquent tax list (PDF, not linked: it prints owner names)",
                                       "as_of": f"{m.group(1)}/{m.group(2)}/20{m.group(3)[-2:]}" if m else None}
    return recs

def delinq_park():
    page = PARK_TREAS_PAGE
    url, title = find_link(page, r"Delinquent Tax List")
    if not url: raise RuntimeError("Park delinquent list link not found on " + page)
    t = pdf_text(get(url, binary=True, timeout=120))
    recs = {}
    for pn, cert, geo, yr, dist, amt in re.findall(r"Parcel#\s+(\d+)\s+(\d+)\s+(\d\d-\d{4}-\d\d-\d-\d\d-\d\d-\d{4})\s+(\d{4})\s+(\S+)\s+([\d,]+\.\d\d)", t):
        e = recs.setdefault(pn, {"years": set(), "due": 0.0, "by_year": {}}); e["years"].add(int(yr)); e["due"] += money(amt)
        e["by_year"][int(yr)] = e["by_year"].get(int(yr), 0.0) + money(amt)
    m = re.search(r"AS OF (\d\d/\d\d/\d{4})", t)
    SOURCES["treasurer_delinquent"] = {"ok": True, "rows": len(recs), "source": "Park County Treasurer department", "list": "Park County delinquent tax list (PDF, not linked: it prints owner names and addresses)",
                                       "as_of": m.group(1) if m else None}
    return recs

MSO_ITAX = "https://itax.missoulacounty.us/itax/"
MSO_TREAS_PAGE = "https://www.missoulacounty.gov/departments/clerk-treasurer/property-taxes/delinquent-taxes/"
MSO_CAP, MSO_GAP = 200, 1.2     # at most 200 parcels (2 pages each) per run, 1.2 s apart (no robots.txt on the host; stay gentle)
def delinq_missoula(parcels, cad):
    """Missoula publishes no delinquent list (request form only). Instead, the top MSO_CAP multifamily parcels by
    ownership signals are checked one by one on the county's free iTax pages: the detail page (opens the parcel), then
    the payoff page's per-half unpaid rows (always: Status only describes the current tax year). A half counts as delinquent once its due date has passed; only the
    tax amount is used (penalty & interest excluded). Owner fields on the pages are never read."""
    def pre(p):
        if p.get("ptype") in EXCLUDE_PTYPES or p.get("owner_type") == "government_nonprofit" or not p.get("tax"): return None
        y = 0
        if p.get("deed"):
            try: y = (TODAY - dt.date.fromisoformat(p["deed"][:10])).days / 365.25
            except ValueError: y = 0
        return (2 if y >= 20 else 1 if y >= 10 else 0) + (2 if p.get("out_of_state") else 1 if p.get("nonlocal_instate") else 0) + \
               (2 if p.get("estate") else 1 if p.get("trust") else 0), p["units"]
    ranked = [p for r, p in sorted(((pre(p), p) for p in parcels if pre(p)), key=lambda x: (-x[0][0], -x[0][1], x[1]["pid"]))][:MSO_CAP]
    S = requests.Session(); S.headers.update(UA); last = [0.0]
    def g(path):
        w = last[0] + MSO_GAP - time.time()
        if w > 0: time.sleep(w)
        try: return S.get(MSO_ITAX + path, timeout=(10, 20)).text
        finally: last[0] = time.time()
    g("default.aspx")                                          # session cookie; detail pages redirect without it
    recs, statuses, errors, t0 = {}, collections.Counter(), 0, time.time()
    for i, p in enumerate(ranked):
        if time.time() - t0 > 900: break                       # hard 15-minute budget
        tid = p["tax"].lstrip("0")
        try:
            h = g(f"detail.aspx?taxid={tid}")
            m = re.search(r'id="_ctl0_ContentPlaceHolder1_lblStatus"[^>]*>(.*?)</', h, re.S)
            if not m:
                if "default.aspx" in h[:3000] or "No records found" in h: g("default.aspx")
                errors += 1; continue
            st = strip_tags(m.group(1)); statuses[st] += 1
            t = re.sub(r"\s+", " ", strip_tags(re.sub(r"<(script|style).*?</\1>", "", g("payoff.aspx"), flags=re.S | re.I).replace("<", " <")))
            by = {}
            for yr, due, half, amt in re.findall(r"\b(\d{4}) (\d{1,2}/\d{1,2}/\d{4}) \d+ (1st|2nd) \$([\d,]+\.\d\d)", t):
                mo, d, y = map(int, due.split("/"))
                if dt.date(y, mo, d) < SALE_TODAY: by[int(yr)] = by.get(int(yr), 0.0) + money(amt)
            by = {y: round(a, 2) for y, a in by.items() if a > 0.005}
            if by: recs[p["tax"]] = {"years": set(by), "due": round(sum(by.values()), 2), "by_year": by}
        except Exception as e:
            errors += 1; Actor.log.warning(f"Missoula iTax check failed for one parcel: {scrub(e)}")
        if errors > 25 and errors > 0.2 * (i + 1): raise RuntimeError(f"Missoula iTax: {errors} failed checks out of {i + 1}; stopping")
    n = sum(statuses.values())
    if n == 0: raise RuntimeError("Missoula iTax: no parcel could be checked")
    SOURCES["treasurer_delinquent"] = {"ok": True, "rows": len(recs), "checked": n, "errors": errors, "statuses": dict(statuses),
                                       "source": "Missoula County Clerk & Treasurer iTax (per-parcel pages, top parcels by ownership signals only)",
                                       "list": f"no public list; {n} top-ranked parcels checked on the county tax pages", "as_of": f"{TODAY.month}/{TODAY.day}/{TODAY.year}"}
    Actor.log.info(f"Missoula iTax: checked {n} parcels in {time.time() - t0:.0f}s, statuses {dict(statuses)}, delinquent {len(recs)}, errors {errors}")
    return recs

SB_DEED_LIST = "https://www.co.silverbow.mt.us/DocumentCenter/View/37936/Tax-Deed-property-list-2026"
SB_ITAX = "https://itax.tylertech.com/ButteSilverBowMT/detail.aspx?taxid={}"   # not linked: the page shows the owner's name and mailing address
def delinq_silverbow():
    """Butte-Silver Bow 'Current Delinquent Property List' (XLSX): parcels at the tax-deed stage (3+ years unpaid). Only column A
    (parcel #) is read; the other columns name the owners and are never parsed. The list has no amounts, so every multifamily
    parcel on it is re-checked on the county's iTax page before it can score (see the candidate loop)."""
    import io, zipfile, xml.etree.ElementTree as ET
    z = zipfile.ZipFile(io.BytesIO(get(SB_DEED_LIST, binary=True, timeout=60))); names = z.namelist()
    M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    ss = ["".join(t.text or "" for t in si.iter(M + "t")) for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(M + "si")] if "xl/sharedStrings.xml" in names else []
    recs = {}
    for c in ET.fromstring(z.read(sorted(n for n in names if n.startswith("xl/worksheets/sheet"))[0])).iter(M + "c"):
        if not re.fullmatch(r"A\d+", c.get("r", "")): continue
        v = c.find(M + "v"); v = (v.text or "") if v is not None else ""
        if c.get("t") == "s" and v.isdigit(): v = ss[int(v)]
        v = v.strip()
        if re.fullmatch(r"\d{3,10}(?:\.0)?", v): recs[v.split(".")[0].zfill(10)] = {"years": set(), "due": 0.0, "by_year": {}, "tax_deed": True}
    if not recs: raise RuntimeError("Silver Bow tax-deed list: no parcel numbers found")
    m = re.search(r"modified[^>]*>(\d{4})-(\d\d)-(\d\d)", z.read("docProps/core.xml").decode("utf-8", "ignore")) if "docProps/core.xml" in names else None
    SOURCES["treasurer_delinquent"] = {"ok": True, "rows": len(recs), "source": "Butte-Silver Bow Treasurer: Current Delinquent Property List (tax-deed stage)",
                                       "list": "Butte-Silver Bow tax-deed property list (XLSX, not linked: it prints owner names)",
                                       "as_of": f"{int(m.group(2))}/{int(m.group(3))}/{m.group(1)}" if m else None}
    return recs

SAFE_URL_PREFIXES = (YC + "/treasurer/propertysearch/csadelin.asp", YC + "/Sheriff/SheriffSales.asp", YC + "/treasurer/TaxSales.asp",
                     *HWM.values(), GALLATIN_DLQ_PAGE, PARK_TREAS_PAGE, MSO_TREAS_PAGE, SB_DEED_LIST, "https://services.arcgis.com/qnjIrwR8z5Izc0ij/arcgis/rest/services/Montana_Cadastral_Framework/FeatureServer/1")
DELINQ = {"Silver Bow": delinq_silverbow, "Yellowstone": delinq_yellowstone, "Gallatin": delinq_gallatin, "Park": delinq_park, "Missoula": delinq_missoula}
TREAS_LINK = {"Yellowstone": lambda tid: f"{YC}/treasurer/propertysearch/csadelin.asp?propid={tid}"}   # csadelin shows no owner name
# Billings code-case records are NOT linked: the city's case pages show the owner's name and mailing address.

# ---------------------------------------------------------------- foreclosure / sale lists (address key -> detail)
# a sale that is cancelled, postponed, already held or sold must not score ('to be sold as-is' in a live notice is NOT a marker)
DEAD_SALE_RE = re.compile(r"cancel|postpon|continued|vacated|withdrawn|sale completed|completed|\bsold to\b|\bsold\b(?! as-is)(?<!be sold)|on hold|stayed|bankrupt|\bBK\b|chapter (?:7|11|12|13)\b", re.I)

def split_addresses(street):
    """'1207 and 1219 S 32nd St W' -> ['1207 S 32nd St W', '1219 S 32nd St W']; '1131 & 1133 No 24th Street' likewise"""
    m = re.match(r"^\s*(\d+[A-Z]?)((?:\s*(?:,|&|and)\s*\d+[A-Z]?)+)\s+(.*)$", street or "", re.I)
    if not m: return [street]
    nums = [m.group(1)] + re.findall(r"\d+[A-Z]?", m.group(2))
    return [f"{n} {m.group(3)}" for n in nums]

def _sale_date(txt):
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", txt or "")
    try: return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None
    except ValueError: return None

def hwm_sales(county, state="MT", today=None):
    """Halliday, Watkins & Mann trustee-sale list for one county of MT, WY or ID (same table on every state page).
    Only sales dated today or later with no cancelled / postponed / sold status count (a past or dead sale must never score or qualify)."""
    url = HWM[state]; h = get(url); out = {}; skipped = 0; today = today or SALE_TODAY
    for row in re.findall(r"<tr>(.*?)</tr>", h, re.S | re.I):
        c = [strip_tags(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]
        if len(c) >= 5 and c[1].strip().lower() == county.lower() and c[2].strip().upper() in ("", state):
            d = _sale_date(c[4]); status = " ".join(c[7:]) if len(c) > 7 else ""
            if d is None or d < today or DEAD_SALE_RE.search(status): skipped += 1; continue
            for street in split_addresses(c[3].split(",")[0]):
                if is_unit_address(street): continue                  # '#Apt 11', 'Condo A': a unit, not the building
                k = addr_key(street)
                if k: out[k] = f"trustee/foreclosure sale {d:%m/%d/%Y} (Halliday, Watkins & Mann file {c[0]})"
    SOURCES["foreclosure_firm_list"] = {"ok": True, "rows": len(out), "skipped_past_or_cancelled": skipped, "url": url}
    return out

def sheriff_yellowstone():
    """Row by row: date cell, control number, address, notes. Only sales dated today or later and not marked
    Cancelled / Sale Completed / SOLD / postponed count; 'A and B Street' rows are split into two addresses."""
    s = get(YC + "/Sheriff/SheriffSales.asp")
    i = s.find("Current Sheriff"); s = s[i:] if i >= 0 else s
    out, skipped, total = {}, 0, 0
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", s, re.S):
        c = [re.sub(r"\s+", " ", strip_tags(x)) for x in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(c) < 3: continue
        total += 1
        d = _sale_date(c[0])
        if d is None or d < SALE_TODAY or DEAD_SALE_RE.search(c[0] + " " + (c[3] if len(c) > 3 else "")): skipped += 1; continue
        for street in split_addresses(c[2].split(",")[0]):
            if is_unit_address(street): continue
            k = addr_key(street)
            if k: out[k] = f"sheriff sale {d:%m/%d/%Y} (Yellowstone County sheriff sale list, control # {c[1].strip()})"
    SOURCES["sheriff_sales"] = {"ok": True, "rows": len(out), "listed": total, "skipped_past_or_cancelled": skipped, "url": YC + "/Sheriff/SheriffSales.asp"}
    return out

def _case_year(num):
    m = re.match(r"CE-?(\d{2})-", num or "") or re.match(r"CE(20\d{2})", num or "")
    return (2000 + int(m.group(1))) if m and len(m.group(1)) == 2 else (int(m.group(1)) if m else None)

def _case_type(r):
    t = r.get("Code_Violations_Coordinates_CaseType") or r.get("Code_Violations_Type") or ""
    d = re.sub(r"\s+", " ", r.get("Code_Violations_Coordinates_CaseDescription") or "").strip()
    if (not t or t.lower().startswith("legacy")) and d:              # 'Legacy Miscellaneous' says nothing; the description does
        return d if len(d) <= 80 else d[:77].rsplit(" ", 1)[0] + "..."
    return t or "unknown"

def code_billings():
    F = ("OBJECTID,TAX_ID,Code_Violations_Type,Code_Violations_FlagDangerous,Code_Violations_DateCited,Code_Violations_DateEntered,"
         "Code_Violations_Coordinates_CaseType,Code_Violations_Coordinates_CaseNumber,Code_Violations_Coordinates_CaseStatus,"
         "Code_Violations_Coordinates_CaseDescription")
    o = arcgis(BIL_CODE.format(8), "1=1", F)
    cutoff = TODAY - dt.timedelta(days=730)
    c = arcgis(BIL_CODE.format(9), f"Code_Violations_DateCited >= DATE '{cutoff.isoformat()}' OR Code_Violations_DateCited IS NULL", F)
    def rec(r, layer, status):
        ms = r.get("Code_Violations_DateCited") or r.get("Code_Violations_DateEntered")
        d = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).date().isoformat() if ms else None
        return {"case_number": r.get("Code_Violations_Coordinates_CaseNumber") or "unknown", "status": status,
                "case_status_detail": r.get("Code_Violations_Coordinates_CaseStatus") or "unknown",
                "type": _case_type(r),
                "date_cited": d or "unknown", "dangerous": bool(r.get("Code_Violations_FlagDangerous")),
                "_oid": f"{layer}/{r['OBJECTID']}"}          # internal de-dup key only, never output
    op, cl = {}, {}
    for r in o:
        if r.get("TAX_ID"): op.setdefault(r["TAX_ID"].strip(), []).append(rec(r, 8, "open"))
    for r in c:
        if not r.get("TAX_ID"): continue
        if not (r.get("Code_Violations_DateCited") or r.get("Code_Violations_DateEntered")):
            y = _case_year(r.get("Code_Violations_Coordinates_CaseNumber"))      # newer cases carry no date; use the case-number year
            if not y or y < TODAY.year - 1: continue
        elif (r.get("Code_Violations_DateCited") or r.get("Code_Violations_DateEntered")) / 1000 < time.mktime(cutoff.timetuple()): continue
        cl.setdefault(r["TAX_ID"].strip(), []).append(rec(r, 9, "closed"))
    def one_per_case(d):                     # a case can have several violation points; keep one entry per case number
        for tid, lst in d.items():
            m = {}
            for x in lst:
                k = x["case_number"] if x["case_number"] != "unknown" else x["_oid"]
                if k in m: m[k]["dangerous"] = m[k]["dangerous"] or x["dangerous"]
                else: m[k] = x
            d[tid] = [{kk: vv for kk, vv in x.items() if not kk.startswith("_")} for x in m.values()]
        return d
    SOURCES["code_cases_billings"] = {"ok": True, "rows": len(o) + len(c), "url": "City of Billings code enforcement GIS (public ArcGIS service; case pages not linked because they show owner names)"}
    return one_per_case(op), one_per_case(cl)

def safe(name, fn, default):
    try: return fn()
    except Exception as e:
        Actor.log.warning(f"source {name} failed: {scrub(e)}"); SOURCES[name] = {"ok": False, "error": scrub(e)[:200]}; return default

# ---------------------------------------------------------------- Gallatin / Park: current status of listed delinquencies
# The county lists are dated (Park ~monthly, Gallatin the August lien list), so a parcel can be paid after the list date.
# Each lead whose delinquency comes from such a list is re-checked on the county's free iTax detail page (one GET per tax
# id, 2 s apart, hard 20 s timeout) before it is pushed. Only tax figures are read from the page; owner fields never are.
_STATUS = {}
def itax_status(county, tid, url=None):
    if tid in _STATUS: return _STATUS[tid]
    try:
        h = get((url or ITAX_DETAIL[county]).format(tid), timeout=20, tries=2)
        f = {}
        for k in ("lblStatus", "lblYearTaxes", "lblTaxFirstHalf", "lblTaxSecondHalf", "lblPayFirstHalf", "lblPaySecondHalf"):
            m = re.search(rf'id="_ctl0_ContentPlaceHolder1_{k}"[^>]*>(.*?)</', h, re.S)
            f[k] = re.sub(r"\s+", " ", strip_tags(m.group(1))) if m else None
        y = re.match(r"(\d{4})", f["lblYearTaxes"] or "")
        if not f["lblStatus"] or not y or None in (f["lblTaxFirstHalf"], f["lblTaxSecondHalf"], f["lblPayFirstHalf"], f["lblPaySecondHalf"]):
            raise ValueError("unexpected page layout")
        unpaid = max(0.0, money(f["lblTaxFirstHalf"]) - money(f["lblPayFirstHalf"])) + max(0.0, money(f["lblTaxSecondHalf"]) - money(f["lblPaySecondHalf"]))
        bill = money(f["lblTaxFirstHalf"]) + money(f["lblTaxSecondHalf"])
        paid = min(money(f["lblPayFirstHalf"]), money(f["lblTaxFirstHalf"])) + min(money(f["lblPaySecondHalf"]), money(f["lblTaxSecondHalf"]))  # payments toward tax (P&I excluded)
        r = {"status": f["lblStatus"], "year": int(y.group(1)), "unpaid_year": round(unpaid, 2), "bill_year": round(bill, 2), "paid_year": round(paid, 2)}
    except Exception as e:
        Actor.log.warning(f"current-status check failed for {county} tax id {tid}: {scrub(e)}"); r = None
    _STATUS[tid] = r
    return r

def retax(L, due, years, U, note):
    """re-score a lead after its delinquent amount changed (same rules as the first pass)"""
    bd = {k: v for k, v in L["score_breakdown"].items() if k not in ("tax_delinquent", "tax_delinquent_multi_year")}
    hard = False
    L["signals"] = [x for x in L["signals"] if not x.startswith(("TAX DELINQUENT", "minor delinquent"))]
    if due >= MIN_DELINQUENT_USD:
        value = L["assessed_value"] if isinstance(L["assessed_value"], (int, float)) else None
        pts, tier, hard, pct = tax_tier(due, value)
        bd["tax_delinquent"] = pts
        if hard and len(years) > 1: bd["tax_delinquent_multi_year"] = 1
        L.update(tax_delinquent="yes", tax_delinquent_amount=round(due, 2), tax_delinquent_years=sorted(years),
                 tax_delinquent_pct_of_value=pct if pct is not None else U, tax_delinquency_tier=tier)
        L["signals"] = [f"TAX DELINQUENT {', '.join(map(str, sorted(years)))} ${due:,.0f}" + (f" = {pct:.2f}% of assessed value" if pct is not None else "") +
                        f", {tier.split(' (')[0]} tier (excl. penalty & interest; {note})"] + L["signals"]
    else:
        L.update(tax_delinquent="no", tax_delinquent_amount=0.0, tax_delinquent_years=[], tax_delinquent_pct_of_value=0.0, tax_delinquency_tier="none")
    L["score_breakdown"] = bd; L["score"] = sum(bd.values()); L["_hard"] = L["_hard_other"] or hard

def _yrs(ys): return ", ".join(map(str, sorted(ys)))

def current_status_pass(county, leads, cap, qualifies, U, max_checks=250, max_secs=540):
    """Walk the leads best-first; re-check the listed delinquency of each one before it can be returned.
    The county page (iTax) shows ONLY its current tax year, so the check works per tax year:
      - the page's year, if it is on the list: confirmed (unpaid = tax minus payments per half) or reduced / zeroed if paid;
      - every other listed year: kept at the list amount and labelled 'not checked (as of the list date)';
      - page year not on the list at all (e.g. after iTax rolls over to the next tax year): nothing on the list can be
        confirmed, so the whole lead is treated as 'not checked' (kept, labelled, amounts as of the list date).
    A lead whose re-checked amount no longer qualifies is dropped (never billed); a reduced lead is re-scored and re-ranked."""
    if county not in ITAX_DETAIL: return leads
    list_date = (SOURCES.get("treasurer_delinquent") or {}).get("as_of") or "unknown date"
    key = lambda L: (-L["score"], -L["units"], L["parcel_id"])
    pending, final, t0 = sorted(leads, key=key), [], time.time()
    st = {"ok": True, "checked": 0, "current_year_confirmed_unpaid": 0, "reduced": 0, "reduced_tax_ids": [], "dropped_paid": 0, "dropped_below_min_score_after_recheck": 0,
          "dropped_below_min_score_tax_ids": [], "not_checked": 0,
          "not_checked_page_year_not_on_list": 0, "page_tax_years": [], "dropped_paid_tax_ids": [], "list_date": list_date,
          "source": f"{TAX_SITE[county]}, per-parcel detail page (current tax year only)"}
    page_years, day = set(), TODAY.isoformat()
    def unchecked(L, why):
        st["not_checked"] += 1
        L["data_as_of"]["treasurer_current_status"] = f"not checked this run ({why}); amounts are as of the list date"
        L["signals"] = [x.replace("(excl. penalty & interest)", f"(excl. penalty & interest; county list dated {list_date}; current status not checked)")
                        if x.startswith("TAX DELINQUENT") else x for x in L["signals"]]
        final.append(L)
    while pending and len(final) < cap:
        L = pending.pop(0)
        if not (L["_tax"] and not L.get("_checked")): final.append(L); continue
        L["_checked"] = True
        budget = st["checked"] < max_checks and time.time() - t0 < max_secs
        res = [itax_status(county, tid) if (tid and budget) else None for tid, _, _ in L["_tax"]]
        if budget: st["checked"] += len(res)
        if any(r is None for r in res): unchecked(L, "status page unavailable or check budget used"); continue
        old, new, years, confirmed, notchk = L["tax_delinquent_amount"], 0.0, set(), set(), set()
        for (tid, by_year, _), r in zip(L["_tax"], res):
            Y = r["year"]; page_years.add(Y)
            for y, amt in by_year.items():
                if y == Y:                                   # the one year the page shows
                    a = 0.0 if r["status"].strip().lower() == "paid" else min(amt, r["unpaid_year"])
                    confirmed.add(y) if a > 0.005 else None
                else: a = amt; notchk.add(y)                # not shown on the page: as of the list
                if a > 0.005: new += a; years.add(y)
        checked_any = any(r["year"] in by_year for (tid, by_year, _), r in zip(L["_tax"], res))
        if not checked_any:
            st["not_checked_page_year_not_on_list"] += 1
            unchecked(L, f"the county tax page shows tax year {_yrs({r['year'] for r in res})}, which is not on the county list; earlier years can't be checked there"); continue
        notchk -= confirmed
        rest = f"; {_yrs(notchk)} not checked (as of the list date; the county page shows only the current tax year)" if notchk else ""
        if abs(new - old) < 0.01:
            st["current_year_confirmed_unpaid"] += 1
            L["data_as_of"]["treasurer_current_status"] = f"tax year {_yrs(confirmed)} re-checked on the county tax page on {day}{rest}"
            L["signals"] = [x.replace("(excl. penalty & interest)", f"(excl. penalty & interest; county list dated {list_date}; {_yrs(confirmed)} confirmed unpaid on the county tax page {day}{rest})")
                            if x.startswith("TAX DELINQUENT") else x for x in L["signals"]]
            final.append(L); continue
        paidy = _yrs({y for (tid, by_year, _), r in zip(L["_tax"], res) for y in by_year if y == r["year"]})
        # say exactly what the county page shows for its year: a payment only if payments > 0, otherwise the (lower) bill
        parts = []
        for (tid, by_year, _), r in zip(L["_tax"], res):
            Y = r["year"]
            if Y not in by_year: continue
            lst = by_year[Y]; tid_s = f" (tax id {tid})" if len(L["_tax"]) > 1 else ""
            if r["status"].strip().lower() == "paid" or r["unpaid_year"] <= 0.005:
                parts.append(f"{Y} shows as paid on the county tax page{tid_s} (list showed ${lst:,.2f})")
            elif r["paid_year"] > 0.005:
                parts.append(f"{Y} bill on county page is ${r['bill_year']:,.2f} (list showed ${lst:,.2f}), ${r['paid_year']:,.2f} paid, ${r['unpaid_year']:,.2f} unpaid{tid_s}")
            else:
                parts.append(f"{Y} bill on county page is ${r['bill_year']:,.2f} (list showed ${lst:,.2f}), nothing paid{tid_s}")
        what = "; ".join(parts) + f", checked {day}"
        retax(L, new, years, U, f"county list dated {list_date}; {what}{rest}")
        L["data_as_of"]["treasurer_current_status"] = f"tax year {paidy} re-checked on the county tax page on {day}{rest}"
        L["verification_notes"] = list(L["verification_notes"]) + [f"tax: the county list dated {list_date} showed ${old:,.2f} in total; re-check: {what}; so ${new:,.2f} is used"]
        if not qualifies(L):                                         # paid since the list date: not returned, not billed
            ids = ", ".join(t for t, _, _ in L["_tax"] if t)
            if new < MIN_DELINQUENT_USD: st["dropped_paid"] += 1; st["dropped_paid_tax_ids"].append(ids)          # nothing (scorable) left owing
            else: st["dropped_below_min_score_after_recheck"] += 1; st["dropped_below_min_score_tax_ids"].append(ids)  # still owes, now below minScore
            continue
        st["reduced"] += 1; st["reduced_tax_ids"].append(", ".join(t for t, _, _ in L["_tax"] if t))
        pending.append(L); pending.sort(key=key)                    # re-rank with its corrected score
    st["page_tax_years"] = sorted(page_years); st["past_cap_not_checked"] = sum(1 for L in pending if L["_tax"] and not L.get("_checked"))
    SOURCES["current_status_check"] = st
    return final + pending                                                 # pending beyond cap are only counted, never pushed

# ---------------------------------------------------------------- main
async def main():
    async with Actor:
        inp = await Actor.get_input() or {}
        county = inp.get("county") or "Yellowstone"
        if county not in COUNTIES:
            await Actor.fail(status_message=f"Unsupported county '{county}'. Supported: {', '.join(COUNTIES)}"); return
        min_score = int(inp.get("minScore", 4) or 0)
        max_results = max(0, int(inp.get("maxResults", 20) or 0))
        min_units = max(2, int(inp.get("minUnits", 2) or 2))
        need_distress = inp.get("requireDistressSignal", True) is not False   # hard = tax >=0.1% of value, sale, recorded notice, OPEN code case
        only_new = bool(inp.get("onlyNewSinceLastRun", False))
        cap = max_results or 10_000

        base = json.load(gzip.open(f"{DATA}/{county.lower()}.json.gz", "rt"))
        meta, parcels = base["meta"], [p for p in base["parcels"] if p["units"] >= 2]     # minUnits is applied AFTER merging
        Actor.log.info(f"{county}: {len(parcels)} multifamily parcels in extract (ORION refresh {meta['orion_refresh']})")

        # live cadastral refresh of address / value / owner flags (names used in memory only)
        cad = {}
        def _cad():
            pids = [p["pid"] for p in parcels]; rows = []
            for i in range(0, len(pids), 250):
                rows += arcgis(MT_CAD, f"CountyName='{county}' AND PropertyID IN ({','.join(map(str, pids[i:i+250]))})", CAD_FIELDS)
            SOURCES["mt_cadastral"] = {"ok": True, "rows": len(rows), "url": MT_CAD.rsplit("/", 1)[0]}
            return {r["PropertyID"]: r for r in rows}
        cad = safe("mt_cadastral", _cad, {})
        # Cadastral has no situs city for some parcels (e.g. ~30% of Gallatin's). Fill it from the parcel's levy district
        # when at least 5 parcels of that district in this run name a city and >= 90% of them name the same one.
        _lc = {}
        for r in cad.values():
            cty, lv = situs_city(r.get("CityStateZip")), (r.get("LevyDistrict") or "").strip()
            if cty and lv: _lc.setdefault(lv, collections.Counter())[cty] += 1
        levy_city = {lv: C.most_common(1)[0][0] for lv, C in _lc.items() if C.most_common(1)[0][1] >= 5 and C.most_common(1)[0][1] >= 0.9 * sum(C.values())}
        def no_list():     # e.g. Cascade, Lewis and Clark: no public bulk delinquent list (per-parcel lookup or records request only)
            SOURCES["treasurer_delinquent"] = {"ok": True, "rows": 0, "note": f"{county} County publishes no bulk delinquent-tax list; trustee sales only"}; return {}
        delq = safe("treasurer_delinquent", (lambda: delinq_missoula(parcels, cad)) if county == "Missoula" else DELINQ.get(county, no_list), {})
        fc = safe("foreclosure_firm_list", lambda: hwm_sales(county), {})
        sh, co, cc = {}, {}, {}
        if county == "Yellowstone":
            sh = safe("sheriff_sales", sheriff_yellowstone, {})
            co, cc = safe("code_cases_billings", code_billings, ({}, {}))

        U = "unknown"
        def yn(v): return U if v is None else ("yes" if v else "no")
        def kv(v): return U if v in (None, "") else v
        tid_count = {}
        for p in parcels:
            c = cad.get(p["pid"]); t = tax_id(county, c.get("AssessmentCode")) if c else p.get("tax")
            if t: tid_count[t] = tid_count.get(t, 0) + 1
        def name_free(cases, owner):            # never let a case description carry the owner's name
            toks = {t for t in re.findall(r"[A-Z]{3,}", (owner or "").upper()) if t not in ("LLC", "INC", "THE", "AND", "TRUST", "ESTATE", "PROPERTIES", "PROPERTY", "GROUP", "HOLDINGS", "INVESTMENTS", "RENTALS", "LIVING", "FAMILY", "REVOCABLE")}
            out = []
            for x in cases:
                if toks and any(re.search(rf"\b{t}\b", x["type"].upper()) for t in toks): x = {**x, "type": "case description withheld (contains a name)"}
                out.append(x)
            return out
        cands = []
        for p in parcels:
            c = cad.get(p["pid"]); okey = None
            if c:
                p = {**p, "tax": tax_id(county, c.get("AssessmentCode")), "addr": (c.get("AddressLine1") or "").strip() or None,
                     "city": situs_city(c.get("CityStateZip")) or levy_city.get((c.get("LevyDistrict") or "").strip()) or None,
                     "city_inferred": not situs_city(c.get("CityStateZip")) and bool(levy_city.get((c.get("LevyDistrict") or "").strip())), "value": c.get("TotalValue"), "ptype": c.get("PropType"),
                     **owner_flags(c.get("OwnerName"), c.get("CareOfTaxpayer")), **absentee_flags(c)}
                okey = owner_key(c.get("OwnerName"), c.get("OwnerAddress1"))      # internal only, never output
            if not okey and p.get("opid"): okey = "party:" + str(p["opid"])
            if p.get("tax") and tid_count.get(p["tax"], 0) > 1:            # fail closed: an ambiguous tax id joins nothing
                notes_pre = [f"treasurer / code join skipped: tax id {p['tax']} maps to more than one parcel"]; p = {**p, "tax": None}
            else: notes_pre = []
            if p.get("ptype") in EXCLUDE_PTYPES or p.get("owner_type") == "government_nonprofit": continue
            sig, labels, notes = {}, [], list(notes_pre)
            if p.get("city_inferred"): notes.append("city: Montana Cadastral lists no city for this address; the city shown is the one named by nearly all other parcels in the same levy district")
            value = p.get("value") if isinstance(p.get("value"), (int, float)) and p.get("value") > 0 else None
            d = delq.get(p.get("tax")) if p.get("tax") else None
            tier = pct = None
            if d and d["due"] >= MIN_DELINQUENT_USD:
                pts, tier, hard, pct = tax_tier(d["due"], value)
                sig["tax_points"] = pts; sig["tax_delinquent_hard"] = hard; sig["tax_delinquent_multi_year"] = len(d["years"]) > 1
                labels.append(f"TAX DELINQUENT {', '.join(map(str, sorted(d['years'])))} ${d['due']:,.0f}" + (f" = {pct:.2f}% of assessed value" if pct is not None else "") +
                              f", {tier.split(' (')[0]} tier (excl. penalty & interest)" +
                              (("; all of it" if abs(d["assigned_due"] - d["due"]) < 0.01 else f"; incl. ${d['assigned_due']:,.0f}") +
                               f" for {', '.join(map(str, sorted(d['assigned_years'])))} under a tax-lien assignment (lien sold to a third party; point-in-time snapshot of {ASSIGN_ASOF or 'unknown date'})"
                               if d.get("assigned_due") else ""))
            elif d and d.get("tax_deed"):      # Silver Bow: tax-deed list (no amounts); scores only if the county page still says Delinquent
                st_, lst = itax_status(county, p["tax"], SB_ITAX), (SOURCES.get("treasurer_delinquent") or {}).get("as_of") or "unknown date"
                if st_ and st_["status"].strip().lower().startswith("delinquent"):
                    sig.update(tax_points=4, tax_delinquent_hard=True, tax_delinquent_multi_year=True)
                    labels.append(f"TAX-DEED STAGE: on the county's tax-deed property list dated {lst} (3+ years of unpaid taxes; the list gives no amounts); "
                                  f"county tax page still shows status Delinquent, checked {dt.date.today():%Y-%m-%d}")
                else: notes.append(f"treasurer: on the county's tax-deed list dated {lst}, but the county tax page " +
                                   (f"now shows status '{st_['status']}'" if st_ else "could not be checked") + " (not scored)")
            elif d: labels.append(f"minor delinquent balance ${d['due']:,.0f} (under ${MIN_DELINQUENT_USD}, not scored)")
            k = addr_key(p.get("addr") or "")
            sale = (fc.get(k) or sh.get(k)) if k else None
            if sale: sig["foreclosure_sale"] = True; labels.append("FORECLOSURE / SALE LISTED: " + sale)
            recs = [{"type": r["type"], "doc_number": r["doc"], "recorded": r["date"],
                     "lookup": f"search document number {r['doc']} (recorded {r['date']}) in the Yellowstone County Clerk & Recorder's free "
                               "guest search (Public/Guest Login); not linked because recorded documents name the parties"}
                    for r in (p.get("rec") or [])]
            if recs:
                sig["recorded_distress"] = True
                labels.append("RECORDED (matched by legal description): " + "; ".join(f"{r['type']} #{r['doc_number']} {r['recorded']}" for r in recs[:3]))
            open_c = name_free(co.get(p.get("tax"), []), c.get("OwnerName") if c else None) if p.get("tax") else []
            closed_c = name_free(cc.get(p.get("tax"), []), c.get("OwnerName") if c else None) if p.get("tax") else []
            if open_c:
                sig["code_open"] = True; sig["code_dangerous"] = any(x["dangerous"] for x in open_c)
                labels.append("OPEN CODE CASE" + (" [dangerous]" if sig["code_dangerous"] else "") + ": " +
                              "; ".join(f"{x['case_number']} {x['type']}" for x in open_c[:3]))
            if closed_c:
                sig["closed_code_case_24mo"] = True
                labels.append("closed code case (24 mo), display only, not scored: " + "; ".join(f"{x['case_number']} {x['type']}" for x in closed_c[:3]))
            yrs, basis, deed_date = None, U, kv(p.get("deed"))
            if p.get("deed"):
                try: yrs = round((TODAY - dt.date.fromisoformat(p["deed"])).days / 365.25, 1); basis = "since last recorded deed"
                except ValueError: pass
            elif p.get("own_since_max"):
                yrs = round((TODAY - dt.date.fromisoformat(p["own_since_max"])).days / 365.25, 1)
                basis = f"minimum: no deed on file with MT DOR; owner of record unchanged since the state's {p['own_since_max'][:4]} system conversion"
                deed_date = f"unknown (no deed on file; owner unchanged since {p['own_since_max']})"
            deed_type = kv(p.get("deed_type"))
            if p.get("deed") and not p.get("deed_type"):
                deed_type = "unknown (the MT DOR deed table has no document type for this deed)"
                notes.append(f"last deed: the state deed table lists the {p['deed']} deed" + (f" ({p['deed_ref']})" if p.get("deed_ref") else "") +
                             " without a document type; look it up on the county recorder if the deed type matters")
            sig["years_owned"] = yrs
            if yrs is not None and yrs >= 10: labels.append(f"long ownership {'at least ' if basis.startswith('minimum') else ''}{yrs:g} yrs")
            sig["out_of_state"] = bool(p.get("out_of_state")); sig["nonlocal_instate"] = bool(p.get("nonlocal_instate"))
            if sig["out_of_state"]: labels.append("out-of-state owner")
            elif sig["nonlocal_instate"]: labels.append("non-local in-state owner")
            sig["estate"] = bool(p.get("estate")); sig["trust"] = bool(p.get("trust"))
            if sig["estate"]: labels.append("estate / deceased indicator in owner of record")
            elif sig["trust"]: labels.append("trust owner")
            if p.get("deed_type") and DEATH_DEED_RE.search(p["deed_type"]) and yrs is not None and yrs <= 5:
                sig["death_deed_5y"] = True; labels.append(f"death / estate deed within 5 yrs ({p['deed_type']})")
            s_, parts = score(sig)
            links = []
            notes.append(f"parcel record: search geocode {p['geo']} on Montana Cadastral (Montana State Library); not linked because the parcel page shows the owner's name and mailing address")
            if p.get("tax") and county in TREAS_LINK: links.append(TREAS_LINK[county](p["tax"]))
            elif p.get("tax"): notes.append(f"county tax record: look up tax id {p['tax']} on {TAX_SITE[county]}; not linked because that page shows the owner's name and mailing address")
            else: notes.append("treasurer: tax id unknown for this parcel; search the geocode on the county treasurer's site")
            if sale:
                links.append(HWM_MT if k in fc else YC + "/Sheriff/SheriffSales.asp")
                notes.append("sale list: the public list has no per-sale page; find this address on the linked list")
            if open_c or closed_c:
                notes.append("code case: look up case " + ", ".join(x["case_number"] for x in (open_c + closed_c)[:5]) +
                             " with the City of Billings Code Enforcement Division (no link: the city's case pages show owner details)")
            if recs: notes.append("recorder: look up each document number in recorded_distress_docs in the Yellowstone County Clerk & Recorder guest search; index entries are free, images are paid and not needed")
            mixed = U if not p.get("ptype") else ("yes" if p["ptype"] == "Improved Property" else "no")    # final value set after merging
            raw = p.get("addr")
            has_due = bool(d and d["due"] >= MIN_DELINQUENT_USD)
            cands.append({"_okey": okey, "_street": street_key(raw), "_hard": any(sig.get(x) for x in HARD_SIGNALS),
                "_hard_other": any(sig.get(x) for x in HARD_SIGNALS if x != "tax_delinquent_hard"),
                "_tax": [(p.get("tax"), dict(d.get("by_year") or {}) if d else {}, d.get("assigned_due", 0.0) if d else 0.0)] if has_due else [], "_due": d["due"] if d and d["due"] >= MIN_DELINQUENT_USD else 0.0,
                "_years": sorted(d["years"]) if d and d["due"] >= MIN_DELINQUENT_USD else [], "_assigned": d.get("assigned_due", 0.0) if d and d["due"] >= MIN_DELINQUENT_USD else 0.0,
                "parcel_id": p["geo"], "property_id": p["pid"], "tax_id": kv(p.get("tax")), "county": county, "state": "MT",
                "address": clean_address(raw) or U, "address_raw": kv(raw), "city": kv(p.get("city")),
                "units": p["units"], "size_class": "5+" if p["units"] >= 5 else "2-4",
                "year_built": kv(p.get("yb")), "assessed_value": value if value is not None else U, "property_type": kv(p.get("ptype")), "possible_mixed_use": mixed, "_mixed": mixed,
                "owner_type": kv(p.get("owner_type")), "absentee_owner": yn(p.get("absentee")), "out_of_state_owner": yn(p.get("out_of_state")),
                "years_owned": yrs if yrs is not None else U, "years_owned_basis": basis, "last_deed_date": deed_date, "last_deed_type": deed_type,
                "tax_delinquent": "yes" if sig.get("tax_points") else "no", "tax_delinquent_years": sorted(d["years"]) if d and d["due"] >= MIN_DELINQUENT_USD else [],
                "tax_delinquent_amount": round(d["due"], 2) if d and d["due"] >= MIN_DELINQUENT_USD else 0.0,
                "tax_delinquent_pct_of_value": pct if pct is not None else (U if sig.get("tax_points") else 0.0),
                "tax_delinquency_tier": tier or "none",
                "code_case": ("open-dangerous" if sig.get("code_dangerous") else "open") if open_c else ("closed (24 mo)" if closed_c else "none"),
                "code_cases": open_c + closed_c, "foreclosure_sale": sale or "none", "recorded_distress_docs": recs,
                "score": s_, "score_breakdown": parts, "signals": labels, "source_links": links, "verification_notes": notes,
                "merged_parcel_ids": [p["geo"]], "merged_parcels": [], "parcel_count": 1,
                "data_as_of": {"orion_extract": meta["orion_refresh"][:10], "treasurer_list": ((SOURCES.get("treasurer_delinquent") or {}).get("as_of") or U) if county == "Yellowstone" else
                                   (f"{(SOURCES.get('treasurer_delinquent') or {}).get('as_of') or 'unknown'} (date on the county's latest published delinquent list)"),
                               "recorder_index": meta.get("recorder_index_as_of") or ("n/a (Yellowstone only)" if county != "Yellowstone" else U), "tax_lien_assignments": (f"point-in-time snapshot taken {ASSIGN_ASOF} (not refreshed automatically)" if ASSIGN_ASOF else U)
                                                       if county == "Yellowstone" else "n/a (Yellowstone only)",
                               "run_date": TODAY.isoformat()}})
        # merge same-owner parcels with the same profile on the same street (e.g. 1431 + 1441 Sourdough Ln) into ONE lead
        groups = {}
        for L in cands:
            key = (L["_okey"], L["_street"], L["city"], L["units"], L["year_built"]) if L["_okey"] and L["_street"] else ("solo", L["parcel_id"], L["property_id"])
            groups.setdefault(key, []).append(L)
        leads = []
        for g in groups.values():
            g.sort(key=lambda L: (-L["score"], L["parcel_id"]))
            L = dict(g[0])
            if len(g) > 1:
                L["merged_parcel_ids"] = [x["parcel_id"] for x in g]; L["parcel_count"] = len(g)
                L["merged_parcels"] = [{"parcel_id": x["parcel_id"], "tax_id": x["tax_id"], "address": x["address"], "units": x["units"],
                                        "assessed_value": x["assessed_value"], "tax_delinquent_amount": x["tax_delinquent_amount"], "score": x["score"]} for x in g]
                L["units"] = sum(x["units"] for x in g); L["size_class"] = "5+" if L["units"] >= 5 else "2-4"
                pm = {x["_mixed"] for x in g}; L["_mixed"] = "yes" if "yes" in pm else ("no" if pm == {"no"} else U)
                nums = [x["address"].split(" ")[0] for x in g if x["address"] != U and re.match(r"\d", x["address"])]
                nums = sorted(nums, key=lambda n: int(re.match(r"\d+", n).group(0)))
                if len(nums) == len(g): L["address"] = ", ".join(nums[:-1]) + " & " + nums[-1] + " " + g[0]["address"].split(" ", 1)[1]
                L["address_raw"] = " | ".join(x["address_raw"] for x in g)
                vals = [x["assessed_value"] for x in g if isinstance(x["assessed_value"], (int, float))]
                L["assessed_value"] = sum(vals) if len(vals) == len(g) else L["assessed_value"]
                L["tax_delinquent_amount"] = round(sum(x["_due"] for x in g), 2)
                L["tax_delinquent_years"] = sorted({y for x in g for y in x["_years"]})
                if L["tax_delinquent_amount"] >= MIN_DELINQUENT_USD:     # re-tier on the combined amount vs the combined value
                    pts, tier, hard, pct = tax_tier(L["tax_delinquent_amount"], L["assessed_value"] if isinstance(L["assessed_value"], (int, float)) else None)
                    bd = dict(L["score_breakdown"]); bd["tax_delinquent"] = pts
                    if hard and len(L["tax_delinquent_years"]) > 1: bd["tax_delinquent_multi_year"] = 1
                    else: bd.pop("tax_delinquent_multi_year", None)
                    L["score_breakdown"] = bd; L["score"] = sum(bd.values()); L["tax_delinquent"] = "yes"
                    L["tax_delinquency_tier"] = tier; L["tax_delinquent_pct_of_value"] = pct if pct is not None else U
                    L["_hard"] = L["_hard"] or hard
                if L["tax_delinquent_amount"] and not any(x.startswith("TAX DELINQUENT") for x in L["signals"]):
                    L["signals"] = ["TAX DELINQUENT"] + [x for x in L["signals"] if not x.startswith("minor delinquent")]
                if L["tax_delinquent_amount"]:
                    pct_s = f" = {L['tax_delinquent_pct_of_value']:.2f}% of combined assessed value" if isinstance(L["tax_delinquent_pct_of_value"], (int, float)) else ""
                    L["signals"] = [f"TAX DELINQUENT {', '.join(map(str, L['tax_delinquent_years']))} ${L['tax_delinquent_amount']:,.0f} across {len(g)} parcels{pct_s}, "
                                    f"{L['tax_delinquency_tier'].split(' (')[0]} tier (excl. penalty & interest)" +
                                    (f"; incl. ${sum(y['_assigned'] for y in g):,.0f} under a tax-lien assignment (point-in-time snapshot of {ASSIGN_ASOF or 'unknown date'})" if sum(y['_assigned'] for y in g) else "")
                                    if x.startswith("TAX DELINQUENT") else x for x in L["signals"]]
                L["code_cases"] = list({(c["case_number"], c["status"], c["case_status_detail"], c["date_cited"]): c for x in g for c in x["code_cases"]}.values())
                L["recorded_distress_docs"] = list({r["doc_number"]: r for x in g for r in x["recorded_distress_docs"]}.values())
                L["source_links"] = list(dict.fromkeys(u for x in g for u in x["source_links"]))
                L["verification_notes"] = list(dict.fromkeys(n for x in g for n in x["verification_notes"]))
                L["signals"] = L["signals"] + [f"merged {len(g)} parcels on the same street with the same owner, units and year built into this one lead (charged once)"]
                L["_hard"] = L["_hard"] or any(x["_hard"] for x in g)
                L["_hard_other"] = any(x["_hard_other"] for x in g); L["_tax"] = [t for x in g for t in x["_tax"]]
            # possible_mixed_use is only assessed for 5+ unit leads (after merging): for 2-4 units Cadastral's generic
            # 'Improved Property' is the normal type and says nothing about commercial use
            if L["units"] >= 5:
                L["possible_mixed_use"] = L["_mixed"]
                if L["_mixed"] == "yes":
                    L["verification_notes"] = list(L["verification_notes"]) + [
                        "property type: Montana Cadastral classes this 5+ unit parcel as generic 'Improved Property', not 'Apartment', so it may be "
                        "mixed-use (e.g. ground-floor commercial with apartments above); the unit count is residential units from MT DOR"]
            else: L["possible_mixed_use"] = MIXED_NA
            leads.append(L)
        # a live trustee / sheriff sale qualifies on its own; everything else needs minScore (+ a hard signal by default)
        def qualifies(L): return L["units"] >= min_units and (L["foreclosure_sale"] != "none" or (L["score"] >= min_score and (L["_hard"] or not need_distress)))
        leads = [L for L in leads if qualifies(L)]
        # a lead without a property address can't be acted on: never returned or billed, only counted
        n_noaddr = sum(1 for L in leads if L["address"] == U)
        leads = [L for L in leads if L["address"] != U]
        SOURCES["skipped_no_address"] = {"ok": True, "rows": n_noaddr, "note": "qualifying parcels with no property address on Montana Cadastral; not returned, not billed"}
        # final de-duplication by parcel id (a parcel can only be billed once), sort, cap
        best, taken = {}, set()
        for L in sorted(leads, key=lambda L: (-L["score"], -L["units"], L["parcel_id"])):
            if any(pid in taken for pid in L["merged_parcel_ids"]): continue
            taken.update(L["merged_parcel_ids"]); best[L["parcel_id"]] = L
        leads = list(best.values())
        seen, store = set(), None
        if only_new:
            try:
                store = await Actor.open_key_value_store(name="mt-distressed-rental-finder-seen")
                seen = set(await store.get_value(county) or [])
            except Exception as e: Actor.log.warning(f"onlyNewSinceLastRun store unavailable, continuing without it: {e}")
            leads = [L for L in leads if not any(pid in seen for pid in L["merged_parcel_ids"])]
        leads = current_status_pass(county, leads, cap, qualifies, U)
        for L in leads:
            for k in [k for k in L if k.startswith("_")]: del L[k]
        pushed, limit_hit = 0, False
        for L in leads[:cap]:
            r = await Actor.push_data(L, charged_event_name="scored-lead")
            pushed += 1; seen.update(L["merged_parcel_ids"])
            if r is not None and getattr(r, "event_charge_limit_reached", False): limit_hit = True; break
        if store is not None:
            try: await store.set_value(county, sorted(seen))
            except Exception as e: Actor.log.warning(f"could not save seen-parcels state: {e}")
        stats = {"county": county, "extract": meta, "candidates": len(leads), "pushed": pushed, "past_cap_not_returned": max(0, len(leads) - pushed), "sources": SOURCES,
                 "filters": {"minScore": min_score, "minUnits": min_units, "requireDistressSignal": need_distress, "maxResults": max_results}}
        await Actor.set_value("STATS", stats)
        failed = [k for k, v in SOURCES.items() if not v.get("ok")]
        chk = SOURCES.get("current_status_check") or {}
        n_sale_below = sum(1 for L in leads[:pushed] if L["score"] < min_score)      # live sales qualify at any score
        pl = lambda n, w: f"{n} {w}{'' if n == 1 else 's'}"
        more = max(0, len(leads) - pushed)              # qualifying leads not returned (past maxResults or the charge limit)
        checked_county = county in ITAX_DETAIL
        msg = f"{county}: {pl(pushed, 'scored rental lead')} returned (score >= {min_score}, or a live trustee / sheriff sale listing, which qualifies at any score" + \
              (f"; {n_sale_below} of them qualified by a sale listing below score {min_score}" if n_sale_below else "") + ")"
        if more:
            why = "stopped at your max charge limit" if limit_hit else f"past maxResults={max_results}"
            unchk = chk.get("past_cap_not_checked", 0) if checked_county else 0
            msg += f"; {more} more {why}, " + ("not checked or billed" if unchk == more else (f"not billed ({unchk} of them not checked)" if unchk else "not billed"))
        drops = []
        if chk.get("dropped_paid"): drops.append(f"{pl(chk['dropped_paid'], 'lead')} paid since the list date")
        if chk.get("dropped_below_min_score_after_recheck"):
            drops.append(f"{pl(chk['dropped_below_min_score_after_recheck'], 'lead')} below minScore after the re-check (part of the listed tax paid or re-billed, the rest still owed)")
        if drops: msg += "; dropped, not billed: " + " and ".join(drops)
        if n_noaddr: msg += f"; {pl(n_noaddr, 'qualifying parcel')} skipped (no property address), not billed"
        if failed: msg += f". Sources unavailable this run: {', '.join(failed)}"
        Actor.log.info(msg); await Actor.set_status_message(msg[:900])

if __name__ == "__main__":
    asyncio.run(main())
