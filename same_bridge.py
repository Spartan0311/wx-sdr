#!/usr/bin/env python3
"""SAME/EAS -> MeshTOC bridge (v2: voice enrichment lane).

Orchestrates the whole receive pipeline: spawns rtl_fm + multimon-ng,
pumps demodulated audio between them, and parses SAME headers
(ZCZC-ORG-EEE-PSSCCC...+TTTT-JJJHHMM-LLLLLLLL-) from the decoder output.

On a SAME activation:
  1. The parsed header fires a text through /api/send IMMEDIATELY
     (deterministic path — never waits on the voice lane).
  2. The audio that follows is recorded until the NNNN EOM (or a max-length
     cap), then a worker thread transcribes it (faster-whisper) and
     condenses it (Ollama) into a "WX DETAIL:" follow-up message.
     Any failure in this lane is logged and swallowed.

Env config (all optional except the API key for real sends):
  MESHTOC_URL       default http://127.0.0.1:5070
  MESHTOC_API_KEY   write-scoped API key; empty = dry-run (print only)
  WX_CHANNEL        mesh channel index (default 4 = test) — operator-settable
                    from the MeshTOC WXSDR page tuning card, which
                    rewrites this var in wx.env and restarts the service
  WX_FREQ           NWR frequency for rtl_fm (default 162.550M)
  WX_GAIN           tuner gain (default 30)
  WX_SERIAL         dongle EEPROM serial to bind to (default 66666666)
  WX_FIPS           comma list of SSCCC county codes to alert on; empty = all
  WX_ALERT_CODES    event codes that get the "🔴 WX ALERT" label prefix.
                    DEFAULT = every warning-class code (WARNING_CODES) —
                    leave unset; set only to deliberately narrow. Label-only:
                    bells were removed 2026-07-21 (operator: no OTA bells at
                    all, the community weather channel must never ring
                    devices), so this never controls ALERT_APP delivery
  WX_VOICE          1 = voice enrichment lane on (default 1)
  WX_VOICE_MAX_SECS max voice recording length (default 150)
  WX_COND_EVERY_H   hours between periodic conditions posts; 0 = off (default)
  WX_COND_CHANNEL   mesh channel for conditions posts (default 4 — TEST
                    channel, deliberately not the community weather channel
                    until the lane earns trust)
  WHISPER_MODEL     faster-whisper model name (default base)
  OLLAMA_URL        default http://127.0.0.1:11434
  OLLAMA_MODEL      default qwen2.5:3b

Hand-test the parse/send path (no radio, no voice):
  echo 'EAS: ZCZC-WXR-RWT-004013+0100-1962000-KEC94   -' | \
    /opt/wx-sdr/venv/bin/python same_bridge.py --stdin
"""
import ctypes
import array
import collections
import json
import math
import os
import re
import socketserver
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler

MESHTOC_URL = (os.environ.get("MESHTOC_URL") or os.environ.get("MESHCMD_URL", "http://127.0.0.1:5070")).rstrip("/")
API_KEY = os.environ.get("MESHTOC_API_KEY") or os.environ.get("MESHCMD_API_KEY", "")
CHANNEL = int(os.environ.get("WX_CHANNEL", "4"))
FREQ = os.environ.get("WX_FREQ", "162.550M")
GAIN = os.environ.get("WX_GAIN", "30")
SERIAL = os.environ.get("WX_SERIAL", "66666666")
FIPS_FILTER = {c.strip() for c in os.environ.get("WX_FIPS", "").split(",") if c.strip()}
# Default = ALL warning-class codes (WARNING_CODES, defined with EVENTS below).
# The env override remains for narrowing, but leaving it unset is the intended
# configuration — see the tier comment at WARNING_CODES. Resolved after the
# tables are defined; kept here so every env knob reads in one place.
_ALERT_CODES_ENV = os.environ.get("WX_ALERT_CODES", "").strip()
VOICE = os.environ.get("WX_VOICE", "1") == "1"
VOICE_MAX_SECS = int(os.environ.get("WX_VOICE_MAX_SECS", "150"))
# Periodic conditions capture (operator, 2026-07-21): every N hours, record the
# routine broadcast off the live tap (NO decode blackout — unlike a sweep this
# never touches the dongle), transcribe + summarize it through the SAME guard
# chain as the alert lane, and send it as "WX COND:". 0 = off (the default).
# GATED TO A TEST CHANNEL while it earns trust: routine observation audio is
# exactly where the summarizer's known weaknesses live, so this does not go to
# the community channel until proven. Audio is discarded after transcription —
# hourly hoarding of routine conditions would drown the recordings lane.
COND_EVERY_H = int(os.environ.get("WX_COND_EVERY_H", "0") or 0)
COND_CHANNEL = int(os.environ.get("WX_COND_CHANNEL", "4"))
COND_CAPTURE_SECS = 75
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")

RTL_FM = "/usr/local/bin/rtl_fm"
SAMPLE_RATE = 22050

# SAME decoder. "samedec" (default) or "multimon" to fall back.
#
# WHY THE DEFAULT CHANGED (measured 2026-08-10, lab/same_lab.py):
# A SAME message is sent as THREE identical bursts precisely so a receiver can
# vote them back together. multimon-ng decodes each burst INDEPENDENTLY — if
# no single burst is clean, it emits nothing. samedec combines all three:
# equality checks at two bursts, bit voting at three.
#
# Synthetic sweep, identical audio to both, 12 trials/step:
#
#       SNR    multimon-ng   samedec
#      +1 dB      11/12        12/12
#       0 dB       8/12        12/12
#      -1 dB       1/12        12/12
#      -2 dB       0/12        12/12
#      -3 dB       0/12        12/12
#      -4 dB       0/12        12/12
#      -5 dB       0/12         6/12
#      -6 dB       0/12         0/12
#
# 50% decode: multimon-ng ~-0.3 dB, samedec ~-5 dB. ~4.7 dB of margin, and a
# 3 dB band where one gets nothing and the other gets everything. That band is
# where our field misses live: 2026-08-09T23:37 and 2026-08-10T03:15 each show
# an EOM triple with NO header and NO [undecoded] line — the short trailer
# survived, the long header did not, in ANY single burst.
#
# ⚠ NO stdbuf ON samedec. It is a Rust binary and does not use libc stdio, so
# stdbuf is a silent no-op there. It does not need one: measured, a header
# emits at +0s with the pipe still open, provided SAMPLES KEEP FLOWING. (With
# the input starved the combiner cannot advance its timeline to conclude the
# message ended, and output waits — an artifact of a test harness, never of
# rtl_fm, which streams continuously.)
DECODER = os.environ.get("WX_DECODER", "samedec").strip().lower()
SAMEDEC = "/opt/wx-sdr/bin/samedec"

# Per-SAME-code gazetteer (built at DEV time by lab/build_gazetteer.py from
# cached NWS ground truth — zero runtime WAN). Missing file = hotwords off =
# exactly the pre-v0.13.130 behaviour.
GAZETTEER = "/opt/wx-sdr/gazetteer.json"


def decoder_argv():
    """Command line for the SAME decoder. Both speak the same dialect:
    mono s16 native-endian on stdin at SAMPLE_RATE in, `ZCZC-...` / `NNNN`
    lines out — which is why handle_line() needs no change to swap them.
    multimon-ng prefixes its headers with 'EAS: '; HEADER_RE.search() does not
    care, and the [undecoded] branch keys on 'ZCZC' either way."""
    if DECODER == "multimon":
        # stdbuf IS required here: multimon-ng is C and would block-buffer.
        return ["stdbuf", "-oL", "multimon-ng", "-t", "raw", "-a", "EAS", "-"]
    return [SAMEDEC, "--rate", str(SAMPLE_RATE), "--file", "-"]

# ---------------------------------------------------------------------------
# GOLD DEFAULT — the known-good receiver configuration.
#
# This is the exact rtl_fm setup running on 2026-07-18 after the attic connector
# swap, which measured **16.4 dB over floor** (signal −14.0, floor −30.4) — the
# first reading above the 13 dB SAME decode margin since 07-16.
#
# DO NOT EDIT THESE VALUES WHEN TUNING. They are the baseline an experiment is
# measured against and reverted to. Tune by setting the matching keys in
# wx.env; every one of them falls back to the value here, so an empty/absent
# wx.env always reproduces the known-good receiver exactly.
# ---------------------------------------------------------------------------
GOLD = {
    "WX_FREQ": "162.550M",   # KEC94 Phoenix / South Mountain
    "WX_GAIN": "30",         # arbitrary but proven; lower can beat local RFI
    "WX_FIR": "",            # rtl_fm default = filter OFF ("bad roll off")
    "WX_DC": "0",            # no DC blocking filter
    "WX_PPM": "0",           # no frequency correction applied
    "WX_DEEMP": "0",         # MUST stay 0 — see below
}

# Experimental knobs, each defaulting to GOLD.
#
# -F 9  low-leakage downsample filter. rtl_fm's own help says the default (off)
#       "has bad roll off": we downsample ~1 MHz → 22050 Hz with no filter, so
#       out-of-band energy aliases into the audio band. That is exactly how
#       broadband hash from nearby electronics becomes audible hiss.
# -E dc DC blocking filter. The R828D is zero-IF, so it has a centre-frequency
#       DC spike by design. SAME's tones sit at 1562.5/2083.3 Hz, far from DC,
#       so this can't disturb the decode.
# -p    ppm frequency correction. If the dongle is off-frequency the signal sits
#       off-centre in the filter and reception degrades.
#
# ⚠ -E deemp is DELIBERATELY NOT WIRED UP by default. 75 µs de-emphasis corners
#   at ~2.1 kHz, right on SAME's SPACE tone (2083.3 Hz), while barely touching
#   MARK (1562.5 Hz). It would skew the very tone balance the decoder depends on
#   — trading alert reliability for listening comfort. If clean audio is wanted,
#   filter the LISTENER TEE instead and leave the decoder's stream raw.
FIR = os.environ.get("WX_FIR", GOLD["WX_FIR"]).strip()
DC_BLOCK = os.environ.get("WX_DC", GOLD["WX_DC"]).strip() == "1"
PPM = os.environ.get("WX_PPM", GOLD["WX_PPM"]).strip()
DEEMP = os.environ.get("WX_DEEMP", GOLD["WX_DEEMP"]).strip() == "1"


