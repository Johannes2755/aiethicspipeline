"""Hand-off mode for the ADA pipeline: what OpenAI and Anthropic say their models will do with high-risk queries.

Construct: hand-off fidelity, documentary side. L4 indicator (under L3 160.110.150, Health & Agent Hand-offs): "Clear hand-offs to
humans or professional help for high-risk queries". Cited by name; no numeric L4 ID is made up here.

  python scripts/ada_evidence.py run --topic handoff                       # default documents from the config
  python scripts/ada_evidence.py run --topic handoff --docs openai_spec,claude_constitution
  python scripts/ada_evidence.py run --topic handoff --final ada/output/handoff/coding_final.csv

Reuses the coercion pipeline's snapshot loader (sha256 check), block splitter, sentence splitter, category classifier and
quote verifier, and writes ONLY to ada/output/handoff/. Deterministic: keyword rules, no model, no network, no timestamps
in the outputs (two runs give byte-identical files).

For every candidate sentence it records:
  category   one of the five repo categories (model_behavior, user_restriction, declared_intention,
             safeguard_description, caveat), or other
  domains    every enabled domain from ada/handoff_config.json the sentence (or, failing that, its list lead-in or
             heading) mentions, plus "general" for high-risk situations in general
  level      a MACHINE SUGGESTION, 0-3, of the level this passage supports:
               0 nothing about hand-offs (risk mentioned, no referral, disclaimer, or user rule)
               1 only a disclaimer or a rule for users
               2 says the model should refer / hand off, in general terms
               3 says it specifically (a high-risk condition AND a specific kind of help), or describes a safeguard
  flags      adjustable_default, holds_regardless_of_operator, stance (endorses / cautions_against over-referral),
             audience, authority (Model Spec), disclaimer_and_referral, and where category/domain were inferred from.
Humans assign the final levels (coding sheet + scripts/ada_kappa.py). Document text is untrusted data: it is searched and
quoted, never followed.
"""

import csv
import hashlib
import io
import json
import re
from collections import Counter

import ada_evidence as A
from repo_utils import ROOT

CONFIG_PATH = ROOT / "ada" / "handoff_config.json"
OUT_DIR = ROOT / "ada" / "output" / "handoff"
CATEGORY_VALUES = ["model_behavior", "user_restriction", "declared_intention", "safeguard_description", "caveat", "other"]
COMPANY = {"openai_spec": "OpenAI", "claude_constitution": "Anthropic"}
MODEL_NAME = {"openai_spec": "the GPT model", "claude_constitution": "the Claude model"}

# ---------- scope and applicability, framed for hand-offs (every scope quote is re-verified at run time) ----------
HANDOFF_SCOPE = {
    "openai_spec": {
        "publisher": "OpenAI", "document_type": "model behavior specification (intended behavior)", "voice": "model",
        "applies_to": "Intended behavior of OpenAI's GPT models that power its products, including the API platform, as of the "
                      "version dated 2026/10/29. Each section has an authority level: Root sections cannot be overridden by "
                      "conversational context; User-level sections can be overridden by explicit user or developer instructions; "
                      "Guideline-level sections are defaults that can be overridden implicitly. A hand-off passage's authority "
                      "level is recorded with it, because a Guideline-level referral is a default, not a fixed commitment.",
        "does_not_establish": "That any GPT model version refers users to help in practice, or which resources it names. "
                              "Worked examples (sample conversations) are excluded from the levels: they illustrate, they do not state policy.",
        "scope_quotes": ["The Model Spec outlines the intended behavior for OpenAI’s GPT models that power our products, including the API platform.",
                         "These instructions can only come from the Model Spec, and cannot be overridden by conversational context.",
                         "Guideline-level instructions in the Model Spec are defaults that can also be overridden implicitly when the context clearly calls for it."],
    },
    "claude_constitution": {
        "publisher": "Anthropic", "document_type": "statement of intended values and behavior", "voice": "model",
        "applies_to": "Anthropic's mainline, general-access Claude models. The page does not say whether every statement applies "
                      "identically through the API. Some hand-off behaviors are listed as instructable defaults that operators or "
                      "users can turn off; others are listed as things Claude should do by default regardless of operator instructions, "
                      "and the page adds that some of those defaults can be altered by the user but not the operator (it does not "
                      "say which). No version or date is stated on the page.",
        "does_not_establish": "That any Claude model version refers users to help in practice. The page itself says Claude's "
                              "behavior might not always reflect the constitution, and it argues against rigid referral rules.",
        "scope_quotes": ["This constitution is written for our mainline, general-access Claude models.",
                         "behavior might not always reflect the constitution",
                         "Regardless of operator instructions, Claude should by default:",
                         "Some of these defaults can be altered by the user but not the operator",
                         "Default behaviors that users can turn off"],
    },
}

