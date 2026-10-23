"""Natrona County, WY (Casper): the same lead rule as the Montana counties, from Wyoming public sources.
- parcels/units: owner-free snapshot data/natrona.json.gz (Natrona Regional GIS CAMA layer + county assessor occupancy/units)
- back taxes: county treasurer's public tax page, one polite check per multifamily account (no bulk list is reachable:
  the published delinquent-list PDF sits behind a bot wall)
- sales: Halliday, Watkins & Mann's public Wyoming sale list
- code cases: none (no public City of Casper case data). Recorder: none (iDoc needs an account).
Treasurer/assessor pages show the owner's name, so they are read for tax status only and never linked."""
import collections, datetime as dt, gzip, html as htmlmod, json, os, re, time
import requests
from apify import Actor
from common import HARD_SIGNALS, MIN_DELINQUENT_USD, addr_key, clean_address, is_unit_address, score, strip_trailing_unit_no, tax_tier

HERE = os.path.dirname(os.path.abspath(__file__)); DATA = os.path.join(os.path.dirname(HERE), "data")
TREAS = "https://treasurersearch.natronacounty-wy.gov/"
HWM_WY = "https://www.halliday-watkins.com/live/WY/bids.htm"
UA = {"User-Agent": "Mozilla/5.0 (GM Labs research; low-rate)"}
GAP, BUDGET = 0.8, 1800          # seconds between treasurer requests; hard 30-minute budget for the whole pass
DEAD = re.compile(r"cancel|postpon|continued|vacated|withdrawn|completed|\bsold\b|on hold|stayed|bankrupt|\bBK\b", re.I)

def _text(h):
    h = re.sub(r"<script.*?</script>|<style.*?</style>|<input[^>]*>", "", h, flags=re.S | re.I)
    return re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", h)))

def treasurer(acct, S):
    t = _text(S.get(TREAS, params={"AcctNo": acct}, timeout=(10, 40)).text)
    m = re.search(r"Payment History Year Amount Status (.*?)(?: \d{4} Tax Payment Information| Pay Tax Bill|$)", t)
    if not m: raise ValueError("no payment history on page")
    hist = [(int(y), float(a.replace(",", "")), s.strip()) for y, a, s in re.findall(r"(\d{4}) \$([\d,.]+) ([A-Za-z ]+?)(?= \d{4} \$| \d{4} Tax|$)", m.group(1))]
    st = re.search(r"Account Status: (.*?) Bill #", t)
    return hist, (st.group(1).strip() if st else "")

def sales():
    h = requests.get(HWM_WY, headers=UA, timeout=60).text; out = {}; skipped = 0
    today = dt.date.today()
    for row in re.findall(r"<tr>(.*?)</tr>", h, re.S | re.I):
        c = [htmlmod.unescape(re.sub(r"<[^>]+>", " ", x)).strip() for x in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]
        if len(c) >= 5 and c[1].lower() == "natrona":
            m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", c[4]); d = dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None
            if d is None or d < today or DEAD.search(" ".join(c[7:])): skipped += 1; continue
            street = c[3].split(",")[0]
            if not is_unit_address(street) and addr_key(street): out[addr_key(street)] = f"trustee/foreclosure sale {d:%m/%d/%Y} (Halliday, Watkins & Mann file {c[0]})"
    return out, skipped