def rtl_argv(idx):
    """Build the rtl_fm command line. Order matches the gold command so a diff
    against it stays readable.

    WX_GAIN="auto" OMITS -g entirely, which is how rtl_fm selects tuner AGC —
    there is no "-g auto" spelling. Worth having as a comparison against the
    fixed gold gain, but note AGC rides the noise floor: it will happily raise
    gain into local RFI between transmissions, so a quieting reading taken under
    AGC is not comparable with a fixed-gain one.
    """
    argv = [RTL_FM, "-d", str(idx), "-f", FREQ, "-M", "fm",
            "-s", str(SAMPLE_RATE)]
    if GAIN.strip().lower() != "auto":
        argv += ["-g", GAIN]
    if FIR:
        argv += ["-F", FIR]
    if PPM and PPM != "0":
        argv += ["-p", PPM]
    if DC_BLOCK:
        argv += ["-E", "dc"]
    if DEEMP:
        argv += ["-E", "deemp"]
    argv.append("-")
    return argv


def tuning_state():
    """Current knob values plus how they differ from GOLD, so the UI can say
    'modified' without re-deriving the comparison."""
    cur = {"WX_FREQ": FREQ, "WX_GAIN": GAIN, "WX_FIR": FIR,
           "WX_DC": "1" if DC_BLOCK else "0", "WX_PPM": PPM or "0",
           "WX_DEEMP": "1" if DEEMP else "0"}
    diff = {k: {"gold": GOLD[k], "current": v}
            for k, v in cur.items() if str(v) != str(GOLD[k])}
    return {"current": cur, "gold": GOLD, "deviations": diff,
            "is_gold": not diff}
# Local zone for every human-facing time this bridge broadcasts. Declared, not
# inherited: this runs under its OWN systemd unit (wx-sdr.service), so unlike
# the default-scripts it gets no runner-injected TZ from the client — a bare
# .astimezone() here would resolve to the host zone, which on a server is very
# often UTC. Honour TZ when the unit sets one, else Arizona. Was a hardcoded -7, which was right
# for AZ but silently wrong for any DST zone and could not follow the
# operator's "Schedule timezone" setting.
def _resolve_local_tz():
    for name in (os.getenv("TZ"), "America/Phoenix"):
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except Exception:
            continue
    return timezone.utc


LOCAL_TZ = _resolve_local_tz()
DEDUP_SECS = 600     # SAME headers repeat 3x per activation
MAX_LEN = 200   # operator call 2026-07-21: the radio's hard wire is 200; the
                # old 180 was conservative headroom. _tagged() still guarantees
                # the OTA tag survives inside this.
# ---------------------------------------------------------------------------
# RECORDINGS.
#
# Until v0.11.110 this was a single fixed path (`/tmp/wx-voice.raw`) that every
# activation overwrote, and voice_worker deleted it afterwards anyway — so NO
# activation audio has ever survived. That's the one artifact you need to retune
# the whisper/Ollama lane when a summary comes out wrong, and the one thing you
# want after a missed alert.
#
# Sizing: 150 s at 22050 Hz/16-bit mono is ~6.6 MB. A busy monsoon week at
# ~10/day is ~66 MB/day, so the cap is generous and exists only to stop
# unbounded growth. Storage is LOCAL by decision — putting recordings on a
# network mount was evaluated and rejected: mounting it would have required
# restarting the host, taking the receiver down with it.
# ---------------------------------------------------------------------------
RECORDINGS_DIR = os.environ.get("WX_RECORDINGS_DIR", "/opt/wx-sdr/recordings")
REC_MAX_FILES = int(os.environ.get("WX_REC_MAX_FILES", "300"))
REC_MAX_BYTES = int(os.environ.get("WX_REC_MAX_MB", "2048")) * 1024 * 1024
# Scratch space for the 16 kHz copy whisper wants. Deliberately NOT in
# RECORDINGS_DIR so a half-finished conversion can never appear in the list.
VOICE_TMP = "/tmp/wx-voice-16k.wav"

# The COMPLETE NWR-SAME event set (verified against weather.gov/nwr/eventcodes
# 2026-07-21), so an unrecognized code can only mean NWS invented a new one.
# Warnings render UPPERCASE — on a 180-char text that casing is the loudest
# severity signal available. FSW/FZW are deliberately absent: those are
# broadcast-EAS codes, never sent over NWR.
EVENTS = {
    "TOR": "TORNADO WARNING", "TOA": "Tornado Watch",
    "SVR": "SEVERE T-STORM WARNING", "SVA": "Severe T-Storm Watch",
    "SVS": "Severe Weather Statement", "SPS": "Special Weather Statement",
    "SQW": "SNOW SQUALL WARNING",
    "FFW": "FLASH FLOOD WARNING", "FFA": "Flash Flood Watch",
    "FFS": "Flash Flood Statement", "FLW": "FLOOD WARNING",
    "FLA": "Flood Watch", "FLS": "Flood Statement",
    "CFW": "COASTAL FLOOD WARNING", "CFA": "Coastal Flood Watch",
    "DSW": "DUST STORM WARNING", "EWW": "EXTREME WIND WARNING",
    "HWW": "HIGH WIND WARNING", "HWA": "High Wind Watch",
    "WSW": "WINTER STORM WARNING", "WSA": "Winter Storm Watch",
    "BZW": "BLIZZARD WARNING",
    "HUW": "HURRICANE WARNING", "HUA": "Hurricane Watch",
    "HLS": "Hurricane Statement",
    "TRW": "TROPICAL STORM WARNING", "TRA": "Tropical Storm Watch",
    "TSW": "TSUNAMI WARNING", "TSA": "Tsunami Watch",
    "SSW": "STORM SURGE WARNING", "SSA": "Storm Surge Watch",
    "SMW": "SPECIAL MARINE WARNING",
    "AVW": "AVALANCHE WARNING", "AVA": "Avalanche Watch",
    "EQW": "EARTHQUAKE WARNING", "VOW": "VOLCANO WARNING",
    "EVI": "EVACUATION IMMEDIATE", "CEM": "CIVIL EMERGENCY MESSAGE",
    "CDW": "CIVIL DANGER WARNING", "LEW": "LAW ENFORCEMENT WARNING",
    "SPW": "SHELTER IN PLACE WARNING", "FRW": "FIRE WARNING",
    "HMW": "HAZARDOUS MATERIALS WARNING", "NUW": "NUCLEAR PLANT WARNING",
    "RHW": "RADIOLOGICAL HAZARD WARNING", "EAN": "NATIONAL EMERGENCY MESSAGE",
    "BLU": "BLUE ALERT", "CAE": "CHILD ABDUCTION EMERGENCY",
    "LAE": "Local Area Emergency", "TOE": "911 Telephone Outage",
    "NPT": "National Periodic Test", "RWT": "Required Weekly Test",
    "RMT": "Required Monthly Test", "DMO": "Practice/Demo",
    "ADR": "Administrative Message",
}

# Severity tiers, decided by CODE — never by parsing the label.
#
# WARNING_CODES is every warning/emergency-class code above: immediately
# actionable, life-safety. It is the DEFAULT for WX_ALERT_CODES so the env
# never needs editing when NWS fires something exotic — a tsunami warning in
# Arizona costs nothing to classify and would be correctly loud if the
# impossible happened. WATCH_CODES is fixed (not env-tunable): a watch is a
# watch. Everything else — statements, tests, administrative — renders plain.
#
# Since the 2026-07-21 bell removal the tier picks only the label prefix and
# its emoji (compose()), never ALERT_APP delivery. The emoji is the one
# severity signal that survives a lock-screen preview truncating the text.
WARNING_CODES = {
    "TOR", "SVR", "SQW", "FFW", "FLW", "CFW", "DSW", "EWW", "HWW", "WSW",
    "BZW", "HUW", "TRW", "TSW", "SSW", "SMW", "AVW", "EQW", "VOW", "EVI",
    "CEM", "CDW", "LEW", "SPW", "FRW", "HMW", "NUW", "RHW", "EAN", "BLU",
    "CAE",
}
WATCH_CODES = {
    "TOA", "SVA", "FFA", "FLA", "CFA", "HWA", "WSA", "HUA", "TRA", "TSA",
    "SSA", "AVA",
}

# An empty/absent env means "the full warning tier", the intended steady state.
ALERT_CODES = ({c.strip() for c in _ALERT_CODES_ENV.split(",") if c.strip()}
               or set(WARNING_CODES))

AZ_FIPS = {
    "04001": "Apache", "04003": "Cochise", "04005": "Coconino",
    "04007": "Gila", "04009": "Graham", "04011": "Greenlee",
    "04012": "La Paz", "04013": "Maricopa", "04015": "Mohave",
    "04017": "Navajo", "04019": "Pima", "04021": "Pinal",
    "04023": "Santa Cruz", "04025": "Yavapai", "04027": "Yuma",
}

