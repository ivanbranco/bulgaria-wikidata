"""Confronta gli elementi Wikidata di ogni oblast bulgara con gli oggetti OSM che hanno il tag wikidata.

Uso:
    python -u update.py                       # tutte le regioni
    python -u update.py --region plovdiv      # una regione
    python -u update.py --region plovdiv,varna --max-minutes 300
"""
import argparse
import csv
import datetime
import json
import os
import sys
import time

import requests

REGIONS_FILE = "regions.json"
WIKIDATA_URL = "https://query.wikidata.org/sparql"
OSM_DIR = "osm"
DATA_DIR = "data_overpass"
METADATA_FILE = "metadata.json"
BOUNDARIES_FILE = "regions_boundaries.geojson"
BLACKLIST_FILE = "blacklist.json"
HISTORY_FILE = "history.json"
CATEGORY_CONFIG_FILE = "category_config.json"

USER_AGENT = "BulgariaWikidataBot/1.3 (https://github.com/ivanbranco/bulgaria-wikidata)"

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
# Server privato opzionale (GitHub Secret OVERPASS_URL, indirizzo completo): viene provato per primo
if os.environ.get("OVERPASS_URL"):
    OVERPASS_ENDPOINTS.insert(0, os.environ["OVERPASS_URL"].strip())

OVERPASS_HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
OVERPASS_QUERY_TIMEOUT = 300      # secondi, lato server
OVERPASS_MAX_AGE_DAYS = 3         # dati OSM più vecchi di così = server in ritardo, provo il successivo
SHRINK_RATIO = 0.8                # se Wikidata restituisce < 80% degli item precedenti, la regione viene saltata

OSM_FAILURES = []


def log(msg):
    print(msg, flush=True)


def warn(msg):
    # Compare come annotazione nel riepilogo del run di GitHub Actions
    log(msg)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning::{msg}", flush=True)


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        warn(f"Impossibile leggere {path}: {e}")
        return default


