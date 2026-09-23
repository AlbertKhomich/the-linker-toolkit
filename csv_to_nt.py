import csv
import json
import sys

RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"

def convert(input_file, output_file):
    with open(input_file, "r", encoding="utf-8-sig", newline="") as src, \
         open(output_file, "w", encoding="utf-8") as dst:

        reader = csv.DictReader(src)

        for row in reader:
            iri = row["class"].strip()
            label = row["label"].strip()

            if not iri or not label:
                continue

            # json.dumps escapes quotes, backslashes and control characters
            literal = json.dumps(label, ensure_ascii=False)

            dst.write(f"<{iri}> <{RDFS_LABEL}> {literal} .\n")

if __name__ == "__main__":
    convert(sys.argv[1], sys.argv[2])