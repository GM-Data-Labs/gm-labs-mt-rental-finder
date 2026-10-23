"""Shared helpers for the Montana Distressed Rental Finder (GM Labs).
Used by the Actor (src/main.py) and the hand-run rebuild (rebuild/build_extracts.py, not scheduled).
Owner names are only ever used in memory to derive flags; they are never written to output or to the baked extracts."""
import re

UA = {"User-Agent": "gm-labs-mt-parcel-research/1.0 (low-rate; public records)"}
MT_CAD = "https://services.arcgis.com/qnjIrwR8z5Izc0ij/arcgis/rest/services/Montana_Cadastral_Framework/FeatureServer/1/query"
CAD_FIELDS = "PARCELID,PropertyID,AssessmentCode,AddressLine1,CityStateZip,LevyDistrict,PropType,TotalValue,OwnerName,OwnerAddress1,OwnerCity,OwnerState,CareOfTaxpayer"

COUNTIES = {
    # name: orion county number, how the treasurer tax id is derived from the cadastral AssessmentCode
    "Yellowstone": {"orion": 3, "fips": "30111", "seat": "Billings"},
    "Gallatin": {"orion": 6, "fips": "30031", "seat": "Bozeman"},
    "Park": {"orion": 49, "fips": "30067", "seat": "Livingston"},
    "Missoula": {"orion": 4, "fips": "30063", "seat": "Missoula"},
}

def tax_id(county, assessment_code):
    a = (assessment_code or "").strip()
    if county == "Yellowstone":
        # 000A010960 -> A01096 ; 000C02514B -> C02514B (the 10th char is a suffix letter or a 0 filler; the treasurer and
        # Billings GIS both key on the suffixed id, so dropping it would join a neighbouring parcel's data)
        if len(a) < 9: return None
        return a[3:9] + (a[9] if len(a) > 9 and a[9].isalpha() else "")
    if county == "Gallatin":
        return a.lstrip("0") or None   # 00REF42631 -> REF42631 (matches treasurer parcelNo)
    return a or None                   # Park / Missoula: 10-digit parcel number used as-is (Missoula iTax taxid)

EST_RE = re.compile(r"\b(ESTATE|EST OF|ESTATE OF|DECD|DECEASED|HEIRS|PERS(ONAL)? REP|PR OF|LIFE EST(ATE)?|DEVISEES)\b")
TRUST_RE = re.compile(r"\b(TRUST|TRUSTEE|TRUSTEES|TRST|TRS|TR|LIVING TR|REV TR|REVOCABLE)\b")
CORP_RE = re.compile(r"\b(LLC|L L C|INC|CORP|CORPORATION|CO|COMPANY|LP|LLP|LLLP|LTD|PARTNERSHIP|PARTNERS|PROPERTIES|PROPERTY|HOLDINGS|INVESTMENTS?|ENTERPRISES|GROUP|ASSOC|ASSOCIATES|ASSOCIATION|APARTMENTS|RENTALS|MANAGEMENT|DEVELOPMENT|VENTURES|CAPITAL|REALTY|BANK)\b")
GOV_RE = re.compile(r"\b(CITY OF|COUNTY OF|STATE OF|HOUSING AUTHORITY|SCHOOL DIST|UNITED STATES|CHURCH|DIOCESE|MINISTRIES|HABITAT FOR HUMANITY|UNIVERSITY|FOUNDATION)\b")

def owner_flags(owner_name, care_of=""):
    o = (owner_name or "").upper() + " " + (care_of or "").upper()
    estate = bool(EST_RE.search(o))
    trust = bool(TRUST_RE.search(o))
    if GOV_RE.search(o): t = "government_nonprofit"
    elif estate or trust: t = "trust_estate"
    elif CORP_RE.search(o): t = "llc_corp"
    elif o.strip(): t = "individual"
    else: t = "unknown"
    return {"owner_type": t, "estate": estate, "trust": trust and not estate}

