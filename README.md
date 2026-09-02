# WXSDR — NOAA Weather Radio SAME → Meshtastic

**Severe-weather alerting for the mesh that needs no internet at any step.**

An RTL-SDR listens to NOAA Weather Radio 24/7, decodes the digital SAME
headers that precede every alert, and pushes a formatted warning onto the
mesh — typically within a second of the tones. A second, optional lane
records the voice message that follows, transcribes it, and sends a plain
language summary as a follow-up.

This complements the internet-based weather scripts rather than replacing
them. API-driven alerting is richer on a normal day. This path still works
when the internet is down, which is exactly when warning-to-mesh matters
most.

Written up after 48 days of continuous production operation. All numbers
below are measured, not estimated.


> **Status — read this first.** This is a one-off snapshot of a system that
> runs in production for one operator. It is **not a maintained project**:
> Issues are closed, there is no support, and no further releases are planned.
> Fork it and make it yours — that is what the MIT licence is here for.
>
> **This is not a substitute for a NOAA weather radio.** It is a hobby
> receiver that can and will miss alerts; §3 and §7 state the measured
> limitations plainly. Keep a real NWR receiver for life-safety alerting.

---

## 1. How it works

```
NWR 162.550 MHz  (one of 7 NWR channels; KEC94 here)
  │
  │  RTL-SDR Blog V4
  ▼
rtl_fm ──── NBFM demod, 22050 Hz ────► samedec  (burst-voting SAME decoder)
  │                                       │
  │                                       ▼
  │                          SAME header decoded (ZCZC-ORG-EEE-PSSCCC…)
  │                                       │
  │                                       ├──►  ALERT SENT TO MESH
  │                                       │     (deterministic, immediate)
  │                                       │
  └────────► voice lane (optional) ───────┘
                │
                ├─ record audio until the NNNN end-of-message
                ├─ transcribe  (faster-whisper, county-scoped hotwords)
                ├─ summarise   (local LLM, strict prompt)
                └──►  DETAIL SENT TO MESH  (follow-up, ~3 min later)
```

### The one design rule that matters

**The alert fires off the SAME header decode, before the voice lane even
starts.**

Everything a warning actually needs — event type, affected counties,
expiry time — is carried in the digital header as structured data. None of
it comes from speech recognition. The transcript is *supplementary
context only*.

This means a transcription failure, a hallucinating summariser, or a dead
LLM can never delay, alter, or suppress a warning. The voice lane runs in a
worker thread; any exception in it is logged and swallowed. If the
summariser is unavailable, the follow-up degrades to a raw transcript
excerpt.

Build it in this order, and the failure modes stay boring.

---

## 2. Does it actually work? — 48 days of production

Continuous operation, 2026-07-16 through 2026-09-02.

### Activations relayed

| Event | Count |
|---|---:|
| Severe Thunderstorm Warning | 29 |
| Flash Flood Warning | 7 |
| Dust Storm Warning | 5 |
| Severe Thunderstorm Watch | 2 |
| Required Weekly Test | 2 |
| Flood Warning | 1 |
| **Total** | **46** |

### The night that proved it

**2026-08-30 evening: 16 activations in 3 hours 25 minutes.** A real
severe-weather event with overlapping warnings — severe thunderstorm and
flash flood warnings interleaving, counties changing between activations.
All 16 relayed correctly, each with its voice follow-up.

Sustained burst load is the case worth testing, and it is the one most
demos never see.

### Latency

The alert itself is effectively instantaneous — it is emitted on the header
decode, in the same log-second the tones end. The measured figure below is
the *voice detail follow-up*, which is the slow path by design.

Measured across the 17 activations held in the detailed message log (a
shorter retention window than the 48-day service journal above):

| | alert → detail |
|---|---|
| Mean | 3 min 00 s |
| Median | 2 min 52 s |
| Fastest | 0 min 59 s |
| Slowest | 4 min 36 s |
| **Pairing** | **17 of 17 — every alert got its detail** |

