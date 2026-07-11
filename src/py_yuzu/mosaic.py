#!/usr/bin/env python3
# MontagePy mosaic with robust in-dir temp handling and parallel execution.

import os
import sys
import argparse
import tempfile
import shutil
import time
import multiprocessing as mp
from functools import partial
from concurrent.futures import ProcessPoolExecutor, as_completed, wait, FIRST_COMPLETED  # <-- FIXED

try:
    from MontagePy.main import *
    MONTAGEPY_AVAILABLE = True
except ModuleNotFoundError as exc:
    if exc.name != "MontagePy":
        raise
    MONTAGEPY_AVAILABLE = False
from conf_manager.conf_manager import cfg

REQUIRED_IMGTBL_COLS = [
    "cntr", "fname", "crpix1", "crpix2", "cdelt1", "cdelt2",
    "naxis1", "naxis2", "crval1", "crval2"
]


class _Progress:
    def __init__(self, total, label):
        self.total = max(1, int(total))
        self.label = label
        self.count = 0
        self.start = time.time()
        self.interactive = sys.stdout.isatty()
        self.last_emit = 0.0
        self.last_pct = -1

    def _line(self):
        elapsed = max(1e-9, time.time() - self.start)
        pct = (self.count / self.total) * 100.0
        rate = self.count / elapsed
        if self.count < self.total and rate > 0:
            remain = (self.total - self.count) / rate
            eta = f"{int(remain // 60):02d}:{int(remain % 60):02d}"
        else:
            eta = "00:00"
        width = 24
        filled = int(width * self.count / self.total)
        bar = "#" * filled + "-" * (width - filled)
        return f"{self.label} [{bar}] {pct:5.1f}% | {self.count}/{self.total} | {rate:.1f}/s | ETA {eta}"

    def update(self, step=1):
        self.count = min(self.total, self.count + int(step))
        now = time.time()
        pct = int((self.count / self.total) * 100.0)
        if self.interactive:
            if now - self.last_emit < 0.2 and self.count < self.total:
                return
            self.last_emit = now
            line = self._line()
            sys.stdout.write("\r" + line)
            sys.stdout.flush()
        else:
            if self.count < self.total and pct < self.last_pct + 5 and now - self.last_emit < 10.0:
                return
            self.last_emit = now
            self.last_pct = pct
            print(self._line(), flush=True)

    def finish(self):
        self.count = self.total
        line = self._line()
        if self.interactive:
            sys.stdout.write("\r" + line + " | Done!\n")
            sys.stdout.flush()
        else:
            print(line + " | Done!", flush=True)

def clean_dir(directory):
    shutil.rmtree(directory, ignore_errors=True)

def read_montage_table(tbl_path):
    rows = []
    with open(tbl_path, 'r') as f:
        for line in f:
            if not line.strip() or line.startswith('\\') or line.startswith('|'):
                continue
            fields = line.split()
            if fields:
                rows.append(fields)
    return rows

def parse_imgtbl_header(tbl_path):
    with open(tbl_path, 'r') as f:
        for line in f:
            if line.startswith('|'):
                return [c.strip() for c in line.strip().strip('|').split('|')]
    return []

def validate_imgtbl(tbl_path, required_cols=REQUIRED_IMGTBL_COLS):
    cols = parse_imgtbl_header(tbl_path)
    missing = [c for c in required_cols if c not in cols]
    if missing:
        print(f"[validate_imgtbl] header columns: {cols}")
        raise RuntimeError(f"Image metadata table missing columns: {missing} (file: {tbl_path})")

def estimate_space_bytes(nimages, naxis1, naxis2, safety_factor=2.0):
    return int(nimages * naxis1 * naxis2 * 8 * safety_factor)

def _project_image_task(header, proj_dir, img):
    try:
        base = os.path.basename(img)
        out_img = os.path.join(proj_dir, base)
        rtn = mProjectQL(img, out_img, header)
        status = rtn.get('status', '1')
        msg = '' if status == '0' else rtn.get('msg', b'unknown error')
        return (img, status, msg)
    except Exception as e:
        return (img, 'EXC', str(e))

