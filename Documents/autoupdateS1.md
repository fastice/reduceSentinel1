# autoupdateS1

Automated **Sentinel-1 IW SLC** archive-update driver, configured by an
`autoupdate.yaml` that normally sits in the project directory. Intended to run
nightly from cron, but also runnable ad hoc from the CLI for other regions and
time periods.

It is the S1 analogue of `autoupdateNISAR` (nisargrimpworkflow), scoped for now
to **keeping the archive current** — later processing stages will be layered on
and `autoupdate.yaml` will grow to carry their config.

## What each run does

1. **Refresh orbits (state vectors).** `refreshOrbits.updateStateVectors` scrapes
   the ASF precise-orbit index (`https://s1qc.asf.alaska.edu/aux_poeorb/`) and
   downloads any `.EOF` files newer than the newest local one, per selected
   sensor, into `orbitDir`. Authenticates via `~/.netrc`. Skip with `--noOrbits`.
2. **Search.** `searchASF --sensor SENTINEL1 --products SLC --beamMode IW`,
   deduped against the existing archive via `--archiveDir <archiveDir>/*/*`
   (which strips `.zip`/`.zip.1` before comparing). URL list and coverage
   GeoPackage are written under `archiveDir/searchResults/`.
3. **Download + reduce.** Passes are downloaded **one at a time** via
   `ariaDownload` (which adjusts aria2c bandwidth by time of day). Each pass is
   filed under `archiveDir/<YYYY>-<MM>/`, where the month comes from the first
   date token in the granule name. After a download completes it is **verified**
   (no leftover `.aria2` control file and `zipfile.is_zipfile` passes); on
   failure it is re-downloaded up to `maxAttempts`. On success the unneeded
   cross-pol is stripped (`reduceSentinel1`) in a **background thread** while the
   next download starts.

A pass already present anywhere under `archiveDir/<YYYY>-<MM>/` as `.zip` or
`.zip.1` (the `.1` marks an already-processed pass) is not re-downloaded.

4. **File.** If `assemblyDir` is configured, `fileS1` unpacks the archive zips
   (`archiveDir/<YYYY-MM>/*.zip`, across all month subdirs) into the per-track/
   per-orbit tree `assemblyDir/track-<n>/<orbit>/`, excluding the cross-pol SLC
   measurement bands, then renames each source `.zip` → `.zip.1`. It writes a
   YAML record of what it filed to `logs/filedS1.<MM-DD-YYYY>.yaml` (`tracks:`
   list of all tracks touched, `granules:` list of zip paths filed this run) for
   downstream steps to consume. Run this step **in isolation** (skipping orbits +
   search/download) with `--fileData`.
5. **Frame check.** If `assemblyDir` is configured, `checkFramesS1` vets each
   filed datatake (burst-frame coverage, gaps, over-length, out-of-range),
   restructures it into clean processing units (`<orbit>_4` split head, `<orbit>_1+`
   far-side gap segments, out-of-range → `tmp/`), and routes each unit into the
   cumulative `toProcess` / `pendingProcessing` / `problem` queues under `queueDir`
   (default `assemblyDir`). Each queued record carries the orbit, date, and in-range
   start/end/total frames. A datatake whose precise (EOF) orbit isn't published yet
   goes to `pendingProcessing`; at the start of each run those are re-checked and
   promoted to `toProcess` once the orbit arrives. Run in isolation with
   `--checkFrames`. See [checkFramesS1](checkFramesS1.md).

## Sensors

Which satellites are handled (both the SLC download filter and the orbit
refresh) comes from the `satellites` config key (default all of
S1A/S1B/S1C/S1D). The CLI flags `--S1A --S1B --S1C --S1D` override the config for
a one-off run.

## Date range

`firstDate` defaults to **today − 6 months**, `lastDate` to **today**. Override
per run with `--firstDate`/`--lastDate` (YYYY-MM-DD), or set `firstDate`/
`lastDate` in the yaml. Because the search is deduped against the archive, a wide
window is safe and simply advances as the archive fills.

## CLI

