# juicer/main.py — entrypoint that cooperates with external launchers
from __future__ import annotations

import logging
import sys
from typing import Iterable, List, Optional, Tuple

from .app import run_app


def _to_float(s: str) -> float:
    return float(s.strip())


def _parse_hms_to_deg(h: float, m: float, s: float) -> float:
    return (abs(h) + m / 60.0 + s / 3600.0) * 15.0 * (1.0 if h >= 0 else -1.0)


def _parse_dms_to_deg(d: float, m: float, s: float) -> float:
    return (abs(d) + m / 60.0 + s / 3600.0) * (1.0 if d >= 0 else -1.0)


def _parse_sexagesimal_token(tok: str) -> Tuple[float, float, float]:
    t = tok.strip().lower().replace("h", ":").replace("m", ":").replace("s", "")
    parts = [p for p in t.replace("::", ":").split(":") if p != ""]
    if len(parts) == 3:
        return (_to_float(parts[0]), _to_float(parts[1]), _to_float(parts[2]))
    raise ValueError("not single-token sexagesimal")


def _parse_three_tokens(tokens: List[str]) -> Tuple[float, float, float]:
    if len(tokens) < 3:
        raise ValueError("need three tokens")
    return (_to_float(tokens[0]), _to_float(tokens[1]), _to_float(tokens[2]))


def _parse_ra_triplet(tokens: List[str]) -> float:
    try:
        h, m, s = _parse_sexagesimal_token(tokens[0])
        return _parse_hms_to_deg(h, m, s)
    except Exception:
        h, m, s = _parse_three_tokens(tokens)
        return _parse_hms_to_deg(h, m, s)


def _parse_dec_triplet(tokens: List[str]) -> float:
    try:
        d, m, s = _parse_sexagesimal_token(tokens[0])
        return _parse_dms_to_deg(d, m, s)
    except Exception:
        d, m, s = _parse_three_tokens(tokens)
        return _parse_dms_to_deg(d, m, s)


def _extract_star(argv: List[str]) -> Tuple[List[str], Optional[Tuple[float, float]]]:
    """
    Remove --star ... from argv and return (argv_without_star, (ra_deg, dec_deg) or None).
    Supports:
      --star 00:38:17.56 +42:27:47.2
      --star 00 38 17.56 +42 27 47.2
      --star 9.573167 42.463111
    """
    if "--star" not in argv:
        return argv, None

    i = argv.index("--star")
    rest = argv[:i] + argv[i + 1 :]
    following = argv[i + 1 : i + 7]
    if not following:
        return rest, None

    # Case A: RA and Dec each in a single sexagesimal token
    if len(following) >= 2 and (":" in following[0] or any(c in following[0].lower() for c in "hms")):
        ra = _parse_ra_triplet([following[0]])
        dec = _parse_dec_triplet([following[1]])
        return argv[:i] + argv[i + 3 :], (ra, dec)

    # Case B: 6 tokens => H M S  D M S
    if len(following) >= 6:
        try:
            ra = _parse_ra_triplet(following[0:3])
            dec = _parse_dec_triplet(following[3:6])
            return argv[:i] + argv[i + 7 :], (ra, dec)
        except Exception:
            pass

    # Case C: two floats in degrees
    if len(following) >= 2:
        try:
            ra = _to_float(following[0])
            dec = _to_float(following[1])
            return argv[:i] + argv[i + 3 :], (ra, dec)
        except Exception:
            pass

    return rest, None


def main(
    *,
    db_path: Optional[str] = None,
    argv: Optional[Iterable[str]] = None,
    start_radec: Optional[Tuple[float, float]] = None,
    **kwargs,  # tolerate extra keys from external launchers
) -> int:
    """
    Entry point used by external launchers (e.g., yuzu) which call:
        juicer.main.main(**kwargs)
    Accepts optional db_path, argv, start_radec and ignores unknown kwargs.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Build argv list: prefer explicit argv, else kwargs 'args', else sys.argv[1:]
    if argv is None:
        maybe_args = kwargs.get("args")
        args_list = list(maybe_args) if maybe_args is not None else list(sys.argv[1:])
    else:
        args_list = list(argv)

    # Some wrappers include a "juicer" token — ignore it
    if args_list and args_list[0] == "juicer":
        args_list = args_list[1:]

    # If start_radec wasn't provided, try to parse --star from args
    if start_radec is None:
        args_list, start_radec = _extract_star(args_list)

    # Delegate to the app runner; it will also pick a .LEMONdB from remaining args if db_path is None
    return run_app(db_path=db_path, argv=args_list, start_radec=start_radec)


if __name__ == "__main__":
    sys.exit(main(argv=sys.argv[1:]))