Of 76 voice recordings captured, **every one terminated on the SAME
end-of-message marker**. Not one hit the 150-second safety cap, which says
the end-of-message detection is reliable rather than the cap quietly doing
the work.

### Reliability

- **2 send failures in 48 days** — both on the same day, both caused by the
  receiving application restarting (HTTP 504, connection refused), not by
  the receiver. The daemon logged both and carried on.
- **Zero crashes.** The service has never exited unexpectedly.
- **RF health, 53 automated daily sweeps: 51 `good`, 2 `poor` (96.2%).**
  The normal reading is a ~19.5 dB signal-over-noise-floor margin. The two
  poor days showed a ~13 dB signal drop against an unchanged noise floor —
  so the signal moved, not the receiver. Both recovered without
  intervention.

Those two bad days are left in deliberately. A 100% number would tell you
less.

### What it costs to run

Measured over a 14-hour window on a 4-core container:

| | |
|---|---|
| CPU | ~22 minutes over 14 hours — **about 2.6% of one core** |
| Memory | 842 MB resident, 1.2 GB peak |
| Dominant consumer | the SAME decoder, ~10% of one core |

The memory figure is almost entirely the speech model held resident. The
core alerting path is close to free — see the two build tiers below.

---

## 3. Quality, honestly

### A real activation, end to end

Severe Thunderstorm Warning, 2026-09-01. What the radio said (transcribed):

> The National Weather Service in Phoenix has issued a severe thunderstorm
> warning for East Central Yuma County in southwestern Arizona, Maricopa
> County in south central Arizona, until 645 p.m. Mountain Standard Time.
> At 618 p.m. Mountain Standard Time, a severe thunderstorm was located 14
> miles north of Paloma, or 42 miles southwest of Tonopah, moving northeast
> at 20 miles per hour. Hazard, 60 miles per hour when gusts and
> quarter-size hail. […] Locations impacted include sun-dot and hider.

What went out on the mesh:

> **Severe thunderstorm warning for Maricopa and East Central Yuma Counties
> until 6:45 p.m. MST. Hail up to quarter-size, winds 60 mph. Flash flooding
> possible.**

The summary is correct, actionable, and fits the wire.

### Where it is weak

Look again at that transcript: **"sun-dot and hider"** is the speech model
failing on *Sundad* and *Hyder* — two small Arizona localities. This is the
known weak spot, and it is consistent:

| Heard as | Actually |
|---|---|
| sun-dot / hider | Sundad / Hyder |
| Hossainpah | Hassayampa |
| Australia Sailport | Estrella Sailport |
| Whitman | Wittmann |

Rural place names, spoken by a synthesised voice, over a noisy NBFM
channel. Priming the model with a county-scoped vocabulary list helps
measurably — *Ak-Chin Village* now decodes correctly, and used to not — but
this is not a solved problem.

**Why it is tolerable:** none of these appear in the alert. The alert's
counties come from FIPS codes in the digital header. A mangled place name
degrades a supplementary detail message; it cannot produce a wrong warning.
That is the whole argument for the architecture in §1.

Two cosmetic defects also seen in production: one summary rendered a time
as `1245 a.m.` without the colon (intermittent — others render correctly),
and one was truncated mid-phrase by the length cap rather than at a
sentence boundary.

---

## 4. Build one — Tier 1: the core

**This is the life-safety path.** SAME header → mesh alert. Deterministic,
immediate, and it runs on very little. If you build only this, you have the
part that matters.

### Hardware

- **An RTL-SDR.** An RTL-SDR Blog V4 is what this was built on. Cheap
  generic dongles work but vary.
- **An antenna for 162 MHz.** NWR transmitters are high-power and
  wide-coverage; a modest antenna usually suffices. Check your local
  transmitter and frequency at `weather.gov/nwr`.
- **A Linux host.** Pi-class hardware is sufficient for this tier — there
  is no LLM and no speech model in the core path.
