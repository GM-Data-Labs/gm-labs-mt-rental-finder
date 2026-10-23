"""Convert out/leads.json (Actor scoring output) to ../assets/s7.dat, the page's compact format.
Only whitelisted, owner-free fields are written: no owner names or mailing addresses ever reach the site."""
import json, os, re, sys, requests

FREE_CAP = 0          # free-tier cap on the number of leads shown on the site. 0 = off (show every lead)

HERE = os.path.dirname(os.path.abspath(__file__)); OUT = os.path.join(HERE, "..", "assets", "s7.dat")
MT_CAD = "https://services.arcgis.com/qnjIrwR8z5Izc0ij/arcgis/rest/services/Montana_Cadastral_Framework/FeatureServer/1/query"
CRITICAL = {"mt_cadastral", "treasurer_delinquent"}   # if either failed, the scores are incomplete: don't publish

def yrs(ys):
    """[2023, 2024, 2025] -> '2023–25'; [2022, 2024] -> '2022, 2024'"""
    ys, out, i = sorted(set(ys)), [], 0
    while i < len(ys):
        j = i
        while j + 1 < len(ys) and ys[j + 1] == ys[j] + 1: j += 1
        out.append(str(ys[i]) if i == j else f"{ys[i]}–{str(ys[j])[2:]}"); i = j + 1
    return ", ".join(out)

def md(iso): y, m, d = iso.split("-"); return f"{int(m)}/{int(d)}"
def mdy(iso): y, m, d = iso.split("-"); return f"{int(m)}/{int(d)}/{y}"

def tax_signal(L):
    raw = next((s for s in L["signals"] if s.startswith("TAX DELINQUENT")), "")
    years, amt = L["tax_delinquent_years"], L["tax_delinquent_amount"]
    m = re.search(r"county list dated ([\d/]+)", raw)
    listed = m.group(1).lstrip("0").replace("/0", "/") if m else (L.get("data_as_of") or {}).get("treasurer_list", "")
    ok = {int(y): d for y, d in re.findall(r"(\d{4}) confirmed unpaid on the county tax page (\d{4}-\d\d-\d\d)", raw)}
    ok.update({int(y): d for y, d in re.findall(r"(\d{4}) bill on county page is [^;]*?nothing paid, checked (\d{4}-\d\d-\d\d)", raw)})
    paid = {int(y): d for y, d in re.findall(r"(\d{4}) shows as paid on the county tax page[^;]*?checked (\d{4}-\d\d-\d\d)", raw)}
    asof = [y for y in years if y not in ok]
    if not ok and not paid: parts = [f"{yrs(years)}; county list {listed}"]
    else:
        parts = []
        if ok: parts.append(f"{yrs(ok)} confirmed unpaid {md(max(ok.values()))}")
        if asof: parts.append(f"{yrs(asof)} as of county list {listed}")
        if paid: parts.append(f"{yrs(paid)} paid per county page {md(max(paid.values()))}")
    out = [f"Back taxes ${amt:,.0f} (excl. penalty & interest; {'; '.join(parts)})"]
    m = re.search(r"tax-lien assignment \(lien sold to a third party; point-in-time snapshot of (\d{4}-\d\d-\d\d)\)", raw) or \
        re.search(r"under a tax-lien assignment \(point-in-time snapshot of (\d{4}-\d\d-\d\d)\)", raw)
    if m: out.append(f"Tax lien sold to a third party (assignment, snapshot {mdy(m.group(1))})")
    return out

