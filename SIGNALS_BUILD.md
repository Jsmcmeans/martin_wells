# Martin Activity — Signals & Tiles Build

This document covers the **new** scripts added in Pass 1 of the Operator Activity Map build:

```
deriveSignals.py     # Computes derived intelligence layers
deriveSignals.sh     # Orchestration wrapper
buildTiles.sh        # GeoParquet → PMTiles
```

It does **not** replace `README.md` — the existing pipeline scripts (`drillingPermits.sh`, `pipeline.sh`, etc.) still work the same way.

---

## 1. Install dependencies (one time)

### System tools

PMTiles generation requires `tippecanoe` ≥ 2.0 (which supports direct `.pmtiles` output and removes the old `mbtiles → pmtiles` two-step):

```bash
brew install tippecanoe
```

Verify:
```bash
tippecanoe --version    # should be 2.x or higher
```

`ogr2ogr` (from GDAL) is already a project dep from the existing pipeline.

### Python packages

Reinstall from the updated `requirements.txt`:

```bash
pip install -r requirements.txt
```

The new addition is `h3>=4.0` (used by the hexgrid layer). The script enforces v4 at startup — v3 has a different API and will not work.

---

## 2. Prerequisites

Before running `deriveSignals.sh`, the following must exist in `data/processed/`:

| File | Required? | Source script |
|---|---|---|
| `drillingPermits.parquet` | **Required** | `drillingPermits.sh` (or `backfillPermits.sh` for history) |
| `drillingPermitsPending.parquet` | Optional | `drillingPermitsPending.sh` |
| `well317s.parquet` | Optional | `pipeline.sh` |
| `martinPipelines.parquet` | Optional | `pipelineData.sh` (enter `Martin` at the prompt) |
| `martinProduction_leases.parquet` | Optional | `productionData.sh` (enter `Martin` at the prompt) |
| `martinProduction_wells.parquet` | Optional | `productionData.sh` (same) |

If an optional file is missing, the script warns and produces the corresponding signal columns as null / 0 rather than failing. For a real build, all should be present.

For tile generation (`buildTiles.sh`), the survey hierarchy layers and parcels are also pulled in:

| File | Source script |
|---|---|
| `martinSurvey_lyr_block.parquet` | `martinSurvey.sh` |
| `martinSurvey_lyr_section.parquet` | `martinSurvey.sh` |
| `martinParcels.parquet` | `parcelTransform.sh` |

---

## 3. Run the build

### Step 1 — Derive signals

```bash
./deriveSignals.sh
```

Produces in `data/processed/`:
- `martinSignals_permits.parquet` — every Martin permit (accumulated + pending) with `signal_class`, `pipeline_dist_mi`, `pipeline_proximity_class`, `wells_within_1mi`
- `martinSignals_wells.parquet` — Martin wells from `well317s` joined to per-lease production trend (`prod_trend_class`, `prod_trend_pct`)
- `martinSignals_hexgrid.parquet` — H3 res-7 hexes with permit counts by signal class, dominant operator, etc.
- `martinSignals_operators.parquet` — non-spatial per-operator rollup with `new_entrant` flag

And in `data/web/`:
- `operators.json` — frontend filter-dropdown source
- `martin_meta.json` — pipeline metadata (generated_at, bbox, layer counts, signal distribution)

### Step 2 — Build tiles

```bash
./buildTiles.sh
```

Produces in `data/tiles/`:
- `permits_signals.pmtiles`
- `wells_signals.pmtiles`
- `pipelines.pmtiles`
- `hexgrid.pmtiles`
- `survey_block.pmtiles`
- `survey_section.pmtiles`
- `parcels.pmtiles`

Total Martin County footprint should land well under 30 MB.

### Step 3 — Stage to the Next.js site

Copy outputs into your site repo (done manually for now; can be automated in `buildMartinActivity.sh` later):

```bash
cp data/tiles/*.pmtiles  /path/to/maplify.dev/public/tiles/
cp data/web/*.json       /path/to/maplify.dev/public/data/
```

---

## 4. What each derived column means

### On `martinSignals_permits.parquet`

| Column | Type | Meaning |
|---|---|---|
| `signal_class` | string | `pending_approval` / `approved_unspud` / `recently_drilled` / `drilled_no_completion` / `historical` |
| `is_pending` | bool | True if the row came from the pending file (vs. accumulated) |
| `pipeline_dist_mi` | float | Miles to nearest active pipeline. Computed in UTM 14N. |
| `pipeline_proximity_class` | string | `near` (<0.5 mi) / `medium` (0.5–2 mi) / `far` (>2 mi) |
| `wells_within_1mi` | int | Count of `well317s` wells within a 1-mile buffer. High = infill drilling. |