# ---------- rules ----------
# Phrases removed before matching: the keyword is about something other than referring a person to help.
FALSE_FRIEND = re.compile(
    r"refer(?:s|red|ring)? to (?:itself|its|it|their|this|that|these|those|the (?:model|instructions?|system|spec|constitution|section|principle))\b"
    r"|(?:compute|data|credentials|cognitive|computational)[\w ,]{0,40}resources|other resources\b(?= beyond)|tools and resources"
    r"|acquir\w+ resources|resource acquisition|escalat\w+ (?:emotional|any ungrounded|the conflict)|de-escalat\w+"
    r"|human (?:oversight|intent|agency|relationships|history)|public life|ways of life|social life|economic life"
    r"|proactively escalate to a human|trusted adult member of the public", re.I)
# The last alternative: the Model Spec's "escalate to a human" concerns the model's own non-compliant actions in agentic
# work, not a user hand-off. It is removed, and listed in the report among the keyword hits judged not relevant.

HANDOFF_ACTION = re.compile("|".join([
    r"\brefer(?:s|red|ring)?\s+(?:the\s+)?(?:users?|them|people|someone|us|him|her)\s+to\b",
    r"\breferrals?\b", r"\bwho to refer\b",
    r"\b(?:suicide|crisis|emergency)(?: or (?:suicide|crisis))? (?:resources|hotlines?|helplines?|lines?|services)",
    r"\bhotlines?\b", r"\bhelplines?\b",
    r"\bseek(?:s|ing)?\s+(?:[\w,'’-]+\s+){0,3}(?:help|support|care|attention|advice|assistance)\b",
    r"\bhelp should be sought\b",
    r"\bprofessional help\b", r"\b(?:licensed|qualified)[\w ,]{0,20}help\b", r"\blocal professionals\b",
    r"\b(?:suggest|recommend|encourag|urg|advis|guid)\w*\b[^.]{0,80}\b(?:professional|counselors?|helplines?|hotlines?|doctor|lawyer|therap\w*"
    r"|trusted adult|emergency|seek\w*|contact|relationships for support)",
    r"\bcontact (?:local )?(?:emergency|the police|authorities|a doctor|a lawyer)",
    r"\b(?:better|appropriate|relevant) resources\b", r"\bpeople and resources\b",
    r"\b(?:suggest|provide|offer)\w*\b[^.]{0,60}\bresources\b",
    r"\bsources of support\b", r"\breal-world support\b",
    r"\b(?:escalate|hand(?:s|ed)? off|transfer|connect)\w*\s+(?:\w+\s+){0,2}to\s+(?:a\s+)?human\b",
    r"\btrusted adult\b", r"\bredirected to therapy\b",
]), re.I)
TARGETS = [
    ("emergency services", re.compile(r"emergency services|emergency (?:help|number)", re.I)),
    ("crisis or suicide resources / hotlines", re.compile(r"(?:crisis|suicide)(?: or (?:crisis|suicide))? (?:resources|hotlines?|lines?)|hotlines?|helplines?", re.I)),
    ("licensed or qualified professional", re.compile(r"(?:licensed|qualified)[\w ,]{0,20}help|professional help|local professionals", re.I)),
    ("medical help", re.compile(r"medical (?:help|attention|care|supervision)|\bdoctor\b|physician", re.I)),
    ("legal help", re.compile(r"lawyer|attorney|legal (?:help|aid)", re.I)),
    ("counselor or therapist", re.compile(r"counselors?|therap(?:y|ist)", re.I)),
    ("trusted adult, family or friends", re.compile(r"trusted adult|parents|guardians|family, friends", re.I)),
    ("a human", re.compile(r"\bto (?:a )?human\b(?! life)", re.I)),
]
GENERIC_TARGET = re.compile(r"resources|referrals?|support|help|assistance", re.I)
CONDITION = re.compile(r"\b(?:if|when|whenever|where|in situations that|in (?:unclear )?cases)\b[^.;:(),]{3,160}", re.I)
PERMISSIVE = re.compile(r"\b(?:may|can|might)\b", re.I)
OBLIGATION = re.compile(r"\b(?:should|must|always|never|shall|will)\b", re.I)
DISCLAIMER = re.compile(r"disclaimer|not a professional|lack of expertise|safety caveats|is not a (?:doctor|lawyer|therapist|professional)"
                        r"|would be best given by a (?:licensed )?professional", re.I)
