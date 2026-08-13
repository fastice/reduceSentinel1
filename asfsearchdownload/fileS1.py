#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Jun 18 15:11:35 2021

@author: ian
"""

import argparse
import utilities as u
import os
from datetime import datetime
from subprocess import call
import threading
import glob
import yaml


def fileS1Args():
    ''' Handle command line args'''
    parser = argparse.ArgumentParser(
        description='\033[1m File S1 images in Data dir \033[0m',
        epilog='Notes:  ', allow_abbrev='False')
    parser.add_argument('--overwrite', action='store_true', default=False,
                        help='Overwrite existing')
    parser.add_argument('--createTrackDir', action='store_true', default=False,
                        help='Create track dir if it does not exist already')
    parser.add_argument('--zipDir', type=str,
                        default='/Volumes/insar10/ian/xfer',
                        help='Directory with zip files')
    parser.add_argument('--assemblyDir', type=str, default='.',
                        help='Root dir under which track-<n>/<orbit>/ trees are '
                        'built [default: current dir]')
    parser.add_argument('--monthSubdirs', action='store_true', default=False,
                        help='Glob zipDir/<YYYY-MM>/*.zip across all month '
                        'subdirs instead of a flat zipDir/*.zip')
    parser.add_argument('--filed', type=str, default=None,
                        help='Path to write a YAML record (tracks:/granules:) '
                        'of what was filed this run')
    parser.add_argument('--check', action='store_true', default=False,
                        help='Dry run: report what would be filed without '
                        'unpacking, renaming, or writing anything')
    args = parser.parse_args()
    return args


def computeTrack(orbit, sat):
    satConst = {'S1A': 72, 'S1B': 26, 'S1C': 171, 'S1D': 41}
    track = orbit % 175 - satConst[sat]
    if track < 0:
        track += 175
    return track


def parseFileName(zipFile):
    ''' Get info from file name'''
    S1File = os.path.basename(zipFile)
    sat, mode, prodType, blank, pol, date1, date2, orbit, _, _ = \
        S1File.split('_')
    date1 = datetime.strptime(date1, '%Y%m%dT%H%M%S')
    date2 = datetime.strptime(date2, '%Y%m%dT%H%M%S')
    orbit = int(orbit)
    track = computeTrack(orbit, sat)
    return track, orbit, date1, date2, sat


def alreadyDownloaded(assemblyDir, track, orbit, safeFile):
    ''' Determine if file downloaded earlier'''
    orbDirs = glob.glob(f'{assemblyDir}/track-{track}/{orbit}')
    orbsExtra = glob.glob(f'{assemblyDir}/track-{track}/{orbit}_1')
    if len(orbDirs) > 0:
        if len(orbsExtra) > 0:
            orbDirs += orbsExtra
    else:
        orbDirs = orbsExtra
    #
    for orbDir in orbDirs:
        safe = f'{orbDir}/{safeFile}'
        if os.path.exists(safe):
            return safe
    return None


def runCommand(command):
    try:
        call(command, shell=True, executable='/bin/csh')
    except Exception:
        # if missing files, reject to the NoResult directory
        u.mywarning(f'could not run \n{command}')


def writeFiledRecord(filedFile, tracks, granules):
    ''' Write a YAML record of the tracks touched and zip paths filed this run.

    Consumed by autoupdateS1 to decide which tracks/files later steps work on.
    '''
    record = {'tracks': sorted(tracks),
              'granules': sorted(granules)}
    with open(filedFile, 'w') as fp:
        yaml.safe_dump(record, fp, default_flow_style=False)


def fileS1(zipDir, assemblyDir='.', monthSubdirs=False, filed=None,
           overwrite=False, createTrackDir=False, check=False):
    ''' Unpack S1 SAFE zips from zipDir into assemblyDir/track-<n>/<orbit>/.

    With monthSubdirs, zips are globbed from zipDir/<YYYY-MM>/*.zip across all
    month subdirs; otherwise from a flat zipDir/*.zip. With check, report what
    would be filed without unpacking, renaming, or writing anything. Returns
    (tracks, granules): the set of tracks touched and the list of source zip
    paths filed this run.
    '''
    assemblyDir = os.path.abspath(assemblyDir)
    if monthSubdirs:
        zipFiles = glob.glob(f'{zipDir}/*-*/*.zip')
    else:
        zipFiles = glob.glob(f'{zipDir}/*.zip')
    #
    threads = []
    tracks = set()
    granules = []
    for zipFile in zipFiles:
        # Absolute so the per-zip `pushd downloadDir; unzip zipFile` command is
        # cwd-independent.
        zipFile = os.path.abspath(zipFile)
        mySafe = os.path.basename(zipFile).replace('.zip', '.SAFE')
        track, orbit, date1, date2, sat = parseFileName(zipFile)
        trackDir = f'{assemblyDir}/track-{track}'
        if not os.path.exists(trackDir) and not check:
            if createTrackDir:
                os.mkdir(trackDir)
            else:
                u.myerror(f'{trackDir} does not exist, rerun with '
                          '--createTrackDir to create')
        #
        downloaded = alreadyDownloaded(assemblyDir, track, orbit, mySafe)
        # if exists and not overwrite, then skip
        if not overwrite and downloaded is not None:
            continue
        if check:
            print(f'[check] would file {os.path.basename(zipFile)} '
                  f'-> track-{track}/{orbit}')
            tracks.add(track)
            granules.append(zipFile)
            continue
        if downloaded is None:
            downloadDir = f'{trackDir}/{orbit}'
            if not os.path.exists(downloadDir):
                os.mkdir(downloadDir)
        else:
            downloadDir = os.path.dirname(downloaded)
        command = f'pushd {downloadDir} '
        command += \
            f'; unzip -u {zipFile} -x "*-slc-hv*"  -x "*-slc-vh*" -d ./ '
        if overwrite:
            command += f'; rm {date1.strftime("%Y%m%d")}*par'
        command += '; popd'
        command += f'; mv {zipFile} {zipFile.replace("zip", "zip.1")}'
        threads.append(threading.Thread(target=runCommand,
                                        args=[command]))
        tracks.add(track)
        granules.append(zipFile)

    # u.myerror('sop')
    if not check:
        u.runMyThreads(threads, 4, 'unzip data')
    if filed is not None and not check:
        writeFiledRecord(filed, tracks, granules)
    return tracks, granules


def main():
    ''' File S1 SAFE zips into the per-track/per-orbit assembly tree. '''
    args = fileS1Args()
    print(vars(args))
    fileS1(zipDir=args.zipDir, assemblyDir=args.assemblyDir,
           monthSubdirs=args.monthSubdirs, filed=args.filed,
           overwrite=args.overwrite, createTrackDir=args.createTrackDir,
           check=args.check)


if __name__ == '__main__':
    main()
