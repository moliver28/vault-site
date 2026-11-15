#!/usr/bin/env python3
"""Independent page verification for the published vault viewer.

Samples a random share of the markdown notes, then checks each one against the
live public viewer at three levels:

  1. mirror integrity   the file on the NAS is byte-identical to the vault copy
  2. index presence     the page appears in metadata.json (tree + search + graph)
  3. render             content.php returns 200, the H1 matches the file, and the
                        rendered word count is within tolerance of the source

Sampling is deterministic (fixed seed) so a run can be repeated and compared.

Usage:
  python3 verify_pages.py --share 0.10 --seed 20261114
  python3 verify_pages.py --share 0.10 --json /tmp/verify.json
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

VAULT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
VIEWER = os.environ.get("VAULT_VIEWER", "http://192.168.0.21:8094")
SSH = ["ssh", "-i", os.path.expanduser("~/.ssh/nas-n8n/id_ed25519"), "-p", "24",
       "-o", "BatchMode=yes", "moliver@192.168.0.21"]
REMOTE = "/volume1/docker/obsidian/public-vault"


def notes() -> list[str]:
    out = []
    for dp, dn, fn in os.walk(VAULT):
        dn[:] = [d for d in dn if d not in {".git", ".obsidian"}]
        for f in fn:
            if f.endswith(".md"):
                out.append(os.path.relpath(os.path.join(dp, f), VAULT).replace(os.sep, "/"))
    return sorted(out)


def body_words(text: str) -> int:
    body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.S)
    return len(re.findall(r"\w+", body))


def first_h1(text: str) -> str:
    body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.S)
    m = re.search(r"^#\s+(.+)$", body, re.M)
    return m.group(1).strip() if m else ""


def norm(s: str) -> str:
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u2014", "-").replace("\u2013", "-")
    return re.sub(r"\s+", " ", s).strip().lower()


def remote_state() -> tuple[dict[str, str], set[str], dict]:
    script = (
        f"cd {REMOTE} && "
        "find . -name '*.md' -type f -print0 | xargs -0 sha256sum; "
        "echo '---METADATA---'; cat metadata.json"
    )
    r = subprocess.run(SSH + [script], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise SystemExit(f"ssh failed: {r.stderr[:400]}")
    hashes, _, meta = r.stdout.partition("---METADATA---")
    remote_hash = {}
    for line in hashes.splitlines():
        m = re.match(r"^([0-9a-f]{64})\s+\./(.+)$", line.strip())
        if m:
            remote_hash[m.group(2)] = m.group(1)
    meta_list = json.loads(meta)
    indexed = {e["relativePath"] for e in meta_list}
    return remote_hash, indexed, {"entries": len(meta_list)}


def fetch(path: str, retries: int = 2) -> tuple[int, str]:
    url = f"{VIEWER}/content.php?mdfile=" + urllib.parse.quote("/" + path[:-3])
    last = (0, "")
    for i in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, ""
        except Exception as e:  # noqa: BLE001
            last = (0, str(e))
            time.sleep(0.6 * (i + 1))
    return last[0], last[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--share", type=float, default=0.10)
    ap.add_argument("--seed", type=int, default=20261114)
    ap.add_argument("--json", default="")
    a = ap.parse_args()

    all_notes = notes()
    k = max(1, round(len(all_notes) * a.share))
    rng = random.Random(a.seed)
    sample = sorted(rng.sample(all_notes, k))

    remote_hash, indexed, meta_stats = remote_state()
    results = []
    for rel in sample:
        p = os.path.join(VAULT, rel)
        text = open(p, encoding="utf-8", errors="replace").read()
        local_hash = hashlib.sha256(open(p, "rb").read()).hexdigest()
        rec = {"page": rel, "h1": first_h1(text), "words": body_words(text)}
        rec["mirrored"] = remote_hash.get(rel) == local_hash
        rec["indexed"] = rel in indexed
        status, out = fetch(rel)
        rec["http"] = status
        rendered = norm(out)
        rec["rendered"] = bool(rendered) and len(rendered.split()) >= max(12, rec["words"] * 0.4)
        if rec["h1"]:
            rec["heading_ok"] = norm(rec["h1"]) in rendered
        else:
            # Notes without an H1 are titled by filename in the viewer; the
            # rendered body opens with the page path, then its properties.
            stem = norm(os.path.splitext(os.path.basename(rel))[0])
            rec["heading_ok"] = stem in rendered[:600]
        rec["callouts"] = out.count("callout-title-inner")
        rec["ok"] = all([rec["mirrored"], rec["indexed"], status == 200, rec["rendered"], rec["heading_ok"]])
        results.append(rec)

    fails = [r for r in results if not r["ok"]]
    print(f"vault notes: {len(all_notes)}  sampled: {k} ({a.share:.0%}, seed {a.seed})")
    print(f"viewer index: {meta_stats['entries']} pages")
    by_area = Counter(r["page"].split("/")[0] + "/" + (r["page"].split("/")[1] if "/" in r["page"] else "") for r in results)
    for area, n in by_area.most_common():
        area_fails = [r for r in fails if r["page"].startswith(area)]
        print(f"  {area:55s} {n:3d} sampled, {len(area_fails)} failed")
    if fails:
        print("\nFAILURES")
        for r in fails:
            why = [k for k in ("mirrored", "indexed", "rendered", "heading_ok") if not r[k]]
            if r["http"] != 200:
                why.insert(0, f"http {r['http']}")
            print(f"  {r['page']}: {', '.join(why)}")
    print(f"\nRESULT: {len(results) - len(fails)}/{len(results)} passed, {len(fails)} failed")
    if a.json:
        json.dump({"sampled": k, "total": len(all_notes), "results": results}, open(a.json, "w"), indent=1)
        print(f"detail: {a.json}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