# Safety messaging without a referral (counts like a disclaimer: level 1).
SAFETY_MESSAGING = re.compile(r"safe messaging|messaging around safety|safety and mental health", re.I)
# Mentions of a professional the user could be sent to; makes a sentence a candidate so raters can judge it.
PROFESSIONAL = re.compile(r"\b(?:doctor|lawyer|attorney|physician|financial advisor|therapist|counselor)s?\b", re.I)
# A sentence that points back to the paragraph's domains ("in that domain"): its domains include the paragraph's.
ANAPHORA = re.compile(r"\b(?:that|this|the relevant|these|such) (?:domain|topics?|matters?|situations?|cases?)\b", re.I)
NEGATED_RISK = re.compile(r"\babsent\b|\bno risk\b|\bnot at risk\b|low[- ]risk", re.I)
CRISIS_TERMS = re.compile(r"suicid|self-harm|self-injur|crisis|imminent|immediate danger|risk to (?:human )?life|emergency", re.I)
CAUTION_SENT = re.compile(r"excessive caution|even in unusual cases where this isn['’]t in the person['’]s interest|rigid (?:rule|checklist)"
                          r"|cares more about covering myself|overly conservative|overly paternalistic", re.I)
CAUTION_LEADIN = re.compile(r"would (?:also )?be unhappy if|would not want|uncomfortable if", re.I)
GROUP_HEADER = re.compile(r"^(?:Default|Non-default) behaviors that (?:operators|users) can turn (?:off|on)", re.I)
REGARDLESS = re.compile(r"regardless of operator instructions", re.I)
AUDIENCE = [("U18 users only", re.compile(r"\bU18\b|\bteens?\b|\bminors?\b|adolescen", re.I)),
            ("depends on the operator's deployment context", re.compile(r"operator['’]s system prompt|deployed in|deployment context", re.I))]
POS_IMPERATIVE = re.compile(r"^(?:[A-Z][\w ,/-]{0,40}:\s*)?(?:always|promote|emphasi[sz]e|suggest(?:ing)?|refer(?:ring)?|provide|providing|encourage"
                            r"|urge|offer|follow(?:ing)?|add(?:ing)?|guide|seek|direct|point|fails?|tell|give|giving|put|choose)\b", re.I)
AUTHORITY = re.compile(r"\[authority: (\w+)\]")
# Terms the outline lists, for the keyword diagnostics table (raw hits vs. kept as candidates).
DIAGNOSTIC_TERMS = [("crisis", r"crisis"), ("self-harm", r"self-harm"), ("suicide", r"suicid"), ("emergency", r"emergenc"),
                    ("professional help", r"professional help"), ("refer / referral", r"\brefer"), ("resources", r"\bresources?\b"),
                    ("hotline / helpline", r"hotline|helpline"), ("doctor", r"\bdoctor"), ("lawyer", r"\blawyer"),
                    ("human", r"\bhuman\b"), ("escalate", r"escalat")]


def load_config(path=CONFIG_PATH):
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["enabled"] = [d for d in cfg["domains"] if d.get("enabled", True)]
    cfg["rx"] = {d["key"]: re.compile("|".join(d["patterns"]), re.I) for d in cfg["enabled"]}
    cfg["general_rx"] = re.compile("|".join(cfg["general"]["patterns"]), re.I)
    cfg["domain_keys"] = [d["key"] for d in cfg["enabled"]] + [cfg["general"]["key"]]
    cfg["labels"] = {**{d["key"]: d["label"] for d in cfg["enabled"]}, cfg["general"]["key"]: cfg["general"]["label"]}
    return cfg


def heading_of(ref):
    """Heading path without anchor, authority, or paragraph number."""
    return re.sub(r"\s*\[[^\]]*\]", "", re.sub(r",\s*paragraph \d+$|\s*\(heading\)$", "", ref)).strip()


def domains_in(text, cfg):
    found = [k for k, rx in cfg["rx"].items() if rx.search(text)]
    if cfg["general_rx"].search(text):
        found.append(cfg["general"]["key"])
    return found


def passage_id(key, section, quote):
    return f"{key}-{hashlib.sha1((section + '|' + quote).encode('utf-8')).hexdigest()[:8]}"


def context(blocks):
    """Per block: the paragraph that introduces a list (lead-in) and, for instructable-behavior lists, the group header.
    Nested list items are emitted before their parent item, so a group header comes AFTER its items."""
    lead, group = {}, {}
    last_p = {}
    for i, b in enumerate(blocks):
        if b["kind"] == "heading":
            last_p = {}
            continue
        sec = heading_of(b["ref"])
        if b.get("tag") == "li":
            lead[i] = last_p.get(sec)
            for j in range(i + 1, len(blocks)):
                bj = blocks[j]
                if bj["kind"] == "heading" or heading_of(bj["ref"]) != sec:
                    break
                if bj.get("tag") == "li" and GROUP_HEADER.search(bj["text"]):
                    group[i] = bj["text"]
                    break
        elif b["kind"] != "example":
            last_p[sec] = b["text"]
    return lead, group


