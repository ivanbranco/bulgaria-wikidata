# Bulgaria OpenStreetMap–Wikidata Overview

Map showing, for each Bulgarian province (oblast), which Wikidata items with coordinates are already linked to an OpenStreetMap object (`wikidata=*` tag) and which are still missing.

**Live map:** https://ivanbranco.github.io/bulgaria-wikidata/

Adapted from [italia-wikidata](https://github.com/ilselvaggio/italia-wikidata).

## How it works

- `update.py` downloads, for each province, the OSM objects with a `wikidata` tag (Overpass API) and the Wikidata items located in it (`P131*`, with coordinates). It writes `data_overpass/data_<province>.geojson`, `metadata.json` and `history.json`.
- `.github/workflows/weekly_update.yml` runs the script every Monday (00:00 UTC) and commits the results. It can also be started manually from the **Actions** tab, for one province (`plovdiv`), several (`plovdiv,varna`) or `all`.
- `index.html` is the map (Leaflet), published with GitHub Pages.

## Configuration files

| File | Content |
|---|---|
| `regions.json` | Provinces: Wikidata QID and OSM relation id (+3600000000) |
| `regions_boundaries.geojson` | Simplified province boundaries |
| `category_config.json` | Groups, subgroups, types (Wikidata `P31` values) and colours |
| `blacklist.json` | Wikidata QIDs to ignore |

Optional GitHub secret `OVERPASS_URL`: full address of a private Overpass server, tried before the public ones.

After changing `regions.json`, `regions_boundaries.geojson` or `category_config.json`, update `STATIC_VERSION` in `index.html` so browsers load the new version.

Data © OpenStreetMap contributors (ODbL) and Wikidata (CC0).
