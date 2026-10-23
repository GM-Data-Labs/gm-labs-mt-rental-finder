"""Run the Actor's scoring code for all three counties, no result cap. Writes data-pipeline/out/leads.json.
Exits non-zero (and writes nothing) on any county failure."""
import asyncio, importlib, json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "actor", "src")]
import apify  # local shim
COUNTIES = ["Yellowstone", "Gallatin", "Park"]
out, stats = [], {}
for c in COUNTIES:
    t = time.time()
    apify.Actor.__init__(); apify.Actor.input = {"county": c, "maxResults": 0}
    import common, main; importlib.reload(common); importlib.reload(main)   # fresh module state per county
    asyncio.run(main.main())
    if apify.Actor.failed: sys.exit(f"{c}: {apify.Actor.failed}")
    items = apify.Actor.items
    st = apify.Actor.kv.get("STATS", {})
    stats[c] = {"rows": len(items), "secs": round(time.time() - t, 1),
                "sources_failed": [k for k, v in (st.get("sources") or {}).items() if not v.get("ok")]}
    print(f"{c}: {len(items)} leads in {stats[c]['secs']}s; failed sources: {stats[c]['sources_failed'] or 'none'}", flush=True)
    out += items
os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
json.dump({"leads": out, "stats": stats}, open(os.path.join(HERE, "out", "leads.json"), "w"))
