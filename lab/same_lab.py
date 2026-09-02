#!/usr/bin/env python3
"""Synthetic SAME/EAS burst generator + decoder A/B harness.

WHY THIS EXISTS
---------------
Recordings arm ON header decode, so the archive contains ZERO header audio:
the one thing needed to compare decoders cannot be replayed from disk. This
synthesises headers we control -- payload, burst count, per-burst corruption,
SNR -- so multimon-ng and samedec are scored on byte-identical input.

  ⚠ VALIDATE THE INSTRUMENT BEFORE TRUSTING THE MEASUREMENT.
  A generator bug and a decoder bug produce the same table. `selftest`
  asserts multimon-ng decodes a CLEAN burst; if that fails, every other
  number in here is measuring my bug, not the decoder's behaviour.

MODULATION (EAS/SAME, 47 CFR 11.31)
  bit rate   520.833 Hz   (= 1562.5 / 3)
  mark  '1'  2083.333 Hz  -> exactly 4 cycles per bit
  space '0'  1562.5   Hz  -> exactly 3 cycles per bit
  preamble   16 bytes of 0xAB, then ASCII payload
  bit order  LSB first within each byte; no start/stop bits (synchronous
             byte stream, not UART framing)

A SAME message is sent as THREE identical bursts. That redundancy is the
whole point of the experiment: multimon-ng decodes each burst independently,
samedec votes across all three.
"""
import argparse
import os
import subprocess
import sys

import numpy as np

BAUD = 1562.5 / 3.0          # 520.8333...
MARK = BAUD * 4.0            # 2083.3333...
SPACE = BAUD * 3.0           # 1562.5
RATE = 22050                 # what rtl_fm -s 22050 gives us, and samedec's default
PREAMBLE = b"\xAB" * 16

MULTIMON = "/usr/bin/multimon-ng"
SAMEDEC = "/opt/wx-sdr/lab/bin/samedec"

# A realistic Phoenix SVR for Maricopa: WXR-SVR-004013+0045-2220100-KPSR/NWS-
DEFAULT_MSG = "ZCZC-WXR-SVR-004013+0045-2220100-KPSR/NWS-"


# ---------------------------------------------------------------- modulation

def _bits(payload):
    """Preamble + payload as a bit stream, LSB first within each byte."""
    for byte in PREAMBLE + payload:
        for i in range(8):
            yield (byte >> i) & 1


def burst(payload, rate=RATE, amplitude=0.5):
    """One AFSK burst, float64 in [-1, 1].

    Samples-per-bit is 42.336 at 22050 Hz -- NOT an integer. Carrying a
    fractional position accumulator keeps the baud rate exact instead of
    letting rounding drift the timing over the ~500 bits of a header, which
    would desynchronise a decoder for reasons that have nothing to do with
    the decoder.
    """
    spb = rate / BAUD
    chunks, phase, pos = [], 0.0, 0.0
    for bit in _bits(payload):
        freq = MARK if bit else SPACE
        n = int(round(pos + spb)) - int(round(pos))
        pos += spb
        t = np.arange(n, dtype=np.float64)
        chunks.append(np.sin(phase + 2.0 * np.pi * freq * t / rate))
        # Continue the phase rather than restarting it each bit: a phase jump
        # at every bit boundary is splatter the real transmitter never emits.
        phase = (phase + 2.0 * np.pi * freq * n / rate) % (2.0 * np.pi)
    return amplitude * np.concatenate(chunks)


def message(msg=DEFAULT_MSG, bursts=3, gap_s=1.0, rate=RATE, amplitude=0.5):
    """Three-burst SAME message. Returns (signal, active_mask).

    `active_mask` marks the burst samples. SNR must be referenced to those
    only -- measuring signal power across the silent gaps too would understate
    it and mislabel every row in the sweep.
    """
    one = burst(msg.encode("ascii"), rate, amplitude)
    gap = np.zeros(int(gap_s * rate))
    sig, mask = [], []
    for i in range(bursts):
        if i:
            sig.append(gap)
            mask.append(np.zeros(len(gap), dtype=bool))
        sig.append(one.copy())
        mask.append(np.ones(len(one), dtype=bool))
    # Lead-in/out silence: decoders need somewhere to settle.
    pad = np.zeros(int(0.5 * rate))
    sig = [pad] + sig + [pad]
    mask = [np.zeros(len(pad), dtype=bool)] + mask + [np.zeros(len(pad), dtype=bool)]
    return np.concatenate(sig), np.concatenate(mask)


