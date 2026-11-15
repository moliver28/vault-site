#!/usr/bin/env python3
"""Generate and check the Obsidian reading layer for investigation cases.

The vault keeps one canonical copy of each case under
Investigations/<slug>/. Reading notes sit at the folder root; raw files sit in
record/. This script derives the notes that must never drift from the record:

  record   -> <Case> Record.md    register of every file in record/
  timeline -> <Case> Timeline.md  dated sequence parsed from the case file
  people   -> <Case> People.md    entity index parsed from record/profiles/
  check    -> link, duplicate-name, and layer-completeness report

Usage:
  python3 vault_cases.py record   Investigations/2608-lakewood-council --name Lakewood
  python3 vault_cases.py timeline Investigations/2608-lakewood-council --name Lakewood
  python3 vault_cases.py people   Investigations/2608-pierce-leadership --name Pierce
  python3 vault_cases.py check

Paths are relative to the vault root, which is resolved from this file
(../../.. from _system/scripts).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from collections import Counter, defaultdict

VAULT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
SKIP_DIRS = {".git", ".obsidian", "_archive", "_system", "Projects", "Public", "Shared", "00-Index"}
TEXT_EXT = {".md", ".txt", ".json", ".csv", ".yml", ".yaml", ".html", ".py", ".js", ".jsonl"}
GROUP_LABEL = {
    "evidence": "Evidence and analysis",
    "evidence/minutes": "Meeting minutes",
    "evidence/minutes-backfill": "Backfilled minutes",
    "evidence/property-records": "Property records",
    "evidence/network": "Network data and bundle",
    "evidence/lindholm-deep-dive": "Lindholm deep dive",
    "profiles": "Entity profiles",
    "working": "Working documents",
    "tooling": "Case tooling and config",
    "tooling/templates": "Profile templates",
}


def rel(path: str) -> str:
    return os.path.relpath(path, VAULT).replace(os.sep, "/")


def wikilink(path: str, label: str | None = None) -> str:
    target = path[:-3] if path.endswith(".md") else path
    return f"[[{target}|{label}]]" if label else f"[[{target}]]"


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def word_count(path: str) -> int:
    if os.path.splitext(path)[1].lower() not in TEXT_EXT:
        return 0
    try:
        return len(re.findall(r"\w+", open(path, encoding="utf-8", errors="replace").read()))
    except OSError:
        return 0


def first_heading(path: str) -> str:
    if not path.endswith(".md"):
        return ""
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return ""
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                lines = lines[i + 1:]
                break
    for line in lines:
        line = line.strip()
        if line.startswith("#"):
            return re.sub(r"^#+\s*", "", line)[:90]
        if line and not line.startswith(("<!--", "**", "|", ">")):
            return line[:90]
    return ""


def human(n: int) -> str:
    return f"{n:,}"


def group_of(relpath: str) -> str:
    """Group key from a path relative to the case's record/ folder."""
    parts = relpath.split("/")
    if len(parts) == 1:
        return "record root"
    return "/".join(parts[:2]) if len(parts) > 2 else parts[0]


