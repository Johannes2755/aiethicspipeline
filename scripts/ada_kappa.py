"""Inter-rater agreement for the hand-off coding sheet (standard library only, deterministic).

  python scripts/ada_kappa.py ada/output/handoff/coding_neeza.csv ada/output/handoff/coding_john.csv

Each input is a copy of ada/output/handoff/coding_sheet_blank.csv filled in by one rater, independently:
  rater_id   the rater's name or initials (same on every row; must differ between the two files)
  relevant   y / n   (n = the keyword rules picked up a sentence that is not about hand-offs)
  category   model_behavior | user_restriction | declared_intention | safeguard_description | caveat | other
  domains    ';'-separated keys from ada/handoff_config.json (e.g. mental_health;general); may be empty
  level      0-3, the level this passage supports
category, domains and level are needed only on rows marked relevant = y.

Reports Cohen's kappa for relevance and category, kappa per domain (present / absent), unweighted and quadratic-weighted
kappa for level (levels are ordered), each with a bootstrap 95% CI (2000 resamples, fixed seed), plus raw agreement on the
company x domain grid each rater's codes imply (too few cells for a meaningful kappa). Writes agreement.md, agreement.json
and reconcile.csv (both raters side by side; final_* columns pre-filled where the raters agree, blank where they must
discuss). The filled reconcile.csv is what `ada_evidence.py run --topic handoff --final` reads.
"""

import argparse
import csv
import json
import random
import sys
from collections import Counter
from pathlib import Path

from ada_handoff import CATEGORY_VALUES, OUT_DIR, load_config

SEED, RESAMPLES = 684, 2000


def read_sheet(path, cfg):
    rows, raters = {}, set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        for n, r in enumerate(csv.DictReader(f), start=2):
            pid = (r.get("passage_id") or "").strip()
            if not pid:
                continue
            raters.add((r.get("rater_id") or "").strip())
            rel = (r.get("relevant") or "").strip().lower()
            if rel not in ("y", "n"):
                sys.exit(f"{path}: line {n} ({pid}): relevant must be y or n, got {rel!r}")
            cat = (r.get("category") or "").strip()
            doms = sorted({d.strip() for d in (r.get("domains") or "").split(";") if d.strip()})
            lvl = (r.get("level") or "").strip()
            if rel == "y":
                if cat not in CATEGORY_VALUES:
                    sys.exit(f"{path}: line {n} ({pid}): category {cat!r} not one of {CATEGORY_VALUES}")
                bad = [d for d in doms if d not in cfg["domain_keys"]]
                if bad:
                    sys.exit(f"{path}: line {n} ({pid}): unknown domain(s) {bad}; allowed {cfg['domain_keys']}")
                if lvl not in ("0", "1", "2", "3"):
                    sys.exit(f"{path}: line {n} ({pid}): level must be 0-3, got {lvl!r}")
            rows[pid] = {"company": r.get("company", ""), "section": r.get("section", ""), "quote": r.get("quote", ""),
                         "relevant": rel, "category": cat if rel == "y" else "", "domains": doms if rel == "y" else [],
                         "level": int(lvl) if rel == "y" else None}
    raters.discard("")
    if len(raters) != 1:
        sys.exit(f"{path}: expected one rater_id on every row, found {sorted(raters) or 'none'}")
    return raters.pop(), rows


def kappa(a, b):
    n = len(a)
    if n == 0:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return None if pe == 1 else (po - pe) / (1 - pe)


def weighted_kappa(a, b, k=4):
    n = len(a)
    if n == 0:
        return None
    w = lambda i, j: (i - j) ** 2 / (k - 1) ** 2
    ca, cb = Counter(a), Counter(b)
    obs = sum(w(x, y) for x, y in zip(a, b)) / n
    exp = sum(w(i, j) * ca[i] * cb[j] for i in range(k) for j in range(k)) / (n * n)
    return None if exp == 0 else 1 - obs / exp


def boot_ci(a, b, fn):
    rng = random.Random(SEED)
    n, vals = len(a), []
    if n < 2:
        return None
    for _ in range(RESAMPLES):
        idx = [rng.randrange(n) for _ in range(n)]
        v = fn([a[i] for i in idx], [b[i] for i in idx])
        if v is not None:
            vals.append(v)
    if len(vals) < RESAMPLES * 0.9:
        return None
    vals.sort()
    return [round(vals[int(0.025 * len(vals))], 3), round(vals[int(0.975 * len(vals)) - 1], 3)]


def stat(name, a, b, fn=kappa):
    k = fn(a, b)
    return {"measure": name, "n": len(a), "percent_agreement": round(100 * sum(x == y for x, y in zip(a, b)) / len(a), 1) if a else None,
            "kappa": None if k is None else round(k, 3), "ci95": boot_ci(a, b, fn) if k is not None else None,
            "note": "" if k is not None else "undefined (no variation, or no rows)"}