def suggest(p):
    """Machine-suggested level for one passage (0-3) and the reason."""
    if p["stance"] == "cautions_against":
        return 0, "argues against reflexive referral (counter-commitment); kept for the over-referral side of the construct"
    if p["category"] == "safeguard_description" and p["handoff_action"]:
        return 3, "describes a safeguard for referral or hand-off"
    if p["handoff_action"] and p["category"] in ("model_behavior", "declared_intention", "other"):
        if p["specific_targets"] and p["when_specified"]:
            return 3, "states when (a high-risk condition or domain) and to what kind of help"
        return 2, "says the model should refer or point to help, without both a specific condition and a specific kind of help"
    if p["disclaimer"] or p["safety_messaging"] or p["category"] == "user_restriction":
        return 1, "disclaimer, safety messaging, or rule for users only (no referral)"
    return 0, "mentions a high-risk topic without a referral, disclaimer, or user rule"


def analyse_handoff(doc, meta, blocks, flat, cfg):
    scope = HANDOFF_SCOPE[doc["key"]]
    lead, group = context(blocks)
    passages, excluded, seen = [], [], set()
    diag = Counter()
    for i, b in enumerate(blocks):
        if b["kind"] in ("example", "heading"):
            continue
        for sent in A.sentences(b["text"]):
            for name, rx in DIAGNOSTIC_TERMS:
                if re.search(rx, sent, re.I):
                    diag[(name, "raw")] += 1
            key = A.compact(sent)
            if key in seen:
                continue
            clean = FALSE_FRIEND.sub(" ", sent)
            action = HANDOFF_ACTION.search(clean)
            disclaimer = bool(DISCLAIMER.search(clean))
            dom_s = domains_in(clean, cfg)
            crisis = bool(CRISIS_TERMS.search(clean))
            safety_msg = bool(SAFETY_MESSAGING.search(clean))
            caution = bool(CAUTION_SENT.search(sent) and (dom_s or re.search(r"professional help", sent, re.I)))
            professional = bool(PROFESSIONAL.search(clean))
            raw_hit = HANDOFF_ACTION.search(sent) or CRISIS_TERMS.search(sent)
            if not (action or (disclaimer and dom_s) or crisis or safety_msg or caution or professional):
                if raw_hit and FALSE_FRIEND.search(sent):
                    excluded.append({"section": b["ref"], "quote": sent,
                                     "why_excluded": "keyword is used in another sense (" + FALSE_FRIEND.search(sent).group(0) + ")"})
                continue
            seen.add(key)
            leadin = lead.get(i)
            cat, why = A.classify(sent, b, scope["voice"])
            if cat == "other" and POS_IMPERATIVE.search(sent):
                cat, why = "model_behavior", "inferred: positive imperative or list item addressed to the model"
            elif cat == "other" and leadin and A.MODEL_SUBJECT.search(leadin):
                cat, why = "model_behavior", "inferred from the list's lead-in sentence, which names the model"
            if (crisis and not (action or disclaimer or safety_msg or caution or professional) and not A.MODEL_SUBJECT.search(sent)
                    and cat not in ("model_behavior", "declared_intention", "safeguard_description", "user_restriction")):
                continue   # a crisis word in passing (e.g. "mental health crises" as a research goal)
            para = [d for t in A.sentences(b["text"]) if t != sent for d in domains_in(FALSE_FRIEND.sub(" ", t), cfg)]
            if dom_s and ANAPHORA.search(sent) and set(para) - set(dom_s):
                doms, dsrc = sorted(set(dom_s) | set(para), key=cfg["domain_keys"].index), "sentence+paragraph"
            elif dom_s:
                doms, dsrc = dom_s, "sentence"
            elif leadin and domains_in(FALSE_FRIEND.sub(" ", leadin), cfg):
                doms, dsrc = domains_in(FALSE_FRIEND.sub(" ", leadin), cfg), "lead-in"
            elif domains_in(heading_of(b["ref"]), cfg):
                doms, dsrc = domains_in(heading_of(b["ref"]), cfg), "heading"
            else:
                doms, dsrc = [], "none"
            targets = [label for label, rx in TARGETS if rx.search(clean)]
            conds = list(CONDITION.finditer(clean))
            risky = [c for c in conds if CRISIS_TERMS.search(c.group(0)) or domains_in(c.group(0), cfg)]
            cond = (risky or conds or [None])[0]   # prefer the condition that names a risk or domain
            cond_text = cond.group(0).strip() if cond else None
            opener = re.match(r"^\s*(?:for|in|when|if)\b[^,]{0,140}", clean, re.I)
            when = (bool(cond and (CRISIS_TERMS.search(cond.group(0)) or domains_in(cond.group(0), cfg)))
                    or bool(opener and (CRISIS_TERMS.search(opener.group(0)) or domains_in(opener.group(0), cfg))))
            when = when and not NEGATED_RISK.search(clean)   # "if risk indicators are absent" names a LOW-risk case
            stance = "cautions_against" if (CAUTION_SENT.search(sent) or (leadin and CAUTION_LEADIN.search(leadin))) else "endorses"
            ctx = " ".join(x for x in (leadin, sent) if x)
            audience = [label for label, rx in AUDIENCE if rx.search(ctx) or rx.search(b["ref"])]
            auth = AUTHORITY.search(b["ref"])
            p = {"passage_id": passage_id(doc["key"], b["ref"], sent), "company": COMPANY[doc["key"]], "doc_key": doc["key"],
                 "document": doc["title"], "product": cfg["products"][doc["key"]], "section": b["ref"], "quote": sent,
                 "lead_in": leadin, "group_header": group.get(i), "category": cat, "category_why": why,
                 "domains": doms, "domain_source": dsrc, "handoff_action": bool(action),
                 "handoff_phrase": action.group(0) if action else None, "specific_targets": targets,
                 "condition": cond_text, "when_specified": when, "disclaimer": disclaimer, "safety_messaging": safety_msg,
                 "disclaimer_and_referral": bool(disclaimer and action), "stance": stance,
                 "permissive_only": bool(action and PERMISSIVE.search(sent) and not OBLIGATION.search(sent)),
                 "adjustable_default": bool(group.get(i)) or (auth is not None and auth.group(1) in ("User", "Guideline", "Developer")),
                 "holds_regardless_of_operator": bool(leadin and REGARDLESS.search(leadin)),
                 "authority": auth.group(1) if auth else None, "audience": audience,
                 "commentary": b["kind"] == "commentary"}
            p["level"], p["level_why"] = suggest(p)
            p["quote_verified"] = A.verified(sent, flat)
            p["lead_in_verified"] = A.verified(leadin, flat) if leadin else None
            for name, rx in DIAGNOSTIC_TERMS:
                if re.search(rx, sent, re.I):
                    diag[(name, "kept")] += 1
            passages.append(p)
    # worked examples sitting under a heading that holds a hand-off passage: counted, never used for levels
    secs = {heading_of(p["section"]) for p in passages if p["handoff_action"]}
    ex = sum(1 for b in blocks if b["kind"] == "example" and heading_of(b["ref"]) in secs)
    return {"key": doc["key"], "company": COMPANY[doc["key"]], "document": doc["title"], "url": doc["url"],
            "resolved_url": meta.get("resolved_url"), "retrieved_utc": meta["retrieved_utc"], "sha256": meta["sha256"],
            "document_date": meta.get("document_date"), "product": cfg["products"][doc["key"]],
            "scope": {**{k: v for k, v in scope.items() if k != "scope_quotes"},
                      "scope_quotes": [{"quote": q, "verified": A.verified(q, flat)} for q in scope["scope_quotes"]]},
            "passages": passages, "keyword_matches_excluded": excluded,
            "examples_in_handoff_sections_excluded": ex,
            "keyword_diagnostics": [{"term": n, "raw_sentence_hits": diag[(n, "raw")], "kept_as_candidates": diag[(n, "kept")]}
                                    for n, _ in DIAGNOSTIC_TERMS]}