def signals(L):
    S = []
    fs = L.get("foreclosure_sale") or "none"
    if fs != "none":
        m = re.search(r"(\d\d/\d\d/\d{4})", fs); d = f" {m.group(1)}" if m else ""
        S.append(("Sheriff sale scheduled" if "sheriff" in fs.lower() else "Trustee (foreclosure) sale scheduled") + d)
    if L.get("tax_delinquent") == "yes" and L.get("tax_delinquent_amount"): S += tax_signal(L)
    for r in L.get("recorded_distress_docs") or []:
        S.append(f"Recorded {r.get('type', 'notice').lower()} {r.get('recorded', '')}".strip())
    for c in (L.get("code_cases") or []):
        if c.get("status") == "open":
            S.append(f"Open city code case, cited {c.get('date_cited')}" + (" (dangerous building)" if c.get("dangerous") else "")); break
    yo = L.get("years_owned")
    if isinstance(yo, (int, float)) and yo >= 10:
        if L.get("years_owned_basis") == "since last recorded deed" and L.get("last_deed_date"):
            S.append(f"Same owner {yo:g} years (since {L['last_deed_date'][:4]})")
        else: S.append(f"Same owner at least {yo:g} years")
    if L.get("out_of_state_owner") == "yes": S.append("Owner's mailing address is out of state")
    elif L.get("absentee_owner") == "yes": S.append("Owner's mailing address is elsewhere")
    if L.get("owner_type") == "trust_estate": S.append("Owned by a trust or estate")
    if (L.get("parcel_count") or 1) > 1: S.append(f"{L['parcel_count']} side-by-side parcels, same owner (one lead)")
    return S

def centroids(pids):
    got = {}
    for i in range(0, len(pids), 100):
        r = requests.post(MT_CAD, data=dict(where=f"PropertyID IN ({','.join(map(str, pids[i:i+100]))})", outFields="PropertyID",
                                            returnGeometry="false", returnCentroid="true", outSR=4326, f="json"), timeout=60).json()
        for f in r.get("features", []):
            c = f.get("centroid") or {}
            if "x" in c: got[f["attributes"]["PropertyID"]] = (round(c["y"], 4), round(c["x"], 4))
    return got

def main():
    src = json.load(open(os.path.join(HERE, "out", "leads.json")))
    for c, st in src["stats"].items():
        bad = CRITICAL & set(st.get("sources_failed") or [])
        if bad: sys.exit(f"{c}: critical source(s) failed ({', '.join(sorted(bad))}); not publishing")
    leads = sorted(src["leads"], key=lambda L: (-L["score"], -(L["units"] or 0), L["parcel_id"] or ""))
    if not leads: sys.exit("0 leads; not publishing")
    if FREE_CAP: leads = leads[:FREE_CAP]
    old = {}
    if os.path.exists(OUT):
        for x in json.load(open(OUT))["r"]:
            for g in x.get("geocode") or []: old[g] = x
    need = [L["property_id"] for L in leads if L.get("property_id") and not any(old.get(g, {}).get("lat") for g in L["merged_parcel_ids"])]
    cen = centroids(need) if need else {}
    r = []
    for L in leads:
        prev = next((old[g] for g in L["merged_parcel_ids"] if g in old), {})
        num = lambda v: v if isinstance(v, (int, float)) else "unknown"
        x = {"county": L["county"], "address": L["address"], "city": L["city"] or "unknown", "units": L["units"],
             "year_built": num(L["year_built"]), "value": num(L["assessed_value"]), "score": L["score"],
             "tax_owed": float(L.get("tax_delinquent_amount") or 0), "sale": (L.get("foreclosure_sale") or "none") != "none",
             "tax": L.get("tax_delinquent") == "yes", "code_open": L.get("code_case") == "open",
             "absentee": L.get("absentee_owner") == "yes", "out_of_state": L.get("out_of_state_owner") == "yes",
             "long_owned": isinstance(L.get("years_owned"), (int, float)) and L["years_owned"] >= 10,
             "years_owned": num(L.get("years_owned")), "signals": signals(L), "geocode": L["merged_parcel_ids"],
             "tax_id": L["tax_id"], "links": L.get("source_links") or []}
        ll = (prev["lat"], prev["lon"]) if prev.get("lat") else cen.get(L.get("property_id"))
        if ll: x["lat"], x["lon"] = ll
        if prev.get("img") and os.path.exists(os.path.join(HERE, "..", prev["img"])): x["img"] = prev["img"]
        r.append(x)
    new = json.dumps({"r": r}, ensure_ascii=False, separators=(",", ":"))
    open(OUT, "w").write(new)
    print(f"s7.dat: {len(r)} leads ({sum('img' in x for x in r)} with aerial, {sum('lat' in x for x in r)} with lat/lon)")

if __name__ == "__main__": main()
