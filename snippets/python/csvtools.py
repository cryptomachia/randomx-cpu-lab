# numbers stay strings here; convert at the call site

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
