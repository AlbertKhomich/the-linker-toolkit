#!/usr/bin/env python3

import argparse
import hashlib
import heapq
import os
import re
import shutil
import sys
import time
import unicodedata
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Iterator, Optional, Tuple

from rapidfuzz import fuzz


RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
DEFAULT_LABEL_PREDICATE = "http://www.w3.org/2000/01/rdf-schema#label"


# ----------------------------------------------------------------------
# Logging / helpers
# ----------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def fmt_gib(nbytes: int) -> str:
    return f"{nbytes / (1024 ** 3):.2f} GiB"


def ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


# ----------------------------------------------------------------------
# N-Triples parsing
# ----------------------------------------------------------------------

def parse_uri(token: str) -> Optional[str]:
    token = token.strip()
    if token.startswith("<") and token.endswith(">"):
        return token[1:-1]
    return None


def decode_nt_literal(token: str) -> Optional[str]:
    """
    Return only the lexical form of an N-Triples literal.

    Examples:
        "Hello"@en                -> Hello
        "Hello"                   -> Hello
        "foo\\nbar"               -> foo<newline>bar
        "1"^^<...#integer>        -> 1

    Language/datatype metadata is intentionally ignored for label matching.
    """
    token = token.strip()

    if not token.startswith('"'):
        return None

    chars = []
    i = 1

    while i < len(token):
        c = token[i]

        if c == '"':
            return "".join(chars)

        if c != "\\":
            chars.append(c)
            i += 1
            continue

        i += 1
        if i >= len(token):
            return None

        esc = token[i]

        mapping = {
            "t": "\t",
            "b": "\b",
            "n": "\n",
            "r": "\r",
            "f": "\f",
            '"': '"',
            "'": "'",
            "\\": "\\",
        }

        if esc in mapping:
            chars.append(mapping[esc])
            i += 1
            continue

        if esc == "u":
            hexpart = token[i + 1:i + 5]
            if len(hexpart) != 4:
                return None
            try:
                chars.append(chr(int(hexpart, 16)))
            except ValueError:
                return None
            i += 5
            continue

        if esc == "U":
            hexpart = token[i + 1:i + 9]
            if len(hexpart) != 8:
                return None
            try:
                chars.append(chr(int(hexpart, 16)))
            except ValueError:
                return None
            i += 9
            continue

        chars.append(esc)
        i += 1

    return None


def parse_nt_line(line: str) -> Optional[Tuple[str, str, str]]:
    """
    Lightweight parser for URI-subject N-Triples.

    Returns:
        subject_uri, predicate_uri, raw_object_token

    Blank-node subjects are ignored.
    """
    line = line.strip()

    if not line or line.startswith("#") or not line.endswith("."):
        return None

    line = line[:-1].strip()

    if not line.startswith("<"):
        return None

    s_end = line.find("> ")
    if s_end == -1:
        return None

    subject = parse_uri(line[:s_end + 1])
    if subject is None:
        return None

    rest = line[s_end + 2:].lstrip()

    if not rest.startswith("<"):
        return None

    p_end = rest.find("> ")
    if p_end == -1:
        return None

    predicate = parse_uri(rest[:p_end + 1])
    if predicate is None:
        return None

    obj = rest[p_end + 2:].strip()
    if not obj:
        return None

    return subject, predicate, obj


# ----------------------------------------------------------------------
# Label normalization
# ----------------------------------------------------------------------

_whitespace = re.compile(r"\s+")