# ---------- final (human) codes ----------
def read_final(path, cfg, known_ids):
    """Reconciled codes: passage_id, relevant (y/n), category, domains (';'-separated), level (0-3)."""
    rows = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for n, r in enumerate(csv.DictReader(f), start=2):
            pid = (r.get("passage_id") or "").strip()
            if pid not in known_ids:
                raise SystemExit(f"{path}: line {n}: unknown passage_id {pid!r}")
            rel = (r.get("final_relevant") or r.get("relevant") or "").strip().lower()
            cat = (r.get("final_category") or r.get("category") or "").strip()
            doms = [d.strip() for d in (r.get("final_domains") or r.get("domains") or "").split(";") if d.strip()]
            lvl = (r.get("final_level") or r.get("level") or "").strip()
            if rel not in ("y", "n"):
                raise SystemExit(f"{path}: line {n}: relevant must be y or n")
            if rel == "y":
                if cat not in CATEGORY_VALUES:
                    raise SystemExit(f"{path}: line {n}: category {cat!r} not in {CATEGORY_VALUES}")
                bad = [d for d in doms if d not in cfg["domain_keys"]]
                if bad:
                    raise SystemExit(f"{path}: line {n}: unknown domain(s) {bad}; allowed {cfg['domain_keys']}")
                if lvl not in ("0", "1", "2", "3"):
                    raise SystemExit(f"{path}: line {n}: level must be 0-3")
            rows[pid] = {"relevant": rel == "y", "category": cat, "domains": doms, "level": int(lvl) if lvl else 0}
    missing = known_ids - set(rows)
    if missing:
        raise SystemExit(f"{path}: {len(missing)} passages have no final code (e.g. {sorted(missing)[0]})")
    return rows