def cmd_record(case_dir: str, name: str, slug: str) -> str:
    root = os.path.join(VAULT, case_dir)
    record = os.path.join(root, "record")
    groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
    total_words = 0
    total_files = 0
    for dp, dn, fn in os.walk(record):
        dn[:] = sorted(d for d in dn if not d.startswith("."))
        for f in sorted(fn):
            p = os.path.join(dp, f)
            r = rel(p)
            groups[group_of(os.path.relpath(p, record).replace(os.sep, "/"))].append((r, f))
            total_words += word_count(p)
            total_files += 1

    out = [
        "---",
        "project: Investigations",
        "type: case-record",
        f"status: generated",
        "created: 2026-11-14",
        f"case: {slug}",
        "generator: _system/scripts/vault_cases.py record",
        "tags: [case-record, evidence]",
        f"aliases: [{name} record, {name} evidence]",
        "---",
        "",
        f"# {name}: the record",
        "",
        f"This register covers {human(total_files)} files and about {human(total_words)} words held in `{case_dir}/record/`. "
        "Each line links to the file in place. Nothing here is rewritten, summarised away, or reordered: the raw file is the source, and the notes at the case root are the reading layer above it.",
        "",
        "Files are grouped the way an investigator reaches for them. Open a group to see its contents. Every change to this folder and to this register is committed to the vault's git history, which is the custody chain for the live record.",
        "",
    ]
    order = [
        "evidence",
        "evidence/lindholm-deep-dive",
        "evidence/minutes",
        "evidence/minutes-backfill",
        "evidence/property-records",
        "evidence/network",
        "working",
        "tooling",
        "record root",
        "other",
    ]
    seen = set()
    for key in order + sorted(groups):
        if key in seen or key not in groups:
            continue
        seen.add(key)
        files = groups[key]
        words = sum(word_count(os.path.join(VAULT, r)) for r, _ in files)
        if key == "profiles":
            out.append(f"> [!abstract]- Entity profiles ({len(files)} files, {human(words)} words)")
            out.append(f"> The profiles are indexed in [[{name} People]], which groups them by domain and power tier. "
                       "Open that note instead of scanning 164 file names here.")
            out.append("")
            continue
        label = GROUP_LABEL.get(key, key.replace("evidence/", "").replace("-", " ").title())
        open_marker = "+" if key in ("evidence", "record root") else "-"
        plural = "file" if len(files) == 1 else "files"
        out.append(f"> [!evidence]{open_marker} {label} ({len(files)} {plural}, {human(words)} words)")
        for r, f in files:
            p = os.path.join(VAULT, r)
            wc = word_count(p)
            head = first_heading(p)
            tail = f" · {human(wc)} words" if wc else ""
            note = f" · {head}" if head and not head.lower().startswith(os.path.splitext(f)[0][:12].lower()) else ""
            if r.endswith(".md"):
                ref = wikilink(r, os.path.splitext(f)[0])
            elif r.lower().endswith(".txt"):
                view = f"{case_dir}/record/rendered/{os.path.splitext(f)[0]}"
                ref = f"{wikilink(view, f)} (view) or `{r}`"
            else:
                ref = f"`{r}`"
            out.append(f"> - {ref}{tail}{note}")
        out.append("")
    return "\n".join(out) + "\n"


def cmd_timeline(case_dir: str, name: str, slug: str) -> str:
    root = os.path.join(VAULT, case_dir)
    case_file = None
    for f in sorted(os.listdir(root)):
        if f == f"{slug}.md":
            case_file = os.path.join(root, f)
    if not case_file:
        raise SystemExit(f"no case file {slug}.md in {case_dir}")
    text = open(case_file, encoding="utf-8").read()
    m = re.search(r"^## Timeline\s*$(.*?)(?=^## |\Z)", text, re.S | re.M)
    if not m:
        raise SystemExit("case file has no ## Timeline section")
    rows = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3 or cells[0].lower() == "date":
            continue
        rows.append(cells)
    out = [
        "---",
        "project: Investigations",
        "type: case-timeline",
        "status: generated",
        "created: 2026-11-14",
        f"case: {slug}",
        "generator: _system/scripts/vault_cases.py timeline",
        "tags: [case-timeline, chronology]",
        f"aliases: [{name} timeline, {name} chronology]",
        "---",
        "",
        f"# {name}: timeline",
        "",
        f"Parsed from the timeline table in [[{case_dir}/{slug}|the case file]], which records each date against an evidence ID. "
        "Confidence is the case file's own grade. A medium confidence row means the date or the attribution is not settled.",
        "",
        "| Date | Event | Source | Confidence |",
        "|---|---|---|---|",
    ]
    for c in rows:
        date = c[0].replace("|", "/")
        event = c[1].replace("|", "/")
        src = c[2].replace("|", "/") if len(c) > 2 else ""
        conf = c[3].replace("|", "/") if len(c) > 3 else ""
        out.append(f"| {date} | {event} | {src} | {conf} |")
    out += ["", f"{len(rows)} dated entries. The register for each source ID sits in [[{name} Record]]."]
    return "\n".join(out) + "\n"


