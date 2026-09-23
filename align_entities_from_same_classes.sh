#!/usr/bin/env bash
#SBATCH --job-name=align-entities
#SBATCH --account=your-account
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=24:00:00
#SBATCH --output=align-entities-%j.log
#SBATCH -p normal

set -euo pipefail

# Change this path to the pairs.csv produced by fetch_alignments.py.
PAIRS_CSV="pairs.csv"

python3 align_entities_from_same_classes.py "$PAIRS_CSV" \
    --fuzzy-script align-fuzzy.py \
    --output fuzzy_results \
    --threshold 0.70 \
    --trigram-threshold 0.40 \
    --anchors 4 \
    --bucket-width 4 \
    --workers "${SLURM_CPUS_PER_TASK:-8}" \
    --sort-memory-mb 1024