```
autoupdateS1 [config] [options]

  config            Path to autoupdate.yaml (default: ./autoupdate.yaml)
  --maxDownloads N  Soft cap on downloads per run (0 = no limit); overrides the
                    config maxDownloads key [default 300]. Soft: once N is
                    reached, the remaining frames of the pass in progress (same
                    orbit + datatake) are finished before stopping, so a pass is
                    never left half-downloaded (e.g. hitting 300 mid-pass with 5
                    frames left stops at 305).
  --firstDate D     Search start YYYY-MM-DD (overrides config; default today-6mo)
  --lastDate  D     Search end   YYYY-MM-DD (overrides config; default today)
  --region  R       Predefined region: greenland | antarctica (overrides config)
  --searchArea F    GeoJSON/.shp/lon,lat polygon (overrides config and --region)
  --S1A --S1B --S1C --S1D   Restrict to specific sensor(s) (default: all)
  --noOrbits        Skip the orbit (state-vector) refresh
  --noDownload      Skip search+download (only refresh orbits)
  --fileData        Run only the filing step: unpack archive zips into the
                    assemblyDir track tree (skips orbits + search/download)
  --checkFrames     Run only the frame-check step: vet filed datatakes and
                    queue them (skips orbits + search/download + filing)
  --check           Dry run across every stage: report what would be
                    downloaded, filed, or written without modifying anything on
                    disk (search results go to a scratch temp dir; the archive
                    is only read for dedup). Composes with the other flags.
```

Companion CLI `refreshS1Orbits [--orbitDir DIR] [--S1A ...]` refreshes just the
orbit archive.

## autoupdate.yaml keys

```yaml
archiveDir: /Volumes/insar4/ian/Data/S1-Greenland          # required
assemblyDir: /Volumes/insar1/ian/S1-Greenland/data          # file stage target tree
orbitDir:   /Volumes/insar9/ian/Data/SentinelGreenland/OPOD # EOF archive
# queueDir:  /Volumes/insar1/ian/S1-Greenland/data          # frame-check queues [default: assemblyDir]
region:     Greenland      # or: Antarctica  (or use searchArea instead)
satellites: S1A S1B S1C S1D  # which sensors [all four]
productType: SLC           # searchASF --products [SLC]
beamMode:   IW             # searchASF --beamMode [IW]
direction:  both           # both | ascending | descending [both]
# searchArea: /path/to/aoi.geojson   # alternative to region
# firstDate: 2026-01-01    # optional; default today - 6 months
# lastDate:  2026-08-01    # optional; default today
# maxDownloads: 300        # soft cap per run (0 = no limit); finishes the pass [300]
# reducePattern: hv        # cross-pol substring to strip (Greenland HH+HV) [hv]
# maxAttempts: 3           # download retries per pass [3]
```

Only `archiveDir` is strictly required; a spatial constraint (`region` or
`searchArea`) is required unless supplied on the CLI. `direction` ascending/
descending is passed through to `searchASF --flightDirection`. `assemblyDir` is
required for the file stage (`--fileData`, and the automatic step 4); if it is
absent the normal run logs `no assemblyDir in config; skipping file stage` and
finishes after download — so existing configs keep working unchanged.

## Locking (multi-machine safe)

The project may be reachable from several machines over NFS, so the run holds
cross-host lock files **beside `autoupdate.yaml`** (not in `/tmp` or `/var`, which
are per-host). Because NFS `flock`/`fcntl` is unreliable, the lock is an atomic
`O_EXCL` lock file:

- `autoupdateS1_download.lock` — held during **search + download**.
- `autoupdateS1_file.lock` — held during **filing and the frame check** (both
  write the assembly tree).

Each lock is **non-blocking**: if another run already holds it, this run logs
`… another run holds <lock>; skipping …` and skips only that stage (the two locks
are independent). A lock older than `STALE_LOCK_HOURS` (48 h — longer than any
normal run) is treated as abandoned (a crashed run) and reclaimed. The lock file
records `host pid time` for debugging. `--check` runs take no locks. These are
independent of the per-host `flock -n` you may wrap the cron line in — that guards
one machine; these guard across machines.

## Cron

Use the versioned wrapper `scripts/runAutoupdateS1.sh` (activates the conda env,
guards against an unmounted project tree, then `exec autoupdateS1`):

```
# nightly at 02:15
15 2 * * *  /home/ian/PycharmProjects/packages/asfSearchAndDownload/scripts/runAutoupdateS1.sh \
            /Volumes/insar1/ian/NISAR/realNISAR/newGreenlandProject \
            >> /Volumes/insar1/ian/NISAR/realNISAR/newGreenlandProject/logs/cron.log 2>&1
```

## Logs

Each run writes a timestamped session log `logs/autoupdateS1_<YYYY-MM-DDThhmmss>.log`
in the project directory (override the location with the `logDir` config key). It
records the session header, orbit files downloaded, the search command, each SLC
downloaded and reduced, and any errors (with traceback on an unexpected failure).

## Debugging while catching up

The user is several months behind. Run one pass at a time with:

```
autoupdateS1 --maxDownloads 1
```

Repeat to walk forward; each run refreshes orbits, downloads a single new pass
(deduped), reduces it, and exits.
