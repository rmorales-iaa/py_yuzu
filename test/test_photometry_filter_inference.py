#!/usr/bin/env python3
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import photometry


class PhotometryFilterInferenceTest(unittest.TestCase):
    def setUp(self):
        self.options = SimpleNamespace(filterk="FILTER")

    def test_sources_image_infers_unique_filter_from_science_inputs(self):
        by_path = {
            "/tmp/stacked.fits": RuntimeError("missing FILTER"),
            "a.fits": "R",
            "b.fits": "R",
        }

        def fake_fits(path):
            value = by_path[path]
            if isinstance(value, Exception):
                return SimpleNamespace(path=path, pfilter=mock.Mock(side_effect=value))
            return SimpleNamespace(path=path, pfilter=mock.Mock(return_value=value))

        with mock.patch.object(photometry.fitsimage, "FITSImage", side_effect=fake_fits):
            sources_img = photometry.fitsimage.FITSImage("/tmp/stacked.fits")
            result = photometry._infer_sources_image_filter(sources_img, ["a.fits", "b.fits"], self.options)

        self.assertEqual(result, "R")

    def test_sources_image_mixed_filters_falls_back_to_unknown(self):
        by_path = {
            "/tmp/stacked.fits": RuntimeError("missing FILTER"),
            "a.fits": "R",
            "b.fits": "V",
        }

        def fake_fits(path):
            value = by_path[path]
            if isinstance(value, Exception):
                return SimpleNamespace(path=path, pfilter=mock.Mock(side_effect=value))
            return SimpleNamespace(path=path, pfilter=mock.Mock(return_value=value))

        with mock.patch.object(photometry.fitsimage, "FITSImage", side_effect=fake_fits):
            sources_img = photometry.fitsimage.FITSImage("/tmp/stacked.fits")
            result = photometry._infer_sources_image_filter(sources_img, ["a.fits", "b.fits"], self.options)

        self.assertEqual(result, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
