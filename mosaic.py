#!/usr/bin/env python3

# Ported to Python 3, using MontagePy, with parallelization using multiprocessing.

import os
import sys
import tempfile
import atexit
import shutil
import argparse
import multiprocessing as mp
from functools import partial

from MontagePy.main import *

def clean_dir(directory):
    shutil.rmtree(directory, ignore_errors=True)

def read_montage_table(tbl_path):
    """Simple parser for Montage IPAC tables."""
    data = []
    with open(tbl_path, 'r') as f:
        for line in f:
            if line.startswith('\\') or line.startswith('|'):
                continue
            fields = line.split()
            data.append(fields)
    return data

def project_image(header, proj_dir, img):
    base = os.path.basename(img)
    out_img = os.path.join(proj_dir, base)
    rtn = mProjectQL(img, out_img, header)
    print(f"Project {img}: {rtn}")
    return (img, rtn['status'], rtn.get('msg', 'unknown error') if rtn['status'] != '0' else '')

def diff_fit(pimages, diffs_dir, header, overlap):
    plus, minus, diff_base = overlap
    img1 = pimages[plus]
    img2 = pimages[minus]
    diff_file = os.path.join(diffs_dir, diff_base)
    rtn_diff = mDiff(img1, img2, diff_file, header)
    print(f"mDiff {plus}-{minus}: {rtn_diff}")
    if rtn_diff['status'] != '0':
        return None

    rtn_fit = mFitplane(diff_file)
    print(f"mFitplane {diff_base}: {rtn_fit}")
    if rtn_fit['status'] != '0':
        return None

    return (plus, minus, rtn_fit['a'], rtn_fit['b'], rtn_fit['c'],
            rtn_fit['xmin'], rtn_fit['ymin'], rtn_fit['xmax'], rtn_fit['ymax'],
            rtn_fit['xcenter'], rtn_fit['ycenter'], int(rtn_fit['npixel']),
            rtn_fit['xrms'], rtn_fit['yrms'], rtn_fit['rss'],
            rtn_fit['boxx'], rtn_fit['boxy'], rtn_fit['boxwidth'],
            rtn_fit['boxheight'], rtn_fit['boxang'])

def bg_correct(pimages, corr_dir, corrections_tbl, idx):
    img = pimages[idx]
    base = os.path.basename(img)
    out_img = os.path.join(corr_dir, base)
    rtn = mBackground(img, out_img, corrections_tbl)
    print(f"mBackground {idx}: {rtn}")
    return rtn['status']

