There is a port of LEMON to Python3 and GTK4 of [LEMON, the differential photometry pipeline](https://github.com/vterron/lemon)

## Millimagnitude differential photometry

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
