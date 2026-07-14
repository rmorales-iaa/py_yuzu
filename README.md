There is a port of LEMON to Python3 and GTK4 of [LEMON, the differential photometry pipeline](https://github.com/vterron/lemon)

## Pipeline execution

Run complete pipeline (default):

```bash
./run_yuzu.bash
```

Run exactly one step:

```bash
./run_yuzu.bash mosaic
./run_yuzu.bash photometry /data/fits HAT-P-16
./run_yuzu.bash diffphot
./run_yuzu.bash juicer
```

Available steps: `mosaic`, `photometry`, `diffphot`, and `juicer`. A selected
step expects output from its prerequisite steps in the object output directory.
`--object-pos "RA DEC"` is passed to photometry and Juicer. Photometry keeps
all SExtractor sources for comparison stars and adds target only if absent.

## Millimagnitude differential photometry

`diffphot` now supports two high-level comparison-star profiles:

- `--precision-mode`
  - strict, high-SNR, complete-epoch, robust Broeg+05 ensemble
  - intended for auditable millimagnitude products
- `--lemon-mode`
  - upstream-LEMON-like complete-star Broeg+05 ensemble
  - no extra candidate SNR gate
  - intended for classic LEMON behavior and broader field usage

For precision products, use the required Broeg et al. (2005) artificial
comparison star profile:

```bash
yuzu diffphot photometry.db light_curve.db --precision-mode --detrend-airmass --diagnostics
```

`--precision-mode` enforces complete-epoch, robust, iterative Broeg+05
synthetic comparison stars with inverse-sigma stability weights. It records
comparison membership and weights, predicted noise, observed RMS, and an
auditable per-curve acceptance result in `diffphot_diagnostics`. Default gate:
at least 20 epochs, median SNR >= 1100, RMS <= 0.001 mag, and
observed/predicted noise <= 1.5. Tune gates only with a documented dataset.

For upstream-style LEMON behavior, use:

```bash
yuzu diffphot photometry.db light_curve.db --lemon-mode --detrend-airmass --diagnostics
```

`--lemon-mode` applies these upstream-like defaults unless you override them:

- complete comparison stars only
- Broeg+05 iterative inverse-sigma weighting
- `--max-cmp 20`
- `--min-cmp 8`
- `--worst-fraction 0.10`
- no extra candidate median-SNR gate

In the HAT-P-16 validation dataset, `--lemon-mode` improved some control-star
RMS values versus strict precision mode, but did not materially improve the
target star at RA `00:38:17.529`, Dec `+42:27:47.06`. See
[DIFFPHOT_MODE_REPORT_HAT-P-16.md](/mnt/uxmal_groups/common_data/apps/py_yuzu/DIFFPHOT_MODE_REPORT_HAT-P-16.md)
for the measured side-by-side results.
