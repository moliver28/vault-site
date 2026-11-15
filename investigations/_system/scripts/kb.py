#!/usr/bin/env python3
"""Knowledge base engine: config, canonisation, claims, and the contribution gate.

Three ideas, one engine.

1. Config is layered. `universal.yaml` holds defaults that never mention a case;
   `kb/<case>.yaml` inherits and extends them. A case can override a threshold,
   add entities, and add blocked surfaces without touching the universal file.

2. Canonisation is a cascade, not a lookup. A mention is resolved by exact
   alias, then normalised form, then acronym, then fuzzy match, then embedding
   similarity. Every resolution carries its method and score, so a reviewer can
   see why a link was made.

3. Claims are variables too. A claim is fingerprinted from its normalised
   subject, predicate, object, and polarity. The same claim in three notes gets
   one ID and three citations, which is what makes contradiction detection
   possible at all.

Commands
  resolve "Lindholm"              show the cascade for one surface form
  audit [--case SLUG] [--write]   canonisation and claim report for the vault
  contribute NOTE --agent NAME    run the contribution gate on one note
  check [--strict]                validate every note against the contract
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher

import yaml

VAULT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
INV = os.path.join(VAULT, "Investigations")
KB = os.path.join(INV, "_system", "kb")
DATA = os.path.join(INV, "_system", "data")
sys.path.insert(0, os.path.join(INV, "_system", "scripts"))

HONORIFICS = {"mr", "mrs", "ms", "dr", "prof", "sir", "hon", "rev", "councilmember", "mayor", "sen", "rep"}


# ---------------------------------------------------------------- config

def load_config(case: str | None) -> dict:
    uni = yaml.safe_load(open(os.path.join(KB, "universal.yaml")))
    cfg = json.loads(json.dumps(uni))
    if not case:
        return cfg
    path = os.path.join(KB, f"{case}.yaml")
    if not os.path.exists(path):
        return cfg
    proj = yaml.safe_load(open(path)) or {}
    for k, v in proj.items():
        if k in {"inherits", "entities", "blocked_surfaces_add"}:
            continue
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k] = {**cfg[k], **v}
        else:
            cfg[k] = v
    cfg.setdefault("blocked_surfaces", [])
    cfg["blocked_surfaces"] = list(dict.fromkeys(cfg["blocked_surfaces"] + (proj.get("blocked_surfaces_add") or [])))
    cfg["entities"] = proj.get("entities") or []
    cfg["case"] = case
    return cfg


def cases() -> list[str]:
    return sorted(f[:-5] for f in os.listdir(KB) if f.endswith(".yaml") and f != "universal.yaml")


# ---------------------------------------------------------------- canon

def key(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    toks = [t for t in s.split() if t not in HONORIFICS]
    return " ".join(toks)


def initials(s: str) -> str:
    return "".join(w[0] for w in key(s).split() if w)


def hash_id(prefix: str, text: str, n: int) -> str:
    return f"{prefix}-{hashlib.sha1(text.encode()).hexdigest()[:n]}"


def build_registry(cfg: dict) -> dict:
    """Config entities, entity notes, and profiles all feed one registry."""
    reg: dict[str, dict] = {}

    def add(name, etype, aliases, source, target=None):
        k = key(name)
        if not k:
            return
        ent = reg.setdefault(k, {"id": hash_id("ENT", k, 8), "name": name, "type": etype,
                                 "aliases": [], "sources": [], "target": target})
        for a in [name] + list(aliases or []):
            if a and a not in ent["aliases"]:
                ent["aliases"].append(a)
        if source not in ent["sources"]:
            ent["sources"].append(source)

    for e in cfg.get("entities", []):
        add(e["name"], e.get("type", "organization"), e.get("aliases", []), "config")

    edir = os.path.join(INV, "Entities")
    if os.path.isdir(edir):
        for f in sorted(os.listdir(edir)):
            if not f.endswith(".md"):
                continue
            text = open(os.path.join(edir, f), encoding="utf-8", errors="replace").read()
            fm = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
            aliases = []
            etype = "organization"
            if fm:
                m = re.search(r"^aliases:\s*\[(.*?)\]", fm.group(1), re.M)
                if m:
                    aliases = [x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()]
                m = re.search(r"^entity_type:\s*(\S+)", fm.group(1), re.M)
                if m:
                    etype = m.group(1)
            add(f[:-3].replace("-", " "), etype, aliases, "entity-note",
                os.path.relpath(os.path.join(edir, f), VAULT)[:-3])

    pdir = os.path.join(INV, "2608-pierce-leadership", "record", "profiles")
    if os.path.isdir(pdir):
        for f in sorted(os.listdir(pdir)):
            if not f.endswith(".md"):
                continue
            text = open(os.path.join(pdir, f), encoding="utf-8", errors="replace").read()
            m = re.search(r"^#\s*(.+?)\s*$", text, re.M)
            name = re.sub(r"\s*[—–-]\s*Deep Profile\.?\s*$", "", m.group(1)) if m else f[:-3]
            add(name, "organization", [], "profile",
                os.path.relpath(os.path.join(pdir, f), VAULT)[:-3])

    # Merge the same concept discovered from different sources. A profile
    # titled "JBLM" and a config entity "Joint Base Lewis-McChord" are one
    # base, not two nodes; left alone they split every link about it.
    priority = {"config": 3, "entity-note": 2, "profile": 1}
    changed = True
    while changed:
        changed = False
        by_name = {key(e["name"]): k for k, e in reg.items()}
        for k in list(reg):
            ent = reg.get(k)
            if not ent:
                continue
            for a in list(ent["aliases"]) + list(ent.get("keys", [])):
                other_k = by_name.get(key(a))
                if not other_k or other_k == k or other_k not in reg:
                    continue
                other = reg[other_k]
                keep, drop = (k, other_k)
                if priority.get(other["sources"][0], 0) > priority.get(ent["sources"][0], 0):
                    keep, drop = other_k, k
                elif priority.get(other["sources"][0], 0) == priority.get(ent["sources"][0], 0):
                    if len(other["name"]) > reg[keep]["name"]:
                        keep, drop = other_k, k
                K, D = reg[keep], reg[drop]
                K["aliases"] = list(dict.fromkeys(K["aliases"] + D["aliases"] + [D["name"]]))
                K["sources"] = list(dict.fromkeys(K["sources"] + D["sources"]))
                K["target"] = K.get("target") or D.get("target")
                reg.pop(drop, None)
                changed = True
                break
            if changed:
                break

    for k, ent in reg.items():
        ent["keys"] = [key(a) for a in ent["aliases"]]
    return reg


EMBED_CACHE: dict[str, list[float]] = {}


def embed(text: str) -> list[float] | None:
    if text in EMBED_CACHE:
        return EMBED_CACHE[text]
    try:
        import urllib.request
        body = json.dumps({"model": "nomic-embed-text:latest", "prompt": text[:2000]}).encode()
        r = urllib.request.Request("http://localhost:11434/api/embeddings", data=body)
        vec = json.loads(urllib.request.urlopen(r, timeout=8).read())["embedding"]
        EMBED_CACHE[text] = vec
        return vec
    except Exception:  # noqa: BLE001
        return None


def cos(a, b) -> float:
    d = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1
    nb = math.sqrt(sum(x * x for x in b)) or 1
    return d / (na * nb)


def resolve(surface: str, reg: dict, cfg: dict, use_embedding: bool = True) -> list[dict]:
    """Cascade: exact, normalised, acronym, fuzzy, embedding. Ranked."""
    k = key(surface)
    out = []
    for ent in reg.values():
        if k == key(ent["name"]):
            out.append({"entity": ent, "method": "exact", "score": 1.0})
            continue
        if k in ent["keys"]:
            out.append({"entity": ent, "method": "alias", "score": cfg["thresholds"]["normalized"]})
            continue
        if len(k) >= 2 and k == initials(ent["name"]).lower():
            out.append({"entity": ent, "method": "acronym", "score": cfg["thresholds"]["acronym"]})
            continue
        best = 0.0
        for a in ent["aliases"]:
            ka = key(a)
            if not ka:
                continue
            r = SequenceMatcher(None, k, ka).ratio()
            ts = SequenceMatcher(None, " ".join(sorted(k.split())), " ".join(sorted(ka.split()))).ratio()
            best = max(best, r, ts)
        if best >= cfg["thresholds"]["fuzzy_review"]:
            out.append({"entity": ent, "method": "fuzzy", "score": round(best, 3)})
    if not out and use_embedding:
        sv = embed(surface)
        if sv:
            scored = []
            for ent in reg.values():
                ev = embed(ent["name"])
                if ev:
                    scored.append((cos(sv, ev), ent))
            for score, ent in sorted(scored, reverse=True)[:3]:
                if score >= cfg["thresholds"]["embedding_review"]:
                    out.append({"entity": ent, "method": "embedding", "score": round(score, 3)})
    out.sort(key=lambda x: (-x["score"], x["entity"]["name"]))
    return out


def verdict(hit: dict, cfg: dict) -> str:
    """Similarity never auto-merges. A fuzzy or embedding hit is a proposal to
    a human or a stronger pass, because "Pierce County" scoring 0.9 against
    "Pierce County Council" is exactly how two concepts become one wrong one.
    """
    if hit["method"] in {"exact", "alias", "acronym"}:
        return "accept"
    return "review"


# ---------------------------------------------------------------- claims

_claims: list = []


def nlp():
    if not _claims:
        import spacy
        _claims.append(spacy.load("en_core_web_lg"))
    return _claims[0]


STOP_OBJ = {"the", "a", "an", "that", "this", "it", "his", "her", "their"}


def prose_only(text: str) -> str:
    keep = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in "|->#" or s.startswith("```"):
            continue
        keep.append(s)
    return "\n".join(keep)


def extract_claims(text: str, cfg: dict) -> list[dict]:
    doc = nlp()(prose_only(text)[:60000])
    cpat = re.compile(cfg["claims"]["evidence_pattern"])
    found = []
    for sent in doc.sents:
        s = sent.text.strip()
        if len(s) < 20 or len(s) > cfg["claims"]["max_sentence_chars"]:
            continue
        root = sent.root
        if root.pos_ not in {"VERB", "AUX"}:
            continue
        subj = [c for c in root.children if c.dep_ in {"nsubj", "nsubjpass"}]
        obj = [c for c in root.children if c.dep_ in {"dobj", "attr", "oprd", "dative"}]
        if not obj:
            for c in root.children:
                if c.dep_ == "prep":
                    obj += [g for g in c.children if g.dep_ == "pobj"]
        if not subj or not obj:
            continue
        subj_txt = " ".join(w.text for w in subj[0].subtree if w.dep_ not in {"det", "punct"})
        obj_txt = " ".join(w.text for w in obj[0].subtree if w.dep_ not in {"det", "punct"})
        pred = root.lemma_.lower()
        neg = any(c.dep_ == "neg" for c in root.children) or any(w.lower() in {"not", "never", "no"} for w in s.split())
        quals = [e.text for e in sent.ents if e.label_ in {"DATE", "MONEY", "CARDINAL", "PERCENT"}]
        sk, ok = key(subj_txt), key(obj_txt)
        if len(sk) < 3 or len(ok) < 2:
            continue
        fp = f"{sk}|{pred}|{ok}|{'neg' if neg else 'pos'}"
        found.append({"id": hash_id("CLM", fp, 10), "fingerprint": fp, "subject": subj_txt.strip(),
                      "predicate": pred, "object": obj_txt.strip(), "negated": neg,
                      "qualifiers": quals, "evidence": sorted(set(cpat.findall(s))),
                      "sentence": s})
    return found


def ner_mentions(text: str) -> list[tuple[str, str]]:
    """Every named thing the parser can see, not just the ones we already know."""
    doc = nlp()(text[:40000])
    out = [(e.text, e.label_) for e in doc.ents if e.label_ in {"PERSON", "ORG", "GPE", "LAW", "FAC"}]
    covered = [(e.start, e.end) for e in doc.ents]
    for nc in doc.noun_chunks:
        s = nc.text.strip()
        if not (2 <= len(s.split()) <= 4) or not s[:1].isupper():
            continue
        if nc.root.pos_ != "PROPN":
            continue
        if any(a <= nc.start and nc.end <= b for a, b in covered):
            continue
        out.append((s, "CHUNK"))
    return out


# ---------------------------------------------------------------- audit

def reading_layer() -> list[str]:
    import linkify as L
    return L.reading_layer()


def audit(case: str | None, write: bool) -> dict:
    import linkify as L
    cfg = load_config(case)
    reg = build_registry(cfg)
    notes = [n for n in reading_layer() if not case or f"/{case}/" in n]
    blocked = {b.lower() for b in cfg["blocked_surfaces"]}
    cpat = re.compile(cfg["claims"]["evidence_pattern"])
    alias_pairs = [(a, k) for k, e in reg.items() for a in e["aliases"] if key(a)]
    alias_pairs.sort(key=lambda x: -len(x[0]))
    alias_map = {a: k for a, k in alias_pairs}
    pat = re.compile(r"(?<![\w-])(" + "|".join(re.escape(a) for a, _ in alias_pairs) + r")(?![\w-])") if alias_pairs else None

    mentions = resolved = 0
    by_method = Counter()
    unresolved: Counter = Counter()
    proposals: dict[tuple[str, str], int] = Counter()
    claims: dict[str, dict] = {}
    claim_notes: dict[str, set] = defaultdict(set)
    for rel in notes:
        text = open(os.path.join(VAULT, rel), encoding="utf-8", errors="replace").read()
        body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.S)
        body = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", body)
        note_evidence = sorted(set(cpat.findall(body)))
        for surface, label in ner_mentions(body):
            if "/" in surface or surface.lower().startswith(("investigations", "record", "projects")):
                continue
            if key(surface) in {key(b) for b in blocked} or len(key(surface)) < 4:
                continue
            mentions += 1
            hits = resolve(surface, reg, cfg, use_embedding=True)
            if hits:
                v = verdict(hits[0], cfg)
                by_method[hits[0]["method"]] += 1
                if v == "accept":
                    resolved += 1
                else:
                    unresolved[surface] += 1
                if hits[0]["method"] in {"fuzzy", "embedding"}:
                    proposals[(hits[0]["entity"]["name"], surface)] += 1
            else:
                unresolved[surface] += 1
        for c in extract_claims(body, cfg):
            if not c["evidence"] and note_evidence:
                c["evidence"] = note_evidence[:5]
                c["evidence_scope"] = "note"
            c["note"] = rel
            prev = claims.get(c["id"])
            if prev:
                prev["citations"] += 1
                prev["notes"].add(rel)
            else:
                claims[c["id"]] = dict(c, citations=1, notes={rel})
            claim_notes[c["id"]].add(rel)

    dup = {i: c for i, c in claims.items() if len(c["notes"]) > 1}
    no_ev = [c for c in claims.values() if not c["evidence"]]
    contra = defaultdict(list)
    for c in claims.values():
        contra[(c["fingerprint"].rsplit("|", 1)[0], c["negated"])].append(c)
    contradictions = [v for v in contra.values() if len({x["negated"] for x in v}) > 1]

    report = {
        "case": case or "all",
        "notes": len(notes),
        "mentions": mentions,
        "resolved": resolved,
        "canonisation_rate": round(resolved / mentions, 3) if mentions else None,
        "by_method": dict(by_method),
        "unresolved_top": unresolved.most_common(20),
        "alias_proposals": [{"entity": e, "surface": s, "count": n}
                            for (e, s), n in proposals.most_common(20) if n >= 2],
        "claims": {"total": len(claims), "duplicated": len(dup), "without_evidence": len(no_ev),
                   "contradictions": len(contradictions)},
        "entities": len(reg),
        "graph": L.graph_metrics(),
    }

    md = [f"# KB audit: {report['case']}", "",
          "Generated by `_system/scripts/kb.py audit`. Do not edit by hand.", "",
          f"- Notes scanned: {report['notes']}",
          f"- Entity mentions: {report['mentions']}",
          f"- Resolved: {report['resolved']} ({report['canonisation_rate'] if report['canonisation_rate'] is not None else 'n/a'})",
          f"- Resolution methods: {report['by_method']}",
          f"- Entities in registry: {report['entities']}",
          f"- Claims extracted: {report['claims']['total']}, appearing in more than one note: {report['claims']['duplicated']}",
          f"- Claims without an evidence ID: {report['claims']['without_evidence']}",
          f"- Contradictions found: {report['claims']['contradictions']}", ""]
    if report["unresolved_top"]:
        md += ["## Unresolved surfaces", ""] + [f"- {s}: {n}" for s, n in report["unresolved_top"]]
    if report["alias_proposals"]:
        md += ["", "## Alias proposals", ""] + [f"- {p['surface']} -> {p['entity']} ({p['count']})" for p in report["alias_proposals"]]
    if contradictions:
        md += ["", "## Contradictions", ""]
        for group in contradictions[:10]:
            md.append(f"- {group[0]['subject']} {group[0]['predicate']} {group[0]['object']} vs its negation")
            for g in group:
                md.append(f"  - {g['note']}: {'not ' if g['negated'] else ''}{g['sentence'][:160]}")

    if write:
        os.makedirs(DATA, exist_ok=True)
        json.dump(report, open(os.path.join(DATA, "kb-report.json"), "w"), indent=1, default=str)
        with open(os.path.join(DATA, "claims.jsonl"), "w") as fh:
            for c in claims.values():
                c = dict(c)
                c["notes"] = sorted(c["notes"])
                fh.write(json.dumps(c, default=str) + "\n")
        open(os.path.join(INV, "KB audit.md"), "w").write("\n".join(md) + "\n")
        print(f"wrote KB audit.md, claims.jsonl ({len(claims)} claims), kb-report.json")
    return report


# ---------------------------------------------------------------- contribution gate

def contribute(note: str, agent: str, write: bool) -> int:
    path = note if os.path.isabs(note) else os.path.join(VAULT, note)
    rel = os.path.relpath(path, VAULT).replace(os.sep, "/")
    text = open(path, encoding="utf-8").read()
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    fm = yaml.safe_load(m.group(1)) if m else {}
    case = next((c for c in cases() if c in rel), None)
    if not case and isinstance(fm, dict) and fm.get("case") in cases():
        case = fm["case"]
    cfg = load_config(case)
    reg = build_registry(cfg)
    problems = [f"missing frontmatter: {k}" for k in cfg["contribution"]["required_frontmatter"] if k not in (fm or {})]
    if cfg["contribution"]["require_case"] and not case:
        problems.append("note is not inside a case folder and no case was given")
    body = text[m.end():] if m else text
    claims = extract_claims(re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", body), cfg)
    missing_ev = [c for c in claims if cfg["claims"]["require_evidence"] and not c["evidence"]]
    digest = hashlib.sha256(text.encode()).hexdigest()
    ledger = os.path.join(VAULT, cfg["contribution"]["ledger"])
    os.makedirs(os.path.dirname(ledger), exist_ok=True)
    seen = False
    if os.path.exists(ledger):
        for line in open(ledger):
            try:
                if json.loads(line).get("sha256") == digest:
                    seen = True
                    break
            except Exception:  # noqa: BLE001
                pass
    record = {"ts": "2026-11-14", "agent": agent, "note": rel, "case": case, "sha256": digest,
              "claims": len(claims), "claims_with_evidence": len(claims) - len(missing_ev),
              "problems": problems, "idempotent": seen}
    print(json.dumps(record, indent=1))
    if missing_ev:
        print(f"claims without evidence: {len(missing_ev)}")
        for c in missing_ev[:5]:
            print(f"  {c['id']} {c['sentence'][:120]}")
    if write and not seen:
        with open(ledger, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    return 1 if problems else 0


# ---------------------------------------------------------------- check

def check(strict: bool) -> int:
    bad = 0
    for case in cases():
        cfg = load_config(case)
        reg = build_registry(cfg)
        notes = [n for n in reading_layer() if f"/{case}/" in n]
        for rel in notes:
            text = open(os.path.join(VAULT, rel), encoding="utf-8", errors="replace").read()
            fm = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
            missing = [k for k in cfg["contribution"]["required_frontmatter"] if not fm or not re.search(rf"^{k}\s*:", fm.group(1), re.M)]
            if missing:
                bad += 1
                print(f"{rel}: missing {missing}")
    print(f"check: {bad} notes with contract problems")
    return 1 if (strict and bad) else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["resolve", "audit", "contribute", "check"])
    ap.add_argument("target", nargs="?")
    ap.add_argument("--case", default=None)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--agent", default="unknown")
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args()

    if a.command == "resolve":
        cfg = load_config(a.case or (cases()[0] if cases() else None))
        reg = build_registry(cfg)
        for hit in resolve(a.target, reg, cfg)[:5]:
            print(f"{hit['score']:.3f}  {hit['method']:9s}  {hit['entity']['id']}  {hit['entity']['name']}  -> {verdict(hit, cfg)}")
        return 0
    if a.command == "audit":
        r = audit(a.case, a.write)
        print(json.dumps({k: r[k] for k in ("notes", "mentions", "resolved", "canonisation_rate", "claims", "entities")}, indent=1))
        return 0
    if a.command == "contribute":
        return contribute(a.target, a.agent, a.write)
    return check(a.strict)


if __name__ == "__main__":
    sys.exit(main())