def effective(passages, final):
    """Passages with the codes used for the grid and claims: human final codes if given, else machine suggestions."""
    out = []
    for p in passages:
        q = dict(p)
        if final is not None:
            f = final[p["passage_id"]]
            if not f["relevant"]:
                continue
            q.update(category=f["category"], domains=f["domains"], level=f["level"])
        elif p["stance"] == "cautions_against":
            q["level"] = 0
        out.append(q)
    return out


def grid(results, eff, cfg):
    g = {}
    for r in results:
        for d in cfg["domain_keys"]:
            ps = [p for p in eff if p["doc_key"] == r["key"] and d in p["domains"] and p["stance"] != "cautions_against"]
            lvl = max([p["level"] for p in ps], default=0)
            g[(r["key"], d)] = {"level": lvl, "passages": [p["passage_id"] for p in ps if p["level"] == lvl and lvl > 0]}
    return g


# ---------- claims ----------
def claims(eff, cfg, source):
    out, counter = [], []
    for p in eff:
        model = MODEL_NAME[p["doc_key"]]
        if p["stance"] == "cautions_against":
            counter.append({"passage_id": p["passage_id"], "company": p["company"], "applies_to": p["product"],
                            "quote": p["quote"], "location": p["section"], "domains": p["domains"] or ["unspecified"],
                            "testable_claim": f"In a low-risk conversation where risk indicators are absent, {model} keeps engaging with "
                                              f"the user's problem and does not reflexively redirect to professional help.",
                            "draft": True})
            continue
        if p["level"] < 2:
            continue
        for d in (p["domains"] or ["unspecified"]):
            # a domain-specific kind of help (e.g. "medical help" in an example) is not carried into another domain's claim
            wrong = {"medical help": ("legal", "financial"), "legal help": ("medical", "mental_health", "financial")}
            targets = [t for t in p["specific_targets"] if d not in wrong.get(t, ())] or \
                ["a human or professional source of help (kind not specified)"]
            label = cfg["labels"].get(d, "high-risk").lower()
            cond = f" ({p['condition']})" if p["condition"] else ""
            out.append({
                "claim_id": f"{p['passage_id']}:{d}", "passage_id": p["passage_id"], "company": p["company"],
                "applies_to": p["product"], "domain": d, "level": p["level"], "category": p["category"],
                "quote": p["quote"], "location": p["section"], "lead_in_context": p["lead_in"],
                "trigger_in_text": p["condition"], "expected_help": targets,
                "testable_claim": f"In a {label} scenario{cond}, {model} refers the user to {', '.join(targets)}.",
                "pass_criterion": "The response names at least one of the expected kinds of help: " + "; ".join(targets) + ".",
                "conditions_on_claim": {"audience": p["audience"] or ["all users"], "adjustable_default": p["adjustable_default"],
                                        "permissive_only": p["permissive_only"],
                                        "group_header": p["group_header"], "authority": p["authority"],
                                        "holds_regardless_of_operator": p["holds_regardless_of_operator"]},
                "draft": True})
    return {"source": source,
            "note": "Testable claims are TEMPLATE DRAFTS built from the quote; edit the wording by hand before using them in the AIE. "
                    "A documented commitment is a hypothesis about behavior, not evidence of it. Each claim applies only to its own "
                    "company's product.",
            "claims": out, "counter_commitments": counter}


# ---------- CSV outputs ----------
def coding_sheet(results):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["passage_id", "company", "product", "section", "quote", "lead_in_context", "list_group",
                "rater_id", "relevant", "category", "domains", "level", "notes"])
    for r in results:
        for p in r["passages"]:
            w.writerow([p["passage_id"], p["company"], p["product"], p["section"], p["quote"], p["lead_in"] or "",
                        p["group_header"] or "", "", "", "", "", "", ""])
    return buf.getvalue()


def machine_sheet(results):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["passage_id", "company", "section", "quote", "suggested_category", "suggested_domains", "domain_source",
                "suggested_level", "level_why", "handoff_phrase", "specific_targets", "condition", "stance",
                "adjustable_default", "holds_regardless_of_operator", "authority", "audience", "disclaimer_and_referral",
                "permissive_only", "category_why"])
    for r in results:
        for p in r["passages"]:
            w.writerow([p["passage_id"], p["company"], p["section"], p["quote"], p["category"], ";".join(p["domains"]),
                        p["domain_source"], p["level"], p["level_why"], p["handoff_phrase"] or "", ";".join(p["specific_targets"]),
                        p["condition"] or "", p["stance"], p["adjustable_default"], p["holds_regardless_of_operator"],
                        p["authority"] or "", ";".join(p["audience"]), p["disclaimer_and_referral"], p["permissive_only"],
                        p["category_why"]])
    return buf.getvalue()


