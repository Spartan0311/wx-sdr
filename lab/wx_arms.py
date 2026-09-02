#!/usr/bin/env python3
"""A/B whisper decoding arms against NWS ground truth.

Arms
----
  base      exactly what ships today (initial_prompt only)
  frag      today + DYNAMIC hotwords as FRAGMENTS the audio never says
  speech    (--speech-arm) arm-1's product-speech style at the SAFE budget --
            kept ONLY to separate the two candidate mechanisms for the
            2026-08-10 collapse (see BUDGET below)

⚠ THE DESIGN SPACE IS BOUNDED ON BOTH ENDS — both extremes are MEASURED:
  - bare noun list       -> corrupted the opening window ("flash ON warning")
  - full product speech  -> collapsed the decode 1025 -> 232 chars (2026-08-10)
The frag arm carries the VOCABULARY in natural grammar the broadcast never
produces verbatim: "Across Maricopa and Pinal counties. Along Interstate 8.
Near Gila Bend, Estrella, Bosque."

The event phrase is deliberately ABSENT from the priming: it decodes 23/23
at base (needs no help) and it is the string most at risk if the decoder
aligns priming text against the opening sentence.

⚠ BUDGET — the REAL constraint is the DECODE WINDOW, not the 223-token cap.
max_length is 448 and the sot_prev region carries hotwords + up to 223 tokens
of rolling context (initial_prompt on the first window, generated text after)
+ ~4 specials. Decode room per window ~= 221 - hotwords_tokens. At 223-token
hotwords that is <= 0 ("The maximum decoding length must be > 0", measured
2026-08-10); just UNDER the crash line, windows truncate SILENTLY — which
scores exactly like suppression. Budget is therefore 96 REAL tokens, measured
with the model's own tokenizer — never a chars/4 heuristic; this vocabulary
measures ~3.5 chars/token — leaving ~125 tokens of decode room against ~100
needed for 30 s of NWR speech.

⚠ The budget math CONFOUNDS the 08-10 arm-1 reading: product-speech hotwords
ran at ~220 tokens -> decode room ~0-10, so the 1025->232 collapse may be
starvation rather than (or as well as) sot_prev repetition suppression.
--speech-arm re-runs that style at the safe budget to separate the mechanisms.

Ranking under the budget: counties -> highways -> places, places trimmed to
fit. Highways ahead of places on purpose: a highway number is the most
actionable field in a dust-storm or flood warning and it is measurably
failing.

Scored on the same fields as wx_wer.py, PLUS transcript length per arm — a
starved or suppressed decode scores like a transcription failure unless you
notice most of the output is missing. `event` is the regression guard: an arm
that gains on places and loses on event is a REGRESSION.
"""
import argparse
import json
import os
import sys
import subprocess
import time

sys.path.insert(0, "/opt/wx-sdr/lab")
import wx_wer  # noqa: E402  -- reuse the scorer, never a second copy of it

REC = "/opt/wx-sdr/recordings"
GAZ = "/opt/wx-sdr/lab/gazetteer.json"
SB = "/opt/wx-sdr/same_bridge.py"
TMP = "/tmp/wx_arm_16k.wav"

_MODELS = {}


def load_bridge():
    import types
    mod = types.ModuleType("sb")
    mod.__dict__["__name__"] = "sb"
    with open(SB, encoding="utf-8") as fh:
        exec(compile(fh.read(), SB, "exec"), mod.__dict__)
    return mod


def get_model(sb, name=None):
    """Per-name cache — the --models A/B holds several models at once."""
    name = name or sb.WHISPER_MODEL
    if name not in _MODELS:
        from faster_whisper import WhisperModel
        _MODELS[name] = WhisperModel(name, device="cpu", compute_type="int8")
    return _MODELS[name]


# The frag builder LIVES IN THE BRIDGE since v0.13.130 (sb.build_hotwords,
# sb.HOTWORD_TOKEN_BUDGET) — this harness measures the shipped one, never a
# private fork of it. Same rule as guard_check exec-loading the guards.


def _collect(same_codes, gaz):
    places, hwys, counties = [], [], []
    for code in same_codes:
        g = gaz.get(code)
        if not g:
            continue
        for c in g["counties"]:
            if c not in counties:
                counties.append(c)
        for h in g["highways"]:
            if h not in hwys:
                hwys.append(h)
        for p in g["places"]:
            if p not in places:
                places.append(p)
    return places, hwys, counties


