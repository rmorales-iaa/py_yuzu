#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np

import diffphot


class DiffPhotScienceTest(unittest.TestCase):
    def _make_input_db(self, path: Path) -> None:
        con = sqlite3.connect(path)
        try:
            con.executescript(
                """
                CREATE TABLE photometric_filters (
                    id INTEGER PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL
                );
                CREATE TABLE stars (
                    id INTEGER PRIMARY KEY,
                    x REAL NOT NULL,
                    y REAL NOT NULL,
                    ra REAL NOT NULL,
                    dec REAL NOT NULL,
                    epoch REAL NOT NULL,
                    pm_ra REAL,
                    pm_dec REAL,
                    imag REAL NOT NULL
                );
                CREATE TABLE images (
                    id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL,
                    filter_id INTEGER,
                    unix_time REAL,
                    object TEXT,
                    airmass REAL,
                    gain REAL,
                    ra REAL NOT NULL,
                    dec REAL NOT NULL,
                    sources INTEGER NOT NULL,
                    UNIQUE (filter_id, unix_time)
                );
                CREATE TABLE photometry (
                    id INTEGER PRIMARY KEY,
                    star_id INTEGER NOT NULL,
                    image_id INTEGER NOT NULL,
                    magnitude REAL NOT NULL,
                    snr REAL NOT NULL,
                    UNIQUE (star_id, image_id)
                );
                """
            )
            con.execute("INSERT INTO photometric_filters(id, name) VALUES (1, 'R')")
            stars = [
                (1, 100.0, 100.0, 10.0),
                (2, 115.0, 105.0, 10.1),
                (3, 130.0, 110.0, 10.2),
                (4, 140.0, 115.0, 13.5),
                (5, 900.0, 900.0, 10.3),
            ]
            con.executemany(
                "INSERT INTO stars(id, x, y, ra, dec, epoch, pm_ra, pm_dec, imag) "
                "VALUES (?, ?, ?, 1.0, 2.0, 2000.0, NULL, NULL, ?)",
                stars,
            )
            airmasses = [1.0, 1.1, 1.2, 1.3, 1.4, 1.5]
            for idx, airmass in enumerate(airmasses, start=1):
                con.execute(
                    "INSERT INTO images(id, path, filter_id, unix_time, object, airmass, gain, ra, dec, sources) "
                    "VALUES (?, ?, 1, ?, 'test', ?, 1.0, 1.0, 2.0, 5)",
                    (idx, f"img_{idx}.fits", float(idx), airmass),
                )

            transparency = [0.00, 0.02, -0.01, 0.015, -0.005, 0.01]
            offsets = {
                1: [0.000, 0.001, -0.001, 0.000, 0.001, -0.001],
                2: [0.001, 0.000, 0.001, -0.001, 0.000, 0.001],
                3: [-0.001, 0.001, 0.000, 0.001, -0.001, 0.000],
                4: [0.000, 0.002, -0.002, 0.000, 0.002, -0.002],
                5: [0.000, -0.001, 0.001, 0.000, -0.001, 0.001],
            }
            row_id = 1
            for star_id, _x, _y, imag in stars:
                for image_id, sky in enumerate(transparency, start=1):
                    mag = imag + sky + offsets[star_id][image_id - 1]
                    con.execute(
                        "INSERT INTO photometry(id, star_id, image_id, magnitude, snr) "
                        "VALUES (?, ?, ?, ?, 500.0)",
                        (row_id, star_id, image_id, mag),
                    )
                    row_id += 1
            con.commit()
        finally:
            con.close()

    def test_weight_modes(self):
        sigmas = np.asarray([1.0, 2.0], dtype=float)

        inv_sigma = diffphot._normalize_weights(
            np.asarray([diffphot._sigma_weight(x, "inverse-sigma") for x in sigmas])
        )
        inv_var = diffphot._normalize_weights(
            np.asarray([diffphot._sigma_weight(x, "inverse-variance") for x in sigmas])
        )

        np.testing.assert_allclose(inv_sigma, [2.0 / 3.0, 1.0 / 3.0])
        np.testing.assert_allclose(inv_var, [0.8, 0.2])

    def test_candidate_quality_filters_snr_and_sigma(self):
        matrix = np.asarray(
            [
                [10.000, 10.002, 9.999, 10.001],
                [10.001, 10.001, 10.000, 10.002],
                [10.000, 10.400, 9.600, 10.300],
                [10.000, 10.001, 10.002, 10.001],
            ],
            dtype=float,
        )
        cids = [1, 2, 3, 4]
        points = {
            1: [(1.0, 10.0, 200.0), (2.0, 10.0, 180.0)],
            2: [(1.0, 10.0, 200.0), (2.0, 10.0, 180.0)],
            3: [(1.0, 10.0, 200.0), (2.0, 10.0, 180.0)],
            4: [(1.0, 10.0, 10.0), (2.0, 10.0, 12.0)],
        }
        star_info = {
            99: (100.0, 100.0, 10.0),
            1: (110.0, 100.0, 10.1),
            2: (120.0, 100.0, 10.2),
            3: (130.0, 100.0, 10.3),
            4: (140.0, 100.0, 10.4),
        }

        filtered, kept_ids, _sig0, stats = diffphot._quality_filter_candidates(
            99,
            matrix,
            cids,
            points,
            star_info,
            robust=False,
            min_epoch_fraction=1.0,
            min_candidate_snr=50.0,
            max_candidate_sigma=0.2,
            max_candidate_mag_diff=float("inf"),
            max_candidate_distance=float("inf"),
        )

        self.assertEqual(kept_ids, [1, 2])
        self.assertEqual(filtered.shape[0], 2)
        self.assertEqual(stats["rejected_sigma"], 1)
        self.assertEqual(stats["rejected_snr"], 1)

    def test_candidate_quality_filters_magnitude_and_distance(self):
        matrix = np.asarray(
            [
                [10.000, 10.001, 10.000],
                [10.000, 10.001, 10.000],
                [10.000, 10.001, 10.000],
            ],
            dtype=float,
        )
        cids = [1, 2, 3]
        points = {sid: [(1.0, 10.0, 200.0), (2.0, 10.0, 180.0)] for sid in [1, 2, 3]}
        star_info = {
            99: (100.0, 100.0, 10.0),
            1: (105.0, 100.0, 10.1),
            2: (106.0, 100.0, 13.0),
            3: (500.0, 100.0, 10.2),
        }

        _filtered, kept_ids, _sig0, stats = diffphot._quality_filter_candidates(
            99,
            matrix,
            cids,
            points,
            star_info,
            robust=False,
            min_epoch_fraction=1.0,
            min_candidate_snr=50.0,
            max_candidate_sigma=float("inf"),
            max_candidate_mag_diff=0.5,
            max_candidate_distance=50.0,
        )

        self.assertEqual(kept_ids, [1])
        self.assertEqual(stats["rejected_mag_diff"], 1)
        self.assertEqual(stats["rejected_distance"], 1)

    def test_predicted_diff_sigma_uses_target_and_synthetic_comparison(self):
        points = {
            99: [(1.0, 10.0, 100.0), (2.0, 10.0, 100.0)],
            1: [(1.0, 10.0, 200.0), (2.0, 10.0, 200.0)],
            2: [(1.0, 10.0, 200.0), (2.0, 10.0, 200.0)],
        }
        sigma = diffphot._predicted_diff_sigma(99, [1, 2], [0.5, 0.5], points)
        target_err = 1.0857362047581296 / 100.0
        comp_err = ((0.5 * 1.0857362047581296 / 200.0) ** 2 * 2) ** 0.5

        self.assertAlmostEqual(sigma, (target_err * target_err + comp_err * comp_err) ** 0.5)

    def test_broeg_iterative_weights_prefer_stable_stars(self):
        matrix = np.asarray(
            [
                [10.000, 10.002, 9.999, 10.001, 10.000],
                [10.001, 10.001, 10.000, 10.002, 10.001],
                [10.000, 10.250, 9.750, 10.300, 9.700],
            ],
            dtype=float,
        )

        weights, sigmas, trace = diffphot._broeg_iterative_weights(
            matrix,
            robust=False,
            weight_mode="inverse-sigma",
            convergence=1e-4,
            max_iters=20,
            want_trace=True,
        )

        self.assertAlmostEqual(float(weights.sum()), 1.0)
        self.assertGreater(weights[0], weights[2])
        self.assertGreater(weights[1], weights[2])
        self.assertLess(sigmas[0], sigmas[2])
        self.assertTrue(trace)

    def test_airmass_detrend_removes_linear_term(self):
        points = [
            (1.0, 0.11, 100.0),
            (2.0, 0.16, 100.0),
            (3.0, 0.21, 100.0),
            (4.0, 0.26, 100.0),
            (5.0, 0.31, 100.0),
        ]
        airmass = {1.0: 1.0, 2.0: 1.5, 3.0: 2.0, 4.0: 2.5, 5.0: 3.0}

        corrected, info = diffphot._robust_linear_airmass_detrend(points, airmass)
        mags = [p[1] for p in corrected]

        self.assertTrue(info["enabled"])
        self.assertAlmostEqual(max(mags) - min(mags), 0.0, places=12)

    def test_precision_assessment_requires_quality_and_noise_floor(self):
        diag = {
            "points": 20,
            "median_snr": 1500.0,
            "rms": 0.0008,
            "excess_noise_ratio": 1.2,
        }
        self.assertEqual(
            diffphot._precision_assessment(diag, 0.001, 1100.0, 20, 1.5),
            (1, 1, "pass"),
        )
        diag["rms"] = 0.0011
        self.assertEqual(
            diffphot._precision_assessment(diag, 0.001, 1100.0, 20, 1.5),
            (1, 0, "rms"),
        )
        diag["median_snr"] = 1000.0
        self.assertEqual(
            diffphot._precision_assessment(diag, 0.001, 1100.0, 20, 1.5),
            (0, 0, "target_snr"),
        )

    def test_precision_mode_forces_strict_auditable_synthetic_star(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_db = Path(tmp) / "input.LEMONdB"
            output_db = Path(tmp) / "output.LEMONdB"
            self._make_input_db(input_db)

            argv = [
                str(input_db), str(output_db), "--precision-mode",
                "--precision-min-snr", "100", "--precision-min-epochs", "5",
                "--cores", "1", "--no-progress",
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                rc = diffphot.main(argv)
            self.assertEqual(rc, 0)

            con = sqlite3.connect(output_db)
            try:
                metadata = dict(con.execute(
                    "SELECT key, value FROM metadata WHERE key LIKE 'diffphot_%'"
                ).fetchall())
                self.assertEqual(str(metadata["diffphot_broeg05_strict"]), "1")
                self.assertEqual(metadata["diffphot_weight_mode"], "inverse-sigma")
                self.assertEqual(str(metadata["diffphot_precision_mode"]), "1")
                cols = {row[1] for row in con.execute("PRAGMA table_info(diffphot_diagnostics)")}
                self.assertTrue({"precision_eligible", "precision_pass", "precision_failure"} <= cols)
                self.assertGreater(con.execute("SELECT COUNT(*) FROM cmp_stars").fetchone()[0], 0)
                self.assertGreater(con.execute("SELECT COUNT(*) FROM light_curves").fetchone()[0], 0)
                self.assertGreater(
                    con.execute(
                        "SELECT COUNT(*) FROM diffphot_diagnostics WHERE points > 0"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    con.execute(
                        "SELECT COUNT(*) FROM cmp_stars WHERE star_id = cstar_id"
                    ).fetchone()[0],
                    0,
                )
                sums = con.execute(
                    "SELECT star_id, SUM(weight) FROM cmp_stars GROUP BY star_id"
                ).fetchall()
                for _star_id, total_weight in sums:
                    self.assertAlmostEqual(total_weight, 1.0, places=12)
            finally:
                con.close()

    def test_strict_cli_writes_auditable_science_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_db = Path(tmp) / "input.LEMONdB"
            output_db = Path(tmp) / "output.LEMONdB"
            self._make_input_db(input_db)

            argv = [
                str(input_db),
                str(output_db),
                "--broeg05-strict",
                "--min-cmp", "2",
                "--min-snr", "50",
                "--min-candidate-snr", "50",
                "--max-candidate-mag-diff", "1.0",
                "--max-candidate-distance", "100",
                "--diagnostics",
                "--cores", "1",
                "--no-progress",
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                rc = diffphot.main(argv)
            self.assertEqual(rc, 0)

            con = sqlite3.connect(output_db)
            try:
                metadata = dict(con.execute(
                    "SELECT key, value FROM metadata WHERE key LIKE 'diffphot_%'"
                ).fetchall())
                self.assertEqual(str(metadata["diffphot_broeg05_strict"]), "1")
                self.assertEqual(metadata["diffphot_weight_mode"], "inverse-sigma")
                self.assertEqual(str(metadata["diffphot_max_candidate_mag_diff"]), "1.0")
                self.assertEqual(str(metadata["diffphot_max_candidate_distance"]), "100.0")

                n_cmp = con.execute("SELECT COUNT(*) FROM cmp_stars").fetchone()[0]
                n_curves = con.execute("SELECT COUNT(*) FROM light_curves").fetchone()[0]
                n_diag = con.execute("SELECT COUNT(*) FROM diffphot_diagnostics").fetchone()[0]
                self.assertGreater(n_cmp, 0)
                self.assertGreater(n_curves, 0)
                self.assertEqual(n_diag, 5)

                rejected_mag, rejected_dist = con.execute(
                    "SELECT SUM(rejected_mag_diff), SUM(rejected_distance) FROM diffphot_diagnostics"
                ).fetchone()
                self.assertGreater(rejected_mag, 0)
                self.assertGreater(rejected_dist, 0)

                noise_rows = con.execute(
                    "SELECT COUNT(*) FROM diffphot_diagnostics "
                    "WHERE predicted_sigma IS NOT NULL AND excess_noise_ratio IS NOT NULL"
                ).fetchone()[0]
                self.assertGreater(noise_rows, 0)
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