def implied_grid(rows, cfg):
    g = {}
    for r in rows.values():
        if r["relevant"] != "y":
            continue
        for d in r["domains"]:
            key = (r["company"], d)
            g[key] = max(g.get(key, 0), r["level"])
    return g


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sheet_a")
    ap.add_argument("sheet_b")
    ap.add_argument("--out", default=str(OUT_DIR), help="folder for agreement.md/json and reconcile.csv")
    args = ap.parse_args()
    cfg = load_config()
    ra, A = read_sheet(args.sheet_a, cfg)
    rb, B = read_sheet(args.sheet_b, cfg)
    if ra == rb:
        sys.exit(f"both sheets have rater_id {ra!r}; independent coding needs two raters")
    if set(A) != set(B):
        sys.exit(f"the sheets list different passages: only in A {sorted(set(A) - set(B))[:3]}, only in B {sorted(set(B) - set(A))[:3]}")
    ids = sorted(A)
    both = [i for i in ids if A[i]["relevant"] == "y" and B[i]["relevant"] == "y"]
    stats = [stat("relevant (y/n), all passages", [A[i]["relevant"] for i in ids], [B[i]["relevant"] for i in ids]),
             stat("category, passages both marked relevant", [A[i]["category"] for i in both], [B[i]["category"] for i in both]),
             stat("level (unweighted)", [A[i]["level"] for i in both], [B[i]["level"] for i in both]),
             stat("level (quadratic-weighted)", [A[i]["level"] for i in both], [B[i]["level"] for i in both], weighted_kappa)]
    for d in cfg["domain_keys"]:
        stats.append(stat(f"domain {d} present", [d in A[i]["domains"] for i in both], [d in B[i]["domains"] for i in both]))
    companies = sorted({A[i]["company"] for i in ids})
    ga, gb = implied_grid(A, cfg), implied_grid(B, cfg)
    cells = [{"company": c, "domain": d, ra: ga.get((c, d), 0), rb: gb.get((c, d), 0)} for c in companies for d in cfg["domain_keys"]]
    cell_agree = sum(x[ra] == x[rb] for x in cells)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "agreement.json").write_text(json.dumps({"raters": [ra, rb], "passages": len(ids), "both_relevant": len(both),
                                                    "bootstrap": {"resamples": RESAMPLES, "seed": SEED}, "statistics": stats,
                                                    "grid_cells": cells, "grid_cells_agree": cell_agree}, indent=2) + "\n", encoding="utf-8")
    L = [f"# Inter-rater agreement: {ra} vs {rb}", "",
         f"{len(ids)} passages; {len(both)} marked relevant by both raters. Kappa CIs are bootstrap percentiles "
         f"({RESAMPLES} resamples, seed {SEED}).", "",
         "| Measure | n | % agreement | Cohen's kappa | 95% CI |", "|---|---|---|---|---|"]
    for s in stats:
        L.append(f"| {s['measure']} | {s['n']} | {s['percent_agreement']} | {s['kappa'] if s['kappa'] is not None else s['note']} | "
                 f"{s['ci95'] if s['ci95'] else '-'} |")
    L += ["", f"## Implied grid levels (company x domain): {cell_agree} of {len(cells)} cells agree", "",
          f"| Company | Domain | {ra} | {rb} |", "|---|---|---|---|"]
    L += [f"| {c['company']} | {c['domain']} | {c[ra]} | {c[rb]} |" for c in cells]
    (out / "agreement.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    with open(out / "reconcile.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["passage_id", "company", "section", "quote", f"{ra}_relevant", f"{rb}_relevant", f"{ra}_category", f"{rb}_category",
                    f"{ra}_domains", f"{rb}_domains", f"{ra}_level", f"{rb}_level", "agree",
                    "final_relevant", "final_category", "final_domains", "final_level", "reconcile_notes"])
        for i in ids:
            a, b = A[i], B[i]
            fa = (a["relevant"], a["category"], a["domains"], a["level"])
            agree = fa == (b["relevant"], b["category"], b["domains"], b["level"])
            fin = [a["relevant"], a["category"], ";".join(a["domains"]), "" if a["level"] is None else a["level"]] if agree else ["", "", "", ""]
            w.writerow([i, a["company"], a["section"], a["quote"], a["relevant"], b["relevant"], a["category"], b["category"],
                        ";".join(a["domains"]), ";".join(b["domains"]), a["level"] if a["level"] is not None else "",
                        b["level"] if b["level"] is not None else "", "yes" if agree else "no", *fin, ""])
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("\n".join(L))
    print(f"\nWrote agreement.md, agreement.json, reconcile.csv to {out}. Fill the blank final_* cells in reconcile.csv, then run:\n"
          f"  python scripts/ada_evidence.py run --topic handoff --final {out / 'reconcile.csv'}")


if __name__ == "__main__":
    main()