- **A Meshtastic node** reachable from that host.

### The driver trap — read this before plugging anything in

This will cost you an evening if you skip it:

1. **Blacklist the DVB-T kernel driver.** Linux ships `dvb_usb_rtl28xxu`,
   which claims RTL-SDR dongles on sight and hands you a TV tuner. Drop a
   file in `/etc/modprobe.d/` blacklisting it.
2. **If you are using a Blog V4, do not install your distro's `rtl-sdr`
   package.** Older `librtlsdr` lacks proper V4 support and fails in
   confusing ways. Purge it and build the RTL-SDR Blog fork from source.
   Installing both is the worst outcome — you get whichever the linker
   finds.
3. **Bind to the dongle's EEPROM serial, not its index.** USB enumeration
   order is not stable across reboots, and if you have more than one SDR
   you will eventually be listening to the wrong one. Set a distinctive
   serial and select on it.

### The signal chain

```
rtl_fm -d <device> -f 162.550M -M fm -s 22050 -g 30 -  |  samedec --rate 22050 --file -
```

`samedec` is a burst-voting SAME decoder — SAME headers are transmitted
three times, and voting across the bursts is materially more robust than
accepting the first clean parse. `multimon-ng` also decodes SAME and is a
workable substitute.

Tuning notes, learned by measurement:

- **Gain 30, 40 and 49 dB were statistically indistinguishable** in an
  interleaved A/B. Only 20 dB was clearly worse. Do not agonise over gain.
- **Do not enable de-emphasis (`-E deemp`).** 75 µs de-emphasis corners at
  ~2.1 kHz, right on SAME's SPACE tone (2083.3 Hz), while barely touching
  MARK (1562.5 Hz). It makes the audio nicer for humans and skews the exact
  tone balance the decoder depends on. If you want pleasant audio, filter a
  separate listener tap and leave the decoder's stream raw.
- **Pin a known-good baseline in code** and have every tuning knob fall
  back to it, so an empty or missing config file reproduces a receiver you
  have proven. Log loudly when running non-baseline. Tuning experiments
  that cannot be reverted are how receivers quietly rot.

### Parsing and sending

A SAME header looks like:

```
ZCZC-WXR-SVR-004013+0045-2440118-KEC94/NWS-
       │   │   │      │     │
       │   │   │      │     └─ issue time (day-of-year + UTC HHMM)
       │   │   │      └─────── purge duration (HHMM)
       │   │   └────────────── affected areas (SSCCC FIPS, repeatable)
       │   └────────────────── event code
       └────────────────────── originator
```

Things worth knowing:

- **Dedupe the 3× burst.** You will receive each header three times. A
  10-minute dedupe window on the header content works well.
- **Carry the complete event-code table**, verified against
  `weather.gov/nwr/eventcodes`. Partial tables silently drop event types
  you have never seen — which are, by definition, the unusual ones.
- **Filter by FIPS after parsing, not before.** Log what you dropped. When
  someone asks "why didn't I get that alert", the log is the answer.
- **Compute expiry from issue time + purge duration,** and see the timezone
  warning in §6.

### The mesh sink is one function

The entire outbound path is a single function. Here it is, essentially in
full:

```python
def send(text, alert, channel=None):
    ch = CHANNEL if channel is None else channel
    body = json.dumps({"text": text, "channel": ch, "alert": alert}).encode()
    req = urllib.request.Request(
        BASE_URL + "/api/send", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            log("[sent ch%d %s] %s" % (ch, r.status, text))
    except Exception as e:
        log("[send FAILED] %s :: %s" % (e, text))
```

That version posts to a self-hosted web client's API. **Replace this one
function and the rest of the system is unchanged.** Using the stock
Meshtastic Python library instead:

