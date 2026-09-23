import argparse
import os
import subprocess
from collections import defaultdict

def dedup_file_sort_u(path_in: str, path_out: str, parallel: int = 8, mem: str = "50%"):
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    cmd = ["sort", "-S", mem, f"--parallel={parallel}", "-u", path_in]
    with open(path_out, "w", encoding="utf-8") as fout:
        subprocess.run(cmd, stdout=fout, check=True, env=env)

def parse_uri(token: str) -> str | None:
    token = token.strip()
    if token.startswith("<") and token.endswith(">"):
        return token[1:-1]
    return None

def parse_link_record(line: str) -> tuple[str, str, float] | None:
    parts = line.split("\t")
    if len(parts) >= 3:
        u1 = parse_uri(parts[0])
        u2 = parse_uri(parts[1])
        if not u1 or not u2:
            return None
        try:
            conf = float(parts[2])
        except ValueError:
            return None
        return u1, u2, conf

    parts = line.split()
    if len(parts) >= 4 and parts[1] == "<http://www.w3.org/2002/07/owl#sameAs>":
        u1 = parse_uri(parts[0])
        u2 = parse_uri(parts[2])
        if not u1 or not u2:
            return None
        return u1, u2, 1.0

    return None

class UnionFind:
    def __init__(self):
        self.parent = {}
        self.rank = {}

    def find(self, x: str) -> str:
        p = self.parent.get(x, x)
        if p != x:
            self.parent[x] = self.find(p)
        else:
            self.parent.setdefault(x, x)
            self.rank.setdefault(x, 0)
        return self.parent[x]
    
    def union(self, a: str, b: str):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        rka, rkb = self.rank[ra], self.rank[rb]
        if rka < rkb:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if rka == rkb:
            self.rank[ra] = rka + 1

def better_canonical(a: str, b: str) -> bool:
    a_has_pct = "%" in a
    b_has_pct = "%" in b
    if a_has_pct != b_has_pct:
        return not a_has_pct
    if len(a) != len(b):
        return len(a) < len(b)
    return a < b
    
def build_mapping(links_tsv: str, conf_threshold: float) -> dict[str, str]:
    uf = UnionFind()
    seen = set()

    with open(links_tsv, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parsed = parse_link_record(line)
            if parsed is None:
                continue
            u1, u2, conf = parsed

            seen.add(u1)
            seen.add(u2)

            if conf < conf_threshold or u1 == u2:
                continue
            uf.union(u1, u2)

    comps = defaultdict(list)
    for x in seen:
        comps[uf.find(x)].append(x)

    canon = {}
    for root, nodes in comps.items():
        best = root
        for n in nodes:
            if better_canonical(n, best):
                best = n
        for n in nodes:
            canon[n] = best

    return canon

def rewrite_nt(in_path: str, out_path: str, mapping: dict[str, str]):
    def map_uri(u: str) -> str:
        return mapping.get(u, u)
    
    with open(in_path, "r", encoding="utf-8", errors="replace") as fin, \
         open(out_path, "w", encoding="utf-8") as fout:
        for line in fin:
            if not line.startswith("<"):
                fout.write(line)
                continue

            s_end = line.find(">")
            if s_end == -1:
                fout.write(line); continue
            s_uri = line[1:s_end]
            rest = line[s_end+1:].lstrip()

            if not rest.startswith("<"):
                fout.write(line); continue
            p_end = rest.find(">")
            if p_end == -1:
                fout.write(line); continue
            p_tok = rest[:p_end+1]
            rest2 = rest[p_end+1:].lstrip()

            if rest2.startswith("<"):
                R = rest2.find(">")
                if R != -1:
                    o_uri = rest2[1:R]
                    tail = rest2[R+1:]
                    out = f"<{map_uri(s_uri)}> {p_tok} <{map_uri(o_uri)}>{tail}"
                    fout.write(out)
                    continue

            out = f"<{map_uri(s_uri)}> {p_tok} {rest2}"
            fout.write(out)

def main():
    ap = argparse.ArgumentParser(description="Merges datasets based on sameAs links.")
    ap.add_argument("--links", required=True, help="TSV: <u1>\\t<u2>\\tconfidence")
    ap.add_argument("--conf", type=float, default=0.7, help="confidence threshold")
    ap.add_argument("--out-mapping", default=None, help="output mapping: old\tcanonical")
    ap.add_argument("--rewrite", nargs="+", help="input .nt files to rewrite/merge")
    ap.add_argument("--out-dir", default="rewritten", help="where to write rewritten .nt")
    args = ap.parse_args()

    mapping = build_mapping(args.links, args.conf)

    if args.out_mapping:
        with open(args.out_mapping, "w", encoding="utf-8") as f:
            for k, v in mapping.items():
                if k != v:
                    f.write(f"{k}\t{v}\n")

    if args.rewrite:
        import os
        os.makedirs(args.out_dir, exist_ok=True)
        for in_file in args.rewrite:
            base = os.path.basename(in_file)
            out_file = os.path.join(args.out_dir, base)
            rewrite_nt(in_file, out_file, mapping)

            tmp = out_file + ".tmp"
            os.replace(out_file, tmp)
            dedup_file_sort_u(tmp, out_file)
            os.remove(tmp)

if __name__ == "__main__":
    main()

                
