# HAT-P-16 diffphot mode report

This report compares the two supported high-level `diffphot` profiles on the
same HAT-P-16 photometry database:

- `--precision-mode`
- `--lemon-mode`

Dataset:

- input photometry DB: `/mnt/uxmal_groups/common_data/photometry/yuzu/HAT-P-16/photometry.db`
- target coordinates of interest: RA `00:38:17.529`, Dec `+42:27:47.06`
- target star auto-selection in the field DB: star `149`

## Commands used

Precision-mode run used for comparison:

```bash
python3 main.py diffphot \
  /mnt/uxmal_groups/common_data/photometry/yuzu/HAT-P-16/photometry.db \
  /mnt/uxmal_groups/common_data/photometry/yuzu/HAT-P-16/light_curve.db \
  --overwrite \
  --precision-mode \
  --precision-min-snr 1000 \
  --detrend-airmass \
  --diagnostics
```

LEMON-like run:

```bash
python3 main.py diffphot \
  /mnt/uxmal_groups/common_data/photometry/yuzu/HAT-P-16/photometry.db \
  /tmp/h16_lemon_mode.db \
  --overwrite \
  --lemon-mode \
  --detrend-airmass \
  --diagnostics
```

## Mode definition

### `--precision-mode`

- complete comparison stars only
- robust scatter metric
- iterative Broeg+05 inverse-sigma ensemble
- extra target and candidate median-SNR gate
- precision acceptance diagnostics written to `diffphot_diagnostics`

### `--lemon-mode`

- complete comparison stars only
- iterative Broeg+05 inverse-sigma ensemble
- upstream-like defaults:
  - `max_cmp=20`
  - `min_cmp=8`
  - `worst_fraction=0.10`
- no extra candidate median-SNR gate

## Side-by-side results

### Key stars

| star_id | role | epochs precision | final_cmp precision | RMS precision (mag) | epochs lemon | final_cmp lemon | RMS lemon (mag) |
|---|---|---:|---:|---:|---:|---:|---:|
| 1 | comparison/control | 518 | 2 | 0.0036047115 | 518 | 20 | 0.0018428879 |
| 2 | comparison/control | 518 | 2 | 0.0019324569 | 518 | 20 | 0.0022246472 |
| 149 | target of interest | 518 | 2 | 0.0041680027 | 518 | 20 | 0.0042119297 |
| 401 | extra usable control | 494 | 3 | 0.0021434661 | 518 | 20 | 0.0018544360 |

### Field-level summaries

| metric | precision-mode (`--precision-min-snr 1000`) | lemon-mode |
|---|---:|---:|
| usable stars | 4 | 667 |
| total comparison stars used | 9 | 13340 |
| average comparison stars per target | 2.2 | 20.0 |
| total differential measurements | 2048 | 154896 |

## Interpretation

What improved under `--lemon-mode`:

- many more stars were retained in the usable field
- control star `1` improved strongly
- star `401` was also stable in the larger ensemble

What did not improve:

- target star `149` remained near `4.2 mmag` RMS in both modes

Conclusion:

- for this dataset, `--lemon-mode` gives a healthier comparison-star field
  overall
- for the target star of interest, the limiting floor is not caused mainly by
  the strict precision SNR gate
- switching mode alone does not reach `~0.001 mag` on star `149`

## Practical recommendation

- use `--lemon-mode` when you want upstream-like LEMON behavior and broader
  comparison pools
- use `--precision-mode` when you need strict auditable gates and want to test
  whether a dataset can satisfy the millimagnitude profile