def corrupt_split(sig, mask, rate=RATE, sev=6.0, rng=None, msg=DEFAULT_MSG):
    """Wreck a DIFFERENT third of each burst's PAYLOAD, preambles intact.

    This is the exact shape of our two field misses: no burst is individually
    clean, but every payload byte survives in 2 of the 3. Independent-burst
    decoding gets nothing; cross-burst voting recovers the message.

    ⚠ THE PREAMBLE IS DELIBERATELY PROTECTED, and the first cut of this
    function got it wrong. Splitting the burst into quarters put quarter 0
    entirely inside the 16-byte preamble (128 of 472 bits = the first 27%),
    so burst 0 was never ACQUIRED -- not byte-corrupt, undetectable. That
    left samedec two bursts, which buys error DETECTION but not CORRECTION,
    and it scored 0/10 for a reason that had nothing to do with voting.
    Corrupting sync is a different experiment from corrupting data; this
    function is the data one.
    """
    rng = rng or np.random.default_rng(0)
    out = sig.copy()
    edges = np.flatnonzero(np.diff(mask.astype(np.int8)))
    starts, stops = edges[0::2] + 1, edges[1::2] + 1
    n = len(starts)
    spb = rate / BAUD
    pre = int(len(PREAMBLE) * 8 * spb)        # preamble length in samples
    for i, (a, b) in enumerate(zip(starts, stops)):
        p0 = a + pre                          # payload begins after the preamble
        span = b - p0
        s0 = p0 + (span * i) // n
        s1 = p0 + (span * (i + 1)) // n
        out[s0:s1] += rng.normal(0.0, sev * 0.5, s1 - s0)
    return out


def add_noise(sig, mask, snr_db, rng=None):
    """AWGN at `snr_db` referenced to burst-only signal power."""
    rng = rng or np.random.default_rng()
    if not mask.any():
        raise ValueError("empty active mask -- SNR would be meaningless")
    p_sig = float(np.mean(sig[mask] ** 2))
    p_noise = p_sig / (10.0 ** (snr_db / 10.0))
    return sig + rng.normal(0.0, np.sqrt(p_noise), len(sig))


def to_s16(sig):
    """float -> mono int16 native-endian, the format BOTH decoders read."""
    return np.clip(sig, -1.0, 1.0).astype(np.float64).__mul__(32767.0
                                                              ).astype("<i2").tobytes()


# ------------------------------------------------------------------ decoders

def run_multimon(raw, timeout=60):
    p = subprocess.run([MULTIMON, "-t", "raw", "-a", "EAS", "-"],
                       input=raw, capture_output=True, timeout=timeout)
    return p.stdout.decode("ascii", "replace")


def run_samedec(raw, rate=RATE, timeout=60):
    p = subprocess.run([SAMEDEC, "--rate", str(rate), "--file", "-"],
                       input=raw, capture_output=True, timeout=timeout)
    # samedec prints headers on stdout; keep stderr out of the match so a
    # diagnostic line can never be mistaken for a decode.
    return p.stdout.decode("ascii", "replace")


def got_header(out, msg):
    """Did the decoder recover the header EXACTLY?

    Exact match, deliberately. A partially-recovered header is not a success:
    compose() would build an alert with the wrong county or the wrong expiry,
    which is worse than the miss it replaces.
    """
    want = msg.rstrip("-")
    return any(want in line for line in out.splitlines())


# -------------------------------------------------------------------- checks

def cmd_selftest(args):
    """GATE: multimon-ng must decode a clean synthetic burst.

    Runs BEFORE any comparison is allowed to mean anything. multimon-ng is
    the known-good reference here precisely because it is what has been
    decoding real KEC94 traffic for weeks -- if it cannot read my synthetic
    header, the header is wrong.
    """
    sig, mask = message(args.msg)
    raw = to_s16(sig)
    print("payload      : %s" % args.msg)
    print("duration     : %.2f s  (%d samples @ %d Hz)"
          % (len(sig) / RATE, len(sig), RATE))
    print("bursts       : 3")

    mm = run_multimon(raw)
    ok_mm = got_header(mm, args.msg)
    print("\n--- multimon-ng (REFERENCE) ---")
    print(mm.strip() or "(no output)")
    print("decoded      : %s" % ("YES" if ok_mm else "NO"))

    sd = run_samedec(raw)
    ok_sd = got_header(sd, args.msg)
    print("\n--- samedec ---")
    print(sd.strip() or "(no output)")
    print("decoded      : %s" % ("YES" if ok_sd else "NO"))

    print()
    if not ok_mm:
        print("INSTRUMENT INVALID -- multimon-ng could not read a CLEAN synthetic")
        print("burst. The generator is wrong, not the decoder. Every sweep result")
        print("would be measuring that bug. Fix this before reading any table.")
        return 1
    print("INSTRUMENT VALID -- multimon-ng reads the clean synthetic burst.")
    if not ok_sd:
        print("NOTE: samedec did NOT read the same clean burst. That is a real")
        print("finding about samedec, not an instrument fault.")
    return 0


