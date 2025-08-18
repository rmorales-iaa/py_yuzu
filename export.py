#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import logging
import sys

import astropy.time
import prettytable

# LEMON modules
import database
import passband
import util
import util.coords

_DESCRIPTION = """
Print the light curve of an object stored in a LEMONdB.

This command takes as input the right ascension and declination of an object,
and finds the one stored in the LEMONdB that's close to these coordinates. It
then prints to standard output the (a) time, (b) differential magnitude and
(c) signal-to-noise ratio of all the points in the light curve of the object
in the specified photometric filter.
"""

logger = logging.getLogger(__name__)

parser = argparse.ArgumentParser(description=_DESCRIPTION)
parser.add_argument(
    "db_path",
    metavar="LEMON_DB",
    type=str,
    help="the LEMON database with the light curves",
)
parser.add_argument(
    "ra",
    metavar="<right ascension>",
    type=float,
    help="Right ascension of the astronomical object, in decimal degrees.",
)
parser.add_argument(
    "dec",
    metavar="<declination>",
    type=float,
    help="Declination of the astronomical object, in decimal degrees.",
)
parser.add_argument(
    "filter",
    metavar="<photometric filter>",
    type=passband.Passband,
    help="The name of the photometric filter.",
)
parser.add_argument(
    "--decimal_places",
    dest="places",
    type=int,
    default=3,
    help="Round floating-point numbers to this many decimal places.",
)
parser.add_argument(
    "--output_file",
    dest="output",
    type=argparse.FileType("w"),
    default=sys.stdout,
    help="File to which to write the light-curve data points.",
)


def main(arguments: list[str] | None = None) -> int:
    if arguments is None:
        arguments = sys.argv[1:]
    args = parser.parse_args(args=arguments)

    logging.basicConfig(level=logging.INFO)

    with database.LEMONdB(args.db_path) as db:
        logger.info("Input coordinates:")
        logger.info("  α: %.6f (%s)", args.ra, util.coords.ra_str(args.ra))
        logger.info("  δ: %.6f (%s)", args.dec, util.coords.dec_str(args.dec))

        star_id, distance = db.star_closest_to_world_coords(args.ra, args.dec)
        ra_star, dec_star = db.get_star(star_id)[2:4]  # (x, y, ra, dec, ...)

        logger.info("")
        logger.info("Selected star:")
        logger.info("  ID: %s", star_id)
        logger.info("  α: %.6f (%s)", ra_star, util.coords.ra_str(ra_star))
        logger.info("  δ: %.6f (%s)", dec_star, util.coords.dec_str(dec_star))
        logger.info("  Distance to input coordinates: %.6f deg", distance)
        logger.info("")

        if args.output == sys.stdout:
            logger.info("Light curve in %r photometric filter:", args.filter)

        star_diff = db.get_light_curve(star_id, args.filter)
        if star_diff is None:
            raise ValueError(f"no light curve for {args.filter!r} photometric filter")

        table = prettytable.PrettyTable()
        table.field_names = ["Date (UTC)", "JD", "Δ Mag", "SNR"]

        def format_float(val: float | None) -> str:
            """Return val as a string rounded to args.places decimals, or '—' if None."""
            if val is None:
                return "—"
            return f"{val:.{args.places}f}"

        for unix_time, magnitude, snr in star_diff:
            jd = astropy.time.Time(unix_time, format="unix").jd
            table.add_row(
                [
                    util.utctime(unix_time, suffix=False),
                    format_float(jd),
                    format_float(magnitude),
                    format_float(snr),
                ]
            )

        args.output.write(str(table))
        args.output.write("\n")

        if args.output is not sys.stdout:
            print(
                f"Wrote light curve in {args.filter!r} photometric filter to {args.output.name!r}."
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