# Provenance stamp on every OTA message: this came off real NWR air via the SDR.
# Deliberately contains no form of the word TEST — these are genuine life-safety
# warnings, and a recipient scanning the channel treats "TEST" as ignore-me.
# ALSO no "Check"/"Ping": keyword-triggered signal-report responders treat those
# as a report request — the original "OTA-SDR-Check" tag had two azmsh nodes
# DMing a reception report for every stamped alert (confirmed with their owner,
# 2026-08-10). An automated broadcast must not say other bots' magic words.
# Appended truncation-safely in compose()/voice_worker(), never sliced.
OTA_TAG = "OTA-SDR"

# Biases whisper's decoder toward Arizona proper nouns INSTEAD of correcting them
# after the fact. Verified need (2026-07-18 FFW): the same audio mangled Gila as
# "Hila" then "Pula", and Pinal as "Pienau"/"Pinot"/"Pinol" across two runs — the
# misspellings are NOT stable, so a fixed correction list in SUMMARY_PROMPT can
# never enumerate them. Counties come from AZ_FIPS so the two can't drift.
# Keep it under ~224 tokens; faster-whisper truncates a longer prompt silently.
WHISPER_PROMPT = (
    # Phrased as real NWR product speech, NOT a bare comma list. A list-shaped
    # prompt measurably corrupted the opening window (2026-07-18: "flash flood
    # warning" decoded as "flash ON warning") by priming the decoder toward
    # nouns — the event phrase is the most important string in the message, so
    # event vocabulary is primed FIRST and the names ride along in context.
    # Counties appear as "X County" because that is how they are spoken; a bare
    # "Gila" in a list did not stop it decoding as "Hila"/"Pula".
    "The National Weather Service in Phoenix has issued a flash flood warning, "
    "a severe thunderstorm warning, a tornado warning, a dust storm warning, "
    "and a special weather statement for the following counties in Arizona: "
    + ", ".join("%s County" % n for n in sorted(AZ_FIPS.values()))
    + ". Locations include Globe, Miami, Claypool, Superior, Top-of-the-World, "
    "Central Heights, Midland City, Apache Lake, Inspiration, Show Low, "
    "Gila Bend, Casa Grande, Mesa Gateway, Deer Valley, Flagstaff, Blythe, "
    "Yuma, Sierra Vista, Nogales, Payson, Prescott, Kingman, Buckeye, "
    "Queen Creek, Apache Junction, Fountain Hills, and Cave Creek."
)

# ---------- dynamic hotwords (scoped whisper priming, v0.13.130) ----------
#
# The decoded header names the warned counties, so the voice decode can be
# primed with THAT warning's vocabulary instead of all of Arizona. Benched
# 2026-08-10 (lab/wx_arms.py, 23 real activations, pilot + full run agreeing):
# highways 22/34 -> 30/34, counties 26->28/29, places 50->56/98, event phrase
# 23/23 held, no transcript-length loss on any recording.
#
# ⚠ FRAGMENTS, NEVER PRODUCT SENTENCES. Both extremes are measured failures:
# a bare noun list corrupted the opening window ("flash ON warning"), and
# sentences mirroring the broadcast can make the decoder treat matching
# speech as already-transcribed and drop it (1 suppression in 23 at safe
# budget; total collapse when over budget).
#
# ⚠ THE BUDGET IS REAL TOKENS, COUNTED BY THE MODEL'S OWN TOKENIZER.
# max_length is 448 and hotwords + up to 223 tokens of rolling context share
# it with the DECODE ROOM (~221 - hotwords_tokens per window). At ~220
# hotwords tokens decoding dies outright ("maximum decoding length must
# be > 0"); just under, windows truncate SILENTLY. 96 tokens leaves ~125 of
# room against ~100 needed for 30 s of NWR speech. A chars/4 estimate is NOT
# a substitute — this vocabulary measures ~3.5 chars/token and overshoot
# fails silently. Full math: lab/wx_arms.py.
HOTWORD_TOKEN_BUDGET = 96

_gaz = None


def _gazetteer():
    global _gaz
    if _gaz is None:
        try:
            with open(GAZETTEER, encoding="utf-8") as fh:
                _gaz = json.load(fh)
        except Exception as e:
            log("[voice] no gazetteer (%s) — hotwords off" % e)
            _gaz = {}
    return _gaz


def build_hotwords(same_codes, gaz, ntok):
    """Compose scoped, token-budgeted priming FRAGMENTS for one activation.

    Ranking: counties -> highways -> places, places trimmed to fit. Highways
    ahead of places on purpose — a highway number is the most actionable
    field in a dust-storm or flood warning and it was the one measurably
    failing (22/34 before, 30/34 with this).

    Shared with the bench: lab/wx_arms.py exec-loads this file and calls THIS
    function, so the arm being measured is always the one shipping.
    """
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
    if not (places or hwys or counties):
        return ""

    parts = []
    if counties:
        if len(counties) == 1:
            parts.append("Across %s county." % counties[0])
        else:
            parts.append("Across %s and %s counties."
                         % (", ".join(counties[:3][:-1]), counties[:3][-1]))
    if hwys:
        keep = list(hwys)
        while keep:
            cand = parts + ["Along %s." % ", ".join(keep)]
            if ntok(" ".join(cand)) <= HOTWORD_TOKEN_BUDGET:
                break
            keep.pop()
        if keep:
            parts.append("Along %s." % ", ".join(keep))
    chosen = []
    for p in places:
        cand = parts + ["Near %s." % ", ".join(chosen + [p])]
        if ntok(" ".join(cand)) > HOTWORD_TOKEN_BUDGET:
            break
        chosen.append(p)
    if chosen:
        parts.append("Near %s." % ", ".join(chosen))
    out = " ".join(parts)
    return out if out and ntok(out) <= HOTWORD_TOKEN_BUDGET else ""

SUMMARY_PROMPT = (
    "You condense NOAA Weather Radio transcripts for a low-bandwidth "
    "emergency mesh radio network.\n"
    "Rules:\n"
    "- Output ONE line, 140 characters maximum. English only.\n"
    "- Use ONLY facts present in the transcript. Never invent times, "
    "numbers, or instructions.\n"
    "- Never widen the geographic scope. Name only the counties and places "
    "the transcript names. NEVER say 'statewide', 'regionwide', 'across "
    "Arizona', or any wording implying a larger area than was stated.\n"
    "- If the transcript states an expiry or 'until' time, ALWAYS keep it in "
    "the summary. It is the single most actionable fact in a warning.\n"
    "- The speech-to-text mangles Arizona place names; correct them "
    "('Shalom'=Show Low, 'Healabend'=Gila Bend, 'Costa Grande'=Casa Grande, "
    "'Messagedway'=Mesa Gateway, 'Dear Valley'=Deer Valley, "
    "'flying staff'=Flagstaff, 'Blives'=Blythe, 'Hila'=Gila, "
    "'Pienau'/'Pinot'=Pinal, 'clapal'=Claypool, 'globe-many'=Globe-Miami, "
    "'UMA'=Yuma, 'Si Va'=Sierra Vista, 'Mary Copa'=Maricopa, "
    "'Action Village'=Ak-Chin Village, 'Vika Wash'=Vekol Wash).\n"
    "- No emoji, no quotes, no preamble. The summary line only.\n\n"
    "TRANSCRIPT:\n"
)

# ---------- summary guards (deterministic, code-side) ----------
# Landed 2026-07-21 off the wx_ab.py A/B (4 runs, 5 clips x 2 arms x 3 reps,
# /opt/wx-sdr/lab/). qwen2.5:3b obeys SUMMARY_PROMPT only PROBABILISTICALLY:
# measured baseline 8/15 summaries carried a number absent from the source,
# name corrections fired ~1 run in 3, the 160 cap broke routinely (worst 336),
# and one FLASH FLOOD WARNING summary leaked Chinese. Deterministic rules are
# therefore ENFORCED here, in code, at the point of production; the prompt's
# copies of these rules stay as belt-and-braces (they make the model mostly
# right, the guards make it certainly right). Order is load-bearing — see
# guard_summary().

# The whisper mishearings. Mirrors SUMMARY_PROMPT's list; new names belong in
# WHISPER_PROMPT first (upstream fix), then here.
CORRECTIONS = [
    ("Shalom", "Show Low"), ("Healabend", "Gila Bend"), ("Costa Grande", "Casa Grande"),
    ("Messagedway", "Mesa Gateway"), ("Dear Valley", "Deer Valley"),
    ("flying staff", "Flagstaff"), ("Blives", "Blythe"), ("Hila", "Gila"),
    ("Pienau", "Pinal"), ("Pinot", "Pinal"), ("clapal", "Claypool"),
    ("globe-many", "Globe-Miami"), ("UMA", "Yuma"), ("Si Va", "Sierra Vista"),
    ("Mary Copa", "Maricopa"),
    ("OnServations", "Observations"),
    # 2026-08-10: confirmed STABLE across two independent voices (KEC94 off-air
    # + third-party TTS fixtures) — the bar the 08-10 "manglings are unstable"
    # amendment set. Full phrases only: a bare "Action" would fire on real
    # sentences ("precautionary actions").
    ("Action Village", "Ak-Chin Village"),
    ("Action Indian Community", "Ak-Chin Indian Community"),
    ("Vika Wash", "Vekol Wash"),
    ("Wife-threatening", "Life-threatening"),
    ("access of", "excess of"),
    # NOT corrected: "miles per hour in dust" (gusts mangling) — "dust" is
    # real vocabulary in DSW products; a blind swap would corrupt the exact
    # products it appears in most.
]

