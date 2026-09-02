"""WX SDR summarizer A/B — baseline vs deterministic guards.

ARM A  current production: raw transcript -> qwen2.5:3b -> raw output.
ARM B  guards:  corrections applied to the TRANSCRIPT before summarizing,
                then invented-time strip + length cap + collapse retry after.

Both arms see the SAME cached transcript per clip. Whisper is nondeterministic,
so re-transcribing per arm would put the biggest variable in the middle of the
comparison. Transcribe once, reuse.

Never transmits. Reads stored audio and calls Ollama on localhost only.
"""
import glob
import json
import os
import re
import subprocess
import sys

import urllib.request

sys.path.insert(0, "/opt/wx-sdr")

OLLAMA_URL = "http://127.0.0.1:11434"
MODEL = os.environ.get("LAB_MODEL", "qwen2.5:3b")
TEMP = 0.2                      # production value; disproved that 0 helps
REPS = 3
TMP = "/tmp/wxab16k.wav"
CACHE = "/opt/wx-sdr/lab/transcripts.json"
MAXLEN = 160

# The whisper mishearings, lifted from SUMMARY_PROMPT's own list. In ARM B these
# become code, not a request to the model.
CORRECTIONS = [
    ("Shalom", "Show Low"), ("Healabend", "Gila Bend"), ("Costa Grande", "Casa Grande"),
    ("Messagedway", "Mesa Gateway"), ("Dear Valley", "Deer Valley"),
    ("flying staff", "Flagstaff"), ("Blives", "Blythe"), ("Hila", "Gila"),
    ("Pienau", "Pinal"), ("Pinot", "Pinal"), ("clapal", "Claypool"),
    ("globe-many", "Globe-Miami"), ("UMA", "Yuma"), ("Si Va", "Sierra Vista"),
    ("OnServations", "Observations"),
]
BAD_NAMES = [b for b, _ in CORRECTIONS]

# Times only — NOT percentages ("10%") or temperature ranges ("98-102").
TIME_PAT = (r"(?:\d{1,2}[:.]\d{2}\s*(?:[ap]\.?m\.?)?(?:\s*(?:MST|MDT|UTC|GMT|Z))?"
            r"|\d{1,2}\s*[ap]\.?m\.?(?:\s*(?:MST|MDT|UTC|GMT|Z))?"
            r"|\d{1,4}\s*(?:UTC|GMT|Z|MST|MDT)\b)")
TIME_RE = re.compile(TIME_PAT, re.I)
CONNECTOR = r"(?:\b(?:until|untill|expires?|till|thru|through|at)\b\s*)?"
STRIP_RE = re.compile(CONNECTOR + "(" + TIME_PAT + ")", re.I)


def norm_time(t):
    d = re.sub(r"\D", "", t)
    return d.lstrip("0") or d


def times_in(text):
    return {norm_time(m.group(0)) for m in TIME_RE.finditer(text)}


# ---------------- guards ----------------

def apply_corrections(text):
    """G1 — fix the known mishearings in the TRANSCRIPT. \\b keeps 'UMA' from
    matching inside 'Yuma' (no word boundary between 'Y' and 'uma')."""
    for bad, good in CORRECTIONS:
        text = re.sub(r"\b%s\b" % re.escape(bad), good, text, flags=re.I)
    return text


