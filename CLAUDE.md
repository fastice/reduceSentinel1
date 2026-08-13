# CLAUDE.md — asfSearchAndDownload

Search the ASF DAAC for NISAR / Sentinel-1 products, dedupe against a local archive, export footprints to a GeoPackage for QGIS, then bulk-download with `aria2c`. Import name is `asfsearchdownload`. See the [packages CLAUDE.md](../CLAUDE.md) for pipeline context (NISAR HDF5 inputs feed `nisargrimpworkflow`).

## Modules / CLI entry points

| Script | Module | Role |
|---|---|---|
| `searchASF` | `searchASF.py:main()` | Search ASF DAAC / CMR; write download-URL lists + optional GeoPackage |
| `ariaDownload` | `ariaDownload.py:main()` | Download a URL list with `aria2c`, time-of-day throttled |
| `reduces1` | `reduceSentinel1.py:main()` | Strip files (e.g. cross-pol) from Sentinel-1 ZIP archives |
| `writeSearchGpkg` | `writeSearchGpkg.py` | Library only (no CLI) — used by `searchASF --gpkg` |

## Workflow

```
searchASF <firstDate> <lastDate> <output> [--sensor NISAR|SENTINEL1] ...
    → <output>                  URLs to download (new granules)
    → <output>.exists           URLs already in --archiveDir (skipped)
    → <output>.updated          URLs for newer versions of archived granules
    → <output>.<PRODUCT>         per-product-type URL lists (e.g. .RUNW, .ROFF, .SLC)
    → --gpkg search.gpkg         footprints, one OGR layer per product type (QGIS)

ariaDownload <output> [--xferDir DIR] [--overWrite] [--noRename]
    → downloads each URL with aria2c, checking xferDir for an existing
      partial (.zip.1) or full (.zip) copy first

reduces1 <file.zip> --pattern hv   (or --directory DIR --suffix zip)
    → strips matching files (e.g. HV/VH cross-pol) from S1 ZIPs in place
```

## searchASF

```
searchASF firstDate lastDate output
          [--sensor {NISAR,SENTINEL1}]            # default NISAR
          [--products PRODUCT [PRODUCT ...]]       # NISAR default: RUNW ROFF RSLC
                                                    # S1 default: SLC
          [--beamMode MODE ...]                    # S1 only, default IW
          [--bandwidth BW ...]                     # NISAR only, default 40 40+5 77
          [--minVersion N] [--specificVersion N ...]   # NISAR CRID filters
          [--startTrack N] [--endTrack N]
          [--startFrame N] [--endFrame N]
          [--searchArea FILE | --greenland | --antarctica]
          [--archiveDir GLOB] [--gpkg FILE] [--s3]
```

- **Dates**: `firstDate`/`lastDate` are `YYYY-MM-DD`; converted to `T00:00:00Z`/`T23:59:59Z` for the CMR temporal filter.
- **Products**: NISAR choices `L0B RSLC RIFG RUNW ROFF GSLC GCOV GUNW GOFF SME2`; Sentinel-1 choices `SLC GRD_HD GRD_MS GRD_HS GRD_FD GRD_MD OCN RAW BURST`.
- **Bandwidth** (`--bandwidth`, NISAR only): `5 5+5 20 20+5 40 40+5 77` MHz; only applied as a hard filter for pair products (`RIFG RUNW ROFF GUNW GOFF`, see `_PAIR_PRODUCTS`) — single-acquisition products (RSLC, GSLC, GCOV, L0B, SME2) are searched without it.
- **Search area** (`--searchArea`): accepts `.geojson`/`.json` (FeatureCollection/Feature/Geometry), `.shp` (reprojected to WGS84 via OGR), or a flat GrIMP `.lonlat` file (`lon,lat` pairs, order auto-detected). `--greenland` uses the bundled `asfsearchdownload/searchRegions/Greenland.lonlat` (also the default). `--antarctica` uses a hardcoded circumpolar WKT and is mutually exclusive with `--greenland`.
- **Track/frame filters**: `--startTrack/--endTrack/--startFrame/--endFrame` filter on `pathNumber`/`frameNumber` from the search result (or parsed from the NISAR_EA granule name).
- **Archive dedup** (`--archiveDir GLOB`): globs existing files (extensions `.h5`/`.zip`/`.zip.1` stripped for comparison). Sentinel-1 dedupes by filename stem; NISAR dedupes by `scene_key` (granule identity minus CRID/version) and tracks the highest CRID version seen — older-or-equal granules go to `<output>.exists`, newer versions go to `<output>.updated`.
- **`--gpkg FILE`**: writes one OGR layer per NISAR product type, with a `status` field (`found`/`exists`/`updated`) and metadata parsed by `parse_nisar_meta()` (track, frame, cycle, direction, polarization, bandwidth_mhz, dates, crid, version). EPSG chosen automatically from mean footprint latitude: `3031` (Antarctic, lat < -60), `3413` (Arctic, lat > 60), else `4326`.
- **`--s3`**: emit `s3://` URIs instead of HTTPS; granules with no S3 link are silently skipped.