# Cap policy (operator, 2026-07-21): smaller is better — SUMMARY_PROMPT asks
# for 140, because the 7b shakedown (5 clips x 3 reps) measured the model
# overshooting its stated target ~10% on 15/15 reps — but an UNDERSTANDABLE
# line beats a small one. So a summary passes VERBATIM up to SUMMARY_HARDLEN,
# chosen as the largest size where "WX DETAIL: " (11) + summary + the OTA tag
# (16) still fit MAX_LEN=200 with _tagged() never trimming a character
# (200 - 11 - 16 - 1 margin = 172). Only past that does G3 cut, sentence-aware.
SUMMARY_MAXLEN = 160    # soft target; the scorer + halfway floor reason on this
SUMMARY_HARDLEN = 172   # verbatim allowance; the cut point when exceeded


def apply_corrections(text):
    """G1 — fix known mishearings. Applied to the TRANSCRIPT before summarize()
    AND to the model's output (it re-abbreviates 'Yuma'->'UMA' even when fed a
    corrected transcript — measured). \\b keeps 'UMA' from matching inside
    'Yuma' (no word boundary between 'Y' and 'uma')."""
    for bad, good in CORRECTIONS:
        text = re.sub(r"\b%s\b" % re.escape(bad), good, text, flags=re.I)
    return text


def unsupported_numbers(summary, transcript):
    """Every number in the summary must appear in the transcript. ALLOWLIST.

    Shape-matching times was a blocklist and it lost three times running in the
    lab: the model answered "2PM", then "18Z", then a bare "1230", each landing
    outside whatever pattern had just been widened. Enumerating the numbers the
    SOURCE actually contains is finite and known. Plain digit runs, PLUS the
    joined form of clock times so "12.30 p.m." still authorises "12:30"."""
    tnums = set(re.findall(r"\d+", transcript))
    # ⚠ THE SEPARATOR CLASS IS LOAD-BEARING — whisper does not render times
    # consistently. Across one night's twelve transcripts it wrote the SAME
    # expiry three different ways: "10.15pm", "815 p.m." and "10-15 pm". The
    # hyphen was missing here, so on 2026-08-10T04:29 the model's correctly
    # normalised "10:15" matched nothing, was judged a fabrication, and the
    # expiry was stripped out of a live severe thunderstorm warning — leaving
    # "for Maricopa County MST." Eight minutes later the SAME warning came
    # through with a period and sailed past untouched, which is the field
    # control that proves it: identical content, different punctuation,
    # opposite outcome. NWS confirmed 10:15 PM was correct.
    tnums |= {re.sub(r"\D", "", x)
              for x in re.findall(r"\d{1,2}[:.\-]\d{2}", transcript)}
    tnums = {n for n in tnums if n}
    out = []
    for m in re.finditer(r"\d[\d:.]*\d|\d", summary):
        tok = m.group(0).rstrip(".")
        if re.sub(r"\D", "", tok) not in tnums:
            out.append(tok)
    return out


# What may be consumed AROUND a stripped number: a leading connector word, and
# a trailing time-unit token from this CLOSED set only. The lab's first cut
# ate any <=4-letter trailing word ('1 to 2 inches fallen' -> 'es fallen');
# enumerating the units is finite, enumerating collateral words is not.
_G2_CONNECTOR = r"(?:\b(?:until|untill|expires?|till|thru|through|at)\b\s*)?"
# {0,2}, not ?: a clock time carries BOTH a meridiem and a zone ("10:15 pm
# MST"). Consuming only one left the other stranded — the 04:29 strip produced
# "for Maricopa County MST." Bounded at 2 rather than * on purpose; the set is
# closed, but an unbounded repeat is one careless edit away from eating words.
_G2_UNIT      = r"(?:\s*(?:[ap]\.?m\.?|MST|MDT|UTC|GMT|Z)\b){0,2}"


def strip_invented_times(summary, transcript):
    """G2 — drop any number the transcript does not support, with its connector
    and (closed-set) time unit, then repair punctuation.

    Strip rather than reject the whole summary: on a warning, a line that has
    lost a fabricated expiry still carries the hazard and the counties, whereas
    no line at all carries nothing."""
    bad = unsupported_numbers(summary, transcript)
    out = summary
    for tok in bad:
        out = re.sub(_G2_CONNECTOR + re.escape(tok) + _G2_UNIT,
                     " ", out, count=1, flags=re.I)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([,.;:])", r"\1", out)
    out = re.sub(r"(?:[,;]\s*){2,}", ", ", out)
    out = re.sub(r"\.{2,}", ".", out)   # 'Yuma..' after a stripped trailing clause
    out = re.sub(r"(?:until|untill|expires?|till|thru|through|at)\s*[.,;:]*\s*$",
                 "", out, flags=re.I).strip()
    # A dangling sentence-opening "Ends"/"End" left behind when the time it
    # introduced was stripped ("Breaks recommended. Ends"). Requires a
    # sentence boundary before it so a legitimate "...until the rain ends."
    # is never touched.
    out = re.sub(r"(?<=[.;:])\s+ends?\s*[.,;:]*\s*$", "", out, flags=re.I)
    return out.rstrip(" ,;:")


def cap_len(summary):
    """G3 — length governor. A truncation cannot fail; asking the model to
    count could (worst: 336). Reworked for 7b (v0.13.63): a summary up to
    SUMMARY_HARDLEN passes VERBATIM (operator: a little over the 160 target
    is fine — understandable beats small; the wire is 200 and _tagged() is
    the absolute guard). Past that, cut SENTENCE-AWARE — the old blind
    word-boundary chop left mid-sentence stumps ('Be cautious, especially
    at.' — 15/15 reps on the adoption shakedown). Prefer the last sentence
    end inside the allowance when it lands past half the soft target; else
    word boundary as before. The lookbehind keeps 'a.m.'/'p.m.' periods from
    reading as sentence ends."""
    if len(summary) <= SUMMARY_HARDLEN:
        return summary
    cut = summary[:SUMMARY_HARDLEN - 1]
    best = -1
    for m in re.finditer(r"(?<![ap]\.m)[.!?](?=\s|$)", cut, flags=re.I):
        best = m.end()
    if best >= SUMMARY_MAXLEN // 2:
        return cut[:best].rstrip()
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return (cut.rstrip(" ,;:.") + ".")[:SUMMARY_HARDLEN]


# Characters worth keeping above ASCII: degree sign, en/em dash, curly
# apostrophe. Everything else non-ASCII is a language leak.
KEEP_EXTRA = "°–—’"


def strip_non_latin(s):
    """G5 — drop non-Latin script. The prompt has said 'English only' since its
    first version and qwen still leaked Chinese into a FLASH FLOOD WARNING
    summary during the A/B. A mesh client rendering CJK for a life-safety alert
    is worse than a shorter line."""
    out = "".join(c for c in s if ord(c) < 128 or c in KEEP_EXTRA)
    out = re.sub(r"\s{2,}", " ", out)
    return re.sub(r"[\s,;:.]+$", "", out).strip()


def guard_summary(summary, transcript):
    """Run the full guard chain over a model summary. ORDER IS LOAD-BEARING:
    strip_non_latin -> apply_corrections -> strip_invented_times -> cap_len.
    Cap LAST, or a stripped clause leaves the line back over 160. G2 runs
    against the corrected transcript the model was fed (corrections never
    touch digits, so the allowlist is unaffected)."""
    out = strip_non_latin(summary)
    out = apply_corrections(out)
    removed = unsupported_numbers(out, transcript)
    if removed:
        log("[guard] stripped unsupported number(s) %s from summary" % removed)
    out = strip_invented_times(out, transcript)
    return cap_len(out)


HEADER_RE = re.compile(
    r"ZCZC-([A-Z]{3})-([A-Z]{3})-([0-9-]+)\+(\d{4})-(\d{7})-(.{1,8})")

_seen = {}
_whisper = None
_tokenizer = None
# Guards both the lazy WhisperModel init and the single scratch file every
# transcription downsamples into — see transcribe().
_whisper_lock = threading.Lock()
_rec_lock = threading.Lock()
_rec = {"active": False, "file": None, "deadline": 0, "label": "", "path": None,
        "codes": []}

# ---------- live audio tap (status/listen transport) ----------
#
# THE RULE: the pump loop feeds the decoder, and that path
# is life-safety. Nothing here may ever block it. Listeners get a bounded deque
# and we DROP for them when it fills — a stalled browser or a sleeping phone
# glitches its own audio and cannot slow the decoder. Never add a blocking put()
# or an unbounded buffer to this.
CTRL_HOST = os.environ.get("WX_CTRL_HOST", "127.0.0.1")
CTRL_PORT = int(os.environ.get("WX_CTRL_PORT", "5071"))
# One listener. The page is owner-gated and single-operator, so a second slot
# only ever served a stale session that hadn't released yet — and each listener
# pins one of the 16 gunicorn threads on the worker that also holds the node TCP
# link. Raise via WX_MAX_LISTENERS if that ever changes.
MAX_LISTENERS = int(os.environ.get("WX_MAX_LISTENERS", "1"))
# ~1.5 s of audio at 22050 Hz/16-bit with 4096-byte chunks (≈10.8 chunks/s).
LISTEN_QUEUE = 16

_listeners = []            # list of collections.deque, one per attached client
_listeners_lock = threading.Lock()
_ctrl_state = {"argv": []}  # the ACTUAL launched command, reported verbatim
_level = {"peak_db": -99.0, "rms_db": -99.0, "at": 0.0, "chunks": 0}
_level_lock = threading.Lock()
# Rolling RMS history (~30 s at ≈10.8 chunks/s) whose MINIMUM is the quiet-passage
# level — i.e. the hiss between words.
#
# WHY THIS AND NOT RAW AUDIO LEVEL: FM is constant-envelope, so demodulated audio
# amplitude follows frequency DEVIATION, not RF signal strength. A strong signal
# and a barely-above-threshold one give the same peak. What a weak FM signal
# actually does is get noisy, so the useful continuous metric is quieting:
# loud-passage peak minus quiet-passage floor ≈ audio SNR. That number should
# TRACK the antenna, where a bare level meter would not.
_rms_hist = collections.deque(maxlen=320)

