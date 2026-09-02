#!/usr/bin/env python3
"""Print the raw transcript each arm produces for ONE recording. Diagnosis, not
scoring — the A/B said hotwords wrecked the decode; read WHAT it produced
before explaining WHY."""
import sys
sys.path.insert(0, "/opt/wx-sdr/lab")
import wx_arms
import wx_wer
import json

name = sys.argv[1] if len(sys.argv) > 1 else "20260810-040618_SVR"
sb = wx_arms.load_bridge()
gaz = json.load(open(wx_arms.GAZ, encoding="utf-8"))
products = json.load(open(wx_wer.CACHE, encoding="utf-8"))
prod, note = wx_wer.match_product(name, products)
code = name.split("_")[-1]
hw = wx_arms.build_hotwords(prod.get("same") or [], wx_wer.EVENT_PHRASE.get(code, ""), gaz)

wav = wx_arms.REC + "/" + name + ".wav"
print("### hotwords string (%d chars) ###" % len(hw))
print(hw)
print()
for arm, h in (("BASE", ""), ("HOT", hw)):
    tr = wx_arms.transcribe_arm(sb, wav, h)
    print("### %s transcript (%d chars) ###" % (arm, len(tr)))
    print(tr[:1200])
    print()
