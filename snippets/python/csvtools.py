# quoted fields with embedded newlines are handled by the csv module
# normalise_header collapses runs of whitespace to one underscore
# semicolon-delimited files need dialect sniffing first
# numbers stay strings here; convert at the call site
# Note: large files: prefer read_rows over pandas for one pass
# empty trailing lines are skipped by DictReader

"""Small helpers for messy CSV exports."""
import csv
import io


def read_rows(path, encoding="utf-8"):
    with open(path, newline="", encoding=encoding) as fh:
        yield from csv.DictReader(fh)


def normalise_header(name):
    return "_".join(name.strip().lower().split())


def to_csv(rows, fieldnames):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()