### On `martinSignals_wells.parquet`

| Column | Type | Meaning |
|---|---|---|
| `prod_trend_class` | string | `growing` (>+20%) / `flat` / `declining` (<−20%) / `new` (no prior data) / `inactive` / `unknown` |
| `prod_trend_pct` | float | Signed fraction: (recent 12 mo BOE − prior 12 mo BOE) / prior 12 mo BOE |

BOE conversion uses the standard 6 Mcf gas = 1 barrel oil equivalent. The 12-month windows are anchored at the most recent month present in the production data — not wall-clock — so results are reproducible across reruns.

### On `martinSignals_hexgrid.parquet`

H3 resolution 7 (~5 km² per hex). Per-hex columns: `permit_count`, `operator_count` (distinct), `dominant_operator`, plus one `count_<signal_class>` column per class.

### On `martinSignals_operators.parquet`

Per-operator rollup. The `new_entrant` flag is true when an operator has permits in the last 90 days but **no** activity in the prior 12 months — the "fresh capital walking into Martin" signal.

---

## 5. Verifying outputs

Quick sanity check with DuckDB:

```bash
duckdb -c "
  INSTALL spatial; LOAD spatial;
  SELECT
    signal_class,
    COUNT(*) AS n,
    AVG(pipeline_dist_mi) AS avg_dist_mi,
    AVG(wells_within_1mi) AS avg_infill_density
  FROM read_parquet('data/processed/martinSignals_permits.parquet')
  GROUP BY signal_class
  ORDER BY n DESC;
"
```

Or in QGIS: load the four `martinSignals_*` parquets and the hexgrid should color cleanly by `permit_count` with a graduated symbology.

PMTiles can be previewed without a website using the Protomaps web viewer:

```bash
open https://protomaps.com/pmtiles
# Then drag the .pmtiles file onto the page
```

---

## 6. Configuration knobs

Edit constants at the top of `deriveSignals.py`:

| Constant | Default | Effect |
|---|---|---|
| `RECENT_DAYS` | 90 | Window for `recently_drilled` and `recent_count` |
| `NEW_ENTRANT_LOOKBACK_DAYS` | 455 | Operator must be silent this many days before recent activity to count as a new entrant |
| `H3_RESOLUTION` | 7 | Hex size. Res 7 ≈ 5 km²; res 8 ≈ 0.7 km². |
| `NEAR_WELL_RADIUS_MI` | 1.0 | Infill density radius |
| `TREND_GROW_THRESHOLD` | 0.20 | +20% threshold for `growing` |
| `TREND_DECLINE_THRESHOLD` | −0.20 | −20% threshold for `declining` |

Per-layer PMTiles zoom + tippecanoe flags are at the bottom of `buildTiles.sh` — adjust if a layer is too sparse at low zooms or too dense at high zooms.

---

## 7. Troubleshooting

**"h3 v4+ required"**
You have h3 v3 installed. `pip install --upgrade 'h3>=4.0'`. The v3 API uses `geo_to_h3` instead of `latlng_to_cell` — they're incompatible.

**"no API column found on wells layer — trend skipped"**
The `well317s.parquet` shapefile uses a column name not in the script's candidate list. Run `python -c "import geopandas as gpd; print(gpd.read_parquet('data/processed/well317s.parquet').columns.tolist())"` and add the actual column name to `_find_api_column()` in `deriveSignals.py`.

**`tippecanoe: not found`**
`brew install tippecanoe`. If you must use the older Mapbox tippecanoe (v1.x), it can't write `.pmtiles` directly — you'd need to build mbtiles first then convert with `pmtiles convert`. Upgrade is easier.

**A layer comes out empty in QGIS but the parquet has rows**
Likely a CRS mismatch. All outputs are EPSG:4326. If QGIS shows them at (0,0), the geometry column may have been written without CRS metadata — check with `gdf.crs` in Python and re-save with `crs="EPSG:4326"` if needed.

**Tiles too large**
- Reduce `--maximum-zoom` per layer in `buildTiles.sh`
- Add `-S 10` (simplification) to tippecanoe args
- Use `-y col1 -y col2 …` to keep only specific attributes (drops the rest)

---

## 8. What comes next (Pass 2)

After verifying `data/tiles/*.pmtiles` looks good locally, Pass 2 builds the Next.js frontend:

- `app/geospatial/martin-activity/page.tsx` (route)
- `components/geospatial/martin/MartinActivityMap.tsx` (MapLibre instance)
- `components/geospatial/martin/` (sidebar, legend, filters, popup)
- `lib/martinLayers.ts` (source/layer specs)
- `lib/martinPmtiles.ts` (protocol registration)
- One new card entry in `lib/geospatialData.ts` with `liveUrl` pointing at the new route
