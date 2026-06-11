# Martin County, TX Well Data Pipeline

A one-command pipeline that downloads Martin County, TX surface well data, converts it to GeoParquet, and lands it ready for analysis in DuckDB, GeoPandas, or QGIS.

## What it does

`pipeline.sh` pulls the raw well data file from the public repository, processes the schema, and converts it to a single GeoParquet file at `data/processed/martin_county_wells.parquet`.

Total runtime: about 90 seconds on a typical home internet connection.

## The data

- **Source:** Railroad Commission of Texas (RRC) County Well Layer Records
- **License:** Public domain (State data)
- **What's in it:** All recorded oil and gas well data for Martin County (FIPS 317), including well location (API), status, depth, and operator information.
- **Data Link:** https://mft.rrc.texas.gov/link/d551fb20-442e-4b67-84fa-ac3f23ecabb4#

## How to run it

Requires GDAL (for `ogr2ogr`) and a custom python script containing playwright to grab the Texas RRC from the website itself.

```bash
git clone [https://github.com/](https://github.com/)Jsmcmeans/martin_wells.git
cd martin_wells
chmod +x pipeline.sh
./pipeline.sh