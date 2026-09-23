# Linker Toolkit

A two-stage, label-based alignment workflow for linking classes and then linking entities belonging to aligned classes across two knowledge graphs (KGs).

1. Export class IRIs and labels from each KG to CSV.
2. Convert the two class CSV files into N-Triples with `csv_to_nt.py`.
3. Align classes with `align-fuzzy.py`.
4. Fetch instances and their labels for the selected class pairs with `fetch_from_alignment.py`.
5. Convert the fetched CSVs and run `align-fuzzy.py` **separately for each class pair** with `align_entities_from_same_classes.py`.

The scripts use files. The fetch and entity-alignment stages can reuse completed work.

## 1. Clone and set up

Requires Git, Python 3 and `pip`. The fuzzy matcher also uses external sorting; make sure the system has GNU `sort` and sufficient temporary disk space (especially on HPC).

```bash
git clone https://github.com/AlbertKhomich/the-linker-toolkit
cd the-linker-toolkit

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## 2. Prepare class CSV files

Export a CSV from **each** knowledge graph with a header and two columns, `class` and `label`:

```csv
class,label
http://dbpedia.org/ontology/Athlete,athlete
```

Save the two exports as, for example:

```text
data/classes/dbpedia.csv
data/classes/wikidata.csv
```

Use valid, absolute IRIs. Keep separate rows when a class has multiple labels. Because matching is based on labels, select a compatible language on both sides if appropriate (for example, English).

## 3. Convert class CSV files to N-Triples

```bash
mkdir -p data/classes results/classes
python csv_to_nt.py data/classes/dbpedia.csv data/classes/dbpedia.nt
python csv_to_nt.py data/classes/wikidata.csv data/classes/wikidata.nt
```

`csv_to_nt.py` reads the `class` and `label` columns and emits one `rdfs:label` triple per nonempty row:

```nt
<http://dbpedia.org/ontology/Athlete> <http://www.w3.org/2000/01/rdf-schema#label> "athlete" .
```

## 4. Align classes

```bash
RDFS_LABEL='http://www.w3.org/2000/01/rdf-schema#label'

python align-fuzzy.py \
  --left-input data/classes/dbpedia.nt \
  --right-input data/classes/wikidata.nt \
  --left-kg "s $RDFS_LABEL ?" \
  --right-kg "s $RDFS_LABEL ?" \
  --left-label-predicate "$RDFS_LABEL" \
  --right-label-predicate "$RDFS_LABEL" \
  --work-dir results/classes/work \
  --out results/classes/class_alignments.tsv \
  --threshold 0.90 \
  --trigram-threshold 0.60 \
  --anchors 4 \
  --bucket-width 4 \
  --workers 8 \
  --sort-memory-mb 1024
```

The output is a tab-separated file, with no header:

```text
<http://dbpedia.org/ontology/Train>\t<http://www.wikidata.org/entity/Q870>\t1.000000
```

## 5. Fetch instances of the aligned classes

Prepare a separate SPARQL SELECT query template for each endpoint. `{{IRI}}` is replaced with the aligned class IRI from the corresponding side. Include stable ordering for pagination; **do not include an outer `LIMIT` or `OFFSET`**, because the fetching script adds them.

`queries/left.sparql` (DBpedia):

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?instance ?label
WHERE {
  ?instance a {{IRI}} ;
            rdfs:label ?label .
  FILTER(LANG(?label) = "en")
}
ORDER BY ?instance ?label
```

`queries/right.sparql` (Wikidata):

```sparql
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
SELECT DISTINCT ?instance ?label
WHERE {
  ?instance wdt:P31 {{IRI}} ;
            rdfs:label ?label .
  FILTER(LANG(?label) = "en")
}
ORDER BY ?instance ?label
```

Fetch instances for class alignments whose scores meet the chosen **class threshold**:

