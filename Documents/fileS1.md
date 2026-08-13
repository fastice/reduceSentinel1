# fileS1

Unpack **Sentinel-1 SAFE zips** into the per-track/per-orbit tree that downstream
GrIMP/ISCE processing expects. Part of the `asfSearchAndDownload` package; the
filing step of [`autoupdateS1`](autoupdateS1.md) calls this in-process, and it is
also runnable standalone as `fileS1`.

## What it does

For each `*.zip` found in `zipDir`, it parses the S1 filename to get the sensor,
absolute orbit and dates, computes the **relative track** (`orbit % 175 −
satConst[sat]`), and unzips the SAFE into
`assemblyDir/track-<n>/<orbit>/` — **excluding** the cross-pol (`*-slc-hv*`,
`*-slc-vh*`) SLC measurement bands to save space. On success the source `.zip`
is renamed `.zip.1` (the `.1` marks an already-processed pass). Unzips run four
at a time.

A pass already unpacked under `assemblyDir/track-<n>/<orbit>[_1]/…SAFE` is
skipped unless `--overwrite`.

## zipDir layout: flat vs month subdirs

- Default: globs a **flat** `zipDir/*.zip`.
- `--monthSubdirs`: globs `zipDir/<YYYY-MM>/*.zip` across **all** month subdirs —
  the layout the S1 archive (`archiveDir/<YYYY>-<MM>/`) uses. `autoupdateS1`
  always passes this so it files the whole archive.

## Filed record (`--filed`)

`--filed <file.yaml>` writes a record of what was filed **this run** (an output,
not an input — `fileS1` never reads it):

```yaml
tracks:   [16, 25, 90]                       # all tracks touched this run
granules: [/archive/2026-07/S1A_..._.zip, …] # source zip paths filed this run
```

`autoupdateS1` uses this to decide which tracks/files later processing steps work
on. (Format may evolve.)

## CLI

```
fileS1 [options]

  --zipDir DIR       Directory with zip files [/Volumes/insar10/ian/xfer]
  --assemblyDir DIR  Root under which track-<n>/<orbit>/ trees are built [.]
  --monthSubdirs     Glob zipDir/<YYYY-MM>/*.zip (all month subdirs) instead of
                     a flat zipDir/*.zip
  --filed FILE       Write a YAML record (tracks:/granules:) of what was filed
  --createTrackDir   Create track-<n> under assemblyDir if it does not exist
  --overwrite        Re-unpack passes already present
  --check            Dry run: report what would be filed (into which track/
                     orbit) without unpacking, renaming, or writing anything
```

Standalone example (flat dir, into the current directory):

```
fileS1 --zipDir /Volumes/insar10/ian/xfer --createTrackDir
```

As driven by `autoupdateS1` (equivalent call):

```
fileS1 --zipDir <archiveDir> --assemblyDir <assemblyDir> --monthSubdirs \
       --createTrackDir --filed <logDir>/filedS1.<MM-DD-YYYY>.yaml
```