# rtl_fm emits a LOUD burst on startup — PLL settling and buffer garbage before
# the stream is real. Measured 2026-07-18: −15.1 dBFS at gain 49 vs a −18.8
# steady state, and it scales with gain. A single such sample poisons max() for
# the entire 30 s the window retains it, so `quieting` reads 15.0 instead of
# 11.2 and then "mysteriously" collapses the moment the window fills. That
# artifact is what made gains 40/49 briefly look better than gold during A/B
# testing; they are not.
#
# Two guards: drop the burst before it ever enters the history, and refuse to
# report a spread until the window is genuinely FULL (a partially-filled window
# under-reports, because it has had fewer chances to see both a loud passage and
# a quiet one).
WARMUP_CHUNKS = 60          # ≈5.5 s at 22050 Hz / 4096-byte chunks
_warmup_left = WARMUP_CHUNKS
_started_at = time.time()


def _tap(chunk):
    """Fan a pump chunk out to the meter and any listeners. Must stay cheap and
    non-blocking — it runs inline in the decoder's path."""
    # Level: array + max/min are C-speed scans, so this is effectively free.
    # A pure-Python per-sample loop here would burn real CPU 10x/second.
    try:
        a = array.array("h")
        a.frombytes(chunk if len(chunk) % 2 == 0 else chunk[:-1])
        if a:
            peak = max(abs(min(a)), abs(max(a)))
            # RMS over a 1-in-16 subsample: same story shape, ~6% of the work.
            sub = a[::16]
            rms = math.sqrt(sum(s * s for s in sub) / len(sub)) if sub else 0
            rms_db = 20 * math.log10(rms / 32768.0) if rms else -99.0
            global _warmup_left
            with _level_lock:
                _level["peak_db"] = 20 * math.log10(peak / 32768.0) if peak else -99.0
                _level["rms_db"] = rms_db
                _level["at"] = time.time()
                _level["chunks"] += 1
                # Live level still updates during warm-up (it's useful to see the
                # receiver come alive); only the quieting HISTORY is protected.
                if _warmup_left > 0:
                    _warmup_left -= 1
                else:
                    _rms_hist.append(rms_db)
    except Exception:
        pass
    # Conditions capture reads a private tap, NOT a _listeners slot — see the
    # conditions section. Plain deque.append: bounded, non-blocking, rule 1.
    cq = _cond_q
    if cq is not None:
        cq.append(chunk)
    if not _listeners:
        return
    with _listeners_lock:
        for q in _listeners:
            # deque(maxlen=) discards the OLDEST on overflow — the drop-on-full
            # contract, with no branch and no lock contention on the pump side.
            q.append(chunk)


def _wav_header(data_len=None, sample_rate=SAMPLE_RATE, bits=16, channels=1):
    """A 44-byte WAV header.

    `data_len=None` declares a length we'll never reach, so browsers treat the
    body as an open-ended stream — that's the live-listen case. A real length is
    used for stored recordings, where the file must be seekable and show a
    correct duration in a player.

    Either way this avoids adding an encoder (ffmpeg/opus) to the life-safety
    container just to play audio back.
    """
    if data_len is None:
        data_len = 0x7FFFFFFF - 36
    byte_rate = sample_rate * channels * bits // 8
    return (b"RIFF" + struct.pack("<I", data_len + 36) + b"WAVE" +
            b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                                  byte_rate, channels * bits // 8, bits) +
            b"data" + struct.pack("<I", data_len))


def level_snapshot():
    with _level_lock:
        d = dict(_level)
        hist = list(_rms_hist)
    # Quiet-passage floor + the loud/quiet spread. `quieting_db` is the number to
    # watch over time: it should hold steady while the antenna does, and fall as
    # the signal weakens or the noise floor rises. It needs speech gaps to be
    # meaningful, so it's only reported once the window has filled enough to have
    # seen some (NWR talks continuously, but pauses between sentences).
    # Only report once the window is genuinely FULL. A partial window
    # under-reports (fewer chances to see both extremes), so a value reported
    # mid-fill would be a different measurement wearing the same name.
    if len(hist) >= _rms_hist.maxlen:
        d["floor_db"] = min(hist)
        d["loud_db"] = max(hist)
        d["quieting_db"] = d["loud_db"] - d["floor_db"]
        d["warming_up"] = False
    else:
        d["floor_db"] = d["loud_db"] = d["quieting_db"] = None
        d["warming_up"] = True
    d["window_s"] = round(len(hist) / 10.8, 1)
    d["window_full_s"] = round(_rms_hist.maxlen / 10.8, 1)
    d["listeners"] = len(_listeners)
    # Reported so the UI states the real cap instead of hardcoding a number that
    # drifts the moment WX_MAX_LISTENERS is set.
    d["max_listeners"] = MAX_LISTENERS
    d["uptime"] = time.time() - _started_at
    # Stale means the pump has stopped feeding us — the receiver is wedged or
    # rtl_fm died. Callers surface that rather than showing a frozen meter.
    d["stale"] = (time.time() - d["at"]) > 5 if d["at"] else True
    return d


# ---------- periodic conditions capture ----------
#
# NWR loops current conditions/forecast continuously, so a bounded capture at
# any moment yields a summarizable product — "weather info with no internet"
# (operator, 2026-07-21). The capture reads a PRIVATE tap deque fed by _tap();
# it deliberately does NOT join _listeners, which would consume the 1-listener
# cap and lie in the UI's "receiver reports N of M" count. The decoder is
# untouched throughout: no dongle handoff, no service restart, no blackout.
_cond_lock = threading.Lock()
_cond = {"running": False, "last": 0.0, "last_summary": None, "last_error": None}
# maxlen bounds pump-side memory if the worker dies mid-capture: ~111 s of
# audio at ≈10.8 chunks/s, comfortably over COND_CAPTURE_SECS so nothing real
# is ever dropped.
_cond_q = None


def conditions_worker(trigger):
    """One capture → transcribe → summarize → send cycle. Runs in its own
    thread. The ALERT LANE ALWAYS WINS: skip if an activation is recording when
    we start, and abort (discard) if one begins while we were capturing —
    whisper time belongs to the warning, and a conditions post delayed an hour
    costs nothing."""
    global _cond_q
    with _cond_lock:
        if _cond["running"]:
            return
        _cond["running"] = True
    tmp = "/tmp/wx-cond-capture.wav"
    try:
        if not VOICE:
            log("[voice] conditions skipped (%s): voice lane is off" % trigger)
            return
        with _rec_lock:
            if _rec["active"]:
                log("[voice] conditions skipped (%s): activation recording in progress" % trigger)
                return
        with _retx_lock:
            if _retx["name"]:
                log("[voice] conditions skipped (%s): re-transcribe running" % trigger)
                return
        log("[voice] conditions capture started (%s, %ds)" % (trigger, COND_CAPTURE_SECS))
        q = collections.deque(maxlen=1200)
        _cond_q = q
        time.sleep(COND_CAPTURE_SECS)
        _cond_q = None
        with _rec_lock:
            if _rec["active"]:
                log("[voice] conditions aborted: activation started mid-capture")
                return
        if len(q) < 100:      # <~10 s of audio = the pump isn't feeding us
            _cond["last_error"] = "no audio captured"
            log("[voice] conditions aborted: no audio captured (receiver down?)")
            return
        with open(tmp, "wb") as fh:
            data = b"".join(q)
            fh.write(_wav_header(len(data)))
            fh.write(data)
        text = transcribe(tmp)
        summary = None
        if len(text) >= 20:
            corrected = apply_corrections(text)
            raw = summarize(corrected)
            if raw:
                summary = guard_summary(raw, corrected)
        if summary:
            # Same guard chain as the alert lane (MUST NOT diverge), but its own
            # prefix and its own channel — this is routine info, never an alert.
            send(_tagged("WX COND: " + summary), False, channel=COND_CHANNEL)
        else:
            log("[voice] conditions produced no summary (transcript %d chars)" % len(text))
        with _cond_lock:
            _cond["last"] = time.time()
            _cond["last_summary"] = summary
            _cond["last_error"] = None if summary else "no summary produced"
    except Exception as e:
        with _cond_lock:
            _cond["last_error"] = str(e)
        log("[voice] conditions cycle failed: %s" % e)
    finally:
        _cond_q = None
        # Audio is DISCARDED by design — see the COND_EVERY_H comment up top.
        try:
            os.remove(tmp)
        except OSError:
            pass
        with _cond_lock:
            _cond["running"] = False


def _cond_snapshot():
    with _cond_lock:
        return {
            "every_h": COND_EVERY_H,
            "channel": COND_CHANNEL,
            "running": _cond["running"],
            "last": _cond["last"] or None,
            "last_summary": _cond["last_summary"],
            "last_error": _cond["last_error"],
        }


def conditions_timer():
    """Fires a cycle every COND_EVERY_H hours. The first cycle waits a full
    interval — a service restart must not immediately key up the mesh."""
    with _cond_lock:
        _cond["last"] = time.time()
    while True:
        time.sleep(60)
        with _cond_lock:
            due = _cond["last"] + COND_EVERY_H * 3600
            busy = _cond["running"]
            if not busy and time.time() >= due:
                # Stamp BEFORE running so a failing cycle retries next interval,
                # not every minute.
                _cond["last"] = time.time()
                fire = True
            else:
                fire = False
        if fire:
            threading.Thread(target=conditions_worker, args=("timer",),
                             daemon=True).start()


