#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Automated Sentinel-1 IW SLC archive-update driver, configured by autoupdate.yaml.

Intended to run as a nightly cron job (but also runnable ad hoc from the CLI
for other regions/periods). Each run:
  1. refreshes the precise-orbit (state-vector) archive (refreshOrbits),
  2. searches ASF for new S1 IW SLC passes (searchASF), deduped against the
     existing archive,
  3. downloads new passes one at a time via ariaDownload, verifies each zip,
     re-downloading on failure, and strips the unneeded cross-pol in a parallel
     thread (reduceSentinel1) while the next download starts.

New passes are filed under archiveDir/<YYYY>-<MM>/ where the month comes from
the first date token in the granule name. A pass already present as .zip or
.zip.1 (the .1 marks an already-processed file) is not re-downloaded.

Part of the asfSearchAndDownload package.
"""
import argparse
import calendar
import contextlib
import datetime
import glob
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

import yaml
import utilities as u

from asfsearchdownload import refreshOrbits
from asfsearchdownload import fileS1
from asfsearchdownload import checkFramesS1
from asfsearchdownload.reduceSentinel1 import remove_files_from_zip

# Per-session logger; handlers are attached by setupLogging() in main().
log = logging.getLogger('autoupdateS1')

# Predefined search regions -> searchASF spatial flag.
regionFlags = {
    'antarctica': '--antarctica',
    'greenland': '--greenland',
}

# First date token in an S1 granule name, e.g. ..._20260115T063045_...
DATE_TOKEN = re.compile(r'(\d{8})T\d{6}')

# Trailing ..._<absOrbit6>_<datatake6hex>_<uniqueId4hex>.zip of an S1 name. The
# frames of one pass share (absOrbit, datatake), so this keys pass identity.
PASS_KEY = re.compile(r'_(\d{6})_([0-9A-Fa-f]{6})_[0-9A-Fa-f]{4}\.zip$')


def parseArgs():
    '''
    Handle command line args.
    '''
    parser = argparse.ArgumentParser(
        description='\n\n\033[1mAutomated Sentinel-1 IW SLC archive-update '
        'driver, configured by autoupdate.yaml\033[0m\n\n',
        epilog='Part of the asfSearchAndDownload package.')
    parser.add_argument('config', type=str, nargs='?', default='autoupdate.yaml',
                        help='Path to autoupdate.yaml (default: ./autoupdate.yaml)')
    parser.add_argument('--maxDownloads', type=int, default=None,
                        help='Soft cap on downloads per run (0 = no limit); '
                        'overrides the config maxDownloads key [default 300]. '
                        'The cap is soft: once reached, the remaining frames of '
                        'the pass in progress (same orbit + datatake) are '
                        'finished before stopping.')
    parser.add_argument('--firstDate', type=str, default=None,
                        help='Search start date YYYY-MM-DD (overrides config; '
                        'default today-6 months)')
    parser.add_argument('--lastDate', type=str, default=None,
                        help='Search end date YYYY-MM-DD (overrides config; '
                        'default today)')
    parser.add_argument('--region', type=str, default=None,
                        help='Predefined search region '
                        f'({"/".join(regionFlags)}); overrides config')
    parser.add_argument('--searchArea', type=str, default=None,
                        help='GeoJSON/.shp/lon,lat search polygon; overrides '
                        'config and --region')
    parser.add_argument('--noOrbits', action='store_true',
                        help='Skip the state-vector (orbit) refresh')
    parser.add_argument('--noDownload', action='store_true',
                        help='Skip search+download (only refresh orbits)')
    parser.add_argument('--fileData', action='store_true',
                        help='Run only the filing step: unpack archive zips '
                        'into the assemblyDir track tree (skips orbits + '
                        'search/download)')
    parser.add_argument('--checkFrames', action='store_true',
                        help='Run only the frame-check step: vet filed '
                        'datatakes and queue them (skips orbits + '
                        'search/download + filing)')
    parser.add_argument('--check', action='store_true',
                        help='Dry run across every stage: report what would be '
                        'downloaded, filed, or written without modifying '
                        'anything on disk')
    refreshOrbits.addSensorArgs(parser)
    return parser.parse_args()


def loadConfig(configFile):
    '''
    Read the autoupdate.yaml config into a dict.
    '''
    with open(configFile) as fp:
        config = yaml.safe_load(fp) or {}
    return config


def setupLogging(logDir):
    '''
    Attach a timestamped per-session log file (autoupdateS1_<date-time>.log) in
    logDir to the module logger, plus a stdout stream so cron.log still sees
    everything. Returns the log-file path.
    '''
    os.makedirs(logDir, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y-%m-%dT%H%M%S')
    logPath = os.path.join(logDir, f'autoupdateS1_{stamp}.log')
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S')
    log.setLevel(logging.INFO)
    log.handlers.clear()
    log.propagate = False
    fileHandler = logging.FileHandler(logPath)
    fileHandler.setFormatter(fmt)
    log.addHandler(fileHandler)
    streamHandler = logging.StreamHandler(sys.stdout)
    streamHandler.setFormatter(fmt)
    log.addHandler(streamHandler)
    return logPath


# Cross-machine locks. They live in the project dir (beside autoupdate.yaml) so
# every machine sharing the NFS tree sees them -- unlike /var or /tmp, which are
# per-host. NFS flock/fcntl is unreliable, so we use an atomic O_EXCL lock file.
LOCK_DOWNLOAD = 'autoupdateS1_download.lock'   # guards search + download
LOCK_FILE = 'autoupdateS1_file.lock'           # guards assembly-tree writes
STALE_LOCK_HOURS = 48                           # abandon a lock older than this


def _tryLock(lockPath, staleHours):
    '''
    Atomically create lockPath (NFS-safe O_CREAT|O_EXCL). Returns True on
    success. A lock older than staleHours is treated as abandoned (crashed run)
    and reclaimed.
    '''
    for attempt in (1, 2):
        try:
            fd = os.open(lockPath, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, (f'{socket.gethostname()} pid {os.getpid()} '
                          f'{datetime.datetime.now().isoformat(timespec="seconds")}'
                          '\n').encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lockPath)
                with open(lockPath) as fp:
                    holder = fp.read().strip()
            except OSError:
                return False
            if attempt == 1 and age > staleHours * 3600:
                log.warning(f'reclaiming stale lock {lockPath} '
                            f'({age / 3600:.1f} h old, held by {holder})')
                try:
                    os.remove(lockPath)
                except OSError:
                    pass
                continue
            log.info(f'lock {lockPath} held by {holder}')
            return False
    return False


@contextlib.contextmanager
def crossHostLock(lockPath, active=True, staleHours=STALE_LOCK_HOURS):
    '''
    Non-blocking cross-host lock. Yields True if acquired (removing the lock file
    on exit), False if another run holds it. With active=False (a dry run) it is
    a no-op that always yields True and touches nothing -- but if a live run
    currently holds the lock it prints a bold-blue notice, since the dry-run
    output may then reflect a mid-flight tree.
    '''
    if not active:
        if os.path.exists(lockPath):
            try:
                with open(lockPath) as fp:
                    holder = fp.read().strip()
            except OSError:
                holder = '?'
            print(f'\033[1;34m*** LOCK ACTIVE: {os.path.basename(lockPath)} held '
                  f'by {holder}; --check output may be mid-flight ***\033[0m')
        yield True
        return
    acquired = _tryLock(lockPath, staleHours)
    try:
        yield acquired
    finally:
        if acquired:
            try:
                os.remove(lockPath)
            except OSError:
                pass


def regionFlag(region):
    '''
    Map a region name to its searchASF spatial flag (case-insensitive).
    '''
    key = str(region).strip().lower()
    if key not in regionFlags:
        u.myerror(f"autoupdateS1: region must be one of {list(regionFlags)}, "
                  f"got '{region}'")
    return regionFlags[key]


def spatialFlags(config, args):
    '''
    Return the searchASF spatial-constraint tokens, CLI first: an explicit
    --searchArea, else --region, else config searchArea, else config region.
    '''
    if args.searchArea:
        return ['--searchArea', args.searchArea]
    if args.region:
        return [regionFlag(args.region)]
    if config.get('searchArea'):
        return ['--searchArea', str(config['searchArea'])]
    if config.get('region'):
        return [regionFlag(config['region'])]
    u.myerror('autoupdateS1: no region or searchArea given (CLI flag or '
              'config key)')


def configList(value, default):
    '''
    Normalize a config value that may be a space-separated string, a YAML list,
    or absent into a list of string tokens (falling back to default).
    '''
    if value is None:
        value = default
    if isinstance(value, str):
        return value.split()
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def resolveSensors(config, args):
    '''
    Return the selected sensors. The CLI --S1A/--S1B/--S1C/--S1D flags win if any
    are set; otherwise the config `satellites` key; otherwise all four.
    '''
    cli = tuple(s for s in refreshOrbits.ALL_SENSORS if getattr(args, s, False))
    if cli:
        return cli
    if config.get('satellites') is None:
        return refreshOrbits.ALL_SENSORS
    sats = configList(config.get('satellites'), '')
    unknown = [s for s in sats if s not in refreshOrbits.ALL_SENSORS]
    if unknown:
        u.myerror(f'autoupdateS1: unknown satellite(s) {unknown} in config '
                  f'satellites; choose from {list(refreshOrbits.ALL_SENSORS)}')
    return tuple(s for s in refreshOrbits.ALL_SENSORS if s in sats)


def directionFlags(config):
    '''
    Map the config `direction` key to searchASF flight-direction tokens.
    'both' (default) applies no constraint. ascending/descending require the
    --flightDirection option in searchASF.
    '''
    direction = str(config.get('direction', 'both')).strip().lower()
    if direction in ('both', 'all', ''):
        return []
    if direction.startswith('asc'):
        return ['--flightDirection', 'ASCENDING']
    if direction.startswith('desc'):
        return ['--flightDirection', 'DESCENDING']
    u.myerror("autoupdateS1: direction must be both/ascending/descending, got "
              f"'{direction}'")


def monthsBack(d, months):
    '''
    Return the date `months` calendar months before d (clamped day-of-month).
    '''
    month = d.month - 1 - months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


def asDateStr(value):
    '''
    Coerce a date/datetime (as YAML may parse an unquoted date) or string into
    a YYYY-MM-DD string.
    '''
    if hasattr(value, 'strftime'):
        return value.strftime('%Y-%m-%d')
    return str(value)


def resolveDateRange(config, args, today):
    '''
    Return (firstDate, lastDate) as YYYY-MM-DD strings. CLI overrides config;
    defaults are today-6 months and today.
    '''
    firstDate = args.firstDate or config.get('firstDate') or monthsBack(today, 6)
    lastDate = args.lastDate or config.get('lastDate') or today
    return asDateStr(firstDate), asDateStr(lastDate)


def searchGranules(config, args, today, firstDate, lastDate, check=False):
    '''
    Search ASF for S1 IW SLC granules over the configured region/time window,
    deduped against the existing archive, writing the URL list and coverage
    GeoPackage under archiveDir/searchResults. Returns the URL-list path.

    With check, the results are written to a scratch temp dir instead of the
    archive so a dry run leaves archiveDir untouched (the search itself is a
    read; the archive is only read for dedup).
    '''
    archiveDir = config['archiveDir']
    if check:
        searchDir = tempfile.mkdtemp(prefix='autoupdateS1_check_')
        gpkgDir = searchDir
    else:
        searchDir = os.path.join(archiveDir, 'searchResults')
        gpkgDir = os.path.join(searchDir, 'gpkg')
    os.makedirs(gpkgDir, exist_ok=True)
    d = today.strftime('%m-%d-%Y')
    out = os.path.join(searchDir, f'Download.{d}')
    gpkg = os.path.join(gpkgDir, f'Download.{d}.gpkg')
    # Dedup glob matches archiveDir/<YYYY-MM>/*.zip and *.zip.1 (searchASF
    # strips .zip/.zip.1 before comparing).
    archiveGlob = os.path.join(archiveDir, '*', '*')
    products = configList(config.get('productType'), 'SLC')
    beamModes = configList(config.get('beamMode'), 'IW')
    command = (['searchASF', '--sensor', 'SENTINEL1',
                '--products'] + products + ['--beamMode'] + beamModes
               + ['--archiveDir', archiveGlob, '--gpkg', gpkg]
               + directionFlags(config)
               + spatialFlags(config, args)
               + [firstDate, lastDate, out])
    log.info('search: ' + ' '.join(command))
    subprocess.run(command, check=True)
    return out


def readUrlList(urlListFile):
    '''
    Return the download URLs listed in a searchASF URL-list file.
    '''
    urls = []
    with open(urlListFile) as fp:
        for line in fp:
            url = line.strip()
            if url:
                urls.append(url)
    return urls


def filterBySensor(urls, sensors):
    '''
    Keep only URLs whose granule name begins with one of the selected sensors.
    '''
    prefixes = tuple(f'{s}_' for s in sensors)
    return [url for url in urls
            if os.path.basename(url).startswith(prefixes)]


def sortByAcqDate(urls):
    '''
    Sort URLs oldest-first by the first acquisition datetime token in the
    granule name (YYYYMMDDThhmmss sorts lexically = chronologically), so a
    capped run (--maxDownloads) fetches the oldest missing passes first.
    '''
    def key(url):
        match = DATE_TOKEN.search(os.path.basename(url))
        return match.group(0) if match else ''
    return sorted(urls, key=key)


def passKey(name):
    '''
    Return the (absOrbit, datatake) pass identity for an S1 granule name, or
    None if it cannot be parsed. Frames of the same pass share this key.
    '''
    match = PASS_KEY.search(name)
    return match.groups() if match else None


def stripArchiveExt(name):
    '''
    Strip a trailing .zip.1 or .zip from a granule basename.
    '''
    for ext in ('.zip.1', '.zip'):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def granuleInArchive(archiveDir, name):
    '''
    True if the granule already exists anywhere under archiveDir as .zip or
    .zip.1 (the .1 marks an already-processed pass). Belt-and-suspenders beyond
    the searchASF dedup.
    '''
    stem = stripArchiveExt(name)
    for ext in ('.zip', '.zip.1'):
        if glob.glob(os.path.join(archiveDir, '*', stem + ext)):
            return True
    return False


def monthDirFor(archiveDir, name):
    '''
    Return archiveDir/<YYYY>-<MM> from the first date token in the granule name,
    or None if no date token is present.
    '''
    match = DATE_TOKEN.search(name)
    if not match:
        return None
    ymd = match.group(1)
    return os.path.join(archiveDir, f'{ymd[:4]}-{ymd[4:6]}')


def zipComplete(zipPath):
    '''
    True if the download finished (no aria2c .aria2 control file) and the file
    is a structurally valid zip (catches truncation cheaply).
    '''
    if not os.path.exists(zipPath) or os.path.exists(f'{zipPath}.aria2'):
        return False
    return zipfile.is_zipfile(zipPath)


def downloadOne(url, monthDir, maxAttempts=3):
    '''
    Download a single granule into monthDir via ariaDownload (which adjusts
    aria2c bandwidth by time of day), verifying the zip and re-downloading on
    failure up to maxAttempts. Returns the zip path on success, else None.
    '''
    os.makedirs(monthDir, exist_ok=True)
    name = os.path.basename(url)
    zipPath = os.path.join(monthDir, name)
    linkFile = os.path.join(monthDir, f'.{name}.url')
    with open(linkFile, 'w') as fp:
        fp.write(url + '\n')
    try:
        for attempt in range(1, maxAttempts + 1):
            if not zipComplete(zipPath):
                # ariaDownload skips an existing file, so clear any partial or
                # corrupt zip (and its .aria2 control) to force a fresh fetch.
                for stale in (zipPath, f'{zipPath}.aria2'):
                    if os.path.exists(stale):
                        os.remove(stale)
                log.info(f'ariaDownload attempt {attempt}/{maxAttempts}: {name}')
                subprocess.run(['ariaDownload', linkFile], cwd=monthDir,
                               check=True)
            if zipComplete(zipPath):
                return zipPath
            log.warning(f'{name} incomplete/corrupt after attempt {attempt}')
        # Give up: remove the partial so the next run's search re-lists it.
        for stale in (zipPath, f'{zipPath}.aria2'):
            if os.path.exists(stale):
                os.remove(stale)
        log.error(f'{name} failed after {maxAttempts} attempts')
        return None
    finally:
        if os.path.exists(linkFile):
            os.remove(linkFile)


def _reduceWorker(zipPath, pattern):
    '''
    Strip the cross-pol from a downloaded zip (runs in a background thread).
    '''
    name = os.path.basename(zipPath)
    try:
        remove_files_from_zip(zipPath, pattern)
        log.info(f'reduced (cross-pol stripped): {name}')
    except Exception as exc:  # keep one bad zip from killing the run
        log.error(f'reduce failed for {name}: {exc}')


def startReduce(zipPath, pattern, threads):
    '''
    Launch the cross-pol reduce for zipPath in a background thread and record
    it so the caller can join it before exiting.
    '''
    thread = threading.Thread(target=_reduceWorker, args=(zipPath, pattern))
    thread.start()
    threads.append(thread)


def main():
    ''' Search/download/reduce new S1 IW SLC passes and refresh orbits. '''
    args = parseArgs()
    config = loadConfig(args.config)
    if 'archiveDir' not in config:
        u.myerror(f"autoupdateS1: required key 'archiveDir' missing from "
                  f'{args.config}')
    archiveDir = os.path.abspath(config['archiveDir'])
    config['archiveDir'] = archiveDir
    # Session log lives in <projectDir>/logs by default (projectDir is the
    # config file's directory); overridable via the logDir config key.
    projectDir = os.path.dirname(os.path.abspath(args.config))
    logDir = config.get('logDir') or os.path.join(projectDir, 'logs')
    logPath = setupLogging(logDir)

    sensors = resolveSensors(config, args)
    reducePattern = config.get('reducePattern', 'hv')
    maxAttempts = int(config.get('maxAttempts', 3))
    today = datetime.date.today()  # fixed once so a midnight-crossing run agrees
    log.info(f'=== autoupdateS1 session start (config {args.config}) ===')
    log.info(f'archiveDir {archiveDir}; sensors {",".join(sensors)}; '
             f'log {logPath}')
    try:
        runUpdate(config, args, archiveDir, sensors, reducePattern,
                  maxAttempts, today, logDir, projectDir)
    except Exception:
        log.exception('autoupdateS1 session failed')
        raise
    log.info('=== autoupdateS1 session done ===')


def fileStage(config, archiveDir, logDir, projectDir, today, check=False):
    '''
    Unpack the archive zips (archiveDir/<YYYY-MM>/*.zip) into the per-track/
    per-orbit tree under assemblyDir, writing a YAML record (tracks:/granules:)
    of what was filed this run for downstream steps to consume. With check,
    report what would be filed without unpacking or writing the record.

    Guarded by the shared file lock so two machines never file into the same
    assembly tree at once.
    '''
    if 'assemblyDir' not in config:
        u.myerror("autoupdateS1: the file stage needs the 'assemblyDir' key "
                  'in the config')
    assemblyDir = os.path.abspath(config['assemblyDir'])
    lockPath = os.path.join(projectDir, LOCK_FILE)
    with crossHostLock(lockPath, active=not check) as acquired:
        if not acquired:
            log.warning(f'file stage: another run holds {lockPath}; '
                        'skipping filing')
            return
        filedPath = os.path.join(logDir, f'filedS1.{today:%m-%d-%Y}.yaml')
        tracks, granules = fileS1.fileS1(zipDir=archiveDir,
                                         assemblyDir=assemblyDir,
                                         monthSubdirs=True, filed=filedPath,
                                         createTrackDir=True, check=check)
        if check:
            log.info(f'[check] would file {len(granules)} zip(s) across tracks '
                     f'{sorted(tracks)}')
        else:
            log.info(f'filed {len(granules)} zip(s) across tracks '
                     f'{sorted(tracks)} -> {filedPath}')


def frameCheckStage(config, today, args, projectDir, check=False):
    '''
    Vet the filed datatakes under assemblyDir (burst-frame coverage, gaps,
    over-length, out-of-range), restructure them into clean processing units,
    and route each into the cumulative toProcess / pendingProcessing / problem
    queues. With check, report the routing without changing anything.

    Shares the file lock with the filing stage: both mutate the assembly tree,
    so two machines must never run either at once on the same project.
    '''
    if 'assemblyDir' not in config:
        u.myerror("autoupdateS1: the frame-check stage needs the 'assemblyDir' "
                  'key in the config')
    assemblyDir = os.path.abspath(config['assemblyDir'])
    lockPath = os.path.join(projectDir, LOCK_FILE)
    with crossHostLock(lockPath, active=not check) as acquired:
        if not acquired:
            log.warning(f'frame-check stage: another run holds {lockPath}; '
                        'skipping')
            return
        orbitDir = config.get('orbitDir', refreshOrbits.DEFAULT_ORBIT_DIR)
        queueDir = config.get('queueDir', assemblyDir)
        firstStr, lastStr = resolveDateRange(config, args, today)
        firstDate = datetime.datetime.strptime(firstStr, '%Y-%m-%d')
        lastDate = (datetime.datetime.strptime(lastStr, '%Y-%m-%d')
                    + datetime.timedelta(days=1))
        entries = checkFramesS1.checkFrames(assemblyDir, orbitDir=orbitDir,
                                            firstDate=firstDate,
                                            lastDate=lastDate,
                                            queueDir=queueDir, check=check)
        verb = 'would queue' if check else 'queued'
        log.info(f'frame check: {verb} toProcess {len(entries["toProcess"])}, '
                 f'pending {len(entries["pendingProcessing"])}, '
                 f'problem {len(entries["problem"])}')


def downloadStage(config, args, archiveDir, sensors, reducePattern, maxAttempts,
                  today):
    '''
    Search ASF then serially download new granules (parallel cross-pol reduce).
    Wrapped by runUpdate in the cross-host download lock.
    '''
    # 2. Search.
    firstDate, lastDate = resolveDateRange(config, args, today)
    log.info(f'search window {firstDate} .. {lastDate}')
    urlListFile = searchGranules(config, args, today, firstDate, lastDate,
                                 check=args.check)
    urls = sortByAcqDate(filterBySensor(readUrlList(urlListFile), sensors))
    log.info(f'{len(urls)} new granule(s) for sensor(s) {",".join(sensors)}')

    # Soft download cap: CLI overrides config, default 300; 0 means no limit.
    if args.maxDownloads is not None:
        maxDownloads = args.maxDownloads
    else:
        maxDownloads = int(config.get('maxDownloads', 300))

    # 3. Serial download; parallel cross-pol reduce. The cap is soft: once
    # reached, keep going while the next granule is the same pass (orbit +
    # datatake) as the last one downloaded, so a pass is never left half-fetched.
    reduceThreads = []
    nDownloaded = nSkipped = nFailed = 0
    lastPassKey = None
    for url in urls:
        name = os.path.basename(url)
        thisPassKey = passKey(name)
        if maxDownloads and nDownloaded >= maxDownloads \
                and thisPassKey != lastPassKey:
            log.info(f'reached --maxDownloads {maxDownloads} at a pass '
                     f'boundary ({nDownloaded} downloaded); stopping')
            break
        if granuleInArchive(archiveDir, name):
            nSkipped += 1
            continue
        monthDir = monthDirFor(archiveDir, name)
        if monthDir is None:
            log.warning(f'cannot parse date from {name}; skipping')
            nFailed += 1
            continue
        if args.check:
            log.info(f'[check] would download: {name} -> {monthDir}')
            nDownloaded += 1
            lastPassKey = thisPassKey
            continue
        zipPath = downloadOne(url, monthDir, maxAttempts)
        if zipPath is None:
            nFailed += 1
            continue
        nDownloaded += 1
        lastPassKey = thisPassKey
        log.info(f'downloaded: {name} -> {monthDir}')
        startReduce(zipPath, reducePattern, reduceThreads)

    for thread in reduceThreads:
        thread.join()
    verb = 'would download' if args.check else 'downloaded'
    log.info(f'summary: {verb} {nDownloaded}, skipped {nSkipped}, '
             f'failed {nFailed}; {len(reduceThreads)} reduced')


def runUpdate(config, args, archiveDir, sensors, reducePattern, maxAttempts,
              today, logDir, projectDir):
    '''
    The search/download/reduce + orbit-refresh workflow (wrapped by main() so
    any error is logged to the session log). Cross-host locks in projectDir keep
    two machines from downloading, or from writing the assembly tree, at once.
    '''
    if args.check:
        log.info('[check] dry run: no data will be written')

    # File-only isolation: skip orbits + search/download, just file the archive.
    if args.fileData:
        fileStage(config, archiveDir, logDir, projectDir, today,
                  check=args.check)
        return

    # Frame-check-only isolation: skip everything but the vet-and-queue step.
    if args.checkFrames:
        frameCheckStage(config, today, args, projectDir, check=args.check)
        return

    # 1. Orbit state vectors.
    if not args.noOrbits:
        orbitDir = config.get('orbitDir', refreshOrbits.DEFAULT_ORBIT_DIR)
        newOrbits = refreshOrbits.updateStateVectors(orbitDir, sensors,
                                                     check=args.check)
        verb = 'would download' if args.check else 'downloaded'
        for name in newOrbits:
            log.info(f'orbit {verb}: {name}')
        log.info(f'orbits: {len(newOrbits)} EOF file(s)')

    if args.noDownload:
        log.info('--noDownload set; done after orbit refresh')
        return

    # 2-3. Search + download, guarded so two machines never download at once.
    dlLock = os.path.join(projectDir, LOCK_DOWNLOAD)
    with crossHostLock(dlLock, active=not args.check) as acquired:
        if acquired:
            downloadStage(config, args, archiveDir, sensors, reducePattern,
                          maxAttempts, today)
        else:
            log.warning(f'download stage: another run holds {dlLock}; '
                        'skipping search + download')

    # 4-5. File the archive zips, then vet & queue the datatakes (if configured).
    if 'assemblyDir' in config:
        fileStage(config, archiveDir, logDir, projectDir, today,
                  check=args.check)
        frameCheckStage(config, today, args, projectDir, check=args.check)
    else:
        log.info('no assemblyDir in config; skipping file + frame-check stages')


if __name__ == '__main__':
    main()
