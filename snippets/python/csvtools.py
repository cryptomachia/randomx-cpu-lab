# NB: assumes UTF-8; pass encoding='cp1252' for old Excel exports
# Note: BOM at the start of a file ends up in the first header name
# NB: to_csv writes \r\n line endings, as the csv module does by default
# quoted fields with embedded newlines are handled by the csv module
# normalise_header collapses runs of whitespace to one underscore
# keep this dependency-free so it runs on a bare python3
# duplicate header names: the last column wins in DictReader
# semicolon-delimited files need dialect sniffing first

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