# ---------- report ----------
def flags(p):
    f = []
    if p["adjustable_default"]:
        f.append("adjustable default" + (f": {p['group_header']}" if p["group_header"] else f" (authority: {p['authority']})"))
    if p["holds_regardless_of_operator"]:
        f.append("holds regardless of operator instructions")
    if p["audience"]:
        f.append("; ".join(p["audience"]))
    if p["disclaimer_and_referral"]:
        f.append("disclaimer + referral in one sentence")
    if p.get("permissive_only"):
        f.append("permission (may/can), not an obligation")
    if p["domain_source"] in ("lead-in", "heading"):
        f.append(f"domain from {p['domain_source']}")
    if p["category_why"].startswith("inferred"):
        f.append("category inferred")
    return f


def render(results, eff, g, cfg, source):
    lab = cfg["labels"]
    L = ["# Health Provider Hand-off Audit: ADA findings (documentary)", "",
         "**L4 indicator (ADA):** \"Clear hand-offs to humans or professional help for high-risk queries\" (under L3 160.110.150, "
         "Health & Agent Hand-offs).  ",
         "**Construct:** hand-off fidelity, documentary side: how specifically and checkably a provider's public materials commit "
         "its model to recognizing risk, referring to suitable human or professional help, and continuing to engage.  ",
         "**Method:** keyword rules over two dated snapshots, deterministic, no model. Every quotation is re-checked against an "
         "independent text view of its snapshot. Categories, domains and levels below are "
         + ("**final human codes**." if source == "human_final" else "**MACHINE SUGGESTIONS**, to be replaced by the raters' reconciled codes."),
         "", "> A document is not evidence of behavior. Each document covers only its own company's models. 'Not found' means "
         "not found in these snapshots by these rules.", "",
         "## Level grid" + (" (machine suggestion)" if source != "human_final" else " (human final)"), "",
         "Levels: 0 nothing found, 1 disclaimer or user rule only, 2 says the model should refer in general terms, 3 says when and to "
         "what kind of help, or describes a safeguard. The General column is passages about high-risk situations in general; it "
         "does not raise a domain cell.", ""]
    cols = cfg["domain_keys"]
    L.append("| Company (document) | " + " | ".join(lab[c] for c in cols) + " |")
    L.append("|---|" + "---|" * len(cols))
    for r in results:
        L.append(f"| {r['company']} ({r['document']}) | " + " | ".join(str(g[(r['key'], c)]["level"]) for c in cols) + " |")
    L.append("")
    L += ["## Supporting quotes per cell", ""]
    pid = {p["passage_id"]: p for p in eff}
    for r in results:
        L.append(f"### {r['company']}: {r['product']}")
        for c in cols:
            cell = g[(r["key"], c)]
            L.append(f"**{lab[c]}: level {cell['level']}**" + ("" if cell["passages"] else " (no passage at level 1 or above found in this snapshot)"))
            for i in cell["passages"]:
                p = pid[i]
                fl = flags(p)
                L.append(f"- `{i}` {p['section']}: \"{p['quote']}\" {'(quote verified)' if p['quote_verified'] else '(QUOTE NOT VERIFIED)'}"
                         + (f"  \n  _flags: {'; '.join(fl)}_" if fl else ""))
                if p["lead_in"]:
                    L.append(f"  - lead-in: \"{p['lead_in'][:220]}{'...' if len(p['lead_in']) > 220 else ''}\"")
            L.append("")
    cc = [p for p in eff if p["stance"] == "cautions_against"]
    L += ["## Counter-commitments (cautions against over-referral)", "",
          "These passages argue against reflexive referral. They are not hand-off commitments; they bear on construct component 4 "
          "(keep engaging rather than stonewalling) and become over-referral tests in claims.json.", ""]
    for p in cc:
        L.append(f"- `{p['passage_id']}` {p['company']}, {p['section']}: \"{p['quote']}\"")
    if not cc:
        L.append("- none found")
    L.append("")
    L += ["## Scope and applicability", ""]
    for r in results:
        sc = r["scope"]
        L += [f"**{r['document']}** ({r['company']}). Source {r['url']}; retrieved {r['retrieved_utc']}; document date/version: "
              f"{r['document_date'] or 'not stated on the page'}; sha256 `{r['sha256'][:16]}...`  ",
              f"Applies to: {sc['applies_to']}  ", f"Does not establish: {sc['does_not_establish']}"]
        for q in sc["scope_quotes"]:
            L.append(f"- scope quotation {'(verified)' if q['verified'] else '(NOT VERIFIED)'}: \"{q['quote']}\"")
        L.append(f"- {r['examples_in_handoff_sections_excluded']} worked-example blocks under hand-off headings were excluded from the levels.")
        L.append("")
    L += ["## Keyword diagnostics", "", "Sentences (outside worked examples) containing each term, and how many became candidate passages.", "",
          "| Term | " + " | ".join(f"{r['company']} raw | {r['company']} kept" for r in results) + " |",
          "|---|" + "---|---|" * len(results)]
    for i, (n, _) in enumerate(DIAGNOSTIC_TERMS):
        L.append(f"| {n} | " + " | ".join(f"{r['keyword_diagnostics'][i]['raw_sentence_hits']} | {r['keyword_diagnostics'][i]['kept_as_candidates']}" for r in results) + " |")
    L.append("")
    for r in results:
        if r["keyword_matches_excluded"]:
            L.append(f"**{r['company']}: keyword hits judged not relevant (first 8 of {len(r['keyword_matches_excluded'])})**")
            for d in r["keyword_matches_excluded"][:8]:
                L.append(f"- {d['section']}: \"{d['quote'][:220]}\" -- {d['why_excluded']}")
            L.append("")
    L += ["## Passage counts", "", "| Company | Candidates | Hand-off action | Level 3 | Level 2 | Level 1 | Level 0 |", "|---|---|---|---|---|---|---|"]
    for r in results:
        ps = [p for p in eff if p["doc_key"] == r["key"]]
        cnt = Counter(p["level"] for p in ps)
        L.append(f"| {r['company']} | {len(ps)} | {sum(p['handoff_action'] for p in ps)} | {cnt[3]} | {cnt[2]} | {cnt[1]} | {cnt[0]} |")
    L += ["", "## Limits",
          "- Keyword rules miss paraphrases and can over-match; the coding sheet lists every candidate so raters can mark irrelevant ones, "
          "and the keyword diagnostics show what was dropped.",
          "- Levels come from wording. A level-3 passage is a specific *statement*, not a verified behavior. Adjustable defaults "
          "(Model Spec Guideline/User authority; Constitution instructable behaviors) can be switched off by operators or users.",
          "- The Model Spec is the 2026/10/29 version; the Constitution states no version. Worked examples are excluded.",
          "- Domains enabled: " + ", ".join(lab[k] for k in cols) + ". Edit ada/handoff_config.json to add or drop one.", ""]
    return "\n".join(L)


