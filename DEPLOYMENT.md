# Deployment notes

Operational detail that the README's build sections (§4, §5) summarise but
don't fully specify. These are the notes from one working installation — adapt
paths and limits to your own box.

Read the README's **driver trap** section (§4) before any of this. Getting the
librtlsdr situation wrong is the single most common way to end up with a
service that looks healthy and never decodes anything.

## Install layout

Everything lives under one directory, `/opt/wx-sdr` in the reference install:

```
/opt/wx-sdr/
  wx_rx.sh          # runner, launched by systemd
  same_bridge.py    # orchestrator
  gazetteer.json    # whisper priming vocabulary (regenerate per region)
  wx.env            # config + API key — chmod 600, never committed
  bin/samedec       # SAME decoder binary
  venv/             # faster-whisper and its deps
  recordings/       # voice captures, bounded (see wx.env.example)
```

`wx.env` is the only file you must create. Copy `wx.env.example` to `wx.env`,
set `MESHTOC_API_KEY`, `WX_FREQ`, `WX_SERIAL` and `WX_CHANNEL`, and
`chmod 600` it.

## The service

```ini
[Unit]
Description=NWR SAME watch (rtl_fm -> samedec -> same_bridge -> mesh)
After=network-online.target

[Service]
ExecStart=/opt/wx-sdr/wx_rx.sh
Restart=always
RestartSec=10
Nice=10
CPUQuota=150%

[Install]
WantedBy=multi-user.target
```

`Restart=always` matters more than it looks. The bridge deliberately **exits**
on conditions it cannot recover from in place — most importantly a dongle
serial it cannot find — so that systemd restarts it cleanly rather than
leaving a half-alive process holding the radio.

`Nice=10` and `CPUQuota=150%` exist because this box also runs the thing the
alerts are being sent *to*. A transcription burst must never starve the mesh
client. If the receiver has a machine to itself, drop both.

## Fencing Ollama (tier 2 only)

If you run the voice lane, Ollama needs the same treatment, as a drop-in
override:

```ini
[Service]
Nice=10
CPUQuota=300%
Environment=OLLAMA_KEEP_ALIVE=-1
```

`OLLAMA_KEEP_ALIVE=-1` keeps the model resident. Without it the first
summary after an idle period pays a cold model load, which on slow storage
lands *inside* the window between the alert and its follow-up detail.

The same reasoning applies to whisper, which is why `same_bridge.py` warm-loads
it eagerly at startup in a daemon thread (~9 s) rather than lazily on first
capture. The lazy path stays as a fallback if warm-up fails.

## Control port

`same_bridge.py` binds a loopback HTTP surface (default `127.0.0.1:5071`,
stdlib only, no extra dependencies). It is unauthenticated **because it is
loopback-only** — do not bind it to an interface without putting auth in front.

- `GET /status` — level snapshot (peak/rms/quieting), the launched `argv`
  **verbatim**, the gold-baseline comparison, and `max_listeners`.
- `GET /listen` — live audio as a streaming WAV. Capped at `WX_MAX_LISTENERS`
  (default 1; 429 past the cap).
- `POST /transcribe` `{"name": "<recording.wav>"}` — re-run transcription and
  summarisation over a stored recording. **It never transmits.** Re-broadcasting
  an old alert as though it were current is exactly what a weather system must
  not do.

The listener tap must never block the decoder: listeners get a bounded `deque`
and frames are **dropped** for them when it fills. A stalled browser glitches
its own audio; it cannot slow the decode path.

## Testing without waiting for weather

Hand-feed a SAME header straight to the parser. `--stdin` skips the radio and
the voice lane entirely:

```sh
echo 'EAS: ZCZC-WXR-RWT-004013+0100-1962000-KEC94   -' \
  | /opt/wx-sdr/venv/bin/python /opt/wx-sdr/same_bridge.py --stdin
```

This **sends for real** if `MESHTOC_API_KEY` is set. Leave the key empty for a
dry run — the bridge prints what it would have sent.

For live end-to-end validation, use the Required Weekly Test. NWS runs the RWT
**Wednesdays, roughly 10 AM–noon local**, skipped when hazardous weather is
expected (a real alert stands in for it). It should land as
`WX: Required Weekly Test — <county> …` on your configured channel, with the
voice summary following about 30 seconds later.

## Checking the signal

Stop the service first — it holds the dongle:

```sh
rtl_power -d <idx> -f 162.35M:162.60M:5k -i 25 -1 -g 30 /tmp/wx.csv
```

Your NWR frequency should sit well above the neighbouring bins. **+13 dB over
floor is proven decode-capable**; a burst was missed at +9.4 dB. If you are
near the bottom of that range, fix the antenna before touching any tuning knob.

## Two dongles on one host

Dongles that belong to *other* processes or containers can still appear in
sysfs as unopenable devices with blank descriptor strings. They shift
librtlsdr's device indexes — breaking a bare `rtl_fm` or a device-0 default —
and can even confuse `-d <serial>` matching.

The bridge therefore resolves the index itself from `WX_SERIAL` via librtlsdr
and pins `rtl_fm -d <idx>`. Set `WX_SERIAL` to your dongle's EEPROM serial
(`rtl_eeprom -d 0` to read it). If it cannot be found, the service logs

```
[radio] dongle serial <x> not found — exiting for systemd restart
```

and restart-loops, which is deliberate: a receiver bound to the wrong radio is
worse than one that is visibly down.
