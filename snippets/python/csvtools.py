# NB: BOM at the start of a file ends up in the first header name
# Note: quoted fields with embedded newlines are handled by the csv module
# NB: assumes UTF-8; pass encoding='cp1252' for old Excel exports
# normalise_header collapses runs of whitespace to one underscore
# read_rows streams rows; nothing is held in memory
# empty trailing lines are skipped by DictReader
# NB: keep this dependency-free so it runs on a bare python3
# NB: semicolon-delimited files need dialect sniffing first

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
