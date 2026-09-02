#!/usr/bin/env python3
"""Harvest a per-county place/highway gazetteer from cached NWS products.

Runs at DEVELOPMENT time against lab/groundtruth.json and emits a static JSON
keyed by SAME code. The receiver loads that file and never touches the network:
the decoded header already carries the county FIPS, so the gazetteer turns
"which counties are warned" into "which proper nouns to prime whisper with".

This is the offline half of the LLM-driven-context idea: instead of one static
prompt covering all of Arizona, scope the vocabulary to the counties the header
actually names. Short and relevant beats long and diluted.
"""
import collections
import json
import sys

sys.path.insert(0, "/opt/wx-sdr/lab")
import wx_wer  # noqa: E402  — ONE extractor; a second copy here already
               # drifted once (county scope-word pollution fixed only in the
               # scorer's copy). Same rule as guard_check exec-loading the
               # shipped guards: never test/harvest through a private fork of
               # the logic under test.

GT = "/opt/wx-sdr/lab/groundtruth.json"
OUT = "/opt/wx-sdr/lab/gazetteer.json"


def _canonical(counter):
    """Collapse cross-product rendering variants of the SAME name.

    extract_targets dedups by _norm WITHIN one product, but the union across
    products kept every raw rendering — the gazetteer carried "Ak Chin",
    "Ak-Chin Village" and the line-wrap artifact "Ak- Chin Village" as three
    places. Group by _norm and keep the most frequent rendering; ties go
    against strings with a dangling "- " (hyphen + linebreak collapse), then
    alphabetically for determinism.
    """
    groups = collections.defaultdict(list)
    for raw, n in counter.items():
        groups[wx_wer._norm(raw)].append((raw, n))
    keep = []
    for variants in groups.values():
        variants.sort(key=lambda x: (-x[1], "- " in x[0], x[0]))
        keep.append(variants[0][0])
    return sorted(keep)


def main():
    products = json.load(open(GT, encoding="utf-8"))
    by = collections.defaultdict(lambda: {"places": collections.Counter(),
                                          "highways": collections.Counter(),
                                          "counties": collections.Counter()})
    for p in products:
        tgt = wx_wer.extract_targets(p)
        for same in p.get("same") or []:
            by[same]["places"].update(tgt["places"])
            by[same]["highways"].update(tgt["highways"])
            by[same]["counties"].update(tgt["counties"])

    out = {k: {kk: _canonical(vv) for kk, vv in v.items()}
           for k, v in sorted(by.items())}
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, ensure_ascii=False)

    print("%-10s %7s %9s %9s" % ("SAME", "places", "highways", "counties"))
    print("-" * 40)
    for k in sorted(out):
        print("%-10s %7d %9d %9d" % (k, len(out[k]["places"]),
                                     len(out[k]["highways"]), len(out[k]["counties"])))
    allp = set()
    for v in out.values():
        allp |= set(v["places"])
    print("-" * 40)
    print("unique places statewide: %d" % len(allp))
    print("wrote %s" % OUT)
    if "004013" in out:
        mp = out["004013"]
        print()
        print("Maricopa (004013) places  :", ", ".join(mp["places"][:16]))
        print("Maricopa (004013) highways:", ", ".join(mp["highways"]))
        # Token cost matters: hotwords is capped at 223 tokens (max_length//2 - 1).
        blob = ", ".join(mp["places"] + mp["highways"])
        print()
        print("hotwords blob would be %d chars (~%d tokens) for ONE county"
              % (len(blob), len(blob) // 4))
        print("⚠ hotwords caps at 223 tokens — a multi-county warning needs")
        print("  ranking or truncation, not the whole list.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