def _diff_fit_task(pimages, diffs_dir, header, overlap):
    try:
        plus, minus, diff_base = overlap
        img1 = pimages[plus]
        img2 = pimages[minus]
        diff_file = os.path.join(diffs_dir, diff_base)
        rtn_diff = mDiff(img1, img2, diff_file, header)
        if rtn_diff.get('status') != '0':
            return None
        rtn_fit = mFitplane(diff_file)
        if rtn_fit.get('status') != '0':
            return None
        return (
            plus, minus, rtn_fit['a'], rtn_fit['b'], rtn_fit['c'],
            rtn_fit['xmin'], rtn_fit['ymin'], rtn_fit['xmax'], rtn_fit['ymax'],
            rtn_fit['xcenter'], rtn_fit['ycenter'], int(rtn_fit['npixel']),
            rtn_fit['xrms'], rtn_fit['yrms'], rtn_fit['rss'],
            rtn_fit['boxx'], rtn_fit['boxy'], rtn_fit['boxwidth'], rtn_fit['boxheight'], rtn_fit['boxang']
        )
    except Exception:
        return None

def _bg_correct_task(pimages, corr_dir, corrections_tbl, idx):
    try:
        img = pimages[idx]
        base = os.path.basename(img)
        out_img = os.path.join(corr_dir, base)
        rtn = mBackground(img, out_img, corrections_tbl)
        return (idx, rtn.get('status', '1'))
    except Exception:
        return (idx, 'EXC')