def unsupported_numbers(summary, transcript):
    """Every number in the summary must appear in the transcript. ALLOWLIST.

    Shape-matching times was a blocklist and it lost three times running: the
    model answered "2PM", then "18Z", then a bare "1230", each landing outside
    whatever pattern had just been widened. Enumerating the ways a model can
    write a time is unwinnable. Enumerating the numbers the SOURCE actually
    contains is finite and known.
    """
    # Collect the transcript's numbers the SAME way they will be read back out.
    # A greedy [\d:.,-]* token turned "98-102" into one blob "98102", so the
    # summary's legitimate "98" looked unsupported and got deleted -- the guard
    # was eating real data to reach a clean score. Take plain digit runs, PLUS
    # the joined form of clock times so "12.30 p.m." still authorises "12:30".
    tnums = set(re.findall(r"\d+", transcript))
    tnums |= {re.sub(r"\D", "", x)
              for x in re.findall(r"\d{1,2}[:.]\d{2}", transcript)}
    tnums = {n for n in tnums if n}
    out = []
    for m in re.finditer(r"\d[\d:.]*\d|\d", summary):
        tok = m.group(0).rstrip(".")
        if re.sub(r"\D", "", tok) not in tnums:
            out.append(tok)
    return out


def strip_invented_times(summary, transcript):
    """G2 - drop any number the transcript does not support, with its connector.

    Strip rather than reject the whole summary: on a warning, a line that has
    lost a fabricated expiry still carries the hazard and the counties, whereas
    no line at all carries nothing.
    """
    bad = unsupported_numbers(summary, transcript)
    out = summary
    for tok in bad:
        out = re.sub(r"(?:(?:until|untill|expires?|till|thru|through|at)\s*)?"
                     + re.escape(tok) + r"\s*(?:[A-Za-z.]{0,4})?",
                     " ", out, count=1)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([,.;:])", r"", out)
    out = re.sub(r"(?:[,;]\s*){2,}", ", ", out)
    out = re.sub(r"(?:until|untill|expires?|till|thru|through|at)\s*[.,;:]*\s*$",
                 "", out, flags=re.I).strip()
    return out.rstrip(" ,;:")


def cap_len(summary):
    """G3 — hard 160 cap on a word boundary. Airtime is the scarce resource."""
    if len(summary) <= MAXLEN:
        return summary
    cut = summary[:MAXLEN - 1]          # leave room for the "." we add back
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return (cut.rstrip(" ,;:.") + ".")[:MAXLEN]


# Characters worth keeping above ASCII: degree sign, en/em dash, curly
# apostrophe. Everything else non-ASCII is a language leak.
KEEP_EXTRA = "°–—’"


def has_non_latin(s):
    return any(ord(c) > 127 and c not in KEEP_EXTRA for c in s)


def strip_non_latin(s):
    """G5 - drop non-Latin script.

    The prompt has said "English only" since its first version and qwen still
    leaked Chinese into a FLASH FLOOD WARNING summary during this A/B. A mesh
    client rendering CJK for a life-safety alert is worse than a shorter line.
    """
    out = "".join(c for c in s if ord(c) < 128 or c in KEEP_EXTRA)
    out = re.sub(r"\s{2,}", " ", out)
    return re.sub(r"[\s,;:.]+$", "", out).strip()


def collapsed(s):
    """G4 — 'UMA71Nogales89SiVa70until2345UTC': all spaces gone, unreadable."""
    return len(s) > 20 and (s.count(" ") / float(len(s))) < 0.05


# ---------------- pipeline ----------------

def load_prompt():
    src = open("/opt/wx-sdr/same_bridge.py", encoding="utf-8").read()
    m = re.search(r"^SUMMARY_PROMPT = \((.*?)^\)$", src, re.S | re.M)
    ns = {}
    exec("SUMMARY_PROMPT = (" + m.group(1) + ")", ns)
    return ns["SUMMARY_PROMPT"]


def ollama(prompt, transcript):
    body = json.dumps({"model": MODEL, "prompt": prompt + transcript,
                       "stream": False,
                       "options": {"temperature": TEMP, "num_predict": 120}}).encode()
    req = urllib.request.Request(OLLAMA_URL + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read()).get("response", "").strip().replace("\n", " ").strip('"')


_whisper = None


def transcribe(wav):
    global _whisper
    subprocess.run(["sox", wav, "-r", "16000", "-c", "1", TMP],
                   check=True, capture_output=True)
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel("base", device="cpu", compute_type="int8")
    segs, _ = _whisper.transcribe(TMP)
    return " ".join(s.text.strip() for s in segs).strip()


