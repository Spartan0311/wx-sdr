#!/usr/bin/env python3
"""Score whisper transcripts against the AUTHORITATIVE NWS product text.

WHY
---
Every prompt change so far has been judged by reading a transcript and deciding
it looked better. That is exactly the method this project has measured at a 0%
hit rate. NWS publishes the real wording of the very products our receiver
heard, so transcription can be SCORED instead of eyeballed.

⚠ WAN IS A HARD NO AT RUNTIME. This is a DEVELOPMENT tool. `fetch` is the only
subcommand that touches the network, and it CACHES to disk so every later run —
and the whole scoring loop — is offline. The receiver never calls this.

WHAT IT SCORES
--------------
Not raw WER. WER over a 1200-character product buries the failures that matter:
one wrong highway number is worth more than twenty filler words. Instead it
scores the fields that decide whether a mesh message is useful:

  event phrase   "severe thunderstorm warning"  -- the REGRESSION GUARD
  counties       the warned counties
  places         towns / landmarks named in the product
  highways       "Interstate 8", "Arizona Route 238"
  measures       "60 mph", "quarter size hail", "1.5 inches"

⚠ THE EVENT PHRASE IS THE REGRESSION GUARD, NOT A NICE-TO-HAVE. A previous
attempt at proper-noun priming corrupted the opening window into "has issued a
flash on warning" -- it fixed county spelling and broke the single most
important string in the message. Any arm that scores better on `places` while
scoring worse on `event` is a REGRESSION, however good its total looks.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

REC = "/opt/wx-sdr/recordings"
LAB = "/opt/wx-sdr/lab"
CACHE = os.path.join(LAB, "groundtruth.json")
UA = "meshtoc-wxsdr (johnspartan0311@gmail.com)"

# SAME event code -> the phrase NWR actually speaks.
EVENT_PHRASE = {
    "SVR": "severe thunderstorm warning",
    "FFW": "flash flood warning",
    "DSW": "dust storm warning",
    "TOR": "tornado warning",
    "SVA": "severe thunderstorm watch",
    "FFA": "flash flood watch",
    "EWW": "extreme wind warning",
    "RWT": "required weekly test",
}


def _norm(s):
    """Casefold + collapse whitespace + drop punctuation that whisper varies.

    Deliberately NOT stripping digits or letters -- only the separators the
    renderer is inconsistent about. Same lesson as the number allowlist: compare
    canonical forms, never renderings.
    """
    s = s.lower().replace("-", " ").replace(".", " ").replace(",", " ")
    return re.sub(r"\s+", " ", s).strip()


def _has(hay, needle):
    return _norm(needle) in _norm(hay)


# ---- measure-aware comparison ------------------------------------------------
# The product WRITES "60 mph"; the voice SPEAKS and whisper faithfully writes
# "60 miles per hour" (and "five" for "5"). Audited 2026-08-10: 28 of the
# bench's 32 measure misses were this rendering gap, not decode failures —
# the same normalise-BOTH-sides lesson as the G2 time allowlist. Canonical
# form: digits + "mph".

_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_NUM_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
             "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def _norm_measure(s):
    s = _norm(s)
    # compounds first ("twenty five" -> 25), then standalone tens/units
    s = re.sub(r"\b(%s)\s+(%s)\b" % ("|".join(_NUM_TENS), "|".join(_NUM_WORDS)),
               lambda m: str(_NUM_TENS[m.group(1)] + _NUM_WORDS[m.group(2)]), s)
    s = re.sub(r"\b(%s)\b" % "|".join(_NUM_TENS),
               lambda m: str(_NUM_TENS[m.group(1)]), s)
    s = re.sub(r"\b(%s)\b" % "|".join(_NUM_WORDS),
               lambda m: str(_NUM_WORDS[m.group(1)]), s)
    s = re.sub(r"\bmiles?\s+(?:an|per)\s+hour\b", "mph", s)
    s = re.sub(r"\bm\s+p\s+h\b", "mph", s)   # _norm split "m.p.h." to "m p h"
    return s


def _has_measure(hay, needle):
    return _norm_measure(needle) in _norm_measure(hay)


# ------------------------------------------------------------------ ground truth

def cmd_fetch(args):
    """Pull NWS products covering our recordings and cache them. NETWORK."""
    recs = sorted(f for f in os.listdir(REC) if f.endswith(".wav.json"))
    if not recs:
        print("no recordings")
        return 1
    stamps = []
    for r in recs:
        m = re.match(r"(\d{8})-(\d{6})_([A-Z]{3})\.wav\.json", r)
        if m:
            stamps.append(m.group(1) + m.group(2))
    lo, hi = min(stamps), max(stamps)

    def iso(s):
        return "%s-%s-%sT%s:%s:%sZ" % (s[0:4], s[4:6], s[6:8], s[8:10], s[10:12], s[12:14])

    # Widen: a product is SENT before the transmitter reads it out.
    url = ("https://api.weather.gov/alerts?area=AZ&start=%s&end=%s&limit=500"
           % (iso(lo)[:-1] + "Z", iso(hi)[:-1] + "Z"))
    print("fetching %s" % url)
    # ⚠ THE USER-AGENT STRING IS LOAD-BEARING AND api.weather.gov WILL NOT SAY SO.
    # It answers a bare 403 — not 429, not a useful body — for a UA it dislikes.
    # Measured: "meshtoc-wxsdr-eval (...)" returns 403 on every attempt
    # while "meshtoc-wxsdr (...)" returns 200 on every attempt, same URL,
    # same headers, back to back. Do not add "-eval"/"-test"/"-bot" style
    # suffixes here.
    #
    # This cost three wrong diagnoses (a missing Accept header, then transient
    # throttling) because the manual spikes used a DIFFERENT UA than the script
    # and that difference was self-inflicted and untracked. When a request works
    # by hand and fails in code, diff the ACTUAL requests before theorising.
    # (measured pre-rename under mesh-commander-wxsdr; re-verify on next lab run)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/geo+json",
    })
    data = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=45) as fh:
                data = json.load(fh)
            break
        except urllib.error.HTTPError as e:
            if e.code not in (403, 429, 500, 502, 503) or attempt == 3:
                raise
            wait = 20 * (attempt + 1)
            print("  HTTP %d — throttled? retry %d/3 in %ds" % (e.code, attempt + 1, wait))
            time.sleep(wait)
    out = []
    for f in data.get("features", []):
        p = f["properties"]
        out.append({
            "event": p.get("event"),
            "same": (p.get("geocode") or {}).get("SAME") or [],
            "sent": p.get("sent"),
            "expires": p.get("expires"),
            "areaDesc": p.get("areaDesc"),
            "description": " ".join((p.get("description") or "").split()),
            "instruction": " ".join((p.get("instruction") or "").split()),
        })
    with open(CACHE, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print("cached %d products -> %s" % (len(out), CACHE))
    print("every later run is OFFLINE.")
    return 0


# ------------------------------------------------------------------- extraction

# NWS scopes counties with leading direction words ("Southeast Gila County",
# "West Central Maricopa County") and the capitalised-word regex happily
# swallows them — plus sentence-initial "In". The 2026-08-10 smoke test built
# hotwords reading "for Eastern La Paz County and In Maricopa County". Strip
# leading scope tokens until the name stabilises; the closed set mirrors the
# words NWS actually uses.
_COUNTY_SCOPE = {"In", "North", "South", "East", "West", "Northern", "Southern",
                 "Eastern", "Western", "Central", "Northeastern", "Northwestern",
                 "Southeastern", "Southwestern", "Northeast", "Northwest",
                 "Southeast", "Southwest", "Extreme"}


def _clean_county(name):
    words = name.split()
    while words and words[0] in _COUNTY_SCOPE:
        words.pop(0)
    # NWS typos the particle lowercase ("eastern la Paz County", measured
    # 2026-08-09T23:12 product) — canonicalise it or the gazetteer carries a
    # phantom "Paz" county.
    if words and words[0] == "la":
        words[0] = "La"
    return " ".join(words)


def extract_targets(prod):
    """Pull the fields that decide whether a mesh message is useful."""
    desc = prod["description"]
    t = {"places": [], "highways": [], "measures": [], "counties": []}

    # `la` is allowed lowercase — NWS typos "la Paz" (see _clean_county).
    # KNOWN GAP: "Maricopa and eastern la Paz County" structurally loses the
    # county before the "and" — the chain can't cross a lowercase scope word.
    # Accepted for now; recovering it needs backward scanning, not regex.
    for m in re.finditer(r"((?:[A-Z][a-z]+|la)(?:\s+(?:[A-Z][a-z]+|la))*)\s+Count(?:y|ies)",
                         desc):
        c = _clean_county(m.group(1))
        if c:
            t["counties"].append(c)

    # Product families phrase the location list differently: SVR/DSW say
    # "Locations impacted include...", FFW says "Some locations that will
    # experience flash flooding include..." and names its watercourses after
    # "...the following streams and drainages...". The FFW forms were
    # invisible until the 2026-08-10 third-party fixture scored places 0/0
    # on a product that plainly names two towns and a wash.
    for lead in (r"[Ll]ocations impacted include",
                 r"[Ll]ocations that will experience flash flooding include",
                 r"streams and drainages"):
        m = re.search(lead + r"\.*\s*(.+?)(?:\s*(?:This includes|PRECAUTIONARY|$))",
                      desc)
        if m:
            for name in re.split(r",| and ", m.group(1)):
                name = name.strip(" .")
                # Two chars filters list debris without dropping real short names.
                if 2 < len(name) < 40:
                    t["places"].append(name)

    for m in re.finditer(r"(Interstate\s+\d+|Arizona\s+Route\s+\d+|Route\s+\d+|"
                         r"Highway\s+\d+|I-(\d+))", desc):
        hw = m.group(1)
        if m.group(2):
            # "I-8" is written shorthand for what the voice SPEAKS as
            # "Interstate 8" — canonicalise so a correct decode isn't a miss.
            hw = "Interstate %s" % m.group(2)
        t["highways"].append(hw)

    for m in re.finditer(r"(\d+\s*mph|\d+(?:\.\d+)?\s*inch(?:es)?|"
                         r"(?:quarter|penny|nickel|golf ball|ping pong ball|half dollar)\s*"
                         r"(?:\s|-)?size)", desc, re.I):
        t["measures"].append(m.group(1))

    for k in t:
        seen, uniq = set(), []
        for v in t[k]:
            if _norm(v) not in seen:
                seen.add(_norm(v))
                uniq.append(v)
        t[k] = uniq
    return t


def match_product(name, products):
    """Match a recording to its NWS product by event family + send time.

    Returns (product, note). The note is printed rather than swallowed: an
    ambiguous match must be visible, because a benchmark that silently scores
    against the WRONG product is worse than no benchmark.
    """
    m = re.match(r"(\d{8})-(\d{6})_([A-Z]{3})", name)
    if not m:
        return None, "unparseable name"
    d, t, code = m.groups()
    hdr = "%s-%s-%sT%s:%s:%sZ" % (d[0:4], d[4:6], d[6:8], t[0:2], t[2:4], t[4:6])
    phrase = EVENT_PHRASE.get(code, "")
    # ⚠ CAUSALITY, NOT NEAREST-ABSOLUTE. NWS issues the product, THEN the
    # transmitter reads it out, so the header always follows its own product.
    # Matching on |delta| lets a LATER product win by a few seconds and it
    # silently scores the transcript against a different storm.
    #
    # That is not hypothetical: 20260810-042650 (header 21:26:50) matched the
    # 21:27:00 West Valley product (+10s) over its true 21:26:00 Gila Bend /
    # Estrella one (-50s), and scored 0/15 places, 0/3 highways, 0/3 measures —
    # a perfect-looking transcription failure that was entirely this bug.
    # NWR re-reads follow-up statements for OTHER storms in the same county
    # minutes apart, so near-ties are the normal case here, not the edge case.
    #   delta = header - sent.  delta > 0  => product PRECEDES the header (right)
    #                           delta < 0  => product FOLLOWS it (impossible bar skew)
    # Want the SMALLEST POSITIVE delta: the nearest product that could actually
    # have been the one being read out.
    hdr_t = _epoch(hdr)
    pre, post = [], []
    for p in products:
        if not p["sent"] or _norm(p["event"]) != _norm(phrase):
            continue
        delta = hdr_t - _epoch(p["sent"])
        (pre if delta >= 0 else post).append((delta, p))
    if not pre and not post:
        return None, "no %s product near %s" % (code, hdr)
    if pre:
        pre.sort(key=lambda x: x[0])           # nearest PRECEDING wins
        best_d, best = pre[0]
        note = "matched %s (sent %s, %ds before header)" % (
            best["event"], best["sent"], best_d)
        if len(pre) > 1 and pre[1][0] - best_d < 120:
            note += "  ⚠ AMBIGUOUS: another %ds earlier" % (pre[1][0] - best_d)
        if best_d > 1800:
            note += "  ⚠ WEAK: %ds before the header" % best_d
    else:
        post.sort(key=lambda x: -x[0])         # least-negative = closest after
        best_d, best = post[0]
        note = ("matched %s (sent %s, ⚠ %ds AFTER the header — clock skew or a "
                "missing product)" % (best["event"], best["sent"], -best_d))
    return best, note


def _epoch(iso):
    import datetime
    iso = iso.replace("Z", "+00:00")
    return int(datetime.datetime.fromisoformat(iso).timestamp())


# ---------------------------------------------------------------------- scoring

def score_one(transcript, prod, code):
    tgt = extract_targets(prod)
    res = {}
    phrase = EVENT_PHRASE.get(code, "")
    res["event"] = (1 if phrase and _has(transcript, phrase) else 0, 1 if phrase else 0)
    for k in ("counties", "places", "highways", "measures"):
        items = tgt[k]
        cmp = _has_measure if k == "measures" else _has
        hit = sum(1 for it in items if cmp(transcript, it))
        res[k] = (hit, len(items))
    res["_targets"] = tgt
    return res


def cmd_score(args):
    if not os.path.exists(CACHE):
        print("no ground truth cached — run `fetch` once (needs WAN)")
        return 1
    products = json.load(open(CACHE, encoding="utf-8"))
    recs = sorted(f for f in os.listdir(REC) if f.endswith(".wav.json"))
    tot = {k: [0, 0] for k in ("event", "counties", "places", "highways", "measures")}
    print("%-26s %-7s %-9s %-9s %-9s %-9s" %
          ("recording", "event", "counties", "places", "highways", "measures"))
    print("-" * 76)
    for r in recs:
        name = r.replace(".wav.json", "")
        code = name.split("_")[-1]
        d = json.load(open(os.path.join(REC, r), encoding="utf-8"))
        tr = d.get("transcript", "")
        prod, note = match_product(name, products)
        if not prod:
            print("%-26s  -- %s" % (name, note))
            continue
        s = score_one(tr, prod, code)
        cells = []
        for k in ("event", "counties", "places", "highways", "measures"):
            h, n = s[k]
            tot[k][0] += h
            tot[k][1] += n
            cells.append("%d/%d" % (h, n) if n else "  - ")
        print("%-26s %-7s %-9s %-9s %-9s %-9s" % (name, *cells))
        if args.verbose:
            print("        %s" % note)
            for k in ("places", "highways"):
                for it in s["_targets"][k]:
                    print("          %-4s %-28s %s"
                          % ("HIT " if _has(tr, it) else "MISS", it, k))
    print("-" * 76)
    cells = []
    for k in ("event", "counties", "places", "highways", "measures"):
        h, n = tot[k]
        cells.append("%d/%d" % (h, n) if n else "  - ")
    print("%-26s %-7s %-9s %-9s %-9s %-9s" % ("TOTAL", *cells))
    print()
    print("⚠ `event` is the REGRESSION GUARD. An arm that gains on `places` and")
    print("  loses on `event` is a regression no matter what the total says.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch", help="cache NWS ground truth (ONLY networked step)")
    p.set_defaults(func=cmd_fetch)
    p = sub.add_parser("score", help="score stored transcripts (offline)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_score)
    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
