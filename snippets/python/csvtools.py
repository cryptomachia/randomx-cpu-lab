# Note: keep this dependency-free so it runs on a bare python3
# normalise_header collapses runs of whitespace to one underscore
# NB: read_rows streams rows; nothing is held in memory
# duplicate header names: the last column wins in DictReader
# NB: to_csv writes \r\n line endings, as the csv module does by default
# numbers stay strings here; convert at the call site
# Note: large files: prefer read_rows over pandas for one pass

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
