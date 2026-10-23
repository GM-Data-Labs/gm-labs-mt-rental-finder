"""Run the Actor's scoring code per county, no result cap. Writes data-pipeline/out/leads.json.
Each county is isolated: a county that errors is recorded as failed (convert.py then keeps that county's previous data)
and the others still update. Exits non-zero (writing nothing) only if every county failed.
Usage: python3 run.py [County ...]   (default: all)"""
import asyncio, importlib, json, os, sys, time, traceback
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "actor", "src")]
import apify  # local shim
# Missoula is built in but on HOLD (0 leads on 10/23: no public delinquent list or code cases); run it by name to test.
COUNTIES = sys.argv[1:] or ["Yellowstone", "Gallatin", "Park", "Natrona"]
out, stats = [], {}
for c in COUNTIES:
    t = time.time()
    try:
        apify.Actor.__init__(); apify.Actor.input = {"county": c, "maxResults": 0}
        if c == "Natrona":   # Wyoming: own sources (actor/src/natrona.py), same scoring and lead rule
            import natrona; importlib.reload(natrona)
            items, srcs = natrona.run(); st = {"sources": srcs}
        else:
            import common, main; importlib.reload(common); importlib.reload(main)   # fresh module state per county
            asyncio.run(main.main())
            if apify.Actor.failed: raise RuntimeError(str(apify.Actor.failed))
            items = apify.Actor.items
            st = apify.Actor.kv.get("STATS", {})
        stats[c] = {"rows": len(items), "secs": round(time.time() - t, 1),
                    "sources_failed": [k for k, v in (st.get("sources") or {}).items() if not v.get("ok")]}
        print(f"{c}: {len(items)} leads in {stats[c]['secs']}s; failed sources: {stats[c]['sources_failed'] or 'none'}", flush=True)
        out += items
    except Exception as e:
        traceback.print_exc()
        stats[c] = {"rows": 0, "secs": round(time.time() - t, 1), "error": str(e)[:300]}
        print(f"{c}: FAILED after {stats[c]['secs']}s ({stats[c]['error']}); its previous data will be kept", flush=True)
if all("error" in s for s in stats.values()): sys.exit("every county failed; nothing written")
os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
json.dump({"leads": out, "stats": stats}, open(os.path.join(HERE, "out", "leads.json"), "w"))