def norm_addr(a):
    a = (a or "").upper().replace(".", " ").replace(",", " ")
    a = re.sub(r"\s\d{5}(-\d{4})?\s*$", " ", a.strip())
    rep = {"STREET": "ST", "AVENUE": "AVE", "AV": "AVE", "DRIVE": "DR", "ROAD": "RD", "LANE": "LN", "BOULEVARD": "BLVD", "PLACE": "PL",
           "COURT": "CT", "NORTH": "N", "NO": "N", "SOUTH": "S", "EAST": "E", "WEST": "W", "SAINT": "ST"}
    return " ".join(rep.get(t, t) for t in a.split())

UNIT_RE = re.compile(r"(#|\bAPT\b|\bAPARTMENT\b|\bUNIT\b|\bCONDO\b|\bSTE\b|\bSUITE\b|\bSPACE\b|\bLOT\b|\bTRLR\b)", re.I)
def is_unit_address(a):
    """a single condo / apartment / space inside a larger parcel: must not be joined to the whole building"""
    return bool(UNIT_RE.search(a or ""))

def addr_key(a):
    """house number + first two street tokens; robust join key for sale lists / mailing-vs-situs comparison."""
    n = norm_addr(a).split()
    if not n or not re.match(r"^\d+", n[0]): return None
    num = re.match(r"^\d+", n[0]).group(0)
    rest, prev = [], None
    for t in n[1:]:
        if t in ("N", "S", "E", "W", "NE", "NW", "SE", "SW", "&") and prev != "AVE": prev = t; continue
        if re.match(r"^\d+(-\d+/\d+)?$", t): prev = t; continue
        rest.append(t); prev = t
    return f"{num} {' '.join(rest[:2])}" if rest else None

def situs_city(csz):
    return re.sub(r"\s*,?\s*MT\b.*$", "", (csz or "").strip(), flags=re.I).strip().title()

def absentee_flags(cad):
    """absentee = owner mailing address is not the property address; out_of_state = mailing state is not MT. None = unknown."""
    st = (cad.get("OwnerState") or "").strip().upper()
    oos = (st != "MT") if st else None
    m1 = cad.get("OwnerAddress1") or ""
    mk, sk = addr_key(m1), addr_key(cad.get("AddressLine1") or "")
    if oos: ab = True
    elif mk and sk: ab = mk != sk
    elif re.search(r"\bP\s*O\s*BOX|\bBOX\b", m1.upper()):
        ab = None if (cad.get("OwnerCity") or "").strip().upper() == situs_city(cad.get("CityStateZip")).upper() else True
    else: ab = None
    return {"absentee": ab, "out_of_state": oos,
            "nonlocal_instate": bool(st == "MT" and (cad.get("OwnerCity") or "").strip().upper() not in ("", situs_city(cad.get("CityStateZip")).upper()))}

DEATH_DEED_RE = re.compile(r"Death|Personal Representative|Executor|Distribution|Life Estate", re.I)

# Additive score (prototype weights minus listing/DOM signals), with tax delinquency scaled by size.
# Tax tiers: delinquent amount (excl. penalty & interest) as % of assessed value. MT property tax runs very roughly 0.8-1.2% of
# market value per year, so >=1.5% is more than a full year unpaid, 0.5-1.5% is about one half-year installment or more,
# and <0.1% is a small fraction of one bill (often an escrow or penalty leftover).
TAX_TIERS = [  # (min pct, points, label, hard signal?)
    (1.5, 4, "severe (>=1.5% of assessed value)", True),
    (0.5, 3, "high (0.5-1.5% of assessed value)", True),
    (0.1, 2, "moderate (0.1-0.5% of assessed value)", True),
    (0.0, 1, "token (<0.1% of assessed value)", False),
]
TAX_UNKNOWN_VALUE = (2, "assessed value unknown", True)
MIN_DELINQUENT_USD = 250

def tax_tier(due, value):
    """-> (points, label, hard, pct) for a delinquent balance >= MIN_DELINQUENT_USD."""
    if not isinstance(value, (int, float)) or value <= 0:
        pts, lab, hard = TAX_UNKNOWN_VALUE; return pts, lab, hard, None
    pct = 100.0 * due / value
    for mn, pts, lab, hard in TAX_TIERS:
        if pct >= mn: return pts, lab, hard, round(pct, 3)
    return TAX_TIERS[-1][1], TAX_TIERS[-1][2], TAX_TIERS[-1][3], round(pct, 3)

