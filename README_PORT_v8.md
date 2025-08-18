# v8 — Most aggressive automated sweep

Applied (bold transforms):
- `os.path.join(...)` -> `Path(...) / ...` with a custom comma-splitter that respects nesting/quotes.
- I/O shortcuts: `open(...).read()` / `open(...,'rb').read()` / `.write(...)` -> Path-based helpers.
- Percent-format tuples up to 4 items -> f-strings (supports %s/%d/%f, respects '%%').
- `.format(name=expr, ...)` -> f-strings by mapping placeholders to expressions.

Review recommended: these are intentionally aggressive; scan AGGRESSIVE_DIFFS.json,
run v6 `smoke_tests.py` adjusted to point at v8, and try your typical workflows.
