#!/usr/bin/env python3
"""Fetch paginated SPARQL SELECT results as CSV for thresholded alignment pairs.

Usage:
    python fetch_alignments.py LEFT_ENDPOINT RIGHT_ENDPOINT LEFT.sparql RIGHT.sparql THRESHOLD PAGE_SIZE ALIGNMENTS [--output DIR]

Queries must contain {{IRI}} (replaced by a safely bracketed <IRI>), and should
contain a stable ORDER BY for reliable OFFSET pagination. Do not include an
outer LIMIT or OFFSET in the query files.
"""

import argparse
import csv
import hashlib
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

IRI_PLACEHOLDER = "{{IRI}}"


def iri_term(iri: str) -> str:
    """Return a SPARQL IRI term, rejecting unsafe or malformed inputs."""
    parsed = urlsplit(iri)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Not an HTTP(S) IRI: {iri!r}")
    if re.search(r'[<>"{}|^`\\\x00-\x20]', iri):
        raise ValueError(f"Invalid character in IRI: {iri!r}")
    return f"<{iri}>"


def read_template(path: Path) -> str:
    template = path.read_text(encoding="utf-8").strip()
    if template.count(IRI_PLACEHOLDER) < 1:
        raise ValueError(f"{path}: missing {IRI_PLACEHOLDER} placeholder")
    # LIMIT and OFFSET in subqueries are allowed; the user should not include
    # an outer LIMIT/OFFSET. This is documented rather than parsed here.
    return template


def alignment_rows(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split()
            if len(fields) != 3:
                raise ValueError(f"{path}:{line_number}: expected LEFT RIGHT SCORE")
            left, right, score_text = fields
            if not (left.startswith("<") and left.endswith(">") and
                    right.startswith("<") and right.endswith(">")):
                raise ValueError(f"{path}:{line_number}: URIs must be enclosed in <...>")
            try:
                score = float(score_text)
            except ValueError as exc:
                raise ValueError(f"{path}:{line_number}: invalid score") from exc
            yield left[1:-1], right[1:-1], score


class EndpointClient:
    def __init__(self, min_interval: float = 1.0):
        self.session = requests.Session()
        self.last_request_start = None
        self.min_interval = min_interval

    def select_page(self, endpoint: str, query: str) -> dict:
        # Rate limit applies globally to requests to either endpoint.
        if self.last_request_start is not None:
            wait = self.min_interval - (time.monotonic() - self.last_request_start)
            if wait > 0:
                time.sleep(wait)
        self.last_request_start = time.monotonic()
        response = self.session.post(
            endpoint,
            data={"query": query},
            headers={"Accept": "application/sparql-results+json"},
            timeout=(15, 180),
        )
        response.raise_for_status()
        result = response.json()
        if "results" not in result or "bindings" not in result["results"]:
            raise ValueError("Expected SPARQL SELECT JSON response")
        return result


def filename_for(iri: str) -> str:
    return hashlib.sha256(iri.encode("utf-8")).hexdigest() + ".csv"


def fetch_iri(client, endpoint, template, iri, destination, page_size):
    if destination.is_file():
        return  # Completed in an earlier alignment or previous script run.

    temporary = destination.with_suffix(".csv.part")
    query_body = template.replace(IRI_PLACEHOLDER, iri_term(iri)).rstrip()
    count = 0
    offset = 0
    columns = None
    try:
        with temporary.open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            while True:
                query = f"{query_body}\nLIMIT {page_size} OFFSET {offset}"
                result = client.select_page(endpoint, query)
                bindings = result["results"]["bindings"]
                page_columns = result.get("head", {}).get("vars", [])
                if columns is None:
                    columns = page_columns
                    if not columns:
                        raise ValueError("SPARQL endpoint did not return SELECT variable names")
                    writer.writerow(columns)
                elif page_columns != columns:
                    raise ValueError(f"SPARQL SELECT columns changed across pages for {iri}")
                for binding in bindings:
                    # Export the lexical value of each bound SPARQL variable.
                    # Unbound OPTIONAL variables become empty CSV cells.
                    writer.writerow([binding.get(column, {}).get("value", "")
                                     for column in columns])
                count += len(bindings)
                print(f"  {iri} | offset={offset} | received={len(bindings)} | total={count}", flush=True)
                if len(bindings) < page_size:
                    break
                offset += page_size
        temporary.replace(destination)  # Only complete downloads are reusable.
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoint_left")
    parser.add_argument("endpoint_right")
    parser.add_argument("sparql_left", type=Path)
    parser.add_argument("sparql_right", type=Path)
    parser.add_argument("threshold", type=float)
    parser.add_argument("offset", type=int, help="Pagination step / LIMIT (e.g. 1000)")
    parser.add_argument("alignment_file", type=Path)
    parser.add_argument("--output", type=Path, default=Path("alignment_results"))
    args = parser.parse_args()

    if args.offset <= 0:
        parser.error("offset must be greater than zero")
    left_query = read_template(args.sparql_left)
    right_query = read_template(args.sparql_right)

    left_dir = args.output / "left"
    right_dir = args.output / "right"
    left_dir.mkdir(parents=True, exist_ok=True)
    right_dir.mkdir(parents=True, exist_ok=True)
    manifest = args.output / "pairs.csv"
    client = EndpointClient(min_interval=1.0)
    selected = 0

    # The manifest can be recreated while completed per-URI downloads are reused.
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["left_iri", "right_iri", "score", "left_file", "right_file"])
        for left_iri, right_iri, score in alignment_rows(args.alignment_file):
            if score < args.threshold:
                continue
            selected += 1
            left_file = left_dir / filename_for(left_iri)
            right_file = right_dir / filename_for(right_iri)
            print(f"Pair {selected}: {left_iri} <-> {right_iri} ({score:.6f})", flush=True)
            fetch_iri(client, args.endpoint_left, left_query, left_iri, left_file, args.offset)
            fetch_iri(client, args.endpoint_right, right_query, right_iri, right_file, args.offset)
            writer.writerow([left_iri, right_iri, f"{score:.6f}",
                             str(left_file.relative_to(args.output)),
                             str(right_file.relative_to(args.output))])
            handle.flush()
    print(f"Done: {selected} alignment pairs; manifest: {manifest}")


if __name__ == "__main__":
    main()