def run(min_score=4):
    snap = json.load(gzip.open(os.path.join(DATA, "natrona.json.gz"), "rt")); P = snap["parcels"]
    Actor.log.info(f"Natrona: {len(P)} multifamily accounts in snapshot (built {snap['meta']['built']})")
    sources = {}
    try:
        sale, skipped = sales(); sources["foreclosure_firm_list"] = {"ok": True, "rows": len(sale), "skipped_past_or_cancelled": skipped}
    except Exception as e:
        sale = {}; sources["foreclosure_firm_list"] = {"ok": False, "error": str(e)[:200]}
    # The county site answers in ~4-5 s, so the 30-minute budget covers ~340 of ~470 accounts. Check first the accounts that can
    # reach the lead bar with a moderate tax bill (ownership points), then the rest in an order that rotates every week.
    pri = [p for p in P if p.get("out_of_state") or p.get("nonlocal_instate") or p.get("estate") or p.get("trust") or p.get("owned10")]
    rest = [p for p in P if p not in pri]; r = dt.date.today().isocalendar()[1] * 97 % max(len(rest), 1)
    order = pri + rest[r:] + rest[:r]
    S = requests.Session(); S.headers.update(UA); tax, stat, errors, t0 = {}, collections.Counter(), 0, time.time()
    for i, p in enumerate(order):
        if time.time() - t0 > BUDGET: break
        try:
            hist, st = treasurer(p["acct"], S); stat[st.split(",")[0] or "?"] += 1
            cur = max([h[0] for h in hist] or [dt.date.today().year])
            unpaid = {y: a for y, a, s in hist if y < cur and not s.lower().startswith("paid")}
            cert = "certificate of purchase" in st.lower()
            if cert and not unpaid:   # taxes were sold at the county tax sale: the buyer paid the bill, the owner must redeem it
                prev = [(y, a) for y, a, s in hist if y < cur]
                if prev: y, a = max(prev); unpaid = {y: a}
            if unpaid and sum(unpaid.values()) >= MIN_DELINQUENT_USD: tax[p["acct"]] = {"by_year": unpaid, "cert": cert, "status": st}
        except Exception as e:
            errors += 1; Actor.log.warning(f"Natrona treasurer check failed for one account: {type(e).__name__}")
        if errors > 25 and errors > 0.2 * (i + 1): raise RuntimeError(f"Natrona treasurer: {errors} failed checks of {i + 1}")
        time.sleep(GAP)
    n = sum(stat.values())
    if n == 0: raise RuntimeError("Natrona treasurer: no account could be checked")
    asof = dt.date.today().isoformat()
    sources["treasurer_delinquent"] = {"ok": True, "rows": len(tax), "checked": n, "of": len(P), "errors": errors, "statuses": dict(stat), "as_of": asof}
    Actor.log.info(f"Natrona treasurer: checked {n}/{len(P)} accounts in {time.time() - t0:.0f}s, statuses {dict(stat)}, delinquent {len(tax)}, errors {errors}")
    leads = []
    for p in P:
        sig = {}; T = tax.get(p["acct"]); due = round(sum(T["by_year"].values()), 2) if T else 0
        if T:
            pts, lab, hard, pct = tax_tier(due, p.get("value"))
            sig.update(tax_points=pts, tax_delinquent_hard=hard, tax_delinquent_multi_year=len(T["by_year"]) > 1)
        raw = strip_trailing_unit_no(p.get("addr_raw") or p.get("addr") or ""); k = addr_key(raw)
        if k and k in sale: sig["foreclosure_sale"] = True
        yo = 10.0 if p.get("owned10") else 5.0 if p.get("owned5") else None
        if yo: sig["years_owned"] = yo
        for f in ("out_of_state", "nonlocal_instate", "estate", "trust"): sig[f] = bool(p.get(f)) and not (f == "nonlocal_instate" and p.get("out_of_state"))
        sc, parts = score(sig)
        hard_any = any(sig.get(x) for x in HARD_SIGNALS)
        if not (sig.get("foreclosure_sale") or (sc >= min_score and hard_any)): continue
        S_ = []
        if T:
            ys = sorted(T["by_year"]); S_.append(f"TAX DELINQUENT {', '.join(map(str, ys))} ${due:,.0f} (excl. interest; county tax page {asof}; status: {T['status']})")
        if sig.get("foreclosure_sale"): S_.append(f"FORECLOSURE {sale[k]}")
        leads.append({"parcel_id": p.get("geo"), "property_id": None, "tax_id": p["acct"], "county": "Natrona", "state": "WY",
            "address": clean_address(raw) or p.get("addr"), "address_raw": p.get("addr_raw"), "city": p.get("city"), "units": p["units"],
            "size_class": "5+" if p["units"] >= 5 else "2-4", "year_built": p.get("yb") or "unknown", "assessed_value": p.get("value") or "unknown",
            "property_type": p.get("occ"), "owner_type": "trust_estate" if (p.get("estate") or p.get("trust")) else "other",
            "absentee_owner": "yes" if (p.get("out_of_state") or p.get("nonlocal_instate")) else "no", "out_of_state_owner": "yes" if p.get("out_of_state") else "no",
            "years_owned": yo if yo else "unknown",
            "years_owned_basis": "same owner name on the 2016 statewide parcel layer (lower bound; WY has no public sale dates)" if yo == 10.0 else
                                 "same owner name on the 2021 statewide parcel layer (lower bound)" if yo else "unknown",
            "tax_delinquent": "yes" if T else "no", "tax_delinquent_years": sorted(T["by_year"]) if T else [], "tax_delinquent_amount": due,
            "tax_certificate_issued": bool(T and T["cert"]), "code_case": "none", "code_cases": [],
            "foreclosure_sale": sale[k] if sig.get("foreclosure_sale") else "none", "recorded_distress_docs": [],
            "score": sc, "score_breakdown": parts, "signals": S_, "source_links": [],
            "verification_notes": [f"county tax record: look up account {p['acct']} on the Natrona County Treasurer's tax search; not linked because that page shows the owner's name and mailing address"],
            "merged_parcel_ids": [p.get("geo") or p["acct"]], "parcel_count": 1, "lat": p.get("lat"), "lon": p.get("lon"),
            "data_as_of": {"snapshot": snap["meta"]["built"], "treasurer_pages": asof, "run_date": asof}})
    Actor.log.info(f"Natrona: {len(leads)} scored rental leads (score >= {min_score} with a hard signal, or a live sale listing)")
    return leads, sources