### Authentication (Earthdata)

`searchASF` builds an `asf.ASFSession()` and tries, in order:
1. `earthaccess.login(strategy='netrc')` — full EDL OAuth2, returns a JWT. Required for the restricted **NISAR_EA** collections (`C4052500045-ASF`, `C4052499921-ASF` — science-team beta data), queried separately via authenticated CMR (`_search_nisar_ea`). Install with `pip install earthaccess`.
2. Fallback: `~/.netrc` entry for `urs.earthdata.nasa.gov` via `asf_search`'s `auth_with_creds()` — works for public collections but may not satisfy ACLs on restricted ones.
3. If neither is configured, searches unauthenticated (public data only).

NISAR_EA results are deduplicated against public-collection results by filename stem (the same granule can appear in both with different URLs).

### Granule-name parsing (`_nisar_parse` / `parse_nisar_meta`)

NISAR granule stems are split on `_`. Two layouts:
- **Pair products** (20 fields: RIFG/RUNW/ROFF/GUNW/GOFF) — bandwidth at field 9, polarization at 10, ref/sec dates at 11/13, CRID at 15, version at 19.
- **Single-acquisition** (18 or 19 fields: RSLC/GSLC/GCOV/L0B) — bandwidth at field 8 or 9 (`_nisar_bw_field` picks whichever is a 4-digit numeric token), CRID/version near the end.

CRID values look like `X05010` → parsed as integer `5010` for `--minVersion`/`--specificVersion` comparisons.

## ariaDownload

```
ariaDownload downloadLinks [--xferDir DIR] [--overWrite] [--noRename]
```

- `downloadLinks` — a file of URLs, one per line (typically `searchASF`'s `<output>`).
- For each URL, checks `xferDirs` for `<file>.zip.1` (moves/renames to `<file>` unless `--noRename`) or `<file>.zip` (copies unless `--noRename`) before downloading — avoids re-downloading partial transfers.
- `--xferDir *` (default) scans `/Volumes/insar{1,3,6,7,8,9,10,11}/ian/xfer` for existing copies.
- Throttling via `getX()`: weekday 07:00–18:00 → `-x 1`; weekend 07:00–18:00 → `-x 4`; all other times → `-x 10` (aria2c `-x` = max connections per server).
- Calls `aria2c -x <N> <url>` via `subprocess.call(..., shell=True, executable='/bin/csh')`.
- Existing files are skipped unless `--overWrite`.

## reduces1 (reduceSentinel1)

```
reduces1 [zipfile] [--pattern hv] [--directory DIR] [--suffix zip]
```

- Removes archive members whose name contains `--pattern` (default `hv`, i.e. cross-pol HV/VH) from a Sentinel-1 ZIP.
- Prefers the system `zip -d` CLI (in-place deletion); falls back to a slower stream-copy-to-new-ZIP if `zip` is not on PATH.
- `--directory DIR` processes all `*.{suffix}` files in a directory; otherwise operates on the single positional `zipfile`.
- Tested on both SLC and L0B-style products; roughly halves archive size for single-pol-only users.

## Notes

- `writeSearchGpkg.py` requires `osgeo.ogr`/`osgeo.osr` (GDAL); `searchASF` imports it lazily only when `--gpkg` is given, and `_read_polygon_shapefile` also needs GDAL for `.shp` search areas.
- `MAX_RESULTS = 10000` in `searchASF`; a warning is printed if a search hits this cap (results may be incomplete — narrow the date range or area).
- Per-product URL files (`<output>.<PRODUCT>`) and the `volume_by_product` summary (printed in GB) are always generated for "found" (new) granules, regardless of `--gpkg`.