# Async re-transcribe job state — exactly one at a time (see do_POST). `name`
# doubles as the busy flag; /status reports it so the app can poll cheaply on
# the meter tick it already runs.
_retx_lock = threading.Lock()
_retx = {"name": None, "started": 0}


def _retx_worker(name):
    try:
        res = retranscribe(name)
        if res.get("error"):
            log("[voice] re-transcribe failed for %s: %s" % (name, res["error"]))
    finally:
        # ALWAYS clear, even on an unexpected raise — a stuck busy flag would
        # block every future re-transcribe until a service restart.
        with _retx_lock:
            _retx["name"] = None
            _retx["started"] = 0


class _Ctrl(BaseHTTPRequestHandler):
    """Loopback-only control/status surface. Deliberately tiny and unauthenticated
    because it binds 127.0.0.1 and MeshTOC is the only client — the auth
    boundary is MeshTOC's owner_required, not here."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass   # journal is for the pipeline, not per-request noise

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/status"):
            with _retx_lock:
                retx = _retx["name"]
            return self._json({
                "ok": True, "freq": FREQ, "gain": GAIN, "channel": CHANNEL,
                "voice": VOICE, "level": level_snapshot(),
                # The launched argv verbatim — not a reconstruction. Debugging
                # the wrong command is how tuning experiments go sideways.
                "argv": _ctrl_state.get("argv") or [],
                "tuning": tuning_state(),
                # Name of the recording an async re-transcribe is chewing on,
                # or None. The app's 2 s meter poll watches this to know when
                # to reload the recordings list.
                "retranscribing": retx,
                "conditions": _cond_snapshot(),
            })
        if self.path.startswith("/listen"):
            return self._listen()
        # NOTE there is deliberately no /recordings listing here. MeshTOC
        # reads the directory directly (resolving it from the same wx.env this
        # daemon reads), so the list still works when the receiver is DOWN —
        # which is exactly when someone wants the recordings. Only /transcribe
        # has to live here, because whisper must not run in the gunicorn worker.
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        """Start a re-transcription of a stored recording. Runs HERE rather
        than in MeshTOC because that process is a single gunicorn worker
        also holding the node TCP link — a multi-second whisper run there would
        contend with the mesh. Never transmits.

        ASYNC since 2026-07-21: returns immediately with {"started": true}
        instead of holding the caller's connection for the minutes whisper +
        Ollama can take — the synchronous shape pinned one of the app's 16
        gunicorn threads for the whole run, the worst of the thread-exhaustion
        cases. Progress is polled via /status's `retranscribing` field, and the
        result lands in the sidecar exactly as before. One job at a time: the
        box serializes whisper/Ollama anyway, and a queue would just hide that.
        """
        if self.path.startswith("/conditions"):
            # Run-now for the conditions lane (UI test button). Async like
            # /transcribe; the worker itself refuses to overlap an activation
            # or a re-transcribe.
            with _cond_lock:
                if _cond["running"]:
                    return self._json({"error": "a conditions cycle is already "
                                       "running"}, 409)
            threading.Thread(target=conditions_worker, args=("manual",),
                             daemon=True).start()
            return self._json({"ok": True, "started": True,
                               "capture_secs": COND_CAPTURE_SECS})
        if not self.path.startswith("/transcribe"):
            return self._json({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, OSError):
            return self._json({"error": "bad request"}, 400)
        name = os.path.basename(str(body.get("name") or ""))
        path = os.path.join(RECORDINGS_DIR, name)
        if not name.endswith(".wav") or not os.path.isfile(path):
            return self._json({"error": "no such recording"}, 400)
        with _retx_lock:
            if _retx["name"]:
                return self._json({"error": "a re-transcription is already "
                                   "running (%s)" % _retx["name"],
                                   "retranscribing": _retx["name"]}, 409)
            _retx["name"] = name
            _retx["started"] = time.time()
        threading.Thread(target=_retx_worker, args=(name,), daemon=True).start()
        return self._json({"ok": True, "started": True, "name": name})

    def _listen(self):
        with _listeners_lock:
            if len(_listeners) >= MAX_LISTENERS:
                # Each listener also pins a gunicorn thread upstream, on the same
                # worker that holds the node TCP link. Refuse rather than creep.
                return self._json({"error": "too many listeners"}, 429)
            q = collections.deque(maxlen=LISTEN_QUEUE)
            _listeners.append(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(_wav_header())
            self.wfile.flush()
            while True:
                if q:
                    self.wfile.write(q.popleft())
                else:
                    time.sleep(0.05)
        except Exception:
            pass       # client hung up — normal
        finally:
            with _listeners_lock:
                try:
                    _listeners.remove(q)
                except ValueError:
                    pass


class _CtrlServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_ctrl():
    """Best-effort: if the port is taken we log and carry on. The receiver
    working matters; its status page does not."""
    try:
        srv = _CtrlServer((CTRL_HOST, CTRL_PORT), _Ctrl)
    except Exception as e:
        log("[ctrl] not started: %s" % e)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("[ctrl] listening on %s:%d" % (CTRL_HOST, CTRL_PORT))


def log(msg):
    print(msg, flush=True)


# ---------- SAME parse + alert send (the deterministic path) ----------

def county_names(loc_field):
    names, codes = [], []
    for loc in loc_field.split("-"):
        loc = loc.strip()
        if len(loc) != 6 or not loc.isdigit():
            continue
        sscc = loc[1:]  # drop the P (county-part) digit
        codes.append(sscc)
        names.append(AZ_FIPS.get(sscc, sscc))
    matched = (not FIPS_FILTER) or bool(FIPS_FILTER.intersection(codes))
    return names, codes, matched


def _same_issue_utc(jjjhhmm):
    """SAME JJJHHMM (day-of-year + UTC HH:MM) -> aware UTC datetime."""
    now = datetime.now(timezone.utc)
    return datetime(now.year, 1, 1, tzinfo=timezone.utc) + timedelta(
        days=int(jjjhhmm[:3]) - 1, hours=int(jjjhhmm[3:5]),
        minutes=int(jjjhhmm[5:7]))


def expiry_local(jjjhhmm, tttt):
    try:
        expiry = _same_issue_utc(jjjhhmm) + timedelta(
            hours=int(tttt[:2]), minutes=int(tttt[2:4]))
        return expiry.astimezone(LOCAL_TZ).strftime("%I:%M %p").lstrip("0")
    except Exception:
        return ""


# SAME test/admin codes never get a hazard emoji — the WAN lane never sends
# these, so there is no format to match; a drill wearing 🌪️ trains people to
# ignore 🌪️.
TEST_CODES = {"RWT", "RMT", "NPT", "DMO", "ADR"}


def severity_tag(label):
    """Emoji tag PORTED VERBATIM from wx_alerts_timer.py severity_tag() so
    both alert lanes speak one visual language (community request, shipped
    2026-08-10 — supersedes the 2026-07-21 one-emoji-per-tier rule for this
    lane, operator-approved). Input is our EVENTS label; the match set is
    the same NWS event vocabulary. Keep the two copies aligned."""
    e = (label or "").lower()
    if "dust storm" in e: return "🌫️⚡"
    if "flash flood" in e: return "🌊🚨"
    if "tornado" in e: return "🌪️⚠️"
    if "red flag" in e or "fire" in e: return "🔥"
    if "heat" in e: return "🌡️⚠️"
    # our EVENTS table spells it "SEVERE T-STORM ..." — match both renderings
    if "severe thunderstorm" in e or "severe t-storm" in e: return "⛈️⚠️"
    if "freeze" in e or "frost" in e or "winter" in e: return "❄️⚠️"
    if "warning" in e: return "🔴"
    if "watch" in e: return "👁️"
    if "advisory" in e: return "🔶"
    return "⚠️"


def issued_hhmm(jjjhhmm):
    """Header issue time as bare local HHMM — matches the WAN blocks' title
    style, so an alert heard relayed later can never read as falsely current."""
    try:
        return _same_issue_utc(jjjhhmm).astimezone(LOCAL_TZ).strftime("%H%M")
    except Exception:
        return ""


def compose(org, event, loc_field, tttt, jjjhhmm, sender):
    """WAN-matched atomic block (community request, operator-approved
    2026-08-10 — see wx_alerts_timer.py format_alert_block for the format
    this mirrors):

        <severity emoji> <Event> <issued HHMM>
        📍 <counties>
        Exp ~<time>

    One message = one packet; a partial delivery leaving a title with no
    expiry is unacceptable for a warning.

    ⚠ THE EXPIRY IS TILDE-MARKED, DELIBERATELY. The header's +TTTT purge
    duration rounds UP on a coarse grid (measured: a 49-min warning encoded
    +0100 and published 10:26 PM against NWS's actual 10:15 PM). It is the
    only expiry available at alert time, so it ships marked approximate;
    the voice DETAIL carries the product's true expiry ~60-90 s later.
    Decided with the restyle, 2026-08-10 — do not drop the tilde.

    The sender callsign was dropped in the restyle (the WAN blocks carry no
    office either; ` · OTA-SDR` already marks the lane)."""
    label = EVENTS.get(event, event)
    names, codes, matched = county_names(loc_field)
    shown = ", ".join(names[:4]) + (" +%d more" % (len(names) - 4) if len(names) > 4 else "")
    until = expiry_local(jjjhhmm, tttt)
    tag = "" if event in TEST_CODES else severity_tag(label)
    # Title Case to match the WAN blocks (CAP event strings arrive Title
    # Case; our EVENTS table is SAME-standard ALL CAPS).
    title = ("%s %s" % (tag, label.title().replace("Weather", "WX"))).strip()
    hhmm = issued_hhmm(jjjhhmm)
    if hhmm:
        title += " " + hhmm
    lines = [title]
    if shown:
        lines.append("📍 " + shown)
    if until:
        lines.append("Exp ~" + until)
    return _tagged("\n".join(lines)), matched


def _tagged(text):
    """Append OTA_TAG so it ALWAYS survives the MAX_LEN cap.

    The body is trimmed to leave room, rather than tagging first and slicing the
    whole thing — otherwise a wordy multi-county alert silently loses its
    provenance stamp, or worse ships a half-sliced 'OTA-S'.

    Since the 2026-08-10 block restyle the tag rides on its OWN line at the
    end of the block (still a suffix, never a prefix — the first token a
    scanner reads must never be provenance chrome, let alone anything
    resembling TEST)."""
    tag = "\n· " + OTA_TAG
    return text[:MAX_LEN - len(tag)] + tag


def send(text, alert, channel=None):
    # `channel` overrides only for the conditions lane (COND_CHANNEL, gated to
    # a test channel); every ALERT-path caller stays on CHANNEL by omission.
    ch = CHANNEL if channel is None else channel
    if not API_KEY:
        log("[dry-run] ch=%d alert=%s :: %s" % (ch, alert, text))
        return
    body = json.dumps({"text": text, "channel": ch, "alert": alert}).encode()
    req = urllib.request.Request(
        MESHTOC_URL + "/api/send", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            log("[sent ch%d %s] %s" % (ch, r.status, text))
    except Exception as e:
        log("[send FAILED] %s :: %s" % (e, text))


# ---------- voice enrichment lane (never blocks the alert path) ----------

def _ntok(s):
    """REAL token count, mirroring faster-whisper's own hotwords encoding
    (leading space). Only callable once `_whisper` exists — enforced by use:
    the sole caller sits after the model init inside transcribe()."""
    global _tokenizer
    if _tokenizer is None:
        from faster_whisper.tokenizer import Tokenizer
        _tokenizer = Tokenizer(_whisper.hf_tokenizer,
                               _whisper.model.is_multilingual,
                               task="transcribe", language="en")
    return len(_tokenizer.encode(" " + s.strip()))


def transcribe(wav_path, same_codes=None):
    """Transcribe a stored 22050 Hz WAV. whisper wants 16 kHz, so sox downsamples
    into scratch space that is cleaned up here — the source recording is left
    alone, because it's the artifact worth keeping.

    `same_codes` (6-digit SAME/FIPS, P-digit zeroed) scopes dynamic hotwords
    to the warned counties — see build_hotwords. Any failure there degrades to
    no hotwords, never to a failed transcription.

    SERIALIZED. Two transcriptions can now be in flight at once (a live
    activation's voice_worker and an operator-triggered re-transcribe), and they
    would otherwise race on the scratch file: one's sox would overwrite the
    other's input, and one's cleanup would delete the file the other is reading.
    The lock also covers the lazy `_whisper` init, which two threads could
    otherwise both run. Serializing costs nothing real — int8 CPU inference on a
    CPU-quota-fenced container is going to be serial regardless, and neither
    caller is on the decode path.
    """
    global _whisper
    with _whisper_lock:
        subprocess.run(["sox", wav_path, "-r", "16000", "-c", "1", VOICE_TMP],
                       check=True, capture_output=True)
        try:
            if _whisper is None:
                from faster_whisper import WhisperModel
                _whisper = WhisperModel(WHISPER_MODEL, device="cpu",
                                        compute_type="int8")
            kw = dict(language="en", initial_prompt=WHISPER_PROMPT,
                      vad_filter=True)
            if same_codes:
                try:
                    hw = build_hotwords(same_codes, _gazetteer(), _ntok)
                    if hw:
                        kw["hotwords"] = hw
                        log("[voice] hotwords %d tok" % _ntok(hw))
                except Exception as e:
                    log("[voice] hotwords skipped: %s" % e)
            segments, _info = _whisper.transcribe(VOICE_TMP, **kw)
            return " ".join(s.text.strip() for s in segments).strip()
        finally:
            try:
                os.remove(VOICE_TMP)
            except OSError:
                pass


def _sidecar(wav_path, transcript, summary, summary_raw=None, same_codes=None):
    """Cache the lane's output beside the recording so the UI can show what was
    said without paying for a re-run every time the page loads.

    DECIDED 2026-07-21: `transcript` is the RAW whisper output, not the
    corrected one — the raw is the evidence of what was actually received;
    summarize() is fed the corrected copy. `summary_raw` is stored only when
    the guards changed the model's output, so guard hit-rate is measurable
    from the field record."""
    try:
        obj = {"transcript": transcript, "summary": summary,
               "model": WHISPER_MODEL, "summarizer": OLLAMA_MODEL,
               "at": time.time()}
        if summary_raw is not None and summary_raw != summary:
            obj["summary_raw"] = summary_raw
        if same_codes:
            # The header's warned counties — lets retranscribe() rebuild the
            # SAME hotwords the live lane used, and the bench correlate a
            # recording with its scope without re-matching NWS products.
            obj["same"] = list(same_codes)
        with open(wav_path + ".json", "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
    except OSError as e:
        log("[voice] could not write sidecar: %s" % e)


def summarize(transcript):
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": SUMMARY_PROMPT + transcript,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 120},
    }).encode()
    req = urllib.request.Request(OLLAMA_URL + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            out = json.loads(r.read()).get("response", "").strip()
        return out.replace("\n", " ").strip('"') or None
    except Exception as e:
        log("[voice] ollama failed: %s" % e)
        return None


def voice_worker(wav_path, label, same_codes=None):
    """The live lane: transcribe, summarize, send the follow-up to the mesh.

    NOTE the recording is NOT deleted here any more. It's the only record of what
    was actually broadcast, and the only input available for retuning this lane
    when a summary comes out wrong. `prune_recordings()` bounds the directory
    instead.
    """
    try:
        text = transcribe(wav_path, same_codes)
        if len(text) < 20:
            log("[voice] transcript too short, skipping (%r)" % text[:40])
            _sidecar(wav_path, text, None, same_codes=same_codes)
            return
        log("[voice] transcript %d chars" % len(text))
        corrected = apply_corrections(text)   # G1 upstream: the model gets clean names
        raw_summary = summarize(corrected)
        if raw_summary:
            summary = guard_summary(raw_summary, corrected)
        else:
            # ollama down — corrected excerpt beats nothing (and beats raw:
            # this line goes OTA, so the name fixes should apply to it too).
            # 130 leaves room for the block title + tag line under MAX_LEN.
            raw_summary = None
            summary = corrected[:130] + "..."
        _sidecar(wav_path, text, summary, summary_raw=raw_summary,
                 same_codes=same_codes)
        # WAN-matched detail block (2026-08-10 restyle): severity emoji +
        # compact event + "Detail" as the title line, summary beneath.
        lbl = EVENTS.get(label, label)
        short = lbl.title().replace("Weather", "WX")
        for sfx in (" Warning", " Watch", " Statement", " Emergency"):
            if short.endswith(sfx):
                short = short[:-len(sfx)]
                break
        tag = "" if label in TEST_CODES else severity_tag(lbl)
        title = ("%s %s Detail" % (tag, short)).strip()
        if label in TEST_CODES:
            # Same OTA silence as the alert path — the sidecar above already
            # preserved the transcript for the WXSDR page.
            log("[voice] test code %s — DETAIL recorded, not sent OTA" % label)
        else:
            send(_tagged(title + "\n" + summary), False)
    except Exception as e:
        log("[voice] lane failed (alert already sent): %s" % e)


def retranscribe(name):
    """Re-run the lane against a stored recording, WITHOUT transmitting.

    This is a diagnostic for retuning whisper/Ollama against real activation
    audio, so it must never reach the mesh — re-broadcasting an old alert as if
    it were current is exactly the kind of thing a weather system must not do.
    Runs in the DAEMON, not in MeshTOC's gunicorn worker, which holds the
    node TCP link and must not host multi-second transcription.
    """
    path = os.path.join(RECORDINGS_DIR, os.path.basename(name))
    if not os.path.isfile(path) or not path.endswith(".wav"):
        return {"error": "no such recording"}
    try:
        # Recover the activation's SAME codes from the sidecar so the re-run
        # primes exactly as the live lane did. Pre-v0.13.130 sidecars have no
        # "same" key -> no hotwords, which matches how THEY were transcribed.
        codes = []
        try:
            with open(path + ".json", encoding="utf-8") as fh:
                codes = json.load(fh).get("same") or []
        except Exception:
            pass
        text = transcribe(path, codes)
        # Same guard wiring as voice_worker — the live lane and the diagnostic
        # lane MUST NOT diverge, or a re-run "verifies" a pipeline that isn't
        # the one broadcasting.
        raw_summary = None
        summary = None
        if len(text) >= 20:
            corrected = apply_corrections(text)
            raw_summary = summarize(corrected)
            if raw_summary:
                summary = guard_summary(raw_summary, corrected)
        _sidecar(path, text, summary, summary_raw=raw_summary, same_codes=codes)
        log("[voice] re-transcribed %s (not sent)" % os.path.basename(path))
        return {"ok": True, "transcript": text, "summary": summary,
                "sent": False}
    except Exception as e:
        return {"error": str(e)}




def rec_name(label):
    """`20260722-101500_RWT.wav` — sorts chronologically as a plain string, and
    says what it is without opening it. Local time, matching the journal."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9]", "", str(label or "UNK"))[:8] or "UNK"
    return "%s_%s.wav" % (ts, safe)


def rec_start(label, same_codes=None):
    with _rec_lock:
        if _rec["active"]:
            return
        try:
            os.makedirs(RECORDINGS_DIR, exist_ok=True)
            path = os.path.join(RECORDINGS_DIR, rec_name(label))
            fh = open(path, "wb")
            # Placeholder header with a zero data length; rec_finish patches the
            # two size fields once we know how long the activation actually ran.
            # Writing the header up front means the capture file IS the playable
            # WAV — no post-hoc copy of a multi-megabyte raw file.
            fh.write(_wav_header(0))
        except OSError as e:
            log("[voice] cannot open capture file: %s" % e)
            return
        _rec["file"] = fh
        _rec["path"] = path
        _rec["active"] = True
        _rec["deadline"] = time.time() + VOICE_MAX_SECS
        _rec["label"] = label
        _rec["codes"] = list(same_codes or [])
        log("[voice] recording %s (max %ds)" % (os.path.basename(path), VOICE_MAX_SECS))


def rec_finish(reason):
    with _rec_lock:
        if not _rec["active"]:
            return
        _rec["active"] = False
        fh, path, label = _rec["file"], _rec["path"], _rec["label"]
        codes = _rec["codes"]
        _rec["file"] = None
        data_len = 0        # bound before the try: the except path reads it below
        try:
            # Patch RIFF size (offset 4) and data size (offset 40) now that the
            # length is known. Without this the file plays but reports a bogus
            # duration and won't seek.
            data_len = max(0, fh.tell() - 44)
            fh.seek(4)
            fh.write(struct.pack("<I", data_len + 36))
            fh.seek(40)
            fh.write(struct.pack("<I", data_len))
        except OSError as e:
            log("[voice] could not finalize wav header: %s" % e)
        finally:
            fh.close()
    secs = data_len / float(SAMPLE_RATE * 2) if data_len else 0
    log("[voice] recording done (%s) — %s, %.0fs" %
        (reason, os.path.basename(path), secs))
    prune_recordings()
    threading.Thread(target=voice_worker, args=(path, label, codes),
                     daemon=True).start()


def prune_recordings():
    """Oldest-first eviction to keep the directory bounded. Best-effort: a prune
    failure must never take down the receiver."""
    try:
        files = []
        for n in os.listdir(RECORDINGS_DIR):
            if not n.endswith(".wav"):
                continue
            p = os.path.join(RECORDINGS_DIR, n)
            try:
                files.append((os.path.getmtime(p), os.path.getsize(p), p))
            except OSError:
                pass
        files.sort()                       # oldest first
        total = sum(f[1] for f in files)
        while files and (len(files) > REC_MAX_FILES or total > REC_MAX_BYTES):
            _mt, size, p = files.pop(0)
            for victim in (p, p + ".json"):
                try:
                    os.remove(victim)
                except OSError:
                    pass
            total -= size
            log("[voice] pruned %s" % os.path.basename(p))
    except Exception as e:
        log("[voice] prune failed: %s" % e)


# ---------- decoder-line handling ----------

def handle_line(line):
    m = HEADER_RE.search(line)
    if not m:
        if "NNNN" in line:
            log("[eom]")
            rec_finish("eom")
        elif "ZCZC" in line or line.strip().startswith("EAS:"):
            # A decoder line that looks like SAME but didn't parse. Almost
            # always a header mangled by marginal SNR — the failure mode that
            # loses a whole activation while its (much shorter) EOM still
            # decodes. Logged raw so a miss leaves evidence instead of silence.
            log("[undecoded] %s" % line.strip()[:200])
        return
    header = m.group(0)
    now = time.time()
    for k in [k for k, t in _seen.items() if now - t > DEDUP_SECS]:
        del _seen[k]
    if header in _seen:
        return
    _seen[header] = now
    org, event, locs, tttt, jjjhhmm, sender = m.groups()
    text, matched = compose(org, event, locs, tttt, jjjhhmm, sender)
    if not matched:
        log("[skip — outside FIPS filter] %s" % text)
        return
    # 2026-07-21 (operator): bells removed entirely — the community weather
    # channel must never ring devices. ALERT_CODES/WATCH_CODES drive only the
    # tiered label prefix in compose() (🔴/🟡/plain), never delivery.
    # 2026-08-10 (operator): test/admin codes are NOT broadcast at all — their
    # event names literally contain "Test", which trips keyword signal-report
    # responders mesh-wide (the OTA-SDR-Check incident). The lane still
    # records/transcribes them below, so the WXSDR page keeps the weekly RWT
    # as local proof-of-life; it just never goes over the air.
    if event in TEST_CODES:
        log("[test code %s — recorded, not sent OTA] %s" % (event, text.replace("\n", " / ")))
    else:
        send(text, False)
    if VOICE:
        # 6-digit SAME codes with the county-part P digit zeroed — the
        # gazetteer is keyed on whole-county codes ("004013"), and a header
        # warning a PORTION of a county (P=1..9) still wants that county's
        # vocabulary.
        _, codes, _ = county_names(locs)
        rec_start(event, ["0" + c for c in codes])


# ---------- main loops ----------

def warm_whisper():
    """Eager-load the whisper model at startup so the FIRST activation after
    a (re)start doesn't pay the cold load inside its EOM->DETAIL time — the
    SMR pool makes cold reads slow, and at `small.en` (~500 MB int8) that is
    tens of seconds on a cold page cache. Same reasoning as ollama's
    KEEP_ALIVE=-1. Runs in a daemon thread: the SAME decode path must come
    up instantly and never wait on this; transcribe() serialises on
    _whisper_lock either way, so an activation racing the warm-up simply
    waits for whichever finishes first. Any failure leaves the original
    lazy-load path intact."""
    global _whisper
    try:
        t0 = time.time()
        with _whisper_lock:
            if _whisper is None:
                from faster_whisper import WhisperModel
                _whisper = WhisperModel(WHISPER_MODEL, device="cpu",
                                        compute_type="int8")
        log("[voice] whisper %s warm in %.1fs" % (WHISPER_MODEL, time.time() - t0))
    except Exception as e:
        log("[voice] warm-load failed (lazy load remains): %s" % e)


def decoder_reader(pipe):
    for raw in pipe:
        try:
            handle_line(raw.decode("utf-8", "replace"))
        except Exception as e:
            log("[decoder] handler error: %s" % e)


def device_index(serial):
    # Other CTs' dongles on the same host show up in our sysfs as phantom
    # devices with unreadable strings; they shift indexes and break both
    # rtl_fm's default device 0 and librtlsdr's own -d <serial> matching.
    # Resolve the index ourselves and skip anything whose strings won't read.
    lib = ctypes.CDLL("librtlsdr.so.0")
    m, p, s = (ctypes.create_string_buffer(256) for _ in range(3))
    for i in range(lib.rtlsdr_get_device_count()):
        if (lib.rtlsdr_get_device_usb_strings(i, m, p, s) == 0
                and s.value.decode(errors="replace") == serial):
            return i
    return None


def run_radio():
    log("[same_bridge v2] up — freq=%s ch=%d filter=%s dry_run=%s voice=%s" %
        (FREQ, CHANNEL, sorted(FIPS_FILTER) or "off", not API_KEY, VOICE))
    idx = device_index(SERIAL)
    if idx is None:
        log("[radio] dongle serial %s not found — exiting for systemd restart" % SERIAL)
        sys.exit(1)
    log("[radio] dongle %s at index %d" % (SERIAL, idx))
    start_ctrl()
    if VOICE:
        threading.Thread(target=warm_whisper, daemon=True).start()
    if COND_EVERY_H > 0:
        threading.Thread(target=conditions_timer, daemon=True).start()
        log("[voice] conditions lane: every %dh -> ch%d" % (COND_EVERY_H, COND_CHANNEL))
    argv = rtl_argv(idx)
    _ctrl_state["argv"] = argv
    ts = tuning_state()
    log("[radio] %s" % " ".join(argv))
    if not ts["is_gold"]:
        # Say so loudly in the journal: a tuned receiver that later misbehaves
        # must not look like the known-good one in a post-mortem.
        log("[radio] NON-GOLD tuning: %s" % json.dumps(ts["deviations"]))
    p_rtl = subprocess.Popen(argv, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL)
    dec_argv = decoder_argv()
    log("[radio] decoder: %s" % " ".join(dec_argv))
    p_mm = subprocess.Popen(
        dec_argv,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    threading.Thread(target=decoder_reader, args=(p_mm.stdout,), daemon=True).start()
    try:
        while True:
            chunk = p_rtl.stdout.read(4096)
            if not chunk:
                log("[radio] rtl_fm stream ended — exiting for systemd restart")
                break
            p_mm.stdin.write(chunk)
            _tap(chunk)          # meter + listeners; never blocks (see _tap)
            if _rec["active"]:
                with _rec_lock:
                    if _rec["active"]:
                        _rec["file"].write(chunk)
                        expired = time.time() > _rec["deadline"]
                if expired:
                    rec_finish("max length")
    finally:
        for p in (p_rtl, p_mm):
            try:
                p.kill()
            except Exception:
                pass
    sys.exit(1)


def run_stdin():
    log("[same_bridge v2] stdin test mode — ch=%d dry_run=%s (voice lane off)" %
        (CHANNEL, not API_KEY))
    global VOICE
    VOICE = False
    for line in sys.stdin:
        handle_line(line)


if __name__ == "__main__":
    if "--stdin" in sys.argv:
        run_stdin()
    else:
        run_radio()
