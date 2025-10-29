# reduceSentinel1
This will remove data with a pattern from Sentinel1 zip files (e.g., all hv files). For users who do not need dual pol data, this can reduce the volume of the zip file by nearly half by removing the cross-pol data.

If zip/unzip is available as a command line call, it will be used to delete the unwanted data (~<10s for an L0 SLC on a fast raid). If not, the data will be removed with python, which is about 3x slower.

**It has been tested successfully on both Sentinel 1 SLC and L0 products.**

Note: zip warnings about directory mismatch do not seem to cause any problem with retrieving data from the reduced file.

# Installation

This utility can be installed with: 
```bash
pip install git+https://github.com/fastice/reduceSentinel1.git@main
```
# Usage

```bash
usage: reduces1 [-h] [--pattern PATTERN] [--directory DIRECTORY] [--suffix SUFFIX] [zipfile]
Remove files from ZIP archives by pattern.

positional arguments:
  zipfile               Path to a single ZIP file

options:
  -h, --help            show this help message and exit
  --pattern PATTERN     Pattern to match filenames to remove [hv]
  --directory DIRECTORY
                        Directory containing ZIP files to process
  --suffix SUFFIX       Suffix of files to process in directory [zip]
```

## Examples

Remove all `hv` (default) data from an S1 zipfile:
```bash
reduces1 zipfile.zip
```

Remove all `vh` data from a directory of zip files that have suffix `.zip1` instead of zip:
```bash
reduces1 --pattern vh --suffix zip1 --directory zipfileDirectory
```



