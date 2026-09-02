# wx-sdr lab — summarizer regression harness

Dev tooling for the NWR voice lane. **Nothing here runs in production and nothing
here is deployed with the receiver.** It exists so a change to
`SUMMARY_PROMPT`, the model, or the guards can be *measured* instead of eyeballed
against one lucky run.

Kept in git because the alternative is losing it: re-deriving these results costs
~30 minutes of Ollama time plus the four runs it took to get the instrument right.

## Running

```bash
# copy to the receiver host, then run it there against the local Ollama
scp wx_ab.py <host>:/opt/wx-sdr/lab/
ssh <host> 'cd /opt/wx-sdr/lab && /opt/wx-sdr/venv/bin/python wx_ab.py'
```

~7 minutes (30 Ollama calls). **Never transmits** — it only reads stored audio and
calls Ollama on localhost. `LAB_MODEL=qwen2.5:7b` swaps the model.

Clips come from `/opt/wx-sdr/lab/*.wav` plus the real activations in
`/opt/wx-sdr/recordings/`. Add a clip by dropping a `.wav` in `lab/` and deleting
`transcripts.json` (the transcript cache). Capture fresh audio off the live
receiver without disturbing the decode path:

```bash
curl -s --max-time 62 http://127.0.0.1:5071/listen -o sample4.wav
```

## What it does

`wx_ab.py` runs two arms over the same cached transcripts and scores both:

- **ARM A** — current production: raw transcript → `qwen2.5:7b` → raw output.
- **ARM B** — guarded: corrections applied to the transcript *and* the summary,
  then non-Latin strip, invented-number strip, and a hard length cap.

Both arms share ONE cached transcript per clip **on purpose**. Whisper is
nondeterministic; re-transcribing per arm would put the largest variable in the
middle of the comparison.

`wx_lab.py` is the earlier temperature sweep, kept because it is the evidence
that `temperature=0` does not help.

## Results (2026-07-20, 4 runs × 5 clips × 2 arms × 3 reps)

| failure mode | baseline | guarded |
|---|---|---|
| place name uncorrected | 5–6 / 15 | 0 / 15 |
| over 160 chars | 5–7 / 15 (worst 336) | 0 / 15 |
| number absent from the transcript | 8 / 15 | see below |
| non-Latin script | once, in a **flash flood warning** | 0 / 15 |

Every one of those rules is already stated in `SUMMARY_PROMPT`. Stating a rule in
a prompt is not enforcement.

## Read this before trusting a number it prints

- **The scorer has been wrong three times, and every bug flattered the result.**
  "0/15 invented" was twice an artefact of a regex that could not see the
  fabrication. **When a guard and its test share a pattern, a gap in that pattern
  hides the failure from both.** Read the actual summaries, not just the totals.
- Blocklisting time *shapes* is unwinnable — strip `2PM`, the model answers
  `18Z`; widen it, it answers a bare `1230`. The allowlist (*every number in the
  summary must appear in the transcript*) is the rule that holds.
- The invented-number **removal** is still wrong: it eats adjacent words
  (`'1 to 2 inches fallen'` → `'es fallen'`) and leaves dangling hyphens. The rule
  is right, the surgery is not. **Do not ship that guard yet.**