PROFILE_KEYS = {
    "id": r"\*\*Entity ID:\*\*\s*`([^`]+)`",
    "type": r"\*\*Type:\*\*\s*([a-z ]+)",
    "domain": r"\*\*Domain:\*\*\s*([a-z, ]+)",
    "tier": r"\*\*Tier:\*\*\s*(\w+)",
    "rank": r"\*\*Rank:\*\*\s*([\d/]+)",
    "score": r"\*\*Composite power:\*\*\s*([\d.]+)",
    "desc": r"\*\*Graph description:\*\*\s*(.+)",
}


def cmd_people(case_dir: str, name: str, slug: str) -> str:
    root = os.path.join(VAULT, case_dir, "record", "profiles")
    rows = []
    for f in sorted(os.listdir(root)):
        if not f.endswith(".md"):
            continue
        text = open(os.path.join(root, f), encoding="utf-8", errors="replace").read()
        head = text[:1600]
        rec = {"file": f}
        for k, pat in PROFILE_KEYS.items():
            m = re.search(pat, head)
            rec[k] = (m.group(1).strip() if m else "")
        title = re.search(r"^#\s*(.+?)\s*$", head, re.M)
        rec["title"] = re.sub(r"\s*[—-]\s*Deep Profile\.?\s*$", "", title.group(1)) if title else rec["id"]
        rec["desc"] = re.sub(r"\s+", " ", rec["desc"])[:150]
        rows.append(rec)
    by_domain = defaultdict(list)
    for r in rows:
        by_domain[r["domain"] or "unclassified"].append(r)
    out = [
        "---",
        "project: Investigations",
        "type: case-people",
        "status: generated",
        "created: 2026-11-14",
        f"case: {slug}",
        "generator: _system/scripts/vault_cases.py people",
        "tags: [case-people, entities, network]",
        f"aliases: [{name} people, {name} entities]",
        "---",
        "",
        f"# {name}: who is on the map",
        "",
        f"{len(rows)} entity profiles, grouped by the domain recorded in each profile header. "
        "The header lines are machine-read by the case's own scripts, so they stay untouched; this note adds the links that make them navigable.",
        "",
        "Rank is the composite power rank from the network model. Treat it as a sorting device, not a verdict: the method note explains why centrality over-counts connected hubs and under-counts isolated veto holders.",
        "",
    ]
    for domain in sorted(by_domain):
        items = sorted(by_domain[domain], key=lambda r: (r["tier"], r["rank"].zfill(6)))
        out.append(f"> [!abstract]- {domain.title()} ({len(items)})")
        for r in items:
            link = wikilink(f"{case_dir}/record/profiles/{r['file'][:-3]}", r["title"])
            meta = " · ".join(x for x in [
                r["type"], r["tier"], (f"rank {r['rank']}" if r["rank"] else ""), (f"power {r['score']}" if r["score"] else "")
            ] if x)
            out.append(f"> - {link} · {meta}" + (f" · {r['desc']}" if r["desc"] else ""))
        out.append("")
    out += ["## Reading the profiles", "",
            "Each profile follows the same fourteen sections and ends with a source table. "
            "Confidence ratings belong to the individual profile, not to this index. "
            f"The case brief is [[{name} Brief]] and the evidence register is [[{name} Record]]."]
    return "\n".join(out) + "\n"


