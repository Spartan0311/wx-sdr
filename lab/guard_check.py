#!/usr/bin/env python3
"""Verify the G2 number-allowlist guard against REAL field cases.

Exec-loads the guards from a same_bridge.py given on argv (the wx_ab.py Arm B
pattern) so this tests the SHIPPED code, never a local copy that could drift.

The point is NOT just "does the 04:29 bug go away". A guard that stops
stripping fabrications is a worse bug than the one being fixed, so the
fabrication cases below are the ones that actually gate the change.
"""
import json
import sys
import types

PATH = sys.argv[1] if len(sys.argv) > 1 else "/opt/wx-sdr/same_bridge.py"
REC = "/opt/wx-sdr/recordings"

mod = types.ModuleType("sb")
mod.__dict__["__name__"] = "sb"          # keep the __main__ block from firing
with open(PATH, encoding="utf-8") as fh:
    exec(compile(fh.read(), PATH, "exec"), mod.__dict__)

guard = mod.guard_summary
unsupported = mod.unsupported_numbers


def load(name):
    with open("%s/%s.wav.json" % (REC, name), encoding="utf-8") as fh:
        d = json.load(fh)
    return d["transcript"], d.get("summary_raw") or d["summary"]


FAILED = []


def check(label, transcript, summary, must_keep=None, must_drop=None,
          must_not_contain=None):
    out = guard(summary, transcript)
    bad = unsupported(summary, transcript)
    ok = True
    notes = []
    if must_keep is not None:
        if must_keep in out:
            notes.append("keeps %r" % must_keep)
        else:
            ok = False
            notes.append("LOST %r" % must_keep)
    if must_drop is not None:
        if must_drop in out:
            ok = False
            notes.append("STILL HAS %r" % must_drop)
        else:
            notes.append("drops %r" % must_drop)
    if must_not_contain is not None:
        if must_not_contain in out:
            ok = False
            notes.append("STRANDED %r" % must_not_contain)
        else:
            notes.append("no stranded %r" % must_not_contain)
    print("%-4s %s" % ("PASS" if ok else "FAIL", label))
    print("       flagged : %s" % (bad or "none"))
    print("       result  : %s" % out)
    print("       %s" % "; ".join(notes))
    print()
    if not ok:
        FAILED.append(label)


# --- A: the live regression. Whisper wrote "10-15 pm"; NWS confirms 10:15 PM.
t, s = load("20260810-042650_SVR")
check("A  hyphen-rendered expiry is SUPPORTED (the 04:29 bug)",
      t, s, must_keep="10:15", must_not_contain="County MST")

# --- B: the control the storm ran for us. Period form already worked; it must
#        keep working, or the fix traded one rendering for another.
t2, s2 = load("20260810-043445_SVR")
check("B  period-rendered expiry still supported (no regression)",
      t2, s2, must_keep="10:15")

# --- C..E: fabrications. These are the cases that gate the change. If any of
#        these now survive, the guard has been weakened and the fix is wrong.
check("C  fabricated time IS still stripped",
      t, "Severe thunderstorm warning for Maricopa County until 11:47 pm MST. "
         "Expect 60 mph wind.",
      must_drop="11:47", must_not_contain="MST")

check("D  fabricated bare number IS still stripped",
      t, "Severe thunderstorm warning for Maricopa County. Hail up to 7 inches.",
      must_drop="7")

check("E  fabricated temperature IS still stripped",
      "Highs near one hundred five degrees expected across the lower deserts.",
      "Extreme heat warning for Central Phoenix. Highs 111 F.",
      must_drop="111")

# --- F: a real number that IS in the transcript must never be touched.
check("F  supported number survives untouched",
      t, "Severe thunderstorm warning, 60 mph wind gusts and quarter-size hail.",
      must_keep="60")

print("=" * 62)
if FAILED:
    print("FAILED: %s" % ", ".join(FAILED))
    sys.exit(1)
print("all guard checks pass")