def build_hotwords_speech(same_codes, gaz, ntok, event_phrase, budget):
    """ARM-1's product-speech style, at the SAME safe token budget.

    Exists only for the mechanism experiment: if this decodes fine at the
    safe budget, the 08-10 collapse was decode-room starvation; if it still
    collapses, it really was sot_prev repetition suppression. (Measured
    2026-08-10 full-23: 22/23 fine, 1 genuine suppression — starvation was
    the collapse, mirror-style priming keeps a residual suppression risk.)
    """
    places, hwys, counties = _collect(same_codes, gaz)
    if not (places or hwys or counties):
        return ""
    county_txt = " and ".join("%s County" % c for c in counties[:4]) or "Arizona"
    head = ("The National Weather Service in Phoenix has issued a %s for %s."
            % (event_phrase or "severe thunderstorm warning", county_txt))
    chosen = []
    for p in places:
        s = head + " Locations impacted include %s." % ", ".join(chosen + [p])
        if ntok(s) > budget:
            break
        chosen.append(p)
    out = head + ((" Locations impacted include %s." % ", ".join(chosen))
                  if chosen else "")
    return out if ntok(out) <= budget else ""


def transcribe_arm(sb, wav, hotwords, prompt=None, model=None):
    """One decode. Mirrors the shipped transcribe() exactly except hotwords,
    (--prompt-ab) the initial_prompt, or (--models) the model under test."""
    subprocess.run(["sox", wav, "-r", "16000", "-c", "1", TMP],
                   check=True, capture_output=True)
    kw = dict(language="en",
              initial_prompt=sb.WHISPER_PROMPT if prompt is None else prompt,
              vad_filter=True)
    if hotwords:
        kw["hotwords"] = hotwords
    segments, _ = get_model(sb, model).transcribe(TMP, **kw)
    return " ".join(s.text.strip() for s in segments).strip()