def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(description='Create image mosaic using MontagePy')
    parser.add_argument('files', nargs='+', help='Input FITS files followed by output FITS file')
    parser.add_argument('--background-match', action='store_true', help='Enable background matching')
    parser.add_argument('--combine', default='mean', choices=['mean', 'median', 'count'], help='Coaddition type')
    parser.add_argument('--ncores', type=int, default=mp.cpu_count(), help='Number of cores for parallel processing')
    args = parser.parse_args(argv)

    if len(args.files) < 2:
        parser.error('At least one input file and one output file required')

    input_files = args.files[:-1]
    output_file = args.files[-1]

    # Verify input files are FITS
    for f in input_files:
        if not f.lower().endswith(('.fits', '.fit')):
            sys.stderr.write(f"Warning: {f} does not appear to be a FITS file\n")

    pid = os.getpid()

    # Create temporary work directory
    work_dir = tempfile.mkdtemp(suffix=f'_LEMON_{pid}_mosaic_work')
    atexit.register(clean_dir, work_dir)
    os.chdir(work_dir)

    print(f"Working directory: {work_dir}")

    # Create input directory with symlinks
    input_dir = tempfile.mkdtemp(dir=work_dir, suffix='_input')
    atexit.register(clean_dir, input_dir)
    for path in input_files:
        source = os.path.abspath(path)
        basename = os.path.basename(path)
        link_name = os.path.join(input_dir, basename)
        os.symlink(source, link_name)
        print(f"Symlinked {source} to {link_name}")

    # Create image table for raw images
    raw_tbl = 'rimages.tbl'
    rtn = mImgtbl(input_dir, raw_tbl)
    print(f"mImgtbl return: {rtn}")
    if rtn['status'] != '0':
        raise RuntimeError(f"mImgtbl failed: {rtn}")
    if rtn['count'] == 0:
        raise RuntimeError("No valid images found")
    print(f"Found {rtn['count']} valid images, {rtn.get('badwcs', 0)} bad WCS, {rtn.get('badfits', 0)} bad FITS")

    # Create output header
    header = 'region.hdr'
    rtn = mMakeHdr(raw_tbl, header)
    print(f"mMakeHdr return: {rtn}")
    if rtn['status'] != '0':
        raise RuntimeError(f"mMakeHdr failed: {rtn}")

    # Create projected directory
    proj_dir = tempfile.mkdtemp(dir=work_dir, suffix='_projected')
    atexit.register(clean_dir, proj_dir)

    # Get input images list
    raw_data = read_montage_table(raw_tbl)
    input_images = [os.path.join(input_dir, row[-1]) for row in raw_data]
    print("Input images for projection:")
    for img in input_images:
        print(img)
        if not os.path.exists(img):
            print(f"Warning: File does not exist: {img}")

    # Parallel projection
    pool = mp.Pool(args.ncores) if args.ncores > 1 else None
    project_func = partial(project_image, header, proj_dir)
    if pool:
        results = pool.map(project_func, input_images)
    else:
        results = [project_func(img) for img in input_images]

    failed = [(orig, msg) for orig, stat, msg in results if stat != '0']
    if failed:
        print("Failed projections:")
        for orig, msg in failed:
            print(f"{orig}: {msg}")

    num_failed = len(failed)
    if num_failed == len(input_images):
        raise RuntimeError("All projections failed")
    elif num_failed > 0:
        print(f"{num_failed} projections failed, continuing with {len(input_images) - num_failed} successful ones")

    # Create image table for projected images
    pimages_tbl = 'pimages.tbl'
    rtn = mImgtbl(proj_dir, pimages_tbl)
    print(f"mImgtbl projected: {rtn}")
    if rtn['status'] != '0':
        raise RuntimeError(f"mImgtbl (projected) failed: {rtn}")

    coadd_map = {'mean': 0, 'median': 1, 'count': 2}
    coadd_type = coadd_map[args.combine]

    if not args.background_match:
        # Direct coadd without background correction
        rtn = mAdd(proj_dir, pimages_tbl, header, output_file, coadd=coadd_type)
        print(f"mAdd: {rtn}")
        if rtn['status'] != '0':
            raise RuntimeError(f"mAdd failed: {rtn}")
    else:
        # Background correction steps
        diffs_dir = tempfile.mkdtemp(dir=work_dir, suffix='_diffs')
        atexit.register(clean_dir, diffs_dir)

        overlaps_tbl = 'overlaps.tbl'
        rtn = mOverlaps(pimages_tbl, overlaps_tbl)
        print(f"mOverlaps: {rtn}")
        if rtn['status'] != '0':
            raise RuntimeError(f"mOverlaps failed: {rtn}")

        # Parse overlaps table
        overlaps_data = []
        overlaps_raw = read_montage_table(overlaps_tbl)
        for row in overlaps_raw:
            cntr = int(row[0])
            plus = int(row[1])
            minus = int(row[2])
            diff_base = row[3]
            overlaps_data.append((plus, minus, diff_base))

        # Parse pimages table for image paths (cntr starts at 1)
        pimages_raw = read_montage_table(pimages_tbl)
        max_cntr = max(int(row[0]) for row in pimages_raw)
        pimages = [''] * (max_cntr + 1)
        for row in pimages_raw:
            cntr = int(row[0])
            fname = row[-1]
            pimages[cntr] = os.path.join(proj_dir, fname)
            if not os.path.exists(pimages[cntr]):
                print(f"Warning: Projected file does not exist: {pimages[cntr]}")

        # Parallel diff and fit
        diff_fit_func = partial(diff_fit, pimages, diffs_dir, header)
        if pool:
            fits_data = pool.map(diff_fit_func, overlaps_data)
        else:
            fits_data = [diff_fit_func(o) for o in overlaps_data]
        fits_data = [d for d in fits_data if d is not None]
        if len(fits_data) != len(overlaps_data):
            print(f"Some diff/fit failed, got {len(fits_data)} out of {len(overlaps_data)}")
            raise RuntimeError("Some diff/fit operations failed")

        # Write fits.tbl
        fits_tbl = 'fits.tbl'
        with open(fits_tbl, 'w') as f:
            f.write('\\ No backslash at start\n')
            f.write('|plus|minus|a|b|c|xmin|ymin|xmax|ymax|xcenter|ycenter|npixel|xrms|yrms|rss|boxx|boxy|boxwidth|boxheight|boxang|\n')
            for d in fits_data:
                f.write(f" {d[0]:4d} {d[1]:4d} {d[2]:12.5e} {d[3]:12.5e} {d[4]:12.5e} {d[5]:6.1f} {d[6]:6.1f} {d[7]:6.1f} {d[8]:6.1f} {d[9]:8.3f} {d[10]:8.3f} {d[11]:7d} {d[12]:6.4f} {d[13]:6.4f} {d[14]:10.4f} {d[15]:6.1f} {d[16]:6.1f} {d[17]:6.1f} {d[18]:6.1f} {d[19]:6.1f}\n")

        # Background modeling
        corrections_tbl = 'corrections.tbl'
        rtn = mBgModel(pimages_tbl, fits_tbl, corrections_tbl)
        print(f"mBgModel: {rtn}")
        if rtn['status'] != '0':
            raise RuntimeError(f"mBgModel failed: {rtn}")

        # Create corrected directory
        corr_dir = tempfile.mkdtemp(dir=work_dir, suffix='_corrected')
        atexit.register(clean_dir, corr_dir)

        # Parallel background correction
        bg_correct_func = partial(bg_correct, pimages, corr_dir, corrections_tbl)
        p_indices = [int(row[0]) for row in pimages_raw]

        if pool:
            statuses = pool.map(bg_correct_func, p_indices)
        else:
            statuses = [bg_correct_func(idx) for idx in p_indices]
        failed_bg = [i for i, s in zip(p_indices, statuses) if s != '0']
        if failed_bg:
            print(f"Failed background corrections for indices: {failed_bg}")
            raise RuntimeError("Some background corrections failed")

        # Create image table for corrected images
        cimages_tbl = 'cimages.tbl'
        rtn = mImgtbl(corr_dir, cimages_tbl)
        print(f"mImgtbl corrected: {rtn}")
        if rtn['status'] != '0':
            raise RuntimeError(f"mImgtbl (corrected) failed: {rtn}")

        # Final coadd
        rtn = mAdd(corr_dir, cimages_tbl, header, output_file, coadd=coadd_type)
        print(f"Final mAdd: {rtn}")
        if rtn['status'] != '0':
            raise RuntimeError(f"mAdd failed: {rtn}")

    if pool:
        pool.close()

    print(f"Mosaic created at {output_file}")

if __name__ == '__main__':
    main()