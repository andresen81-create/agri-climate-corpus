#!/usr/bin/env python3
"""
Corpus integrity validator for agri-climate-corpus.

Run from the repo root:   python3 validate.py
Exit code 0 = every check passed. 1 = at least one FAIL.

No dependencies. Reads only; never writes.

Each check names the document clause it enforces, so a failure points at the rule
rather than at this file.
"""

import json
import os
import re
import sys
from collections import Counter
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
fails = []
warns = []
notes = []


def load(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        return json.load(fh)


def fail(check, msg):
    fails.append((check, msg))


def warn(check, msg):
    warns.append((check, msg))


# ---------------------------------------------------------------- load
try:
    ALL = load("corpus/all.json")
    CI = load("index/claims_index.json")
    SI = load("index/series_index.json")
    SL = load("index/series_labels.json")
    SM = load("index/source_manifest.json")
    BM = load("index/badge_map.json")
    RS = load("index/run_state.json")
except Exception as exc:                                    # noqa: BLE001
    print(f"FATAL: could not load a required file: {exc}")
    sys.exit(1)

ROWS = {r["id"]: r for r in ALL}
notes.append(f"{len(ALL)} rows, {len(CI)} claim keys, {len(SI)} series")

# ---------------------------------------------------------------- 1. shards
# 01 §9: a shard must equal the all.json rows for its month, field for field.
by_month = {}
for r in ALL:
    by_month.setdefault(r.get("month"), []).append(r)

shard_files = sorted(
    f[:-5] for f in os.listdir(os.path.join(ROOT, "corpus"))
    if re.fullmatch(r"\d{4}-\d{2}\.json", f)
)
for m in sorted(set(list(by_month) + shard_files)):
    if m not in by_month:
        fail("1 shards", f"corpus/{m}.json exists but no row in all.json has month {m}")
        continue
    if m not in shard_files:
        fail("1 shards", f"{len(by_month[m])} rows have month {m} but corpus/{m}.json is missing")
        continue
    shard = load(f"corpus/{m}.json")
    a = {r["id"]: json.dumps(r, sort_keys=True) for r in by_month[m]}
    b = {r["id"]: json.dumps(r, sort_keys=True) for r in shard}
    if set(a) - set(b):
        fail("1 shards", f"{m}: in all.json but not the shard: {sorted(set(a) - set(b))}")
    if set(b) - set(a):
        fail("1 shards", f"{m}: in the shard but not all.json: {sorted(set(b) - set(a))}")
    diff = sorted(k for k in a if k in b and a[k] != b[k])
    if diff:
        fail("1 shards", f"{m}: rows differ between all.json and the shard: {diff}")

# ---------------------------------------------------------------- 2. claim keys
# 01 §7: merge on claim_key, never insert a second row on the same key.
# Documented exemptions to the one-row-per-claim_key rule. Each entry names the ids and the
# reason it was kept deliberately, decided by Andre, recorded in 05-decision-log.md.
# Add to this list only with a decision-log entry — an undocumented exemption is a silent defect.
DUPLICATE_EXEMPTIONS = {
    frozenset(("j022", "a004")):
        "Same figure (UK oilseed rape 3.9 t/ha, up) from two outlets against DIFFERENT baselines: "
        "j022 is +17% on the five-year average, a004 is against a ten-year average of 3.3. "
        "corroborations[] carries only {url, publisher, date}, so merging would discard the "
        "ten-year framing. Two baselines for one figure was judged more informative than one row. "
        "Decided 2026-08-09.",
}

keys = Counter(r["claim_key"] for r in ALL)
dupes = {k: [r["id"] for r in ALL if r["claim_key"] == k] for k, v in keys.items() if v > 1}
for k, ids in dupes.items():
    reason = DUPLICATE_EXEMPTIONS.get(frozenset(ids))
    if reason:
        warn("2 claim keys",
             f"{len(ids)} rows share claim_key {k!r}: {ids} — EXEMPT: {reason}")
    else:
        fail("2 claim keys", f"{len(ids)} rows share claim_key {k!r}: {ids} — should have merged")

bases = {k.split("#")[0] for k in CI}
for k in keys:
    if k not in CI and k not in bases:
        fail("2 claim keys", f"claim_key {k!r} is in the corpus but not in claims_index")
for k, v in CI.items():
    if v.get("id") not in ROWS:
        fail("2 claim keys", f"claims_index key {k!r} points at unknown row {v.get('id')!r}")
    if k.split("#")[0] not in keys:
        fail("2 claim keys", f"claims_index key {k!r} has no row in the corpus")

# 01 §7: unquantified rows use the degraded form, never the collapsed one.
for r in ALL:
    if not r.get("is_quantified") and "|na|None|" in r["claim_key"]:
        fail("2 claim keys", f"{r['id']}: unquantified row on a legacy collapsed key {r['claim_key']!r}")

# ---------------------------------------------------------------- 3. quotes
# 01 §3: verbatim_quote and event_date are mandatory where is_quantified.
for r in ALL:
    if r.get("is_quantified"):
        if not r.get("verbatim_quote"):
            fail("3 quotes", f"{r['id']}: is_quantified with no verbatim_quote")
        if not r.get("event_date"):
            fail("3 quotes", f"{r['id']}: is_quantified with no event_date")
        for f in ("metric_name", "metric_value", "metric_unit"):
            if r.get(f) in (None, ""):
                fail("3 quotes", f"{r['id']}: is_quantified with no {f}")
    else:
        for f in ("metric_name", "metric_value", "metric_unit"):
            if r.get(f) not in (None, ""):
                fail("3 quotes", f"{r['id']}: not quantified but {f} is set to {r.get(f)!r}")

# ---------------------------------------------------------------- 4. badges
# 01 §6 / 02 §3: the badge map is the only authority; drop hosts need origin_publisher.
flat = {}
for badge, entries in BM.items():
    if isinstance(entries, list):
        for d in entries:
            flat[d] = badge


def host(url):
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def lookup(url):
    h, path, best = host(url), urlparse(url).path.lstrip("/"), None
    for d, badge in flat.items():
        if "/" in d:
            dh, prefix = d.split("/", 1)
            ok = (h == dh or h.endswith("." + dh)) and path.startswith(prefix)
        else:
            ok = h == d or h.endswith("." + d)
        if ok and (best is None or len(d) > len(best[0])):
            best = (d, badge)
    return best


for r in ALL:
    m = lookup(r["url"])
    if m is None:
        fail("4 badges", f"{r['id']}: host {host(r['url'])!r} matches nothing in badge_map")
    elif m[1] == "drop":
        if not r.get("origin_publisher"):
            fail("4 badges",
                 f"{r['id']}: sourced from drop-listed {host(r['url'])!r} with no origin_publisher")
        if r.get("source_badge") != "Press":
            fail("4 badges", f"{r['id']}: kept from a drop host but badge is {r.get('source_badge')!r}, not Press")
    elif m[1] != r.get("source_badge"):
        fail("4 badges",
             f"{r['id']}: badge is {r.get('source_badge')!r} but badge_map says {m[1]!r} for {host(r['url'])!r}")

# ---------------------------------------------------------------- 5. vocabularies
# 01 §3: controlled values only. Free text fragments the series.
VOCAB = {
    "crop": {"wheat", "winter_wheat", "spring_wheat", "barley", "winter_barley", "spring_barley",
             "oats", "oilseed_rape", "maize", "soybean", "rice", "potato", "field_veg", "sugar",
             "coffee", "cocoa", "palm_oil", "cotton", "grapes_wine", "citrus", "orange", "mango",
             "livestock", "dairy", "sorghum", "mixed_grains"},
    "hazard_type": {"drought", "flood", "heat", "frost", "storm", "cyclone",
                    "pest_weather_driven", "favourable"},
    "impact_class": {"yield", "farmer_livelihood", "economy_macro", "company_corporate",
                     "price_market", "trade_policy", "food_security"},
    "attribution": {"enso_explicit", "weather_explicit", "implied", "unattributed"},
    "source_badge": {"Official", "Trade", "Press"},
    "direction": {"down", "up", None, ""},
    "metric_name": {"production", "yield", "planted_area", "harvested_area", "abandonment_rate",
                    "condition_good_excellent", "futures_price", "spot_price", "export_price",
                    "export_volume", "revenue", "cost", "rainfall_mm", "rainfall_pct_normal",
                    "forecast_rainfall_pct", "temperature_anomaly", "soil_moisture",
                    "reservoir_fill", "deliveries", "damaged_area", "crop_damage_pct",
                    "grain_moisture", "rainfall_in", None, ""},
}
for r in ALL:
    for field, allowed in VOCAB.items():
        if r.get(field) not in allowed:
            fail("5 vocabulary", f"{r['id']}: {field} = {r.get(field)!r} is not a controlled value")

# 01 §14: rainfall_mm must hold millimetres. An inch value under this metric is a wrong
# dimension, not a wording problem — any chart of the metric would be wrong by a factor of 25.4.
for r in ALL:
    if r.get("metric_name") == "rainfall_mm":
        unit = (r.get("metric_unit") or "").strip().lower()
        if "inch" in unit or unit in ('"', "in"):
            fail("5 vocabulary",
                 f"{r['id']}: metric_name rainfall_mm but metric_unit is {r.get('metric_unit')!r} "
                 f"— an inch value in a millimetre metric")
        elif unit not in ("mm", "millimetres", "millimeters"):
            warn("5 vocabulary",
                 f"{r['id']}: rainfall_mm with a non-canonical unit string {r.get('metric_unit')!r}; "
                 f"the value looks like mm but the string will not group")

for r in ALL:
    if r.get("metric_name") == "rainfall_in":
        unit = (r.get("metric_unit") or "").strip().lower()
        if "mm" in unit or "millimet" in unit:
            fail("5 vocabulary",
                 f"{r['id']}: metric_name rainfall_in but metric_unit is {r.get('metric_unit')!r}")
        elif unit not in ("inches", "inch", "in"):
            warn("5 vocabulary",
                 f"{r['id']}: rainfall_in with a non-canonical unit string {r.get('metric_unit')!r}")

# ---------------------------------------------------------------- 6. run_state
# 01 §9: latest_summary is a BARE filename and must name a file that exists.
ls = RS.get("latest_summary")
if not ls:
    fail("6 run_state", "run_state.latest_summary is missing or empty")
else:
    if "/" in ls:
        fail("6 run_state", f"latest_summary {ls!r} is a path; it must be a bare filename")
    if not os.path.exists(os.path.join(ROOT, "summaries", os.path.basename(ls))):
        fail("6 run_state", f"latest_summary names summaries/{ls}, which does not exist")
for f in ("window_start", "window_end"):
    if not RS.get(f):
        fail("6 run_state", f"run_state.{f} is missing")
if RS.get("window_start") and RS.get("window_end") and RS["window_start"] > RS["window_end"]:
    fail("6 run_state", f"window_start {RS['window_start']} is after window_end {RS['window_end']}")

# ---------------------------------------------------------------- 7. series index
# 01 §9 / 03 STEP 8B: chartable requires >1 reading and a human label.
for k, e in SI.items():
    readings = e.get("readings") or []
    if not readings:
        fail("7 series", f"series {k!r} has no readings")
    for rd in readings:
        if rd.get("claim_id") not in ROWS:
            fail("7 series", f"series {k!r} reading points at unknown row {rd.get('claim_id')!r}")
    dates = [rd.get("event_date") or "" for rd in readings]
    if dates != sorted(dates):
        fail("7 series", f"series {k!r} readings are not sorted by event_date")
    if e.get("chartable"):
        if len(readings) < 2:
            fail("7 series", f"series {k!r} is chartable with only {len(readings)} reading(s)")
        if not e.get("label"):
            fail("7 series", f"series {k!r} is chartable with no label")
    if e.get("label") and SL.get(k) != e.get("label"):
        fail("7 series", f"series {k!r} label does not match series_labels.json")

indexed = set(SI)
for r in ALL:
    if r.get("is_quantified") and r.get("event_date") and r.get("series_key") not in indexed:
        fail("7 series", f"{r['id']}: quantified with an event_date but its series_key is not indexed")

# ---------------------------------------------------------------- 8. summaries
# 03 STEP 8: the dashboard binds to this shape. Every key present, ids resolvable.
SUMMARY_KEYS = {"date", "row_count", "headline", "key_points", "notable_numbers",
                "what_changed", "conflicts"}
sdir = os.path.join(ROOT, "summaries")
for name in sorted(os.listdir(sdir)) if os.path.isdir(sdir) else []:
    if not name.endswith(".json"):
        continue
    s = load(f"summaries/{name}")
    missing = SUMMARY_KEYS - set(s)
    extra = set(s) - SUMMARY_KEYS
    if missing:
        fail("8 summaries", f"{name}: missing keys {sorted(missing)}")
    if extra:
        fail("8 summaries", f"{name}: unexpected keys {sorted(extra)}")
    for c in s.get("conflicts") or []:
        for i in c.get("ids") or []:
            if i not in ROWS:
                fail("8 summaries", f"{name}: conflict references unknown row {i!r}")
    for n in s.get("notable_numbers") or []:
        for i in n.get("ids") or []:
            if i not in ROWS:
                fail("8 summaries", f"{name}: notable_numbers references unknown row {i!r}")
    no_ids = [n.get("label") for n in (s.get("notable_numbers") or []) if "ids" not in n]
    if no_ids:
        warn("8 summaries",
             f"{name}: {len(no_ids)} of {len(s.get('notable_numbers') or [])} notable_numbers "
             f"entries have no ids — expected on summaries written before the field existed, "
             f"a defect on any written after")

# ---------------------------------------------------------------- 9. manifest
# 01 §8: the silence check needs last_publication to compare against.
for src, v in SM.items():
    if v.get("last_publication") in (None, ""):
        warn("9 manifest", f"{src}: last_publication is null, so the silence check cannot fire")

# ---------------------------------------------------------------- report
print()
for n in notes:
    print(f"  {n}")
print()
if warns:
    print(f"WARN  {len(warns)}")
    for c, m in warns:
        print(f"  [{c}] {m}")
    print()
if fails:
    print(f"FAIL  {len(fails)}")
    for c, m in fails:
        print(f"  [{c}] {m}")
    print()
    print(f"FAILED — {len(fails)} problem(s), {len(warns)} warning(s)")
    sys.exit(1)
print(f"PASSED — 0 problems, {len(warns)} warning(s)")
sys.exit(0)