```python
import meshtastic.tcp_interface

def send(text, alert, channel=None):
    ch = CHANNEL if channel is None else channel
    try:
        iface = meshtastic.tcp_interface.TCPInterface(hostname=NODE_HOST)
        iface.sendText(text, channelIndex=ch)
        iface.close()
        log("[sent ch%d] %s" % (ch, text))
    except Exception as e:
        log("[send FAILED] %s :: %s" % (e, text))
```

Serial, BLE and MQTT sinks are the same shape. Keep the try/except: a send
failure must never take down the receiver.

### Message discipline

- **Respect the wire limit.** Meshtastic's hard cap is 200 bytes. Truncate
  deliberately, and truncate at a boundary you chose rather than wherever
  the byte count lands.
- **Put provenance at the END, never the start.** A short suffix like
  `· OTA-SDR` marks where a message came from. As a *prefix* it is actively
  harmful — the first tokens of a message are what keyword responders and
  bots scan, and an early word like "Check" or "Test" will trip them. This
  was learned the hard way.
- **Do not ring devices for routine traffic.** Weather alerts on a shared
  community channel that trigger notification bells get the channel muted,
  and then nobody sees the real one. Severity is carried as a text label
  prefix instead. Reserve any alerting bell for a genuinely narrow set of
  events, if at all.
- **Suppress decoration on weekly tests.** The Required Weekly Test fires
  every week forever. Make it visually boring.

---

## 5. Build one — Tier 2: the voice lane

Optional. Adds a plain-language follow-up roughly three minutes after the
alert. **Costs real hardware** — this is where the 842 MB and the ~6 GB of
model weights come from.

### Recording

Start recording when the header decodes; stop on the SAME end-of-message
marker (`NNNN`), with a hard cap (150 s here) as a backstop. In 48 days the
cap never fired — end-of-message detection is dependable — but a runaway
recording with no cap will eventually fill a disk.

### Transcription

`faster-whisper`, model `small.en`, int8. Load it **eagerly at startup**
(~9 s) rather than lazily on first use — otherwise your first alert of the
day pays the model load in addition to the transcription, exactly when you
least want the delay.

**Prime the model with a scoped vocabulary.** Build a gazetteer mapping
each SAME county code to the place names inside that county, and pass only
the relevant counties' names as hotwords for that activation. Scoped
priming beats a single large static prompt: it is a fixed token budget
spent on locations the message can plausibly mention.

Measure the token count with the model's own tokeniser. This vocabulary —
proper nouns and route numbers — runs about 3.5 characters per token, not
the ~4 a generic estimate assumes, and an over-budget hotword list fails at
runtime.

### Summarisation

A local LLM (`qwen2.5:7b` here, via Ollama on loopback). The prompt is
load-bearing and needs to be strict:

- **Explicitly require English only.** Without it, multilingual models will
  occasionally emit other scripts.
- **Explicitly forbid inventing times, places or numbers.** Without it,
  models will helpfully fabricate a plausible expiry time.
- **Give it a hard length budget** matching your wire limit.

A 3B model was tried first and was not adequate — it mangled place names
and dropped safety instructions. If you cannot host 7B, **skip the
summariser and send a raw transcript excerpt.** That path already exists as
the fallback for when the LLM is down, it needs no LLM at all, and it is
far better than a confident wrong summary.

### Never re-transmit an old recording

If you build a "re-transcribe this stored recording" tool — useful for
tuning — make certain it cannot transmit. Re-broadcasting a stale alert as
though it were current is the single worst thing a weather system can do.

---

## 6. Gotchas we paid for

Ranked by how much they cost.

**Declare your timezone. Never inherit it.** A bare `.astimezone()`
resolves against the host's zone. If your host runs UTC and you hardcode a
fixed offset, you can be right by accident for years — until someone in a
DST-observing zone runs your code and every expiry time is an hour wrong
for half the year. Alert times are the payload. Resolve an explicit zone
(`ZoneInfo`), honour a `TZ` override, and never hardcode an offset.