def save_json(path, data):
    """Scrittura atomica: un'interruzione a metà non lascia file corrotti."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def build_qid_lookup(category_config):
    lookup = {}
    for group_name, group_data in category_config.items():
        for subgroup_name, types_dict in group_data["subgroups"].items():
            for type_name, qids in types_dict.items():
                for qid in qids:
                    lookup.setdefault(qid, {"group": group_name, "subgroup": subgroup_name, "type": type_name})
    return lookup


def get_bbox_from_feature(feature):
    lats, lons = [], []

    def extract(coords):
        for item in coords:
            if isinstance(item, list) and len(item) >= 2 and isinstance(item[0], (int, float)):
                lons.append(item[0])
                lats.append(item[1])
            elif isinstance(item, list):
                extract(item)

    extract(feature["geometry"]["coordinates"])
    if not lats:
        return None
    pad = 0.02
    return (min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad)


# ---------------------------------------------------------------- Overpass

def osm_data_age_days(data):
    ts = (data.get("osm3s") or {}).get("timestamp_osm_base")
    if not ts:
        return None
    try:
        d = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        return (utcnow() - d).total_seconds() / 86400
    except ValueError:
        return None


def run_overpass(query, label):
    """Prova i server in ordine. Restituisce la prima risposta completa e aggiornata.
    Se tutti i server rispondono con dati vecchi, restituisce la più recente tra queste."""
    best_stale, best_age = None, None
    for url in OVERPASS_ENDPOINTS:
        host = url.split("/")[2]
        for attempt in range(2):
            try:
                r = requests.post(url, data={"data": query}, headers=OVERPASS_HEADERS,
                                  timeout=OVERPASS_QUERY_TIMEOUT + 30)
                if r.status_code in (429, 504):
                    log(f"      [!] {host}: HTTP {r.status_code} ({label}), attendo 60s")
                    time.sleep(60)
                    continue
                r.raise_for_status()
                data = r.json()
                remark = data.get("remark", "")
                if "elements" not in data or "error" in remark.lower() or "timed out" in remark.lower():
                    log(f"      [!] {host}: risposta incompleta ({label}): {remark[:150]}")
                    break  # lo stesso server risponderebbe uguale -> server successivo
                age = osm_data_age_days(data)
                if age is not None and age > OVERPASS_MAX_AGE_DAYS:
                    log(f"      [!] {host}: dati OSM vecchi di {age:.0f} giorni ({label}), provo il server successivo")
                    if best_age is None or age < best_age:
                        best_stale, best_age = data, age
                    break
                log(f"      -> OK da {host} ({len(data['elements'])} oggetti)")
                return data
            except Exception as e:
                log(f"      [!] {host}: errore ({label}, tentativo {attempt + 1}/2): {str(e)[:200]}")
                time.sleep(15)
    if best_stale is not None:
        warn(f"Nessun server Overpass aggiornato ({label}): uso dati vecchi di {best_age:.0f} giorni")
    return best_stale


def osm_query(area_filter):
    return f"""
    [out:json][timeout:{OVERPASS_QUERY_TIMEOUT}];
    (
      node["wikidata"]{area_filter};
      way["wikidata"]{area_filter};
      relation["wikidata"]{area_filter};
    );
    out tags qt;
    """


def fetch_osm_bbox(bbox, depth=0):
    """Scarica una bbox; se fallisce la divide in 4 riquadri (max 2 livelli = 16 riquadri)."""
    s, w, n, e = bbox
    data = run_overpass(osm_query(f"({s},{w},{n},{e})"), f"bbox livello {depth}")
    if data or depth >= 2:
        return data
    log(f"      -> Divido la bbox in 4 riquadri (livello {depth + 1})")
    mlat, mlon = (s + n) / 2, (w + e) / 2
    merged, seen = None, set()
    for tile in [(s, w, mlat, mlon), (s, mlon, mlat, e), (mlat, w, n, mlon), (mlat, mlon, n, e)]:
        part = fetch_osm_bbox(tile, depth + 1)
        if not part:
            return None  # meglio la cache che dati incompleti
        if merged is None:
            merged = {k: v for k, v in part.items() if k != "elements"}
            merged["elements"] = []
        for el in part["elements"]:
            k = (el["type"], el["id"])
            if k not in seen:
                seen.add(k)
                merged["elements"].append(el)
        time.sleep(5)
    return merged


def fetch_osm_area_fallback(area_id):
    q = osm_query("(area.searchArea)").replace("(\n", f"area({area_id})->.searchArea;\n    (\n", 1)
    return run_overpass(q, "area")


def slim_osm(data):
    """Per la cache servono solo tipo, id e tag wikidata: il file diventa molto più piccolo."""
    return {
        "osm3s": data.get("osm3s", {}),
        "elements": [
            {"type": el["type"], "id": el["id"], "tags": {"wikidata": el["tags"]["wikidata"]}}
            for el in data.get("elements", []) if "wikidata" in el.get("tags", {})
        ],
    }


# ---------------------------------------------------------------- Wikidata

def get_wikidata_csv(qid, retries=3):
    query = f"""SELECT DISTINCT ?qid ?lat ?lon ?itemLabel ?type WHERE {{
       ?item wdt:P131* wd:{qid}; wdt:P625 ?loc .

       FILTER NOT EXISTS {{ ?item wdt:P582 ?end. FILTER(?end < NOW()) }}
       FILTER NOT EXISTS {{ ?item wdt:P576 ?dissolved. FILTER(?dissolved < NOW()) }}
       FILTER NOT EXISTS {{ ?item wdt:P5817 wd:Q56556915 }}
       FILTER NOT EXISTS {{ ?item wdt:P5816 wd:Q56556915 }}
       FILTER NOT EXISTS {{ ?item wdt:P5817 wd:Q11639308 }}

       MINUS {{ ?item p:P131 ?stmt . ?stmt pq:P582 ?linkEnd . FILTER(?linkEnd < NOW()) }}

       OPTIONAL {{ ?item wdt:P31 ?type. }}

       BIND(STRAFTER(STR(?item), '/entity/') as ?qid)
       BIND(geof:latitude(?loc) as ?lat)
       BIND(geof:longitude(?loc) as ?lon)

       SERVICE wikibase:label {{ bd:serviceParam wikibase:language "bg,en". }}
    }}"""
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip", "Accept": "text/csv"}
    for attempt in range(retries):
        try:
            r = requests.get(WIKIDATA_URL, params={"query": query}, headers=headers, timeout=90)
            if r.status_code in (429, 503):
                retry_after = r.headers.get("Retry-After", "")
                wait = int(retry_after) if retry_after.isdigit() else 30
                log(f"      [!] Wikidata HTTP {r.status_code}, attendo {wait}s (tentativo {attempt + 1}/{retries})")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.text
        except Exception as e:
            log(f"      [!] Wikidata errore (tentativo {attempt + 1}/{retries}): {str(e)[:200]}")
            time.sleep(15)
    return None


def parse_wikidata(csv_text, qid_lookup, blacklist):
    """Una riga per ogni combinazione item/P31: raccoglie tutti i tipi di un item
    e sceglie il primo riconosciuto in category_config.json."""
    items = {}
    for row in csv.DictReader(csv_text.splitlines()):
        qid = (row.get("qid") or "").split("/")[-1].upper()
        if not qid or qid in blacklist:
            continue
        item = items.get(qid)
        if item is None:
            try:
                lat, lon = float(row["lat"]), float(row["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            item = items[qid] = {"name": row.get("itemLabel") or qid, "lat": lat, "lon": lon, "cat": None}
        if item["cat"] is None:
            type_qid = (row.get("type") or "").split("/")[-1].upper()
            item["cat"] = qid_lookup.get(type_qid)
    return items


# ---------------------------------------------------------------- Main

def oldest(dates):
    dates = [d for d in dates if d and d[0].isdigit()]
    return min(dates) if dates else None


def save_state(new_meta, now_str):
    save_json(METADATA_FILE, {
        "global_osm_date": oldest(m.get("osm") for m in new_meta.values()) or now_str,
        "global_wiki_date": oldest(m.get("wiki") for m in new_meta.values()) or now_str,
        "last_run": now_str,
        "regions": new_meta,
    })
    history = load_json(HISTORY_FILE, [])
    today = now_str[:10]
    history = [h for h in history if h.get("date") != today]
    history.append({"date": today, "data": {k: {"done": v["done"], "total": v["total"]} for k, v in new_meta.items()}})
    history.sort(key=lambda h: h["date"])
    save_json(HISTORY_FILE, history)


def process_region(key, config, ctx):
    log(f"\n=== {config['name']} ===")
    old = ctx["old_meta"].get(key, {})
    file_osm = os.path.join(OSM_DIR, f"osm_{key}.json")
    osm_date = ctx["now_str"]

    rel_id = str(int(config["osm"]) - 3600000000)
    feature = ctx["boundaries"].get(rel_id)
    bbox = get_bbox_from_feature(feature) if feature else None

    if bbox:
        log("   -> Scarico OSM (bbox)")
        new_data = fetch_osm_bbox(bbox)
    else:
        log("   -> Scarico OSM (area)")
        new_data = fetch_osm_area_fallback(config["osm"])

    if new_data:
        osm_data = slim_osm(new_data)
        save_json(file_osm, osm_data)
        ts = (osm_data.get("osm3s") or {}).get("timestamp_osm_base")
        if ts:
            osm_date = ts.replace("Z", "+00:00")
    else:
        warn(f"Download OSM fallito per {config['name']}: uso la cache del {old.get('osm', '?')[:10]}")
        OSM_FAILURES.append(key)
        osm_date = old.get("osm", "Unknown")
        osm_data = load_json(file_osm, None)
        if not osm_data:
            log("   -> Nessun dato OSM disponibile, salto la regione")
            return False

    osm_ids = {}
    for el in osm_data.get("elements", []):
        for raw in el.get("tags", {}).get("wikidata", "").replace(",", ";").split(";"):
            raw = raw.strip().upper()
            if raw.startswith("Q"):
                osm_ids.setdefault(raw, f"{el['type']}/{el['id']}")

    log("   -> Scarico Wikidata")
    csv_text = get_wikidata_csv(config["qid"])
    if not csv_text:
        warn(f"Wikidata non disponibile per {config['name']}: regione saltata, restano i dati precedenti")
        return False

    items = parse_wikidata(csv_text, ctx["qid_lookup"], ctx["blacklist"])
    total = len(items)
    prev_total = old.get("total", 0)
    if prev_total and total < prev_total * SHRINK_RATIO:
        warn(f"Wikidata per {config['name']} ha restituito {total} item invece di ~{prev_total} (risposta troncata?): regione saltata")
        return False

    other = {"group": "Other", "subgroup": "Other", "type": "Other"}
    features = []
    for qid, it in items.items():
        cat = it["cat"] or other
        features.append({
            "type": "Feature",
            "properties": {
                "wikidata": qid,
                "name": it["name"],
                "status": "done" if qid in osm_ids else "missing",
                "osm_id": osm_ids.get(qid),
                "group": cat["group"],
                "subgroup": cat["subgroup"],
                "subcategory": cat["type"],
            },
            "geometry": {"type": "Point", "coordinates": [round(it["lon"], 6), round(it["lat"], 6)]},
        })
    done = sum(1 for f in features if f["properties"]["status"] == "done")

    save_json(os.path.join(DATA_DIR, f"data_{key}.geojson"), {"type": "FeatureCollection", "features": features})
    ctx["new_meta"][key] = {"osm": osm_date, "wiki": ctx["now_str"], "done": done, "total": total}
    save_state(ctx["new_meta"], ctx["now_str"])  # salvo subito: un run interrotto non perde le regioni già fatte
    log(f"   -> Salvato: {total} item, {done} collegati a OSM ({round(100 * done / total) if total else 0}%)")
    return True


def main():
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="all", help="'all' oppure una o più chiavi separate da virgola")
    parser.add_argument("--max-minutes", type=float, default=0,
                        help="dopo questo tempo non inizia nuove regioni (0 = nessun limite)")
    args = parser.parse_args()
    start = time.time()

    regions = load_json(REGIONS_FILE, None)
    if not regions:
        log(f"Errore: {REGIONS_FILE} non trovato.")
        sys.exit(1)
    os.makedirs(OSM_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    boundaries = {}
    for feat in load_json(BOUNDARIES_FILE, {}).get("features", []):
        osm_id = feat.get("id") or feat.get("properties", {}).get("@id") or feat.get("properties", {}).get("id")
        if osm_id and feat.get("geometry", {}).get("type") in ("Polygon", "MultiPolygon"):
            boundaries[str(osm_id).replace("relation/", "")] = feat

    old_meta = load_json(METADATA_FILE, {}).get("regions", {})
    ctx = {
        "now_str": utcnow().isoformat(timespec="seconds"),
        "old_meta": old_meta,
        "new_meta": dict(old_meta),
        "boundaries": boundaries,
        "blacklist": set(load_json(BLACKLIST_FILE, [])),
        "qid_lookup": build_qid_lookup(load_json(CATEGORY_CONFIG_FILE, {})),
    }

    if args.region == "all":
        keys = list(regions.keys())
    else:
        keys = [k.strip() for k in args.region.split(",") if k.strip()]
        for k in keys:
            if k not in regions:
                warn(f"Regione sconosciuta: {k}")
        keys = [k for k in keys if k in regions]
    # prima le regioni aggiornate meno di recente: se il tempo finisce, al run successivo tocca alle altre
    keys.sort(key=lambda k: old_meta.get(k, {}).get("wiki", ""))

    ok, skipped_time = 0, []
    for key in keys:
        if args.max_minutes and (time.time() - start) / 60 > args.max_minutes:
            skipped_time.append(key)
            continue
        try:
            if process_region(key, regions[key], ctx):
                ok += 1
            time.sleep(5)  # rispetto per i server
        except Exception as e:
            warn(f"Errore imprevisto nella regione {key}: {e}")

    log(f"\nRegioni aggiornate: {ok}/{len(keys)} in {(time.time() - start) / 60:.0f} minuti")
    if skipped_time:
        warn(f"Tempo esaurito, rimandate al prossimo run: {', '.join(skipped_time)}")
    if OSM_FAILURES:
        warn(f"OSM fallito per {len(OSM_FAILURES)} regione/i (usata la cache): {', '.join(OSM_FAILURES)}")


if __name__ == "__main__":
    main()