def lean_prompt(sb):
    """The shipped WHISPER_PROMPT minus its 27-town 'Locations include'
    sentence — the towns are hypothesised redundant now that hotwords carry
    the activation's OWN places. Counties + event vocabulary stay: they are
    the first-window content, where initial_prompt actually operates."""
    full = sb.WHISPER_PROMPT
    cut = full.find(" Locations include ")
    return full[:cut].rstrip() if cut != -1 else full


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only the N richest recordings")
    ap.add_argument("--show-hotwords", action="store_true")
    ap.add_argument("--speech-arm", action="store_true",
                    help="also run arm-1's product-speech style at the safe budget")
    ap.add_argument("--prompt-ab", action="store_true",
                    help="A/B the shipped WHISPER_PROMPT vs the lean (no-towns) "
                         "variant, hotwords ON in both (the production config)")
    ap.add_argument("--models", default="",
                    help="comma list of whisper models to A/B at the production "
                         "config (e.g. base,small,small.en). Downloads on first "
                         "use; per-decode wall time is reported alongside fields "
                         "because latency is half of this decision.")
    args = ap.parse_args()

    sb = load_bridge()
    gaz = json.load(open(GAZ, encoding="utf-8"))
    products = json.load(open(wx_wer.CACHE, encoding="utf-8"))
    recs = sorted(f for f in os.listdir(REC) if f.endswith(".wav.json"))

    m = get_model(sb)
    from faster_whisper.tokenizer import Tokenizer
    tok = Tokenizer(m.hf_tokenizer, m.model.is_multilingual,
                    task="transcribe", language="en")

    def ntok(s):
        # mirrors faster-whisper's own hotwords encoding (leading space)
        return len(tok.encode(" " + s.strip()))

    # Rank by how much ground truth each recording actually has -- a recording
    # with no places/highways cannot discriminate the arms either way.
    work = []
    for r in recs:
        name = r.replace(".wav.json", "")
        code = name.split("_")[-1]
        prod, note = wx_wer.match_product(name, products)
        if not prod:
            continue
        tgt = wx_wer.extract_targets(prod)
        weight = len(tgt["places"]) + len(tgt["highways"]) + len(tgt["measures"])
        work.append((weight, name, code, prod, note))
    work.sort(key=lambda x: -x[0])
    if args.limit:
        work = work[:args.limit]

    fields = ("event", "counties", "places", "highways", "measures")
    model_names = [m.strip() for m in args.models.split(",") if m.strip()]
    if model_names:
        arm_names = model_names
    elif args.prompt_ab:
        arm_names = ["cur", "lean"]
    else:
        arm_names = ["base", "frag"] + (["speech"] if args.speech_arm else [])
    tot = {a: {k: [0, 0] for k in fields} for a in arm_names}
    secs_tot = {a: 0.0 for a in arm_names}
    suppressed = []

    hdr = "%-26s" % "recording"
    for a in arm_names:
        hdr += " %-22s" % ("%s (e/c/p/h/m)" % a.upper())
    hdr += " chars"
    print(hdr)
    print("-" * len(hdr))
    for weight, name, code, prod, note in work:
        wav = os.path.join(REC, name + ".wav")
        if not os.path.exists(wav):
            continue
        same = prod.get("same") or []
        hw = sb.build_hotwords(same, gaz, ntok)
        if model_names:
            arms = [(m, hw, None) for m in model_names]
        elif args.prompt_ab:
            arms = [("cur", hw, None), ("lean", hw, lean_prompt(sb))]
        else:
            arms = [("base", "", None), ("frag", hw, None)]
            if args.speech_arm:
                phrase = wx_wer.EVENT_PHRASE.get(code, "")
                arms.append(("speech", build_hotwords_speech(
                    same, gaz, ntok, phrase, sb.HOTWORD_TOKEN_BUDGET), None))
        if args.show_hotwords:
            for a, ahw, _p in arms:
                if ahw:
                    print("  %s[%d tok]: %s" % (a, ntok(ahw), ahw[:200]))
        cells, lens, secs = {}, {}, {}
        for arm, hotwords, prompt in arms:
            t0 = time.time()
            tr = transcribe_arm(sb, wav, hotwords, prompt,
                                model=arm if model_names else None)
            secs[arm] = time.time() - t0
            secs_tot[arm] += secs[arm]
            lens[arm] = len(tr)
            s = wx_wer.score_one(tr, prod, code)
            parts = []
            for k in fields:
                h, n = s[k]
                tot[arm][k][0] += h
                tot[arm][k][1] += n
                parts.append("%d/%d" % (h, n) if n else "-")
            cells[arm] = " ".join(parts)
        flag = ""
        for arm in arm_names[1:]:
            if lens[arm] < 0.6 * lens[arm_names[0]]:
                flag = "  ⚠ SUPPRESSION(%s)" % arm
                suppressed.append("%s:%s" % (name, arm))
        row = "%-26s" % name
        for a in arm_names:
            row += " %-22s" % cells[a]
        row += " " + "/".join(str(lens[a]) for a in arm_names)
        row += " " + "/".join("%.0fs" % secs[a] for a in arm_names) + flag
        print(row)
        sys.stdout.flush()

    print("-" * len(hdr))
    print("%-12s %-9s %-9s %-9s %-9s %-9s" % ("ARM", *fields))
    for arm in arm_names:
        r = []
        for k in fields:
            h, n = tot[arm][k]
            r.append("%d/%d" % (h, n) if n else "-")
        print("%-12s %-9s %-9s %-9s %-9s %-9s" % (arm, *r))
    done = max(1, len([1 for _ in work]))
    print("avg s/decode: " + "  ".join(
        "%s %.1f" % (a, secs_tot[a] / done) for a in arm_names))
    base_arm = arm_names[0]
    be, bn = tot[base_arm]["event"]
    print()
    if suppressed:
        print("⚠⚠ SUPPRESSION on: %s" % ", ".join(suppressed))
        print("   transcript <60%% of %s length — priming is starving or" % base_arm)
        print("   suppressing the decode. REJECT regardless of field scores.")
    regressed = False
    for a in arm_names[1:]:
        ae, an = tot[a]["event"]
        if an and ae < be:
            regressed = True
            print("⚠⚠ REGRESSION: event phrase %s %d -> %s %d. REJECT that arm"
                  % (base_arm, be, a, ae))
            print("   regardless of any gain elsewhere — that is the exact failure")
            print("   mode noun-priming caused before.")
    if not regressed and not suppressed:
        print("event phrase held at %d/%d across all arms." % (be, bn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