**A retention rule runs against data that predates it, on its first pass,
at deploy time.** A 3-day recording-retention setting shipped and
immediately deleted the archived activation recordings used as regression
fixtures — seconds after deploy. Before shipping any cleanup rule, list
what already qualifies and disposition it. "The cleanup ran" and "the
cleanup was safe" are different claims. Keep fixtures somewhere no prune
path can reach.

**Beware startup transients in any loud/quiet metric.** `rtl_fm` emits a
burst on startup that poisoned a signal-quality window for ~30 seconds —
and it *scaled with gain*, which briefly made high gain settings look
better than they were. Discard the first seconds of any such measurement.

**Measure the thing you are actually changing.** An RF power sweep cannot
see audio-chain filter settings, because it never runs the audio chain.
Two different knobs, two different measurement tools. Confusing them
produces confident nonsense.

**Verify the deployed copy matches your source.** A daemon running an older
build can accept a config value its code does not understand. In one case
an "auto gain" setting reached an older daemon that passed the string to
`atof()`, silently yielding **gain 0.0** — a receiver at zero gain, with a
healthy service, a filling log, and no alerts, ever. Checksum both copies.

**Recognise the shapes of silent failure.** This system's failure modes are
mostly quiet: a healthy service that receives nothing, a decoder listening
on the wrong dongle, a receiver at zero gain. Build a heartbeat that proves
reception rather than proves the process is running. A daily automated RF
sweep with a recorded verdict is cheap and catches all of the above.

---

## 7. Status and caveats

**What this is:** a production system, running continuously since
2026-07-16, that has relayed 46 real SAME activations including a 16-alert
severe-weather night, with no crashes and two upstream-caused send
failures.

**What it is not:** a packaged, installable product. It is a single Python
orchestrator (~1,700 lines) plus a shell runner, built for one operator's
stack. The description above is complete enough to rebuild from, but there
is no installer.

**Known limitations, stated plainly:**

- Rural place-name transcription is unreliable (§3). Mitigated by
  architecture, not solved.
- The full voice lane needs roughly 6 GB of model weights. Tier 1 does not.
- Region-specific pieces — county name tables, the local gazetteer — are
  built for Arizona. The approach ports; the data does not.
- Coverage is limited to what your antenna can hear. NWR is line-of-sight
  at VHF.

**On the timezone warning in §6:** treat that one as the highest-value item
in this document. It is the defect most likely to be reproduced by someone
building from these notes, it produces wrong output rather than no output,
and it is invisible to anyone who happens to live where the accident holds.


---

## Repository layout

| Path | What it is |
|---|---|
| `same_bridge.py` | The orchestrator — SAME parsing, message composition, the mesh sink, the voice lane, and the loopback control server. Everything of interest is in here. |
| `wx_rx.sh` | The runner `wx-sdr.service` launches. Builds the `rtl_fm` pipeline and pipes it into the bridge. |
| `wx-sdr.service` | The systemd unit. |
| `wx.env.example` | **Every configuration knob, documented**, with the code default noted for each. Copy to `wx.env`, `chmod 600`. Start here. |
| `gazetteer.json` | Per-SAME-code place-name vocabulary used to prime transcription (§5). Arizona data; regenerate for your region with `lab/build_gazetteer.py`. |
| `DEPLOYMENT.md` | Service setup, resource fencing, the control port, and how to hand-test the parse/send path without a radio. |
| `lab/` | The measurement tools behind the numbers in §2 and §3 — A/B harness, word-error scoring, the SAME parser bench, gazetteer builder. Not needed to run the receiver. |

The one file that is **not** here is `wx.env` itself: it carries an API key and
is never committed. `wx.env.example` documents it completely.

## Licence

MIT — see [`LICENSE`](LICENSE). Use it, change it, ship it, no attribution
beyond keeping the copyright notice. No warranty of any kind; see §7 and the
life-safety note at the top.

Meshtastic® is a registered trademark of Meshtastic LLC. This software is not
affiliated with, endorsed by, or sponsored by the Meshtastic project.
