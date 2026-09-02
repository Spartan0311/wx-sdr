"""WX SDR summarizer lab — does temperature=0 stop the invented expiry times?

Runs on the receiver host (where Ollama and the venv live). Never transmits: this only reads
stored audio and calls Ollama. Mirrors same_bridge.py's own transcribe/summarize
so the result transfers to production.
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
MODEL = "qwen2.5:3b"
TMP = "/tmp/wxlab16k.wav"

SUMMARY_PROMPT = None  # pulled from the live daemon source, never re-typed


def load_prompt():
    """Import the REAL prompt from the deployed daemon. Re-typing it here would
    test a copy that has already drifted."""
    src = open("/opt/wx-sdr/same_bridge.py", encoding="utf-8").read()
    ns = {}
    m = re.search(r"^SUMMARY_PROMPT = \((.*?)^\)$", src, re.S | re.M)
    exec("SUMMARY_PROMPT = (" + m.group(1) + ")", ns)
    return ns["SUMMARY_PROMPT"]


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


def summarize(prompt, transcript, temperature, seed=None):
    opts = {"temperature": temperature, "num_predict": 120}
    if seed is not None:
        opts["seed"] = seed
    body = json.dumps({"model": MODEL, "prompt": prompt + transcript,
                       "stream": False, "options": opts}).encode()
    req = urllib.request.Request(OLLAMA_URL + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        out = json.loads(r.read()).get("response", "").strip()
    return out.replace("\n", " ").strip('"')


# --- the thing we are actually measuring -----------------------------------
# A time in the SUMMARY that is absent from the TRANSCRIPT is a fabricated fact.
# That is the prompt rule under test ("Never invent times, numbers"), and on a
# warning the expiry is the most actionable line in the message.
TIME_RE = re.compile(r"\b(\d{1,2}[:.]\d{2}\s*(?:[AP]\.?M\.?)?|\d{1,2}\s*[AP]\.?M\.?)", re.I)


def norm_time(t):
    return re.sub(r"[^0-9apm]", "", t.lower())


def invented_times(summary, transcript):
    tset = {norm_time(x) for x in TIME_RE.findall(transcript)}
    # digits alone also count as present (transcript "12.30 p.m." vs "12:30 PM")
    tdigits = {re.sub(r"\D", "", x) for x in tset}
    out = []
    for cand in TIME_RE.findall(summary):
        n = norm_time(cand)
        if n in tset:
            continue
        if re.sub(r"\D", "", n) in tdigits and re.sub(r"\D", "", n):
            continue
        out.append(cand)
    return out


def main():
    prompt = load_prompt()
    wavs = sorted(glob.glob("/opt/wx-sdr/lab/*.wav")) + \
        sorted(glob.glob("/opt/wx-sdr/recordings/*.wav"))
    print("prompt chars:", len(prompt), "| clips:", len(wavs))

    transcripts = {}
    for w in wavs:
        t = transcribe(w)
        transcripts[os.path.basename(w)] = t
        print("\n--- %s (%d chars)" % (os.path.basename(w), len(t)))
        print("   ", t[:220].replace("\n", " "))

    REPS = 3
    TEMPS = [0.2, 0.0]
    print("\n\n=== sweep: %d clips x %d temps x %d reps ===" %
          (len(transcripts), len(TEMPS), REPS))
    tally = {}
    for temp in TEMPS:
        bad = total = 0
        for name, tr in transcripts.items():
            if len(tr) < 20:
                continue
            for rep in range(REPS):
                s = summarize(prompt, tr, temp, seed=None)
                inv = invented_times(s, tr)
                total += 1
                flag = ""
                if inv:
                    bad += 1
                    flag = "  <== INVENTED %s" % inv
                over = "  [%d chars OVER 160]" % len(s) if len(s) > 160 else ""
                print("t=%.1f %-28s r%d: %s%s%s" %
                      (temp, name[:28], rep, s[:150], over, flag))
        tally[temp] = (bad, total)

    print("\n=== RESULT ===")
    for temp, (bad, total) in tally.items():
        print("temperature=%.1f -> %d/%d summaries contained an invented time"
              % (temp, bad, total))


if __name__ == "__main__":
    main()