def cmd_render(case_dir: str, name: str, slug: str) -> str:
    """Write a verbatim markdown view of every .txt file under record/.

    Obsidian and the public viewer cannot open .txt as a note, so the minutes
    and transcripts would be unreachable from a link. The view keeps the
    original path and hash in frontmatter and puts the text in a fenced block,
    untouched. The .txt file remains the record.
    """
    root = os.path.join(VAULT, case_dir, "record")
    outdir = os.path.join(root, "rendered")
    os.makedirs(outdir, exist_ok=True)
    written = []
    for dp, dn, fn in os.walk(root):
        if "rendered" in dp.split(os.sep):
            continue
        for f in sorted(fn):
            if not f.lower().endswith(".txt"):
                continue
            src = os.path.join(dp, f)
            body = open(src, encoding="utf-8", errors="replace").read()
            h = sha256(src)
            src_rel = rel(src)
            dst = os.path.join(outdir, os.path.splitext(f)[0] + ".md")
            head = [
                "---",
                "project: Investigations",
                f"case: {slug}",
                "type: rendered-evidence",
                "status: verbatim",
                "created: 2026-11-14",
                "generator: _system/scripts/vault_cases.py render",
                f"source_path: {src_rel}",
                f"source_sha256_12: {h}",
                "verbatim: true",
                "---",
                "",
                f"# {os.path.splitext(f)[0]}",
                "",
                f"Verbatim view of `{src_rel}`. The text below is the file, unchanged. "
                f"SHA-256 prefix `{h}`. The `.txt` original stays in the record.",
                "",
                f"```text",
                body.rstrip("\n"),
                "```",
                "",
            ]
            open(dst, "w", encoding="utf-8").write("\n".join(head))
            written.append(rel(dst))
    return f"rendered {len(written)} text files into {case_dir}/record/rendered/"


def cmd_check() -> int:
    cases = []
    inv = os.path.join(VAULT, "Investigations")
    for d in sorted(os.listdir(inv)):
        p = os.path.join(inv, d)
        if os.path.isdir(p) and not d.startswith("_") and re.match(r"^\d{4}-", d):
            cases.append(d)
    problems = []
    names = defaultdict(list)
    for dp, dn, fn in os.walk(VAULT):
        dn[:] = [x for x in dn if x not in {".git", ".obsidian"}]
        for f in fn:
            if f.endswith(".md"):
                names[f[:-3]].append(rel(os.path.join(dp, f)))
    for k, v in names.items():
        if len(v) > 1:
            problems.append(f"duplicate note name: {k} -> {len(v)} files")
    for c in cases:
        p = os.path.join(inv, c)
        files = os.listdir(p)
        short = c.split("-", 1)[1]
        for required in (f"{c}.md", "record"):
            if required not in files:
                problems.append(f"{c}: missing {required}")
        layer = [f for f in files if f.endswith(".md") and f != f"{c}.md"]
        if len(layer) < 4:
            problems.append(f"{c}: reading layer has {len(layer)} notes")
        if not os.path.isdir(os.path.join(p, "record")):
            problems.append(f"{c}: no record/ folder")
    print(f"cases: {len(cases)} ({', '.join(cases)})")
    print(f"notes: {sum(len(v) for v in names.values())} across {len(names)} names")
    if problems:
        print("PROBLEMS")
        for p in problems:
            print("  -", p)
        return 1
    print("OK: no duplicate names, every case has a case file, record/, and a reading layer")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["record", "timeline", "people", "render", "check"])
    ap.add_argument("case", nargs="?")
    ap.add_argument("--name", default="")
    ap.add_argument("--slug", default="")
    ap.add_argument("--write", action="store_true", help="write the note next to the case file")
    a = ap.parse_args()
    if a.command == "render":
        print(cmd_render(a.case, a.name, a.slug or os.path.basename(a.case.rstrip("/"))))
        return 0
    if a.command == "check":
        return cmd_check()
    if not a.case or not a.name:
        raise SystemExit("case path and --name are required")
    slug = a.slug or os.path.basename(a.case.rstrip("/"))
    fn = {"record": cmd_record, "timeline": cmd_timeline, "people": cmd_people}[a.command]
    text = fn(a.case, a.name, slug)
    if a.write:
        target = os.path.join(VAULT, a.case, f"{a.name} {a.command.title() if a.command != 'people' else 'People'}.md")
        open(target, "w", encoding="utf-8").write(text)
        print(f"wrote {rel(target)} ({len(text.splitlines())} lines)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