def cmd_sweep(args):
    """Decode rate vs SNR, both decoders, identical audio per trial."""
    print("SNR sweep: %d trials per step, %.1f..%.1f dB step %.1f"
          % (args.trials, args.hi, args.lo, args.step))
    print("payload: %s\n" % args.msg)
    print("%8s %14s %14s" % ("SNR dB", "multimon-ng", "samedec"))
    print("%8s %14s %14s" % ("-" * 8, "-" * 14, "-" * 14))
    rows = []
    snr = args.hi
    while snr >= args.lo - 1e-9:
        mm_ok = sd_ok = 0
        for t in range(args.trials):
            rng = np.random.default_rng(hash((round(snr, 3), t)) & 0xFFFFFFFF)
            sig, mask = message(args.msg)
            raw = to_s16(add_noise(sig, mask, snr, rng))
            if got_header(run_multimon(raw), args.msg):
                mm_ok += 1
            if got_header(run_samedec(raw), args.msg):
                sd_ok += 1
        rows.append((snr, mm_ok, sd_ok))
        print("%8.1f %10d/%-3d %10d/%-3d"
              % (snr, mm_ok, args.trials, sd_ok, args.trials))
        sys.stdout.flush()
        snr -= args.step
    return 0


def cmd_split(args):
    """THE decisive test -- our field failure mode, reproduced deliberately.

    Each burst is wrecked in a different quarter. No burst is individually
    clean; every byte survives in >=2 of 3. Independent-burst decoding should
    fail; cross-burst voting should recover.
    """
    print("split-burst corruption: a different quarter of each burst wrecked")
    print("payload: %s" % args.msg)
    print("trials : %d  severity: %.1f  floor SNR: %.1f dB\n"
          % (args.trials, args.sev, args.snr))
    mm_ok = sd_ok = 0
    for t in range(args.trials):
        rng = np.random.default_rng(1000 + t)
        sig, mask = message(args.msg)
        sig = add_noise(sig, mask, args.snr, rng)
        sig = corrupt_split(sig, mask, sev=args.sev, rng=rng)
        raw = to_s16(sig)
        m = got_header(run_multimon(raw), args.msg)
        s = got_header(run_samedec(raw), args.msg)
        mm_ok += m
        sd_ok += s
        print("  trial %2d   multimon-ng %-4s   samedec %-4s"
              % (t + 1, "OK" if m else "MISS", "OK" if s else "MISS"))
        sys.stdout.flush()
    print("\n%-12s %d/%d" % ("multimon-ng", mm_ok, args.trials))
    print("%-12s %d/%d" % ("samedec", sd_ok, args.trials))
    return 0


def cmd_write(args):
    """Emit a WAV for listening / feeding the live pipeline by hand."""
    import wave
    sig, mask = message(args.msg)
    if args.snr is not None:
        sig = add_noise(sig, mask, args.snr)
    if args.split:
        sig = corrupt_split(sig, mask, sev=args.sev)
    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(to_s16(sig))
    print("wrote %s  (%.2f s)" % (args.out, len(sig) / RATE))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("selftest", help="validate the generator (RUN THIS FIRST)")
    p.add_argument("--msg", default=DEFAULT_MSG)
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("sweep", help="decode rate vs SNR, both decoders")
    p.add_argument("--msg", default=DEFAULT_MSG)
    p.add_argument("--hi", type=float, default=20.0)
    p.add_argument("--lo", type=float, default=-2.0)
    p.add_argument("--step", type=float, default=2.0)
    p.add_argument("--trials", type=int, default=5)
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("split", help="split-burst corruption (the decisive test)")
    p.add_argument("--msg", default=DEFAULT_MSG)
    p.add_argument("--trials", type=int, default=10)
    p.add_argument("--sev", type=float, default=6.0)
    p.add_argument("--snr", type=float, default=20.0)
    p.set_defaults(func=cmd_split)

    p = sub.add_parser("write", help="write a WAV")
    p.add_argument("--msg", default=DEFAULT_MSG)
    p.add_argument("--out", default="/opt/wx-sdr/lab/synth_same.wav")
    p.add_argument("--snr", type=float, default=None)
    p.add_argument("--split", action="store_true")
    p.add_argument("--sev", type=float, default=6.0)
    p.set_defaults(func=cmd_write)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