def normalize_label(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.casefold()
    value = _whitespace.sub(" ", value).strip()
    return value


# ----------------------------------------------------------------------
# Stable compact entity IDs
# ----------------------------------------------------------------------

def entity_id(side: str, uri: str) -> str:
    """
    128-bit stable identifier.

    Include graph side so identical URI strings from different inputs cannot
    accidentally become the same internal entity.
    """
    return hashlib.blake2b(
        f"{side}\0{uri}".encode("utf-8"),
        digest_size=16,
    ).hexdigest()


# ----------------------------------------------------------------------
# Triple-pattern selection
# ----------------------------------------------------------------------

def parse_kg_pattern(pattern: str) -> tuple[str, str, Optional[str], str]:
    """Parse `s P URI`, `s P ?`, `? P o`, or `URI P o`.

    `s` / `o` marks exactly one position to EXTRACT. `?` is an
    unselected wildcard; the other position may instead be a fixed URI.
    URI strings may optionally be enclosed in angle brackets.
    """
    parts = pattern.split()
    if len(parts) != 3:
        raise ValueError("Expected exactly three tokens: SUBJECT PREDICATE OBJECT")
    subject, predicate, obj = parts
    if subject == "s" and obj in ("o", "s"):
        raise ValueError("Use '?' for the unselected position: 's P ?' or '? P o'")
    if obj == "o" and subject in ("s", "o"):
        raise ValueError("Use '?' for the unselected position: 's P ?' or '? P o'")
    if subject not in ("s", "?") and obj != "o":
        raise ValueError("Exactly one selected position ('s' or 'o') is required")
    if subject != "s" and obj != "o":
        raise ValueError("Exactly one selected position ('s' or 'o') is required")
    if predicate in ("s", "o", "?"):
        raise ValueError("Predicate must be a URI")

    def uri(token: str) -> str:
        value = token[1:-1] if token.startswith("<") and token.endswith(">") else token
        if not value.startswith(("http://", "https://")) or any(x in value for x in '<>'):
            raise ValueError(f"Expected an absolute HTTP(S) URI, got: {token!r}")
        return value

    predicate = uri(predicate)
    if subject == "s":
        return ("subject", predicate, None if obj == "?" else uri(obj), "object")
    return ("object", predicate, None if subject == "?" else uri(subject), "subject")


# ----------------------------------------------------------------------
# Stage 1: stream each graph once
# ----------------------------------------------------------------------

def extract_graph(
    nt_path: str,
    side: str,
    selection: tuple[str, str, Optional[str], str],
    label_predicate: str,
    types_path: str,
    labels_path: str,
    mapping_path: str,
    progress_every_s: int = 10,
) -> None:
    """
    Single sequential pass over one graph.

    Outputs:
        types.tsv
            entity_id

        labels.tsv
            entity_id<TAB>normalized_label

        mapping.tsv
            entity_id<TAB>URI

    Selection/label triple order does not matter. They are joined later.
    For object selection the chosen URI may never appear as a subject
    of a selection triple, so its mapping is written independently.
    """
    total_bytes = os.path.getsize(nt_path)
    read_bytes = 0
    started = time.time()
    last_report = started

    # Only a bounded duplicate-suppression cache. Duplicates are harmless
    # because mapping files are externally sorted/deduplicated later.
    seen_mapping = set()
    mapping_cache_limit = 1_000_000

    ensure_parent(types_path)
    ensure_parent(labels_path)
    ensure_parent(mapping_path)

    with (
        open(nt_path, "rb") as src,
        open(types_path, "w", encoding="utf-8", buffering=1024 * 1024) as types_out,
        open(labels_path, "w", encoding="utf-8", buffering=1024 * 1024) as labels_out,
        open(mapping_path, "w", encoding="utf-8", buffering=1024 * 1024) as mapping_out,
    ):
        for raw in src:
            read_bytes += len(raw)

            parsed = parse_nt_line(raw.decode("utf-8", "replace"))
            if parsed is None:
                continue

            subject, predicate, obj = parsed
            eid = None

            select_mode, select_predicate, fixed_uri, _fixed_side = selection
            if predicate == select_predicate:
                obj_uri = parse_uri(obj)
                if select_mode == "subject":
                    if fixed_uri is None or obj_uri == fixed_uri:
                        chosen_uri = subject
                    else:
                        chosen_uri = None
                else:
                    if (fixed_uri is None or subject == fixed_uri) and obj_uri is not None:
                        chosen_uri = obj_uri
                    else:
                        chosen_uri = None

                if chosen_uri is not None:
                    eid = entity_id(side, chosen_uri)
                    types_out.write(f"{eid}\n")
                    if eid not in seen_mapping:
                        mapping_out.write(f"{eid}\t{chosen_uri}\n")
                        seen_mapping.add(eid)
                        if len(seen_mapping) >= mapping_cache_limit:
                            seen_mapping.clear()

            # Independent `if`: one predicate can serve as both selector
            # and label predicate; selection must not discard labels.
            if predicate == label_predicate:
                literal = decode_nt_literal(obj)
                if literal is not None:
                    normalized = normalize_label(literal)
                    normalized = (
                        normalized
                        .replace("\t", " ")
                        .replace("\n", " ")
                        .replace("\r", " ")
                    )
                    if normalized:
                        label_eid = entity_id(side, subject)
                        labels_out.write(f"{label_eid}\t{normalized}\n")
                        if label_eid not in seen_mapping:
                            mapping_out.write(f"{label_eid}\t{subject}\n")
                            seen_mapping.add(label_eid)
                            if len(seen_mapping) >= mapping_cache_limit:
                                seen_mapping.clear()

            now = time.time()
            if now - last_report >= progress_every_s:
                elapsed = now - started
                pct = (read_bytes / total_bytes * 100.0) if total_bytes else 0.0
                mib_s = (read_bytes / (1024 ** 2)) / elapsed if elapsed > 0 else 0.0

                log(
                    f"[{side}] extract {pct:6.2f}% | "
                    f"{fmt_gib(read_bytes)} / {fmt_gib(total_bytes)} | "
                    f"{mib_s:,.1f} MiB/s"
                )
                last_report = now

    log(f"[{side}] extraction complete: {fmt_gib(read_bytes)}")


# ----------------------------------------------------------------------
# External sorting
# ----------------------------------------------------------------------

def external_sort(
    src_path: str,
    dst_path: str,
    tmp_dir: str,
    chunk_bytes: int,
    unique: bool = False,
) -> None:
    """
    External lexicographical sort with bounded RAM.

    The current intermediate formats deliberately put the sort key first.
    """
    Path(tmp_dir).mkdir(parents=True, exist_ok=True)
    ensure_parent(dst_path)

    chunk_paths = []
    chunk = []
    estimated_bytes = 0
    chunk_number = 0

    def flush_chunk() -> None:
        nonlocal chunk, estimated_bytes, chunk_number

        if not chunk:
            return

        chunk.sort()

        if unique:
            deduped = []
            previous = None
            for line in chunk:
                if line != previous:
                    deduped.append(line)
                    previous = line
            to_write = deduped
        else:
            to_write = chunk

        p = os.path.join(
            tmp_dir,
            f"sort-{os.getpid()}-{chunk_number:06d}.txt",
        )

        with open(p, "w", encoding="utf-8", buffering=1024 * 1024) as out:
            out.writelines(to_write)

        chunk_paths.append(p)

        chunk = []
        estimated_bytes = 0
        chunk_number += 1

    with open(src_path, "r", encoding="utf-8", buffering=1024 * 1024) as src:
        for line in src:
            chunk.append(line)

            # Approximate Python object/list overhead as well as string bytes.
            estimated_bytes += len(line.encode("utf-8")) + 64

            if estimated_bytes >= chunk_bytes:
                flush_chunk()

        flush_chunk()

    if not chunk_paths:
        Path(dst_path).write_text("", encoding="utf-8")
        return

    handles = [
        open(p, "r", encoding="utf-8", buffering=1024 * 1024)
        for p in chunk_paths
    ]

    try:
        with open(dst_path, "w", encoding="utf-8", buffering=1024 * 1024) as out:
            previous = None

            for line in heapq.merge(*handles):
                if unique and line == previous:
                    continue

                out.write(line)
                previous = line

    finally:
        for handle in handles:
            handle.close()

        for p in chunk_paths:
            try:
                os.remove(p)
            except FileNotFoundError:
                pass


# ----------------------------------------------------------------------
# Typed entity-label streams
# ----------------------------------------------------------------------

def iter_unique_ids(sorted_types_path: str) -> Iterator[str]:
    previous = None

    with open(sorted_types_path, "r", encoding="utf-8") as f:
        for line in f:
            eid = line.rstrip("\n")

            if eid != previous:
                yield eid
                previous = eid


def build_entities(
    sorted_types_path: str,
    sorted_labels_path: str,
    entities_path: str,
) -> None:
    """
    Merge-join typed subjects with labels.

    Keeps all distinct normalized labels for a typed entity.
    """
    type_iter = iter_unique_ids(sorted_types_path)

    try:
        current_type = next(type_iter)
    except StopIteration:
        Path(entities_path).write_text("", encoding="utf-8")
        return

    previous_output = None

    with (
        open(sorted_labels_path, "r", encoding="utf-8") as labels,
        open(entities_path, "w", encoding="utf-8", buffering=1024 * 1024) as out,
    ):
        for line in labels:
            eid, label = line.rstrip("\n").split("\t", 1)

            while current_type < eid:
                try:
                    current_type = next(type_iter)
                except StopIteration:
                    return

            if current_type != eid:
                continue

            output_line = f"{eid}\t{label}\n"

            if output_line == previous_output:
                continue

            out.write(output_line)
            previous_output = output_line


# ----------------------------------------------------------------------
# Exact cross-graph matching
# ----------------------------------------------------------------------

def exact_key(label: str) -> str:
    return hashlib.blake2b(
        label.encode("utf-8"),
        digest_size=16,
    ).hexdigest()


def write_exact_index(
    entities_path: str,
    side: str,
    exact_path: str,
) -> None:
    with (
        open(entities_path, "r", encoding="utf-8") as src,
        open(exact_path, "a", encoding="utf-8", buffering=1024 * 1024) as out,
    ):
        for line in src:
            eid, label = line.rstrip("\n").split("\t", 1)
            out.write(f"{exact_key(label)}\t{side}\t{eid}\t{label}\n")


def emit_exact_cross_matches(
    sorted_exact_path: str,
    out_path: str,
    max_comparisons_per_block: int,
) -> None:
    current_key = None
    left_ids = []
    right_ids = []

    def flush(out) -> None:
        nonlocal left_ids, right_ids

        if not left_ids or not right_ids:
            left_ids = []
            right_ids = []
            return

        left_unique = list(dict.fromkeys(left_ids))
        right_unique = list(dict.fromkeys(right_ids))

        comparisons = len(left_unique) * len(right_unique)

        if comparisons > max_comparisons_per_block:
            log(
                "[exact] skipping pathological duplicate-label block: "
                f"{len(left_unique)} x {len(right_unique)} "
                f"= {comparisons:,} pairs"
            )
        else:
            for left_id in left_unique:
                for right_id in right_unique:
                    out.write(f"{left_id}\t{right_id}\t1.000000\n")

        left_ids = []
        right_ids = []

    with (
        open(sorted_exact_path, "r", encoding="utf-8") as src,
        open(out_path, "w", encoding="utf-8", buffering=1024 * 1024) as out,
    ):
        for line in src:
            key, side, eid, _label = line.rstrip("\n").split("\t", 3)

            if current_key is None:
                current_key = key

            if key != current_key:
                flush(out)
                current_key = key

            if side == "L":
                left_ids.append(eid)
            elif side == "R":
                right_ids.append(eid)

        flush(out)


# ----------------------------------------------------------------------
# Fuzzy candidate blocking
# ----------------------------------------------------------------------

def trigrams(value: str) -> set[str]:
    padded = f"  {value}  "
    return {
        padded[i:i + 3]
        for i in range(len(padded) - 2)
    }


def gram_hash(gram: str) -> int:
    return int.from_bytes(
        hashlib.blake2b(
            gram.encode("utf-8"),
            digest_size=8,
        ).digest(),
        "big",
    )


def fuzzy_block_keys(
    label: str,
    anchors: int = 4,
    bucket_width: int = 4,
) -> list[str]:
    """
    Use a few deterministic character-trigram anchors plus neighboring
    length buckets.

    This gives high recall without materializing every trigram.
    """
    grams = trigrams(label)

    selected = sorted(
        ((gram_hash(g), g) for g in grams),
        key=lambda item: item[0],
    )[:anchors]

    length_bucket = len(label) // bucket_width
    result = set()

    for _, gram in selected:
        result.add(f"F:{length_bucket}:{gram}")

        if length_bucket > 0:
            result.add(f"F:{length_bucket - 1}:{gram}")

        result.add(f"F:{length_bucket + 1}:{gram}")

    return sorted(result)


def write_fuzzy_blocks(
    entities_path: str,
    side: str,
    blocks_path: str,
    anchors: int,
    bucket_width: int,
) -> None:
    with (
        open(entities_path, "r", encoding="utf-8") as src,
        open(blocks_path, "a", encoding="utf-8", buffering=1024 * 1024) as out,
    ):
        for line in src:
            eid, label = line.rstrip("\n").split("\t", 1)

            for key in fuzzy_block_keys(
                label,
                anchors=anchors,
                bucket_width=bucket_width,
            ):
                out.write(f"{key}\t{side}\t{eid}\t{label}\n")


# ----------------------------------------------------------------------
# Similarity
# ----------------------------------------------------------------------

def trigram_dice(a: str, b: str) -> float:
    ga = trigrams(a)
    gb = trigrams(b)

    denominator = len(ga) + len(gb)

    if denominator == 0:
        return 0.0

    return (2.0 * len(ga & gb)) / denominator


def length_possible(
    a: str,
    b: str,
    threshold: float,
) -> bool:
    la = len(a)
    lb = len(b)

    if not la or not lb:
        return False

    # Deliberately looser than final threshold to avoid false negatives
    # caused by token reorderings or short insertions/deletions.
    return min(la, lb) / max(la, lb) >= threshold * 0.80


def final_similarity(a: str, b: str) -> float:
    char_score = fuzz.ratio(a, b)
    token_score = fuzz.token_sort_ratio(a, b)

    return max(char_score, token_score) / 100.0


# ----------------------------------------------------------------------
# Parallel block scoring
# ----------------------------------------------------------------------

def scan_block_offsets(
    sorted_blocks_path: str,
    workers: int,
) -> list[Tuple[int, int]]:
    """
    Divide the sorted block file into byte ranges aligned to complete keys.
    """
    size = os.path.getsize(sorted_blocks_path)

    if size == 0:
        return []

    workers = max(1, workers)

    if workers == 1:
        return [(0, size)]

    targets = [
        size * i // workers
        for i in range(1, workers)
    ]

    boundaries = [0]

    with open(sorted_blocks_path, "rb") as f:
        for target in targets:
            f.seek(target)
            f.readline()  # discard partial line

            first = f.readline()

            if not first:
                boundaries.append(size)
                continue

            first_key = first.split(b"\t", 1)[0]

            while True:
                next_pos = f.tell()
                line = f.readline()

                if not line:
                    boundaries.append(size)
                    break

                key = line.split(b"\t", 1)[0]

                if key != first_key:
                    boundaries.append(next_pos)
                    break

    boundaries.append(size)
    boundaries = sorted(set(boundaries))

    return [
        (start, end)
        for start, end in zip(boundaries, boundaries[1:])
        if end > start
    ]


def score_one_group(
    left: list[Tuple[str, str]],
    right: list[Tuple[str, str]],
    threshold: float,
    trigram_threshold: float,
    max_comparisons_per_block: int,
    out,
) -> Tuple[int, int, int]:
    if not left or not right:
        return 0, 0, 0

    left = list(dict.fromkeys(left))
    right = list(dict.fromkeys(right))

    possible_pairs = len(left) * len(right)

    if possible_pairs > max_comparisons_per_block:
        return possible_pairs, 0, 0

    considered = 0
    rapid_calls = 0
    matches = 0

    for left_id, left_label in left:
        for right_id, right_label in right:
            considered += 1

            if not length_possible(
                left_label,
                right_label,
                threshold,
            ):
                continue

            if trigram_dice(
                left_label,
                right_label,
            ) < trigram_threshold:
                continue

            rapid_calls += 1

            score = final_similarity(
                left_label,
                right_label,
            )

            if score >= threshold:
                out.write(
                    f"{left_id}\t{right_id}\t{score:.6f}\n"
                )
                matches += 1

    return considered, rapid_calls, matches


def score_block_range(
    sorted_blocks_path: str,
    start: int,
    end: int,
    out_path: str,
    threshold: float,
    trigram_threshold: float,
    max_comparisons_per_block: int,
) -> Tuple[str, int, int, int, int]:
    current_key = None
    left = []
    right = []

    blocks_seen = 0
    comparisons = 0
    rapid_calls = 0
    matches = 0

    with (
        open(sorted_blocks_path, "rb") as src,
        open(out_path, "w", encoding="utf-8", buffering=1024 * 1024) as out,
    ):
        src.seek(start)

        def flush() -> None:
            nonlocal left, right
            nonlocal blocks_seen, comparisons, rapid_calls, matches

            if current_key is None:
                return

            blocks_seen += 1

            c, r, m = score_one_group(
                left,
                right,
                threshold,
                trigram_threshold,
                max_comparisons_per_block,
                out,
            )

            comparisons += c
            rapid_calls += r
            matches += m

            left = []
            right = []

        while src.tell() < end:
            raw = src.readline()

            if not raw:
                break

            key, side, eid, label = (
                raw.decode("utf-8")
                .rstrip("\n")
                .split("\t", 3)
            )

            if current_key is None:
                current_key = key

            if key != current_key:
                flush()
                current_key = key

            if side == "L":
                left.append((eid, label))
            elif side == "R":
                right.append((eid, label))

        flush()

    return (
        out_path,
        blocks_seen,
        comparisons,
        rapid_calls,
        matches,
    )


def score_fuzzy_blocks_parallel(
    sorted_blocks_path: str,
    candidate_path: str,
    work_dir: str,
    threshold: float,
    trigram_threshold: float,
    max_comparisons_per_block: int,
    workers: int,
) -> None:
    ranges = scan_block_offsets(
        sorted_blocks_path,
        workers,
    )

    if not ranges:
        Path(candidate_path).write_text("", encoding="utf-8")
        return

    shard_dir = Path(work_dir) / "score_shards"
    shard_dir.mkdir(parents=True, exist_ok=True)

    jobs = []

    for i, (start, end) in enumerate(ranges):
        shard_path = str(
            shard_dir / f"matches-{i:04d}.tsv"
        )

        jobs.append(
            (start, end, shard_path)
        )

    total_blocks = 0
    total_comparisons = 0
    total_rapid = 0
    total_matches = 0

    with ProcessPoolExecutor(
        max_workers=min(workers, len(jobs))
    ) as pool:
        futures = [
            pool.submit(
                score_block_range,
                sorted_blocks_path,
                start,
                end,
                shard_path,
                threshold,
                trigram_threshold,
                max_comparisons_per_block,
            )
            for start, end, shard_path in jobs
        ]

        results = [
            future.result()
            for future in futures
        ]

    with open(
        candidate_path,
        "w",
        encoding="utf-8",
        buffering=1024 * 1024,
    ) as out:
        for (
            shard_path,
            blocks_seen,
            comparisons,
            rapid_calls,
            matches,
        ) in results:

            total_blocks += blocks_seen
            total_comparisons += comparisons
            total_rapid += rapid_calls
            total_matches += matches

            with open(
                shard_path,
                "r",
                encoding="utf-8",
            ) as shard:
                shutil.copyfileobj(
                    shard,
                    out,
                    length=1024 * 1024,
                )

            os.remove(shard_path)

    try:
        shard_dir.rmdir()
    except OSError:
        pass

    log(
        "[fuzzy] "
        f"blocks={total_blocks:,} | "
        f"block cross-products={total_comparisons:,} | "
        f"RapidFuzz calls={total_rapid:,} | "
        f"raw fuzzy matches={total_matches:,}"
    )


# ----------------------------------------------------------------------
# Candidate merge + dedup
# ----------------------------------------------------------------------

def concatenate_files(
    paths: list[str],
    output_path: str,
) -> None:
    with open(
        output_path,
        "wb",
    ) as out:
        for path in paths:
            if not os.path.exists(path):
                continue

            with open(path, "rb") as src:
                shutil.copyfileobj(
                    src,
                    out,
                    length=16 * 1024 * 1024,
                )


def deduplicate_pairs(
    sorted_candidates_path: str,
    output_path: str,
) -> None:
    """
    Multiple blocks and multiple labels may discover the same L/R pair.
    Keep its maximum score.
    """
    previous_pair = None
    best_score = 0.0

    with (
        open(sorted_candidates_path, "r", encoding="utf-8") as src,
        open(output_path, "w", encoding="utf-8", buffering=1024 * 1024) as out,
    ):
        for line in src:
            left_id, right_id, score_s = (
                line.rstrip("\n").split("\t")
            )

            pair = (left_id, right_id)
            score = float(score_s)

            if previous_pair is None:
                previous_pair = pair
                best_score = score
                continue

            if pair == previous_pair:
                if score > best_score:
                    best_score = score
                continue

            out.write(
                f"{previous_pair[0]}\t"
                f"{previous_pair[1]}\t"
                f"{best_score:.6f}\n"
            )

            previous_pair = pair
            best_score = score

        if previous_pair is not None:
            out.write(
                f"{previous_pair[0]}\t"
                f"{previous_pair[1]}\t"
                f"{best_score:.6f}\n"
            )


# ----------------------------------------------------------------------
# URI resolution
# ----------------------------------------------------------------------

def deduplicate_mapping(
    sorted_mapping_path: str,
    unique_mapping_path: str,
) -> None:
    previous_id = None

    with (
        open(sorted_mapping_path, "r", encoding="utf-8") as src,
        open(unique_mapping_path, "w", encoding="utf-8", buffering=1024 * 1024) as out,
    ):
        for line in src:
            eid, uri = line.rstrip("\n").split("\t", 1)

            if eid == previous_id:
                continue

            out.write(f"{eid}\t{uri}\n")
            previous_id = eid


def collect_needed_ids(
    pairs_path: str,
    left_needed_path: str,
    right_needed_path: str,
) -> None:
    with (
        open(pairs_path, "r", encoding="utf-8") as src,
        open(left_needed_path, "w", encoding="utf-8") as left_out,
        open(right_needed_path, "w", encoding="utf-8") as right_out,
    ):
        for line in src:
            left_id, right_id, _score = line.rstrip("\n").split("\t")

            left_out.write(f"{left_id}\n")
            right_out.write(f"{right_id}\n")


def filter_mapping_to_needed(
    sorted_needed_ids: str,
    sorted_mapping: str,
    filtered_mapping: str,
) -> None:
    needed_iter = iter_unique_ids(sorted_needed_ids)

    try:
        current_needed = next(needed_iter)
    except StopIteration:
        Path(filtered_mapping).write_text("", encoding="utf-8")
        return

    with (
        open(sorted_mapping, "r", encoding="utf-8") as mapping,
        open(filtered_mapping, "w", encoding="utf-8", buffering=1024 * 1024) as out,
    ):
        for line in mapping:
            eid, uri = line.rstrip("\n").split("\t", 1)

            while current_needed < eid:
                try:
                    current_needed = next(needed_iter)
                except StopIteration:
                    return

            if eid == current_needed:
                out.write(f"{eid}\t{uri}\n")


def resolve_pairs_to_uris(
    pairs_path: str,
    left_mapping_path: str,
    right_mapping_path: str,
    output_path: str,
) -> None:
    """
    The filtered mappings contain only IDs appearing in final pairs, so they
    are usually small enough to load into RAM even when the source KGs are huge.
    """
    left_map = {}
    right_map = {}

    with open(left_mapping_path, "r", encoding="utf-8") as src:
        for line in src:
            eid, uri = line.rstrip("\n").split("\t", 1)
            left_map[eid] = uri

    with open(right_mapping_path, "r", encoding="utf-8") as src:
        for line in src:
            eid, uri = line.rstrip("\n").split("\t", 1)
            right_map[eid] = uri

    missing_left = 0
    missing_right = 0
    written = 0

    with (
        open(pairs_path, "r", encoding="utf-8") as src,
        open(output_path, "w", encoding="utf-8", buffering=1024 * 1024) as out,
    ):
        for line in src:
            left_id, right_id, score = line.rstrip("\n").split("\t")

            left_uri = left_map.get(left_id)
            right_uri = right_map.get(right_id)

            if left_uri is None:
                missing_left += 1
                continue

            if right_uri is None:
                missing_right += 1
                continue

            out.write(
                f"<{left_uri}>\t"
                f"<{right_uri}>\t"
                f"{score}\n"
            )

            written += 1

    log(
        f"[resolve] wrote {written:,} links | "
        f"missing left={missing_left:,} | "
        f"missing right={missing_right:,}"
    )


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Large-scale fuzzy label linking between two N-Triples graphs. "
            "Select resources independently using --left-kg and --right-kg."
        )
    )

    ap.add_argument(
        "--left-input",
        required=True,
        help="Left graph, e.g. DBpedia .nt file",
    )

    ap.add_argument(
        "--right-input",
        required=True,
        help="Right graph, e.g. Wikidata .nt file",
    )

    ap.add_argument(
        "--left-kg",
        required=True,
        metavar='"s P URI"',
        help="Triple pattern selecting left resources, e.g. 's RDF_TYPE OWL_CLASS'",
    )

    ap.add_argument(
        "--right-kg",
        required=True,
        metavar='"? P o"',
        help="Triple pattern selecting right resources, e.g. '? WDT_P31 o'",
    )

    ap.add_argument(
        "--left-label-predicate",
        default=DEFAULT_LABEL_PREDICATE,
    )

    ap.add_argument(
        "--right-label-predicate",
        default=DEFAULT_LABEL_PREDICATE,
    )

    ap.add_argument(
        "--work-dir",
        required=True,
        help="Temporary working directory. Deleted after success by default.",
    )

    ap.add_argument(
        "--out",
        required=True,
        help="Final TSV: <left-uri> TAB <right-uri> TAB score",
    )

    ap.add_argument(
        "--threshold",
        type=float,
        default=0.90,
        help="Final RapidFuzz threshold in [0,1]. Default 0.90",
    )

    ap.add_argument(
        "--trigram-threshold",
        type=float,
        default=0.60,
        help="Cheap Dice trigram prefilter threshold. Default 0.60",
    )

    ap.add_argument(
        "--anchors",
        type=int,
        default=4,
        help="Number of deterministic trigram anchors per label. Default 4",
    )

    ap.add_argument(
        "--bucket-width",
        type=int,
        default=4,
        help="Label-length bucket width. Default 4",
    )

    ap.add_argument(
        "--max-comparisons-per-block",
        type=int,
        default=1_000_000,
        help="Skip fuzzy/exact blocks whose L x R cross-product exceeds this.",
    )

    ap.add_argument(
        "--workers",
        type=int,
        default=max(1, os.cpu_count() or 1),
        help="Parallel fuzzy-scoring workers.",
    )

    ap.add_argument(
        "--sort-memory-mb",
        type=int,
        default=8192,
        help="Approximate memory budget per external-sort chunk.",
    )

    ap.add_argument(
        "--progress-every-s",
        type=int,
        default=10,
    )

    ap.add_argument(
        "--keep-work-dir",
        action="store_true",
        help=(
            "Keep temporary extracted/index/sort files after success. "
            "By default they are deleted."
        ),
    )

    args = ap.parse_args()
    try:
        left_selection = parse_kg_pattern(args.left_kg)
        right_selection = parse_kg_pattern(args.right_kg)
    except ValueError as exc:
        ap.error(str(exc))

    log(f"[L] selector: {args.left_kg} => extract {left_selection[0]}s")
    log(f"[R] selector: {args.right_kg} => extract {right_selection[0]}s")

    if not 0.0 <= args.threshold <= 1.0:
        ap.error("--threshold must be between 0 and 1")

    if not 0.0 <= args.trigram_threshold <= 1.0:
        ap.error("--trigram-threshold must be between 0 and 1")

    if args.anchors < 1:
        ap.error("--anchors must be >= 1")

    if args.bucket_width < 1:
        ap.error("--bucket-width must be >= 1")

    if args.workers < 1:
        ap.error("--workers must be >= 1")

    work = Path(args.work_dir).resolve()
    output = Path(args.out).resolve()

    # Avoid catastrophic accidental deletion if output is placed inside work dir.
    try:
        output.relative_to(work)
        ap.error(
            "--out must be outside --work-dir because --work-dir is deleted "
            "after a successful run by default"
        )
    except ValueError:
        pass

    if work.exists() and any(work.iterdir()):
        ap.error(f"--work-dir must be empty or nonexistent to avoid deleting existing files: {work}")
    for input_arg in (args.left_input, args.right_input):
        input_path = Path(input_arg).resolve()
        if input_path == work or work in input_path.parents:
            ap.error(f"Input must be outside --work-dir: {input_path}")
    work.mkdir(parents=True, exist_ok=True)

    tmp_dir = work / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    chunk_bytes = (
        args.sort_memory_mb
        * 1024
        * 1024
    )

    # Left
    left_types_raw = work / "left.types.tsv"
    left_labels_raw = work / "left.labels.tsv"
    left_mapping_raw = work / "left.mapping.tsv"

    left_types_sorted = work / "left.types.sorted.tsv"
    left_labels_sorted = work / "left.labels.sorted.tsv"
    left_mapping_sorted = work / "left.mapping.sorted.tsv"
    left_mapping_unique = work / "left.mapping.unique.tsv"

    left_entities = work / "left.entities.tsv"

    # Right
    right_types_raw = work / "right.types.tsv"
    right_labels_raw = work / "right.labels.tsv"
    right_mapping_raw = work / "right.mapping.tsv"

    right_types_sorted = work / "right.types.sorted.tsv"
    right_labels_sorted = work / "right.labels.sorted.tsv"
    right_mapping_sorted = work / "right.mapping.sorted.tsv"
    right_mapping_unique = work / "right.mapping.unique.tsv"

    right_entities = work / "right.entities.tsv"

    # Exact
    exact_raw = work / "exact.tsv"
    exact_sorted = work / "exact.sorted.tsv"
    exact_matches = work / "exact.matches.tsv"

    # Fuzzy
    fuzzy_blocks_raw = work / "fuzzy.blocks.tsv"
    fuzzy_blocks_sorted = work / "fuzzy.blocks.sorted.tsv"
    fuzzy_matches = work / "fuzzy.matches.tsv"

    # Combined/deduped pair IDs
    all_candidates = work / "all.candidates.tsv"
    all_candidates_sorted = work / "all.candidates.sorted.tsv"
    pair_ids = work / "pairs.ids.tsv"

    # URI resolution
    left_needed = work / "left.needed.tsv"
    right_needed = work / "right.needed.tsv"
    left_needed_sorted = work / "left.needed.sorted.tsv"
    right_needed_sorted = work / "right.needed.sorted.tsv"
    left_filtered_mapping = work / "left.mapping.filtered.tsv"
    right_filtered_mapping = work / "right.mapping.filtered.tsv"

    # Ensure append-target files start empty.
    exact_raw.write_text("", encoding="utf-8")
    fuzzy_blocks_raw.write_text("", encoding="utf-8")

    success = False

    try:
        log("1/15 Extracting LEFT graph")
        extract_graph(
            args.left_input,
            "L",
            left_selection,
            args.left_label_predicate,
            str(left_types_raw),
            str(left_labels_raw),
            str(left_mapping_raw),
            args.progress_every_s,
        )

        log("2/15 Extracting RIGHT graph")
        extract_graph(
            args.right_input,
            "R",
            right_selection,
            args.right_label_predicate,
            str(right_types_raw),
            str(right_labels_raw),
            str(right_mapping_raw),
            args.progress_every_s,
        )

        log("3/15 Sorting extracted type/label files")
        external_sort(
            str(left_types_raw),
            str(left_types_sorted),
            str(tmp_dir),
            chunk_bytes,
            unique=True,
        )
        external_sort(
            str(left_labels_raw),
            str(left_labels_sorted),
            str(tmp_dir),
            chunk_bytes,
            unique=True,
        )
        external_sort(
            str(right_types_raw),
            str(right_types_sorted),
            str(tmp_dir),
            chunk_bytes,
            unique=True,
        )
        external_sort(
            str(right_labels_raw),
            str(right_labels_sorted),
            str(tmp_dir),
            chunk_bytes,
            unique=True,
        )

        log("4/15 Building typed entity-label streams")
        build_entities(
            str(left_types_sorted),
            str(left_labels_sorted),
            str(left_entities),
        )
        build_entities(
            str(right_types_sorted),
            str(right_labels_sorted),
            str(right_entities),
        )

        log("5/15 Building exact-match index")
        write_exact_index(
            str(left_entities),
            "L",
            str(exact_raw),
        )
        write_exact_index(
            str(right_entities),
            "R",
            str(exact_raw),
        )

        external_sort(
            str(exact_raw),
            str(exact_sorted),
            str(tmp_dir),
            chunk_bytes,
            unique=True,
        )

        log("6/15 Emitting exact cross-graph matches")
        emit_exact_cross_matches(
            str(exact_sorted),
            str(exact_matches),
            args.max_comparisons_per_block,
        )

        log("7/15 Building fuzzy blocks")
        write_fuzzy_blocks(
            str(left_entities),
            "L",
            str(fuzzy_blocks_raw),
            args.anchors,
            args.bucket_width,
        )
        write_fuzzy_blocks(
            str(right_entities),
            "R",
            str(fuzzy_blocks_raw),
            args.anchors,
            args.bucket_width,
        )

        log("8/15 Sorting fuzzy blocks")
        external_sort(
            str(fuzzy_blocks_raw),
            str(fuzzy_blocks_sorted),
            str(tmp_dir),
            chunk_bytes,
            unique=True,
        )

        log(
            f"9/15 Fuzzy scoring with {args.workers} worker(s)"
        )
        score_fuzzy_blocks_parallel(
            str(fuzzy_blocks_sorted),
            str(fuzzy_matches),
            str(work),
            args.threshold,
            args.trigram_threshold,
            args.max_comparisons_per_block,
            args.workers,
        )

        log("10/15 Combining exact + fuzzy candidates")
        concatenate_files(
            [
                str(exact_matches),
                str(fuzzy_matches),
            ],
            str(all_candidates),
        )

        log("11/15 Sorting candidate pairs")
        external_sort(
            str(all_candidates),
            str(all_candidates_sorted),
            str(tmp_dir),
            chunk_bytes,
            unique=False,
        )

        log("12/15 Deduplicating candidate pairs")
        deduplicate_pairs(
            str(all_candidates_sorted),
            str(pair_ids),
        )

        log("13/15 Sorting/deduplicating URI mappings")
        external_sort(
            str(left_mapping_raw),
            str(left_mapping_sorted),
            str(tmp_dir),
            chunk_bytes,
            unique=True,
        )
        external_sort(
            str(right_mapping_raw),
            str(right_mapping_sorted),
            str(tmp_dir),
            chunk_bytes,
            unique=True,
        )

        deduplicate_mapping(
            str(left_mapping_sorted),
            str(left_mapping_unique),
        )
        deduplicate_mapping(
            str(right_mapping_sorted),
            str(right_mapping_unique),
        )

        log("14/15 Filtering mappings to IDs actually used in final links")
        collect_needed_ids(
            str(pair_ids),
            str(left_needed),
            str(right_needed),
        )

        external_sort(
            str(left_needed),
            str(left_needed_sorted),
            str(tmp_dir),
            chunk_bytes,
            unique=True,
        )
        external_sort(
            str(right_needed),
            str(right_needed_sorted),
            str(tmp_dir),
            chunk_bytes,
            unique=True,
        )

        filter_mapping_to_needed(
            str(left_needed_sorted),
            str(left_mapping_unique),
            str(left_filtered_mapping),
        )
        filter_mapping_to_needed(
            str(right_needed_sorted),
            str(right_mapping_unique),
            str(right_filtered_mapping),
        )

        log("15/15 Resolving IDs back to URIs")
        ensure_parent(str(output))

        resolve_pairs_to_uris(
            str(pair_ids),
            str(left_filtered_mapping),
            str(right_filtered_mapping),
            str(output),
        )

        success = True

        log(f"Done: {output}")

    finally:
        if success and not args.keep_work_dir:
            log(
                f"Cleaning temporary working directory: {work}"
            )
            shutil.rmtree(work, ignore_errors=False)
            log("Temporary working files deleted.")
        elif success:
            log(
                f"Keeping working directory because --keep-work-dir was set: {work}"
            )
        else:
            log(
                f"Run failed; keeping working directory for inspection/resume: {work}"
            )


if __name__ == "__main__":
    main()