def get_transcripts():
    if os.path.isfile(CACHE):
        return json.load(open(CACHE, encoding="utf-8"))
    wavs = sorted(glob.glob("/opt/wx-sdr/lab/*.wav")) + \
        sorted(glob.glob("/opt/wx-sdr/recordings/*.wav"))
    out = {}
    for w in wavs:
        out[os.path.basename(w)] = transcribe(w)
        print("transcribed %s (%d chars)" % (os.path.basename(w), len(out[os.path.basename(w)])))
    json.dump(out, open(CACHE, "w", encoding="utf-8"), indent=1)
    return out


def score(summary, transcript):
    return {
        "invented": unsupported_numbers(summary, transcript),
        "badname": [b for b in BAD_NAMES
                    if re.search(r"\b%s\b" % re.escape(b), summary, re.I)],
        "toolong": len(summary) > MAXLEN,
        "collapsed": collapsed(summary),
        "nonlatin": has_non_latin(summary),
    }


def run_arm(name, prompt, transcripts, guarded):
    print("\n" + "=" * 78)
    print("ARM %s  (%s)" % (name, "GUARDED" if guarded else "BASELINE — current production"))
    print("=" * 78)
    agg = {"invented": 0, "badname": 0, "toolong": 0, "collapsed": 0,
           "nonlatin": 0, "n": 0}
    for clip, raw in sorted(transcripts.items()):
        if len(raw) < 20:
            continue
        fed = apply_corrections(raw) if guarded else raw
        for rep in range(REPS):
            s = ollama(prompt, fed)
            if guarded:
                if collapsed(s):
                    s = ollama(prompt, fed)          # G4: one retry
                s = strip_non_latin(s)            # G5: language leak
                s = apply_corrections(s)          # G1b: model re-abbreviated
                s = strip_invented_times(s, fed)
                s = cap_len(s)
            sc = score(s, fed)
            agg["n"] += 1
            for k in ("invented", "badname"):
                agg[k] += 1 if sc[k] else 0
            for k in ("toolong", "collapsed", "nonlatin"):
                agg[k] += 1 if sc[k] else 0
            flags = []
            if sc["invented"]:
                flags.append("INVENTED%s" % sc["invented"])
            if sc["badname"]:
                flags.append("BADNAME%s" % sc["badname"])
            if sc["toolong"]:
                flags.append("LEN%d" % len(s))
            if sc["collapsed"]:
                flags.append("COLLAPSED")
            if sc["nonlatin"]:
                flags.append("NON-LATIN")
            print("%-26s r%d %-3s %s" % (clip[:26], rep,
                                         "OK" if not flags else "!!",
                                         s[:120]))
            if flags:
                print("%31s^ %s" % ("", " ".join(flags)))
    return agg


def main():
    prompt = load_prompt()
    tr = get_transcripts()
    print("clips: %d | model: %s | temp: %.1f | reps: %d" % (len(tr), MODEL, TEMP, REPS))
    a = run_arm("A", prompt, tr, guarded=False)
    b = run_arm("B", prompt, tr, guarded=True)
    print("\n\n" + "=" * 78)
    print("RESULT   (lower is better; n=%d summaries per arm)" % a["n"])
    print("=" * 78)
    print("%-26s %10s %10s" % ("failure mode", "BASELINE", "GUARDED"))
    for k, label in (("invented", "invented a time"),
                     ("badname", "uncorrected place name"),
                     ("toolong", "over 160 chars"),
                     ("collapsed", "format collapse"),
                     ("nonlatin", "non-Latin script")):
        print("%-26s %10s %10s" % (label, "%d/%d" % (a[k], a["n"]),
                                   "%d/%d" % (b[k], b["n"])))


if __name__ == "__main__":
    main()
