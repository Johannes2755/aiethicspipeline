"""Offline tests for the hand-off ADA mode and the agreement script. No network, no credentials, no model."""
import csv, json, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import ada_evidence, ada_handoff, ada_kappa  # noqa: E402


class HandoffRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "handoff"
        cls.results, cls.files, cls.bad = ada_handoff.run(None, out_dir=cls.out)
        cls.passages = [p for r in cls.results for p in r["passages"]]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def by_quote(self, start):
        return next(p for p in self.passages if p["quote"].startswith(start))

    def test_only_the_two_documents(self):
        self.assertEqual([r["key"] for r in self.results], ["openai_spec", "claude_constitution"])

    def test_every_quote_lead_in_and_scope_quote_verified(self):
        self.assertEqual(self.bad, [])
        for p in self.passages:
            self.assertTrue(p["quote_verified"], p["quote"])
            self.assertTrue(p["section"])

    def test_output_folder_is_separate_from_the_coercion_run(self):
        self.assertEqual(ada_handoff.OUT_DIR, ROOT / "ada" / "output" / "handoff")
        self.assertNotEqual(ada_handoff.OUT_DIR, ada_evidence.OUT_DIR)

    def test_deterministic(self):
        out2 = Path(self.tmp.name) / "again"
        _, files2, _ = ada_handoff.run(None, out_dir=out2)
        self.assertEqual(self.files, files2)

    def test_passage_ids_unique_and_sheet_complete_and_blind(self):
        ids = [p["passage_id"] for p in self.passages]
        self.assertEqual(len(ids), len(set(ids)))
        rows = list(csv.DictReader((self.out / "coding_sheet_blank.csv").read_text(encoding="utf-8").splitlines()))
        self.assertEqual([r["passage_id"] for r in rows], ids)
        for r in rows:
            for col in ("rater_id", "relevant", "category", "domains", "level"):
                self.assertEqual(r[col], "")
        self.assertFalse([h for h in rows[0] if "suggest" in h or "why" in h], "machine suggestions must not be in the rater sheet")

    def test_levels_are_marked_as_machine_suggestions(self):
        f = json.loads(self.files["handoff_findings.json"])
        self.assertEqual(f["codes_source"], "machine_suggestion")
        self.assertIn("MACHINE SUGGESTIONS", self.files["handoff_report.md"])
        self.assertEqual(json.loads(self.files["claims.json"])["source"], "machine_suggestion")

    def test_known_passages(self):
        p = self.by_quote("Always refer users to relevant emergency services")
        self.assertEqual((p["level"], p["company"]), (3, "Anthropic"))
        self.assertIn("general", p["domains"])
        self.assertTrue(p["holds_regardless_of_operator"])
        p = self.by_quote("If the user appears to be in immediate danger")
        self.assertEqual(p["level"], 3)
        self.assertIn("U18 users only", p["audience"])
        p = self.by_quote("For example, if Claude was taught to follow a rule like")
        self.assertEqual(p["stance"], "cautions_against")
        p = self.by_quote("Suggesting professional help when discussing personal struggles")
        self.assertTrue(p["adjustable_default"])
        self.assertLess(p["level"], 3, "a low-risk ('risk indicators are absent') default is not a level-3 commitment")

    def test_false_friends_are_not_handoffs(self):
        quotes = {p["quote"] for p in self.passages if p["handoff_action"]}
        self.assertFalse([q for q in quotes if "proactively escalate to a human" in q])
        self.assertFalse([q for q in quotes if "trusted adult member of the public" in q])

    def test_claims_are_level_2_or_3_and_traceable(self):
        c = json.loads(self.files["claims.json"])
        pids = {p["passage_id"]: p for p in self.passages}
        self.assertTrue(c["claims"])
        for cl in c["claims"]:
            self.assertGreaterEqual(cl["level"], 2)
            self.assertTrue(cl["draft"])
            self.assertEqual(cl["quote"], pids[cl["passage_id"]]["quote"])
            self.assertIn(cl["company"], cl["applies_to"].split()[0] + " " + cl["applies_to"])

    def test_domain_list_comes_from_config(self):
        cfg = json.loads(ada_handoff.CONFIG_PATH.read_text(encoding="utf-8"))
        for d in cfg["domains"]:
            d["enabled"] = d["key"] != "financial" or False
            if d["key"] == "interpersonal_violence":
                d["enabled"] = True
        path = Path(self.tmp.name) / "cfg.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        _, files, _ = ada_handoff.run(None, out_dir=Path(self.tmp.name) / "cfg_out", config_path=path)
        doms = json.loads(files["handoff_findings.json"])["domains"]
        self.assertIn("interpersonal_violence", doms)
        self.assertNotIn("financial", doms)

    def test_coercion_run_still_reproduces_committed_output(self):
        results = []
        for d in ada_evidence.DOCS:
            meta, blocks, flat = ada_evidence.load_doc(d)
            results.append(ada_evidence.analyse(d, meta, blocks, flat))
        committed = (ada_evidence.OUT_DIR / "ada_findings.md").read_text(encoding="utf-8")
        self.assertEqual(ada_evidence.render_md(results), committed)
        committed_json = json.loads((ada_evidence.OUT_DIR / "ada_findings.json").read_text(encoding="utf-8"))
        self.assertEqual(json.loads(json.dumps(results, ensure_ascii=False)), committed_json)

    def test_kappa_end_to_end_and_final_codes(self):
        rows = list(csv.DictReader((self.out / "coding_sheet_blank.csv").read_text(encoding="utf-8").splitlines()))
        machine = {r["passage_id"]: r for r in csv.DictReader((self.out / "machine_suggestions.csv").read_text(encoding="utf-8").splitlines())}
        sheets = []
        for name, flip in (("A", False), ("B", True)):
            path = Path(self.tmp.name) / f"sheet_{name}.csv"
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                for n, r in enumerate(rows):
                    m = machine[r["passage_id"]]
                    lvl = int(m["suggested_level"])
                    if flip and n % 4 == 0:
                        lvl = (lvl + 1) % 4
                    w.writerow({**r, "rater_id": name, "relevant": "y", "category": m["suggested_category"],
                                "domains": m["suggested_domains"], "level": lvl})
            sheets.append(path)
        out = Path(self.tmp.name) / "agree"
        res = subprocess.run([sys.executable, str(ROOT / "scripts" / "ada_kappa.py"), *map(str, sheets), "--out", str(out)],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        stats = {s["measure"]: s for s in json.loads((out / "agreement.json").read_text())["statistics"]}
        self.assertEqual(stats["category, passages both marked relevant"]["percent_agreement"], 100.0)
        self.assertLess(stats["level (unweighted)"]["kappa"], 1)
        # unresolved disagreements must block --final; resolved ones are accepted
        rec = list(csv.DictReader((out / "reconcile.csv").read_text(encoding="utf-8").splitlines()))
        with self.assertRaises(SystemExit):
            ada_handoff.run(None, final_path=out / "reconcile.csv", out_dir=Path(self.tmp.name) / "final")
        for r in rec:
            if r["agree"] == "no":
                r.update(final_relevant="y", final_category=r["A_category"], final_domains=r["A_domains"], final_level=r["A_level"])
        with (out / "reconcile.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rec[0]))
            w.writeheader()
            w.writerows(rec)
        _, files, bad = ada_handoff.run(None, final_path=out / "reconcile.csv", out_dir=Path(self.tmp.name) / "final")
        self.assertEqual(json.loads(files["claims.json"])["source"], "human_final")
        self.assertIn("human final", files["handoff_report.md"])


class KappaMath(unittest.TestCase):
    def test_known_values(self):
        self.assertAlmostEqual(ada_kappa.kappa([1, 1, 0, 0], [1, 0, 0, 0]), 0.5)
        self.assertEqual(ada_kappa.kappa(["a", "b"], ["a", "b"]), 1.0)
        self.assertIsNone(ada_kappa.kappa(["a", "a"], ["a", "a"]))
        self.assertAlmostEqual(ada_kappa.weighted_kappa([0, 1, 2, 3], [0, 1, 2, 3]), 1.0)
        self.assertGreater(ada_kappa.weighted_kappa([0, 1, 2, 3], [0, 1, 2, 2]), ada_kappa.kappa([0, 1, 2, 3], [0, 1, 2, 2]))


if __name__ == "__main__":
    unittest.main()