```bash
mkdir -p queries
# Save the two query templates above in queries/left.sparql and queries/right.sparql.

python fetch_from_alignment.py \
  https://dbpedia.org/sparql \
  https://query.wikidata.org/sparql \
  queries/left.sparql \
  queries/right.sparql \
  0.90 \
  1000 \
  results/classes/class_alignments.tsv \
  --output results/fetched
```

Here `0.90` is the **class-alignment threshold** and `1000` is the per-request page size (`LIMIT`) and `OFFSET` increment. The script waits at least one second between requests. It saves endpoint-selected columns as CSV, caches downloads for repeated class IRIs and creates `results/fetched/pairs.csv` to identify the two fetched files for every selected pair.

Public endpoints may enforce request limits or time out on large classes; adapt query templates and pagination to your endpoint's capabilities. A local endpoint is often more suitable for very large exports.

### Observed resource usage

In one reported fetch run against a left KG of approximately **100 GB** and a right KG of approximately **1 TB**, SLURM reported:

| Metric               | Observed value |
| -------------------- | -------------: |
| Allocated CPU cores  |              1 |
| Wall-clock time      |       07:17:36 |
| CPU time utilized    |       00:00:55 |
| CPU efficiency       |          0.21% |
| Peak memory utilized |      111.20 MB |
| Allocated memory     |        2.00 GB |
| Memory efficiency    |          5.43% |

This is a measurement of **one fetch job**, not a benchmark or a sizing guarantee. The low CPU utilization is consistent with a network/query-wait-bound job; actual duration depends on endpoint performance, request throttling and number and sizes of classes. It does not mean the script loads the 100 GB and 1 TB KGs into local memory.

## 6. Align instances within each matched class pair

Run the orchestrator after the fetch stage finishes:

```bash
python align_entities_from_same_classes.py results/fetched/pairs.csv \
  --fuzzy-script align-fuzzy.py \
  --output results/entities \
  --threshold 0.90 \
  --trigram-threshold 0.60 \
  --anchors 4 \
  --bucket-width 4 \
  --workers 8 \
  --sort-memory-mb 1024
```

This stage automatically:

- Converts each fetched instance CSV into N-Triples containing **only** `rdfs:label` triples (using the CSV-to-NT conversion logic).
- Reuses a converted file when the same class URI is part of several alignments.
- Calls `align-fuzzy.py` independently for every selected class pair, matching **instances**, not classes.
- Writes an alignment TSV per class pair and a summary index at `results/entities/pair_results.csv`.
- Skips completed pair outputs on a rerun. An interrupted pair restarts rather than resuming entity by entity.

The instance threshold in this command is independent of the class threshold in the fetching command. The fetched CSVs are expected to expose `instance` and `label`; if you used different SPARQL variable names, pass the corresponding `--left-iri-column`, `--right-iri-column`, `--left-label-column` and `--right-label-column` options.

To recalculate completed pairs (for example, after changing matching thresholds), add `--force`. If an interrupted run leaves a nonempty per-pair working directory, inspect or remove **that pair's** working directory before retrying; the orchestrator preserves it rather than silently deleting it.

## Output overview

```text
results/
  classes/
    class_alignments.tsv       # class-to-class alignments
  fetched/
    pairs.csv                  # class-pair index and downloaded CSV paths
    left/                      # downloaded left-class instance CSVs
    right/                     # downloaded right-class instance CSVs
  entities/
    converted/                 # cached instance label N-Triples
    pairs/                     # instance alignment TSV per class pair
    pair_results.csv           # class-pair → instance alignment output index
```

The result for an individual class pair contains rows of the form:

```text
<http://dbpedia.org/resource/Example>\t<http://www.wikidata.org/entity/Q123>\t0.950000
```

The score is derived from the labels compared by the fuzzy matcher; candidate blocking can exclude some possible comparisons.

## On a SLURM cluster

Use a SLURM submission script for CPU-intensive class and entity fuzzy matching, with `--cpus-per-task` aligned to `--workers`. Account for temporary sorting space and memory. The fetch stage's observed resource usage above suggests that allocating many CPUs to **that stage alone** is unlikely to help unless you change its request concurrency and comply with endpoint rate limits.