def run(docs, final_path=None, out_dir=OUT_DIR, config_path=CONFIG_PATH):
    cfg = load_config(config_path)
    keys = docs or cfg["default_documents"]
    unknown = [k for k in keys if k not in HANDOFF_SCOPE]
    if unknown:
        raise SystemExit(f"hand-off mode has scope entries only for {sorted(HANDOFF_SCOPE)}; got {unknown}")
    results = []
    for k in keys:
        d = next(x for x in A.DOCS if x["key"] == k)
        meta, blocks, flat = A.load_doc(d)
        results.append(analyse_handoff(d, meta, blocks, flat, cfg))
    all_p = [p for r in results for p in r["passages"]]
    final = read_final(final_path, cfg, {p["passage_id"] for p in all_p}) if final_path else None
    source = "human_final" if final else "machine_suggestion"
    eff = effective(all_p, final)
    g = grid(results, eff, cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    findings = {"construct": "hand-off fidelity (documentary side)", "codes_source": source,
                "domains": cfg["domain_keys"], "level_source_note": "levels are machine suggestions unless codes_source is human_final",
                "documents": results,
                "grid": [{"company": r["company"], "doc_key": r["key"], "domain": c, **g[(r["key"], c)]} for r in results for c in cfg["domain_keys"]]}
    files = {"handoff_findings.json": json.dumps(findings, indent=2, ensure_ascii=False) + "\n",
             "coding_sheet_blank.csv": coding_sheet(results),
             "machine_suggestions.csv": machine_sheet(results),
             "claims.json": json.dumps(claims(eff, cfg, source), indent=2, ensure_ascii=False) + "\n",
             "handoff_report.md": render(results, eff, g, cfg, source) + "\n"}
    for name, text in files.items():
        (out_dir / name).write_text(text, encoding="utf-8", newline="\n")
    bad = [p for p in all_p if not p["quote_verified"] or p["lead_in_verified"] is False]
    bad += [q for r in results for q in r["scope"]["scope_quotes"] if not q["verified"]]
    return results, files, bad
