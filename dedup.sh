#!/usr/bin/env bash
#SBATCH --job-name=wikidata-dedup
#SBATCH --account=hpc-prf-whale
#SBATCH --mail-type=END,FAIL
#SBATCH --mail-user=you-email
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=2:00:00
#SBATCH --output=dedup-%j.log
#SBATCH -p normal

set -euo pipefail
export LC_ALL=C

DIR=/results/merged

mkdir -p "$DIR/sort-tmp"

sort \
    -S 4G \
    --parallel=8 \
    -T "$DIR/sort-tmp" \
    -u \
    -o "$DIR/output.nt" \
    "$DIR/input.nt.tmp"

echo "Sorting completed successfully."
ls -lh "$DIR/output.nt"