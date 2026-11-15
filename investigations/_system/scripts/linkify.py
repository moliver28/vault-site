#!/usr/bin/env python3
"""NER-driven linkifier and integration audit for the investigation reading layer.

The idea in one line: an entity that appears in five notes should have one home,
and every mention should point at it. This script finds those mentions with NER,
turns recurring entities into canonical notes (the "variables"), links the first
mention of each in each note, and measures whether the graph became more
integrated.

Boundaries that matter:
  * `record/` is never edited. Evidence stays verbatim.
  * Only notes that already exist are link targets, so the lint gate cannot break.
  * Linking is deterministic and idempotent: a second run changes nothing.
  * Every insert is recorded in a manifest and can be reverted.

Usage:
  python3 linkify.py --dry-run          # what would change, no writes
  python3 linkify.py --apply            # insert links, create entity stubs, write report
  python3 linkify.py --apply            # second run: expect 0 changes
  python3 linkify.py --revert           # remove every link listed in the manifest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict

VAULT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
INV = os.path.join(VAULT, "Investigations")
MANIFEST = os.path.join(INV, "_system", "data", "links.json")
REPORT = os.path.join(INV, "Link audit.md")
SKIP_DIRS = {"_archive", "_templates", "record", "rendered"}

FM = re.compile(r"^---\s*\n.*?\n---\s*\n", re.S)


def reading_layer() -> list[str]:
    """Notes we authored. Evidence and other projects are out of scope."""
    out = []
    for dp, dn, fn in os.walk(INV):
        dn[:] = sorted(d for d in dn if d not in SKIP_DIRS and not d.startswith(".hermes"))
        for f in sorted(fn):
            if not f.endswith(".md"):
                continue
            rel = os.path.relpath(os.path.join(dp, f), VAULT).replace(os.sep, "/")
            if "/record/" in rel:
                continue
            out.append(rel)
    if os.path.exists(os.path.join(VAULT, "README.md")):
        out.append("README.md")
    return sorted(set(out))


def split_fm(text: str) -> tuple[str, str]:
    m = FM.match(text)
    return (text[: m.end()], text[m.end():]) if m else ("", text)


def fm_list(fm: str, key: str) -> list[str]:
    m = re.search(rf"^{key}:\s*(.+)$", fm, re.M)
    if not m:
        return []
    raw = m.group(1).strip()
    if raw.startswith("[") and raw.endswith("]"):
        return [x.strip().strip("'\"") for x in raw[1:-1].split(",") if x.strip()]
    return [x.strip().strip("'\"") for x in raw.split(",") if x.strip()]


def load_registry() -> tuple[dict[str, str], dict[str, list[str]]]:
    """alias -> target note path (or entity title), plus target -> aliases."""
    alias: dict[str, set[str]] = defaultdict(set)
    for rel in reading_layer():
        p = os.path.join(VAULT, rel)
        text = open(p, encoding="utf-8", errors="replace").read()
        fm, _ = split_fm(text)
        target = rel[:-3]
        stem = os.path.splitext(os.path.basename(rel))[0]
        alias[stem].add(target)
        for a in fm_list(fm, "aliases"):
            alias[a].add(target)
    # entity profiles are canonical homes even though they live in record/
    pdir = os.path.join(INV, "2608-pierce-leadership", "record", "profiles")
    if os.path.isdir(pdir):
        for f in sorted(os.listdir(pdir)):
            if not f.endswith(".md"):
                continue
            p = os.path.join(pdir, f)
            text = open(p, encoding="utf-8", errors="replace").read()
            m = re.search(r"^#\s*(.+?)\s*$", text, re.M)
            title = re.sub(r"\s*[—–-]\s*Deep Profile\.?\s*$", "", m.group(1)) if m else f[:-3]
            target = os.path.relpath(p, VAULT).replace(os.sep, "/")[:-3]
            alias[title].add(target)
            alias[f[:-3].replace("-", " ")].add(target)
    # keep only unambiguous aliases that read as names, not as furniture.
    # A note title like "Findings" or "Record" is a navigation word; linking it
    # everywhere produces link soup and buries the entities that matter.
    structural = {"findings", "brief", "record", "people", "timeline", "questions", "method",
                  "system", "archive", "conventions", "investigations", "cases", "index",
                  "readme", "review", "link audit", "lakewood record", "pierce record",
                  "lakewood timeline", "pierce people", "lakewood findings", "pierce findings"}
    clean = {a: next(iter(t)) for a, t in alias.items()
             if len(t) == 1 and len(a) >= 4 and a.lower() not in structural
             and (len(a.split()) > 1 or len(a) >= 6)}
    by_target: dict[str, list[str]] = defaultdict(list)
    for a, t in clean.items():
        by_target[t].append(a)
    return clean, by_target


def mask(text: str) -> list[tuple[int, int]]:
    """Regions the linker must not touch, each with its own flags.

    Using DOTALL for line-anchored patterns turns `.*` into the rest of the
    file, which silently protected whole documents in the first build.
    """
    rules = [
        (r"^---\s*\n.*?\n---\s*\n", re.S),
        (r"```.*?```", re.S),
        (r"`[^`\n]+`", 0),
        (r"\[\[[^\]]*\]\]", 0),
        (r"!\[[^\]]*\]\([^)]*\)", 0),
        (r"\[[^\]]*\]\([^)]*\)", 0),
        (r"https?://\S+", 0),
        (r"<[^>\n]+>", 0),
        (r"^#{1,6} .*$", re.M),
        (r"^\|.*\|\s*$", re.M),
        (r"^> \[![^\]]+\][-+]?.*$", re.M),
    ]
    spans = []
    for pat, flags in rules:
        for m in re.finditer(pat, text, flags):
            spans.append((m.start(), m.end()))
    return spans


def protected(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in spans)


ENTITY_PREFIXES = ("Investigations/Entities/",)
PROFILE_MARK = "/record/profiles/"


def entity_links(text: str) -> int:
    """Entity links already present in a note, from any earlier run."""
    n = 0
    for m in re.finditer(r"\[\[([^\]|#]+)", text):
        tgt = m.group(1)
        if tgt.startswith(ENTITY_PREFIXES) or PROFILE_MARK in tgt:
            n += 1
    return n


def candidates(text: str, alias: dict[str, str], self_target: str,
               per_target: int = 1, cap: int = 8, budget: int = 6) -> list[dict]:
    body_off = 0
    spans = mask(text)
    pat = re.compile(r"(?<![\w-])(" + "|".join(re.escape(a) for a in sorted(alias, key=lambda s: (-len(s), s))) + r")(?![\w-])")
    have = entity_links(text)
    if have >= budget:
        return []
    linked_targets = set(re.findall(r"\[\[([^\]|#]+)", text))
    seen: Counter = Counter()
    found = []
    for m in pat.finditer(text):
        surface = m.group(1)
        target = alias[surface]
        if target == self_target or target in linked_targets or protected(m.start(), spans):
            continue
        if not surface[0].isupper() and not surface[0].isdigit():
            continue
        if surface.lower() in BLOCK:
            continue
        # never butt a new link against an existing one
        before = text[max(0, m.start() - 2):m.start()]
        after = text[m.end():m.end() + 2]
        if "]]" in before or "[[" in after:
            continue
        if seen[target] >= per_target:
            continue
        seen[target] += 1
        found.append({"surface": surface, "target": target, "start": m.start(), "end": m.end(),
                      "target_title": target.split("/")[-1]})
        if len(found) >= min(cap, budget - have):
            break
    return found


# Places and furniture read badly as links: "[[Lakewood]] did not build one"
# is noise, not knowledge. A link should point at an actor or an object with a
# record behind it, never at a word that appears in every paragraph.
BLOCK = {"lakewood", "washington", "pierce", "tacoma", "kent", "state", "council",
         "tesseract", "hermes", "claude", "gliner", "spacy", "paddleocr", "bank examiner",
         "county", "city", "court", "board", "commission", "chamber", "office",
         "department", "party", "html", "json", "csv", "api", "pdf"}

GENERIC = {"html", "json", "csv", "api", "pdf", "url", "http", "https", "state", "council",
           "county", "city", "court", "board", "commission", "office", "department", "party",
           "university", "college", "school", "district", "association", "company", "group",
           "report", "minutes", "video", "podcast", "campaign", "committee", "union", "fund"}


def canonicalise(discovered: dict[str, dict], min_notes: int) -> dict[str, dict]:
    """Merge NER variants onto one entity: a surname joins the full name.

    GLiNER and spaCy disagree constantly ("Philip Lindholm" as PERSON,
    "Lindholm" as ORG). Two notes for one person is the exact problem this
    script exists to remove, so a single-token variant becomes an alias of the
    one multiword name it belongs to.
    """
    multi = {k: v for k, v in discovered.items()
             if len(k.split()) > 1 and len(v["notes"]) >= min_notes}
    tails: dict[str, list[str]] = defaultdict(list)
    for k in multi:
        tails[k.split()[-1].lower()].append(k)
    merged = {k: dict(v, aliases=set()) for k, v in multi.items()}
    for k, v in discovered.items():
        if len(k.split()) > 1:
            continue
        if k.lower() in GENERIC:
            continue
        hit = tails.get(k.lower(), [])
        if len(hit) == 1:
            m = merged[hit[0]]
            m["notes"] = set(m["notes"]) | set(v["notes"])
            m["count"] += v["count"]
            m["aliases"].add(k)
        elif len(v["notes"]) >= min_notes and len(k) >= 5:
            merged[k] = dict(v, aliases=set())
    out = {}
    for k, v in merged.items():
        if k.split()[-1].lower() in GENERIC:
            continue
        if len(k.split()) == 1 and v.get("label") == "GPE":
            continue
        out[k] = v
    return out


def ner_discover(notes: list[str]) -> dict[str, dict]:
    """GLiNER + spaCy over the reading layer, to surface entities with no home."""
    try:
        import spacy
        nlp = spacy.load("en_core_web_lg")
    except Exception as e:  # noqa: BLE001
        print(f"NER: spaCy unavailable ({e})", file=sys.stderr)
        return {}
    gliner = None
    try:
        from gliner import GLiNER
        gliner = GLiNER.from_pretrained("urchade/gliner_medium")
    except Exception as e:  # noqa: BLE001
        print(f"NER: GLiNER unavailable, spaCy only ({type(e).__name__})", file=sys.stderr)
    labels = ["person", "organization", "government body", "location", "law"]
    found: dict[str, dict] = {}
    for rel in notes:
        body = split_fm(open(os.path.join(VAULT, rel), encoding="utf-8", errors="replace").read())[1]
        body = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", body)
        doc = nlp(body[:20000])
        ents = [(e.text, e.label_) for e in doc.ents if e.label_ in {"PERSON", "ORG", "GPE", "LAW"}]
        if gliner is not None:
            try:
                for e in gliner.predict_entities(body[:4000], labels, threshold=0.5):
                    ents.append((e["text"], e["label"].upper()))
            except Exception:  # noqa: BLE001
                pass
        for text, label in ents:
            t = text.strip().strip('",.;:()')
            if len(t) < 4 or len(t.split()) > 5:
                continue
            if t.lower() in {"the county", "the council", "the city", "the state", "the port"}:
                continue
            rec = found.setdefault(t, {"name": t, "label": label, "notes": set(), "count": 0})
            rec["notes"].add(rel)
            rec["count"] += 1
    return found


def stub(name: str, label: str, notes: list[str], aliases: list[str] | None = None) -> tuple[str, str]:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")
    body = [f"# {name}", "",
            f"Canonical entry for {name}. Every mention in the reading layer points here, so the entity has one address instead of forty spellings.", "",
            "## Where it appears", ""]
    body += [f"- [[{n[:-3]}]]" for n in notes[:20]]
    body += ["", "## What the record says", "",
             "Not written yet. Fill this from the evidence before publishing a claim about this entity. Until then this note is a pointer, not a source.", ""]
    al = ", ".join(dict.fromkeys([name] + (aliases or [])))
    fm = ["---", "project: Investigations", "type: entity", "status: stub",
          f"entity_type: {label.lower()}", f"aliases: [{al}]",
          f"created: 2026-11-14", "tags: [entity]", "---", ""]
    return "\n".join(fm + body), slug


def graph_metrics() -> dict:
    import networkx as nx
    G = nx.DiGraph()
    notes = [n for n in reading_layer()]
    for rel in notes:
        G.add_node(rel)
    link_re = re.compile(r"\[\[([^\]|#]+)")
    for rel in notes:
        text = split_fm(open(os.path.join(VAULT, rel), encoding="utf-8", errors="replace").read())[1]
        for m in link_re.finditer(text):
            t = m.group(1).strip()
            hit = [n for n in notes if n[:-3].endswith(t) or os.path.basename(n)[:-3] == t]
            if hit:
                G.add_edge(rel, hit[0])
    U = G.to_undirected()
    n = U.number_of_nodes()
    e = U.number_of_edges()
    if n == 0:
        return {}
    lcc = max((len(c) for c in nx.connected_components(U)), default=0)
    folders = {x: x.split("/")[1] if len(x.split("/")) > 2 else x.split("/")[0] for x in U.nodes}
    cross = sum(1 for a, b in U.edges if folders[a] != folders[b])
    comms = nx.community.greedy_modularity_communities(U) if e else []
    modularity = nx.community.modularity(U, comms) if comms else 0.0
    cohesion = lcc / n
    linkage = min(1.0, (e / n) / 3.0)
    porosity = cross / e if e else 0.0
    reach = sum(len(nx.descendants(G, x)) for x in G.nodes) / (n * max(1, n - 1))
    integrated = (cohesion * max(0.0, 1 - modularity) * max(0.02, porosity) * max(0.02, reach)) ** 0.25
    return {"notes": n, "edges": e, "density": round(nx.density(U), 4), "lcc_share": round(cohesion, 3),
            "cross_folder_share": round(porosity, 3), "modularity": round(modularity, 3),
            "reach": round(reach, 3), "integration_index": round(integrated, 3),
            "components": nx.number_connected_components(U)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ner", action="store_true")
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--per-note", type=int, default=1)
    ap.add_argument("--min-notes", type=int, default=3)
    args = ap.parse_args()

    if args.revert:
        if not os.path.exists(MANIFEST):
            print("no manifest")
            return 0
        data = json.load(open(MANIFEST))
        n = 0
        for ins in sorted(data["inserts"], key=lambda x: -x["start"]):
            p = os.path.join(VAULT, ins["note"])
            t = open(p).read()
            chunk = t[ins["start"]:ins["end"]]
            if chunk == ins["inserted"]:
                open(p, "w").write(t[: ins["start"]] + ins["surface"] + t[ins["end"]:])
                n += 1
        print(f"reverted {n} inserts")
        return 0

    alias, by_target = load_registry()
    before = graph_metrics()
    notes = reading_layer()

    created = []
    discovered = {}
    if args.ner and args.apply:
        discovered = canonicalise(ner_discover(notes), args.min_notes)
        recurring = {k: v for k, v in discovered.items()
                     if len(v["notes"]) >= args.min_notes and k not in alias}
        ranked = sorted(recurring.items(), key=lambda kv: (-len(kv[1]["notes"]), kv[0]))[:20]
        edir = os.path.join(INV, "Entities")
        os.makedirs(edir, exist_ok=True)
        for name, rec in ranked:
            body, slug = stub(name, rec["label"], sorted(rec["notes"]), sorted(rec.get("aliases", [])))
            fp = os.path.join(edir, slug + ".md")
            if not os.path.exists(fp):
                open(fp, "w").write(body)
                created.append(slug)
        if created:
            alias, by_target = load_registry()
        print(f"created {len(created)} entity stubs")

    plan = {}
    for rel in notes:
        text = open(os.path.join(VAULT, rel), encoding="utf-8", errors="replace").read()
        c = candidates(text, alias, rel[:-3], per_target=args.per_note)
        if c:
            plan[rel] = c

    total = sum(len(v) for v in plan.values())
    print(f"reading layer: {len(notes)} notes | registry aliases: {len(alias)} | link opportunities: {total}")
    top = Counter(x["target_title"] for v in plan.values() for x in v).most_common(12)
    print("top targets:", ", ".join(f"{t}({n})" for t, n in top))

    discovered = {}
    if args.ner:
        discovered = ner_discover(notes)
        recurring = {k: v for k, v in discovered.items()
                     if len(v["notes"]) >= args.min_notes and k not in alias}
        ranked = sorted(recurring.items(), key=lambda kv: (-len(kv[1]["notes"]), kv[0]))[:20]
        print(f"NER discovered {len(discovered)} distinct strings; {len(recurring)} recur in >= {args.min_notes} notes")
        for k, v in ranked[:12]:
            print(f"  {k} [{v['label']}] {len(v['notes'])} notes")

    if not args.apply:
        print("dry run: nothing written")
        return 0

    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    inserts = []
    for rel, items in plan.items():
        p = os.path.join(VAULT, rel)
        text = open(p, encoding="utf-8", errors="replace").read()
        for it in sorted(items, key=lambda x: -x["start"]):
            link = f"[[{it['target']}|{it['surface']}]]" if it["target"].split("/")[-1] != it["surface"] else f"[[{it['target']}]]"
            text = text[: it["start"]] + link + text[it["end"]:]
            inserts.append({"note": rel, "surface": it["surface"], "target": it["target"],
                            "start": it["start"], "end": it["start"] + len(link),
                            "inserted": link})
        open(p, "w").write(text)
    print(f"inserted {len(inserts)} links across {len(plan)} notes")

    after = graph_metrics()
    json.dump({"run": "2026-11-14", "inserts": inserts, "created": created,
               "before": before, "after": after}, open(MANIFEST, "w"), indent=1)
    print("before:", before)
    print("after :", after)
    print(f"manifest: {os.path.relpath(MANIFEST, VAULT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