def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(description='Create image mosaic using MontagePy (parallel, temp in output dir)')
    parser.add_argument('files', nargs='+', help='Input FITS files followed by output FITS file')
    parser.add_argument('--background-match', action='store_true', help='Enable background matching')
    parser.add_argument('--combine', default='mean', choices=['mean', 'median', 'count'], help='Coaddition type')
    parser.add_argument('--ncores', type=int, default=mp.cpu_count(), help='Worker processes for CPU-bound steps')
    parser.add_argument('--io-workers', type=int, default=min(mp.cpu_count(), 6),
                        help='Workers for I/O-heavy projection (defaults to min(cpu, 6))')
    parser.add_argument('--chunksize', type=int, default=8, help='Chunk size for parallel maps')
    parser.add_argument('--overwrite', action='store_true', help='Overwrite output file if it exists')
    parser.add_argument('--allow-reference-fallback', action='store_true',
                        help='when MontagePy is unavailable, use first calibrated WCS FITS as source reference')
    args = parser.parse_args(argv)

    if len(args.files) < 2:
        parser.error('At least one input file and one output file required')

    input_files = args.files[:-1]
    output_file_arg = args.files[-1]

    for f in input_files:
        if not f.lower().endswith(('.fits', '.fit', '.fz')):
            sys.stderr.write(f"Warning: {f} does not appear to be a FITS file\n")

    pid = os.getpid()
    orig_cwd = os.getcwd()
    output_file = output_file_arg if os.path.isabs(output_file_arg) else os.path.abspath(os.path.join(orig_cwd, output_file_arg))
    out_root = os.path.dirname(output_file) or "."
    os.makedirs(out_root, exist_ok=True)
    if not os.access(out_root, os.W_OK):
        raise RuntimeError(f"Output directory is not writable: {out_root}")

    if os.path.exists(output_file):
        if args.overwrite:
            print(f"Overwriting existing output file: {output_file}")
            os.remove(output_file)
        else:
            raise RuntimeError(f"Output file exists: {output_file} (use --overwrite to replace)")

    if not MONTAGEPY_AVAILABLE:
        if not args.allow_reference_fallback:
            raise RuntimeError(
                "MontagePy is required to build a reprojected mosaic. Install MontagePy or "
                "pass --allow-reference-fallback to use the first calibrated WCS FITS only."
            )
        from astropy.io import fits
        with fits.open(input_files[0], memmap=False) as hdul:
            hdul.writeto(output_file, overwrite=True)
        print(f"MontagePy unavailable; using source-reference FITS: {input_files[0]}")
        print(f"Source reference created at {output_file}")
        return

    work_dir = tempfile.mkdtemp(dir=out_root, prefix='.mosaic_', suffix=f'_LEMON_{pid}_work')

    cpu_workers = max(1, int(args.ncores))
    io_workers = max(1, int(args.io_workers))
    chunksize = max(1, int(args.chunksize))

    try:
        os.chdir(work_dir)
        print(f"Working directory: {work_dir}")

        input_dir = tempfile.mkdtemp(dir=work_dir, suffix='_input')
        for i, path in enumerate(input_files):
            source = os.path.abspath(path)
            basename = os.path.basename(path)
            link_name = os.path.join(input_dir, f"{i:06d}_{basename}")
            os.symlink(source, link_name)

        raw_tbl     = os.path.join(work_dir, 'rimages.tbl')
        header_file = os.path.join(work_dir, 'region.hdr')
        pimages_tbl = os.path.join(work_dir, 'pimages.tbl')

        rtn = mImgtbl(input_dir, raw_tbl)
        print(f"mImgtbl return: {rtn}")
        if rtn.get('status') != '0':
            raise RuntimeError(f"mImgtbl failed: {rtn}")
        if int(rtn.get('count', 0)) == 0:
            raise RuntimeError("No valid images found")

        n_input = int(rtn['count'])
        print(f"Found {n_input} valid images, {rtn.get('badwcs', 0)} bad WCS, {rtn.get('badfits', 0)} bad FITS")

        rtn_hdr = mMakeHdr(raw_tbl, header_file)
        print(f"mMakeHdr return: {rtn_hdr}")
        if rtn_hdr.get('status') != '0':
            raise RuntimeError(f"mMakeHdr failed: {rtn_hdr}")

        nax1 = int(rtn_hdr['naxis1']); nax2 = int(rtn_hdr['naxis2'])
        free_bytes = shutil.disk_usage(work_dir).free
        need_bytes = estimate_space_bytes(n_input, nax1, nax2, safety_factor=2.0)
        if free_bytes < need_bytes:
            need_gb = need_bytes / (1024**3); free_gb = free_bytes / (1024**3)
            raise RuntimeError(f"Insufficient free space in {work_dir}: need ~{need_gb:0.1f} GiB, have {free_gb:0.1f} GiB.")

        proj_dir = tempfile.mkdtemp(dir=work_dir, suffix='_projected')
        raw_rows = read_montage_table(raw_tbl)
        input_images = [os.path.join(input_dir, row[-1]) for row in raw_rows]
        print(f"Projection plan: {len(input_images)} images | cpu_workers={cpu_workers} | io_workers={io_workers}")

        with ProcessPoolExecutor(max_workers=max(cpu_workers, io_workers)) as executor:
            # Projection with throttled concurrency
            project = partial(_project_image_task, header_file, proj_dir)
            inflight = set()
            results = []
            proj_progress = _Progress(len(input_images), "Projection")

            def submit_more(start_index):
                i = start_index
                while i < len(input_images) and len(inflight) < io_workers:
                    inflight.add(executor.submit(project, input_images[i]))
                    i += 1
                return i

            idx = submit_more(0)
            while inflight:
                done, inflight = wait(inflight, return_when=FIRST_COMPLETED)  # <-- FIXED
                for fut in done:
                    results.append(fut.result())
                    proj_progress.update(1)
                idx = submit_more(idx)
            proj_progress.finish()

            failed = [(orig, msg) for orig, stat, msg in results if stat != '0']
            if failed:
                print("Failed projections:")
                for orig, msg in failed:
                    print(f"{orig}: {msg}")
                if len(failed) == len(results):
                    raise RuntimeError("All projections failed")
                print(f"{len(failed)} projections failed, continuing with {len(results) - len(failed)} successful ones")

            rtn_p = mImgtbl(proj_dir, pimages_tbl)
            print(f"mImgtbl projected: {rtn_p}")
            if rtn_p.get('status') != '0':
                raise RuntimeError(f"mImgtbl (projected) failed: {rtn_p}")

            validate_imgtbl(pimages_tbl)

            coadd_map = {'mean': 0, 'median': 1, 'count': 2}
            coadd_type = coadd_map[args.combine]

            if not args.background_match:
                rtn_add = mAdd(proj_dir, pimages_tbl, header_file, output_file, coadd=coadd_type)
                print(f"mAdd: {rtn_add}")
                if rtn_add.get('status') != '0':
                    raise RuntimeError(f"mAdd failed: {rtn_add}")
            else:
                diffs_dir = tempfile.mkdtemp(dir=work_dir, suffix='_diffs')
                overlaps_tbl = os.path.join(work_dir, 'overlaps.tbl')
                rtn_ov = mOverlaps(pimages_tbl, overlaps_tbl)
                print(f"mOverlaps: {rtn_ov}")
                if rtn_ov.get('status') != '0':
                    raise RuntimeError(f"mOverlaps failed: {rtn_ov}")

                p_rows = read_montage_table(pimages_tbl)
                max_cntr = max(int(r[0]) for r in p_rows)
                pimages = [''] * (max_cntr + 1)
                for r in p_rows:
                    cntr = int(r[0]); fname = r[-1]
                    path = os.path.join(proj_dir, fname)
                    pimages[cntr] = path
                    if not os.path.exists(path):
                        print(f"Warning: Projected file missing: {path}")

                overlaps = []
                for row in read_montage_table(overlaps_tbl):
                    plus = int(row[1]); minus = int(row[2]); diff_base = row[3]
                    overlaps.append((plus, minus, diff_base))
                print(f"Background-match plan: {len(overlaps)} overlap pairs")

                diff_fit_fn = partial(_diff_fit_task, pimages, diffs_dir, header_file)
                fits_data = []
                diff_progress = _Progress(len(overlaps), "Diff/Fit")
                for i in range(0, len(overlaps), chunksize):
                    batch = overlaps[i:i+chunksize]
                    futs = [executor.submit(diff_fit_fn, o) for o in batch]
                    for fut in as_completed(futs):
                        res = fut.result()
                        diff_progress.update(1)
                        if res is not None:
                            fits_data.append(res)
                diff_progress.finish()

                if len(fits_data) != len(overlaps):
                    print(f"Some diff/fit failed, got {len(fits_data)} / {len(overlaps)}")
                    raise RuntimeError("Some diff/fit operations failed")

                fits_tbl = os.path.join(work_dir, 'fits.tbl')
                with open(fits_tbl, 'w') as f:
                    f.write('\\ No backslash at start\n')
                    f.write('|plus|minus|a|b|c|xmin|ymin|xmax|ymax|xcenter|ycenter|npixel|xrms|yrms|rss|boxx|boxy|boxwidth|boxheight|boxang|\n')
                    for d in fits_data:
                        f.write(
                            f" {d[0]:4d} {d[1]:4d} {d[2]:12.5e} {d[3]:12.5e} {d[4]:12.5e}"
                            f" {d[5]:6.1f} {d[6]:6.1f} {d[7]:6.1f} {d[8]:6.1f}"
                            f" {d[9]:8.3f} {d[10]:8.3f} {d[11]:7d} {d[12]:6.4f}"
                            f" {d[13]:6.4f} {d[14]:10.4f} {d[15]:6.1f} {d[16]:6.1f}"
                            f" {d[17]:6.1f} {d[18]:6.1f} {d[19]:6.1f}\n"
                        )

                corrections_tbl = os.path.join(work_dir, 'corrections.tbl')
                rtn_bg = mBgModel(pimages_tbl, fits_tbl, corrections_tbl)
                print(f"mBgModel: {rtn_bg}")
                if rtn_bg.get('status') != '0':
                    raise RuntimeError(f"mBgModel failed: {rtn_bg}")

                corr_dir = tempfile.mkdtemp(dir=work_dir, suffix='_corrected')
                bg_fn = partial(_bg_correct_task, pimages, corr_dir, corrections_tbl)
                p_indices = [int(r[0]) for r in p_rows]

                inflight = set()
                statuses = []
                bg_progress = _Progress(len(p_indices), "Background")

                def submit_bg(start_index):
                    i = start_index
                    while i < len(p_indices) and len(inflight) < io_workers:
                        inflight.add(executor.submit(bg_fn, p_indices[i]))
                        i += 1
                    return i

                idx = submit_bg(0)
                while inflight:
                    done, inflight = wait(inflight, return_when=FIRST_COMPLETED)  # <-- FIXED
                    for fut in done:
                        statuses.append(fut.result())
                        bg_progress.update(1)
                    idx = submit_bg(idx)
                bg_progress.finish()

                failed_bg = [i for (i, s) in statuses if s != '0']
                if failed_bg:
                    print(f"Failed background corrections for indices: {failed_bg}")
                    raise RuntimeError("Some background corrections failed")

                cimages_tbl = os.path.join(work_dir, 'cimages.tbl')
                rtn_ci = mImgtbl(corr_dir, cimages_tbl)
                print(f"mImgtbl corrected: {rtn_ci}")
                if rtn_ci.get('status') != '0':
                    raise RuntimeError(f"mImgtbl (corrected) failed: {rtn_ci}")

                rtn_final = mAdd(corr_dir, cimages_tbl, header_file, output_file, coadd=coadd_type)
                print(f"Final mAdd: {rtn_final}")
                if rtn_final.get('status') != '0':
                    raise RuntimeError(f"mAdd failed: {rtn_final}")

        print(f"Mosaic created at {output_file}")

    finally:
        keep = os.environ.get("KEEP_MONTAGE_TMP") == "1"
        if keep:
            print(f"Keeping work dir for inspection: {work_dir}")
        else:
            clean_dir(work_dir)

if __name__ == '__main__':
    main()
