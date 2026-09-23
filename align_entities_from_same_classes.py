#!/usr/bin/env python3
"""Convert fetched alignment CSVs to N-Triples, then fuzzy-match each class pair.

Input is pairs.csv produced by fetch_alignments.py. Its CSV paths are relative
 to the manifest's parent directory (or may be absolute).
"""
import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

RDFS_LABEL = 'http://www.w3.org/2000/01/rdf-schema#label'


def nt_iri(value):
    value = value.strip()
    p = urlsplit(value)
    if p.scheme not in ('http', 'https') or not p.netloc or re.search(r'[<>"{}|^`\\\x00-\x20]', value):
        raise ValueError(f'Invalid HTTP(S) IRI in CSV or manifest: {value!r}')
    return f'<{value}>'


def resolve_file(manifest, name):
    path = Path(name)
    return (path if path.is_absolute() else manifest.parent / path).resolve()


def nt_for_csv(source, class_iri, directory, iri_column, label_column, force=False):
    """Write one rdfs:label triple per valid CSV row."""
    source_stat = source.stat()
    signature = json.dumps([str(source), source_stat.st_size, source_stat.st_mtime_ns,
                            iri_column, label_column, 'labels-only-v1'], ensure_ascii=False)
    digest = hashlib.sha256(signature.encode('utf-8')).hexdigest()
    target = directory / (digest + '.nt')
    if target.is_file() and not force:
        return target
    temporary = target.with_suffix('.nt.part')
    count = 0
    with source.open('r', encoding='utf-8-sig', newline='') as src, \
         temporary.open('w', encoding='utf-8', newline='') as dst:
        reader = csv.DictReader(src)
        if not reader.fieldnames or iri_column not in reader.fieldnames or label_column not in reader.fieldnames:
            raise ValueError(f'{source}: need CSV columns {iri_column!r} and {label_column!r}; got {reader.fieldnames}')
        for number, row in enumerate(reader, 2):
            iri = (row.get(iri_column) or '').strip()
            label = (row.get(label_column) or '').strip()
            if not iri or not label:
                continue
            try:
                subject = nt_iri(iri)
            except ValueError as exc:
                raise ValueError(f'{source}:{number}: {exc}') from exc
            literal = json.dumps(label, ensure_ascii=False)
            dst.write(f'{subject} <{RDFS_LABEL}> {literal} .\n')
            count += 1
    temporary.replace(target)
    print(f'[convert] {source.name}: {count:,} labeled rows -> {target.name}', flush=True)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pairs_csv', type=Path, help='pairs.csv from fetch_alignments.py')
    parser.add_argument('--fuzzy-script', type=Path, default=Path('fuzzy_cross_graph_linking_kg.py'))
    parser.add_argument('--output', type=Path, default=Path('fuzzy_results'))
    parser.add_argument('--threshold', type=float, default=0.90, help='INSTANCE matching threshold; independent of fetch threshold')
    parser.add_argument('--trigram-threshold', type=float, default=0.60)
    parser.add_argument('--anchors', type=int, default=4)
    parser.add_argument('--bucket-width', type=int, default=4)
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--sort-memory-mb', type=int, default=1024)
    parser.add_argument('--max-comparisons-per-block', type=int, default=1_000_000)
    parser.add_argument('--left-iri-column', default='instance')
    parser.add_argument('--right-iri-column', default='instance')
    parser.add_argument('--left-label-column', default='label')
    parser.add_argument('--right-label-column', default='label')
    parser.add_argument('--force', action='store_true', help='Rerun completed matches and refresh conversions')
    parser.add_argument('--keep-work-dir', action='store_true')
    args = parser.parse_args()
    if not 0 <= args.threshold <= 1 or not 0 <= args.trigram_threshold <= 1:
        parser.error('thresholds must be between 0 and 1')
    manifest = args.pairs_csv.resolve()
    fuzzy_script = args.fuzzy_script.resolve()
    if not fuzzy_script.is_file():
        parser.error(f'Fuzzy script not found: {fuzzy_script}')
    out_dir = args.output.resolve()
    converted = out_dir / 'converted'
    pair_dir = out_dir / 'pairs'
    work_root = out_dir / 'work'
    for folder in (converted, pair_dir, work_root):
        folder.mkdir(parents=True, exist_ok=True)
    summary_path = out_dir / 'pair_results.csv'
    rows = []
    with manifest.open('r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        required = {'left_iri', 'right_iri', 'score', 'left_file', 'right_file'}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            parser.error(f'Manifest requires columns: {sorted(required)}')
        rows = list(reader)
    index = []
    # Rebuild result index from existing or newly generated pair files.
    for position, row in enumerate(rows, 1):
        left_iri, right_iri = row['left_iri'], row['right_iri']
        digest = hashlib.sha256((left_iri + '\0' + right_iri).encode('utf-8')).hexdigest()[:20]
        pair_id = f'{position:06d}_{digest}'
        left_csv = resolve_file(manifest, row['left_file'])
        right_csv = resolve_file(manifest, row['right_file'])
        pair_output = pair_dir / f'{pair_id}.tsv'
        print(f'[{position}/{len(rows)}] {left_iri} <-> {right_iri}', flush=True)
        left_nt = nt_for_csv(left_csv, left_iri, converted, args.left_iri_column, args.left_label_column, args.force)
        right_nt = nt_for_csv(right_csv, right_iri, converted, args.right_iri_column, args.right_label_column, args.force)
        if pair_output.is_file() and not args.force:
            print(f'[skip] completed: {pair_output.name}', flush=True)
        else:
            temporary = pair_output.with_suffix('.tsv.part')
            temporary.unlink(missing_ok=True)
            work_dir = work_root / pair_id
            if work_dir.exists() and any(work_dir.iterdir()):
                # The fuzzy matcher requires a clean work directory; preserve
                # failed working files by asking the user to remove them.
                parser.error(f'Nonempty work dir from an earlier failure: {work_dir}; inspect/remove it before retrying')
            command = [
                sys.executable, str(fuzzy_script),
                '--left-input', str(left_nt), '--right-input', str(right_nt),
                '--left-kg', f's {RDFS_LABEL} ?',
                '--right-kg', f's {RDFS_LABEL} ?', 
                '--left-label-predicate', RDFS_LABEL,
                '--right-label-predicate', RDFS_LABEL,
                '--work-dir', str(work_dir), '--out', str(temporary),
                '--threshold', str(args.threshold),
                '--trigram-threshold', str(args.trigram_threshold),
                '--anchors', str(args.anchors),
                '--bucket-width', str(args.bucket_width),
                '--workers', str(args.workers),
                '--sort-memory-mb', str(args.sort_memory_mb),
                '--max-comparisons-per-block', str(args.max_comparisons_per_block),
            ]
            if args.keep_work_dir:
                command.append('--keep-work-dir')
            try:
                subprocess.run(command, check=True)
                if not temporary.is_file():
                    raise RuntimeError(f'Fuzzy matcher exited successfully without output: {temporary}')
                temporary.replace(pair_output)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            print(f'[done] {pair_output}', flush=True)
        index.append({**{name: row[name] for name in ('left_iri', 'right_iri', 'score')},
                      'result_file': str(pair_output.relative_to(out_dir))})
        # Incrementally update index so earlier completed pairs are visible after failure.
        tmp_summary = summary_path.with_suffix('.csv.part')
        with tmp_summary.open('w', encoding='utf-8', newline='') as dst:
            writer = csv.DictWriter(dst, fieldnames=['left_iri', 'right_iri', 'score', 'result_file'])
            writer.writeheader()
            writer.writerows(index)
        tmp_summary.replace(summary_path)
    print(f'Complete: {len(index)} pairs. Index: {summary_path}', flush=True)


if __name__ == '__main__':
    main()
