# Health Provider Hand-off Audit

DS684 AI Ethics (Boston University, Prof. Wesley Wildman), Assignment 3: Ethics Category Measurement Paper.
Team: Neeza Singh and John Bestavros.

**Question.** When a chat model gets a high-risk query (mental health crisis, medical emergency, legal jeopardy, a
financial scam in progress), does it hand the user off to suitable human or professional help? And does that match
what its provider says the model will do?

**Construct: hand-off fidelity.** The degree to which a chat model given a high-risk query (1) recognizes the risk,
(2) refers the user to a human or professional resource suited to the domain and severity, (3) makes that referral
specific, actionable, and prominent, (4) keeps engaging supportively rather than stonewalling, and (5) sustains this
across turns. The documentary counterpart is the degree to which a provider's public materials commit to these
behaviors in specific, checkable terms.

| | ADA (automated document analysis) | AIE (automated instrumented evaluation) |
|---|---|---|
| L4 indicator (under L3 160.110.150, Health & Agent Hand-offs) | "Clear hand-offs to humans or professional help for high-risk queries" | "Performs appropriate referral or hand-off in scenario tests" |
| Evidence | What OpenAI and Anthropic **say** their models will do | What the models **do** in scenario tests |
| Owner | Neeza | John |

Concordance: every Level 2/3 documented commitment from the ADA becomes a testable claim (`ada/output/handoff/claims.json`)
that the AIE checks against observed behavior. Companies: OpenAI and Anthropic only. Their documents are the only ones
here that describe how the model itself should behave, and testing the same two companies in the AIE lets every "says"
be checked against a "does".

Both L4 indicators are cited by name; no numeric L4 IDs are made up here.

## ADA: hand-off mode

Documents (existing snapshots, hashes unchanged, nothing downloaded):

| Key | Document | Version | Applies to |
|---|---|---|---|
| `openai_spec` | OpenAI Model Spec, `ada/snapshots/openai_model_spec_2026-10-29.html` | dated 2026/10/29 | OpenAI's GPT models, products and API |
| `claude_constitution` | Claude's Constitution, `ada/snapshots/claude_constitution.html` | none stated; retrieved 2026-11-10 04:08 UTC | Anthropic's mainline, general-access Claude models |

```bash
python scripts/ada_evidence.py run --topic handoff
```

This writes `ada/output/handoff/`:

| File | What it is |
|---|---|
| `handoff_report.md` | 2 companies x 4 domains (+ General) level grid, supporting quotes with locations, scope notes, counter-commitments, keyword diagnostics, limits |
| `handoff_findings.json` | every candidate passage with category, domains, suggested level, flags, and verification status |
| `coding_sheet_blank.csv` | one row per passage, blank `rater_id / relevant / category / domains / level / notes`. It has no machine suggestions, so raters aren't anchored |
| `machine_suggestions.csv` | the script's suggested category, domains and level per passage, with reasons and flags |
| `claims.json` | Level 2/3 passages as draft testable claims for the AIE, plus counter-commitments (over-referral tests) |

Method in brief:
1. Split each snapshot into blocks with section references, then into sentences. Worked examples are excluded.
2. Keyword rules find sentences about referral or hand-off, crisis or risk, disclaimers, safety messaging, and
   professionals. A list of false matches removes other senses (e.g. "human oversight", "compute resources",
   "escalate to a human" about the model's own agentic errors).
3. Each passage gets one of the five categories (`model_behavior`, `user_restriction`, `declared_intention`,
   `safeguard_description`, `caveat`, else `other`). It also gets one or more domains from `ada/handoff_config.json`,
   plus `general` for high-risk situations in general.
4. A **machine-suggested** level from 0 to 3:
   - 0: nothing found
   - 1: disclaimer or user rule only
   - 2: refer, in general terms
   - 3: a high-risk condition and a specific kind of help, or a safeguard

   A cell's suggestion is the highest level among its passages. The General column never raises a domain cell.
5. Flags for review: adjustable defaults (Model Spec Guideline/User/Developer authority; Constitution instructable
   behaviors), "regardless of operator instructions", audience (e.g. U18 only), permission vs. obligation,
   disclaimer + referral in one sentence, stance (cautions against over-referral), and categories or domains that
   were inferred.
6. Every quote, list lead-in and scope quote is re-verified against an independent text view of its snapshot. A run
   with any unverified quote exits non-zero. Deterministic: there is no model call and no timestamps, so reruns are
   byte-identical.

The domain list is a config value. Interpersonal violence or abuse is defined but `"enabled": false`. Set it to true
to add the column.

## Inter-rater coding

1. Copy `ada/output/handoff/coding_sheet_blank.csv` once per rater (e.g. `coding_neeza.csv`, `coding_john.csv`) and
   code independently.
2. Compute agreement:

```bash
python scripts/ada_kappa.py ada/output/handoff/coding_neeza.csv ada/output/handoff/coding_john.csv
```

This reports Cohen's kappa for relevance, category, and each domain, plus unweighted and quadratic-weighted kappa for
level, each with a bootstrap 95% CI, and agreement on the implied grid cells. It also writes `reconcile.csv` with both
raters side by side, pre-filling the final columns where you agree.

3. Fill in the remaining `final_*` cells in `reconcile.csv`, then rebuild the grid, report and claims from the human
   codes:

```bash
python scripts/ada_evidence.py run --topic handoff --final ada/output/handoff/reconcile.csv
```

## AIE (John)

`scripts/aie_demo.py`, `aie/prompts.json`, `aie/rubric_adaptation.md`, `aie/saved/`. These still hold the TA's
financial-coercion setup and runs, which are to be replaced with the hand-off scenarios. See [TA_DEMO.md](TA_DEMO.md)
for how the tool works (providers, keys, subscription connections, replay).

## The original coercion ADA (kept working, unchanged output)

```bash
python scripts/ada_evidence.py run
```

This reads all five snapshots (also Google, Gemini app, Llama) and writes `ada/output/ada_findings.{md,json}` exactly
as before. A test checks that the output still matches the committed files.

## Tests

```bash
python -m unittest discover -s tests
```

- `test_offline.py`: the TA's tests (ADA coercion, AIE coherence, saved runs, clients).
- `test_handoff.py`: the hand-off mode, kappa, and an unchanged-output check for the coercion run.
- `test_connections.py`: subscription connections (local stubs).

Python 3.10+. ADA and kappa need only the standard library. `requirements-live.txt` is for live AIE runs.

## Limits (ADA)

- Keyword search can miss paraphrases. "Not found" means not found in these two snapshots by these rules.
- A document is not evidence of behavior. Each document covers only its own company's models.
- Levels are machine suggestions until replaced by reconciled human codes.

## Provenance

Forked from <https://github.com/Johannes2755/llm_safety_evaluation_pipeline>, the TA's teaching fork for this
assignment, which demonstrated ADA + AIE on financial coercion. That fork comes from
<https://github.com/TheFlashhhh/llm_safety_evaluation_pipeline>, and the original project is
<https://github.com/pratrt141098/llm_safety_evaluation_pipeline>; see the commit history for authorship. The original
project's historical prompts, responses, scores, notebook and classroom demo were removed here as unused by this study.
They remain in the git history and upstream. The original repository has no license file and none is added here, so
check with the original authors before redistributing beyond coursework.