WEIGHTS = {
    "tax_delinquent_multi_year": 1,             # only on hard (>=0.1%) delinquencies
    "foreclosure_sale": 4, "recorded_distress": 2,  # a live trustee / sheriff sale also qualifies a lead on its own (any minScore)
    "code_open": 2, "code_dangerous": 1,        # OPEN cases only = hard signal
    "closed_code_case_24mo": 0,                 # display only: closed cases are shown in code_cases but never score
    "owned_20y": 2, "owned_10y": 1,
    "out_of_state": 2, "nonlocal_instate": 1,
    "estate": 2, "trust": 1, "death_deed_5y": 1,
}
HARD_SIGNALS = ("tax_delinquent_hard", "foreclosure_sale", "recorded_distress", "code_open")

def score(sig):
    parts = {}
    if sig.get("tax_points"): parts["tax_delinquent"] = sig["tax_points"]
    if sig.get("tax_delinquent_hard") and sig.get("tax_delinquent_multi_year"): parts["tax_delinquent_multi_year"] = 1
    for k in ("foreclosure_sale", "code_open", "code_dangerous", "estate", "trust", "death_deed_5y"):
        if sig.get(k): parts[k] = WEIGHTS[k]
    if sig.get("recorded_distress") and not sig.get("foreclosure_sale"): parts["recorded_distress"] = WEIGHTS["recorded_distress"]
    y = sig.get("years_owned")
    if y is not None and y >= 20: parts["owned_20y"] = 2
    elif y is not None and y >= 10: parts["owned_10y"] = 1
    if sig.get("out_of_state"): parts["out_of_state"] = 2
    elif sig.get("nonlocal_instate"): parts["nonlocal_instate"] = 1
    return sum(parts.values()), parts

# ---------------------------------------------------------------- address cleanup
_DIRS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}
def _tc(tok):
    if tok in _DIRS or len(tok) == 1 or re.fullmatch(r"[A-Z]?\d+[A-Z]?", tok) and not re.fullmatch(r"\d+(ST|ND|RD|TH)", tok): return tok
    if re.fullmatch(r"\d+(ST|ND|RD|TH)", tok): return tok.lower()
    return tok[:1] + tok[1:].lower()

def clean_address(raw):
    """'1512,42,84 GOVERNORS BLVD' -> '1512, 1542 & 1584 Governors Blvd'; '310, 320 W MAIN ST' -> '310 & 320 W Main St'."""
    a = re.sub(r"\s+", " ", (raw or "").strip().upper())
    if not a: return None
    m = re.match(r"^(\d+[A-Z]?(?:\s*[,&]\s*\d+[A-Z]?)+)\s+(.*)$", a)
    if m:
        nums = [x.strip() for x in re.split(r"\s*[,&]\s*", m.group(1)) if x.strip()]
        first = re.match(r"\d+", nums[0]).group(0); out = [nums[0]]
        for n in nums[1:]:
            d = re.match(r"\d+", n).group(0)
            out.append((first[:len(first) - len(d)] + n) if len(d) < len(first) else n)   # abbreviated suffix: 42 -> 1542
        num = ", ".join(out[:-1]) + " & " + out[-1]; street = m.group(2)
    else:
        m = re.match(r"^(\d+[A-Z]?(?:-\d+[A-Z]?)?(?:\s+\d/\d)?)\s+(.*)$", a)
        num, street = (m.group(1), m.group(2)) if m else ("", a)
    street = " ".join(_tc(t) for t in street.split())
    return (num + " " + street).strip()

def street_key(addr):
    n = norm_addr(re.sub(r"^\d+[A-Z]?(?:\s*[,&-]\s*\d+[A-Z]?)*(?:\s+\d/\d)?\s+", "", (addr or "").strip().upper())).split()
    return " ".join(n) or None

def owner_key(name, mail1=""):
    """Internal-only identity for merging same-owner parcels. Never output."""
    n = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper()); n = re.sub(r"\s+", " ", n).strip()
    return (n + "|" + (addr_key(mail1) or norm_addr(mail1))) if n else None
