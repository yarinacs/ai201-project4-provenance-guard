# Provenance Guard

A service that takes a piece of text, estimates how likely it is to be AI-generated, and returns an **honest, appealable transparency label** — not a verdict. It pairs two independent detection signals, separates *how AI-like* a text is from *how confident* it is in that read, exposes the uncertainty in the label, logs every decision immutably, and lets a creator contest any label.

> The point is not to "catch AI." It is to attach a calibrated, contestable provenance signal to text and keep a tamper-evident record — so being *wrong* is a recoverable event, not a permanent accusation. Full design rationale is in **[planning.md](planning.md)**.

## How a submission flows

`POST /submit` → **Signal 1 (burstiness)** + **Signal 2 (stylometric LLM)** → **confidence scorer** (combined score *and* a separate confidence) → **transparency label** → **append-only audit log** → JSON response with a `content_id`. An appeal (`POST /appeal`) flips the submission's status to `under_review` and appends the challenge to the audit log *beside* the original decision — never overwriting it. See the labeled diagram in [planning.md → Architecture](planning.md#architecture).

## Detection signals

| Signal | Type | Measures | AI-likelihood mapping |
|---|---|---|---|
| **1 — Burstiness** | Local, deterministic | Variation in sentence length (`cv = stdev/mean`). Humans write bursty; AI regresses to an even cadence. | `score_b = clamp((0.70 − cv) / (0.70 − 0.30), 0, 1)` |
| **2 — Stylometric** | Groq `llama-3.3-70b-versatile` | Discourse-level hallmarks: formulaic transitions, even hedging, generic register, absent lived detail. | Model returns `ai_likelihood ∈ [0,1]`; failure → `ok:false` (treated as *no signal*, not "human") |

**Why these two signals.** I deliberately paired one *cheap, deterministic, meaning-blind* signal with one *expensive, semantic, fallible* signal so their blind spots barely overlap. Burstiness can be computed instantly with no network and no cost, and it's fully reproducible — but it understands nothing about meaning, so it flags any uniformly-structured human writing (legal, technical, formal) as AI-like. The stylometric LLM judge reads at the discourse level where a length statistic is blind, but it is itself a miscalibrated detector and carries a known bias against formal and non-native-English prose. Two signals of the *same* kind (say, two statistical text metrics) would share blind spots and produce false confidence; pairing across mechanisms means **agreement is genuinely informative and disagreement is the system's most honest output** — it routes the text to `uncertain` instead of guessing.

**What I'd change for a real deployment.** The reference constants (burstiness `cv` anchors 0.70/0.30, the 0.35/0.65 signal weights, the band thresholds) are hand-set; for production I'd calibrate them against a labeled corpus with a held-out test set and track precision/recall per class rather than eyeballing a handful of examples. I'd also replace the LLM judge's self-reported likelihood with something better grounded — token-level perplexity from a model that exposes logprobs — because a model's *opinion* that text "looks AI" is weaker evidence than a measured statistic. And given the documented bias against non-native writers, I would not ship the LLM signal as a high-stakes gate without a human-review step in front of any consequence.

Each signal's full blind-spot analysis is in [planning.md §1](planning.md).

## Confidence scoring

Two separate numbers (so a text can be "70% AI-like" with *low* confidence):

```
combined_score  S = 0.35·score_b + 0.65·score_s        (semantic weighted higher)
confidence      C = agreement · length · available
    agreement = 1 − |score_b − score_s|
    length    = clamp(n_sentences / 5, 0.30, 1.0)
    available = 1.0 if Signal 2 ok else 0.60
```

Attribution is decided by **both** `S` and `C` — never a binary flip at 0.5:

```
if C < 0.50:           uncertain      # low confidence always abstains
elif S >= 0.65:        likely_ai
elif S <= 0.35:        likely_human
else:                  uncertain      # the muddy middle abstains
```

**Why split the score from the confidence.** The single most important design decision in this project is that *how AI-like* a text is and *how much I trust that read* are two different questions. A naive detector collapses them — it outputs one number and flips to "AI" at 0.5 — which means a coin-flip borderline case and a slam-dunk case are reported identically, and a real person's writing gets a confident accusation on the strength of a 0.51. By keeping confidence separate and letting it be driven by **signal agreement, text length, and signal availability**, the system can say "70% AI-like, but I don't trust it" and route that to `uncertain` instead of an accusation. The `uncertain` band is not a failure mode — it is the feature that protects people, because no scoring scheme can eliminate false positives on formal/non-native human writing (see Known limitations).

### Two submissions with meaningfully different confidence

Lifted from Milestone 4 testing — same pipeline, very different confidence:

**High confidence (C = 0.84) — casual human writing**
> *"ok so i finally tried that new ramen place downtown and honestly? underwhelming. the broth was fine but they put WAY too much sodium in it…"*
>
> `burstiness = 0.04`, `llm = 0.20` → **S = 0.13, C = 0.84** → **likely_human**
> Both signals strongly agree (irregular rhythm *and* idiosyncratic personal detail), and the text is long enough — so confidence is high.

**Lower confidence (C = 0.30) — formal human writing on monetary policy**
> *"The relationship between monetary policy and asset price inflation has been extensively studied in the literature. Central banks face a fundamental tension…"*
>
> `burstiness = 0.85`, `llm = 0.60` → **S = 0.69, C = 0.30** → **uncertain**
> Burstiness screams AI (uniform academic sentences) while the LLM is only moderately suspicious — the **signals disagree**, so confidence collapses and the system abstains rather than mislabeling a real human's formal prose. This is the false-positive protection working exactly as designed.

The same constant scoring function produces 0.84 and 0.30 on these two inputs — meaningful variation, not a fixed number.

## Transparency label — three variants

The served label carries the confidence, so the text changes with the score (never a constant string). No variant asserts authorship as fact — "likely," "appears," "we can't tell" are load-bearing. Each is served as a `headline` plus a `detail` body; `{cid}` is the submission's `content_id`. The exact text of all three:

**Variant 1 — high-confidence AI** (`C ≥ 0.50` and `S ≥ 0.65`)
> **Likely AI-generated (confidence 0.5745)**
> Both of our checks point toward machine generation. This is a probabilistic assessment, not proof — if this is your own writing, you can appeal it with content id `{cid}`.

**Variant 2 — high-confidence human** (`C ≥ 0.50` and `S ≤ 0.35`)
> **Likely human-written (confidence 0.8)**
> Both of our checks point toward a human author. This is a probabilistic assessment, not proof. Content id `{cid}`.

**Variant 3 — uncertain** (`C < 0.50`, *or* `0.35 < S < 0.65`)
> **Inconclusive — we can't make a reliable call (confidence 0.3018)**
> Our two checks disagree, the text is too short to judge, or one check was unavailable. We are deliberately not labeling this as AI or human. Content id `{cid}`.

(The confidence values shown are real examples from testing; the headline interpolates whatever confidence the submission actually scored.)

## Appeals workflow

The `content_id` returned at submit time *is* the credential (no accounts in this build).

- **`POST /appeal`** accepts `{ content_id, creator_reasoning }`.
- It sets the submission's status to `under_review`, writes an `appeals` row, and **appends** an `appeal` entry to the audit log beside the original `decision` (the original is never overwritten).
- Errors: `400` (missing fields), `404` (unknown `content_id`).

## API

| Endpoint | Accepts | Returns |
|---|---|---|
| `POST /submit` | `{ text, creator_id }` | `200` — `content_id`, `attribution`, `label`, `label_detail`, `confidence`, `combined_score`, `signals` |
| `POST /appeal` | `{ content_id, creator_reasoning }` | `201` — `appeal_id`, `content_id`, `status` |
| `GET /log` | — | `{ entries: [...] }` newest-first audit entries |
| `GET /health` | — | `{ status, groq }` |

## Rate limiting (defensible limits)

Applied with Flask-Limiter (`storage_uri="memory://"`, keyed per client IP):

- **`/submit`: `10 per minute; 100 per day`**
- **`/appeal`: `10 per minute; 50 per day`**

**Reasoning.** A genuine writer checking their own work submits a *handful* of pieces per session; `10/minute` comfortably covers iterative re-checks and pasting several drafts in a row, while a script trying to flood the service trips it almost immediately. `100/day` per IP caps sustained abuse while staying generous for heavy legitimate use. The cap also has a real cost dimension: **every `/submit` triggers a paid Groq call**, so the limit protects spend, not just availability. Appeals are rarer than submissions, so `/appeal` gets a tighter daily cap.

### Rate-limit evidence

12 rapid `/submit` requests against the `10/minute` limit — first 10 succeed, the rest are rejected with `429`:

```
$ for i in $(seq 1 12); do curl -s -o /dev/null -w "%{http_code}\n" -X POST \
    http://localhost:5000/submit -H "Content-Type: application/json" \
    -d '{"text":"This is a test submission for rate limit testing purposes only.","creator_id":"ratelimit-test"}'; done
200
200
200
200
200
200
200
200
200
200
429
429
```

## Audit log (complete, structured)

Every submission and every appeal writes a structured JSON entry (SQLite-backed, append-only). Submission entries capture timestamp, content id, attribution, confidence, **both** individual signal scores, and signal availability; appeal entries capture the reasoning and the status change. Newest-first sample from `GET /log` (an appeal sitting beside its original decision):

```json
{
  "entries": [
    {
      "appeal_id": "f47cce4d-5d19-45bc-be71-74aa390c960c",
      "appeal_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical.",
      "content_id": "617fe834-34d8-4eb7-b128-cd67c19c4a16",
      "creator_id": "maria-nonnative",
      "event_type": "appeal",
      "original_attribution": "uncertain",
      "original_confidence": 0.3018,
      "status": "under_review",
      "timestamp": "2026-06-29T05:30:50.858Z"
    },
    {
      "attribution": "uncertain",
      "burstiness_score": 0.8456,
      "combined_score": 0.686,
      "confidence": 0.3018,
      "content_id": "617fe834-34d8-4eb7-b128-cd67c19c4a16",
      "creator_id": "maria-nonnative",
      "llm_score": 0.6,
      "signal2_ok": true,
      "status": "classified",
      "timestamp": "2026-06-29T05:30:50.833Z"
    },
    {
      "attribution": "likely_human",
      "burstiness_score": 0.0,
      "combined_score": 0.13,
      "confidence": 0.8,
      "content_id": "7d929e74-8ee2-47cd-b919-8e69dfd61ae1",
      "creator_id": "user-human",
      "llm_score": 0.2,
      "signal2_ok": true,
      "status": "classified",
      "timestamp": "2026-06-29T05:30:50.416Z"
    },
    {
      "attribution": "likely_ai",
      "burstiness_score": 0.7425,
      "combined_score": 0.7149,
      "confidence": 0.5745,
      "content_id": "f19edb8d-ba8c-4dd2-91d6-83602a8b330c",
      "creator_id": "user-ai",
      "llm_score": 0.7,
      "signal2_ok": true,
      "status": "classified",
      "timestamp": "2026-06-29T05:30:49.988Z"
    }
  ]
}
```

> The same `content_id` appears in both a `decision` and an `appeal` entry — the appeal is appended, the original decision is preserved. That append-only property is what makes the log tamper-evident.

## Known limitations

This system will get some content **confidently wrong**, and the failures are tied to properties of the signals, not to a lack of data.

- **Formal / non-native-English human writing is the headline failure.** Both signals lean the wrong way on it for *different* reasons: burstiness penalizes uniform sentence length (formal writing is even), and the LLM judge carries a documented bias toward reading "elevated register" and "formulaic transitions" as machine-generated — exactly the hallmarks of fluent non-native or academic prose. When both happen to agree (as they can on a polished business letter), confidence *rises* and the system can hand a real person a "likely AI-generated" label. The `uncertain` band and the appeal path blunt this, but they don't prevent it — which is why the appeal is load-bearing rather than a nice-to-have.

- **Short text is unreliable by construction.** Burstiness needs several sentences to have any variance to measure; under ~5 sentences the length factor caps confidence low and most short inputs land in `uncertain`. That's the safe behavior, but it means the system simply can't make a call on a tweet-length passage — a real limitation, not a bug.

- **A repetitive, simple-vocabulary poem** (e.g. a villanelle) trips burstiness — short, even, repeated lines read as low-`cv` "AI-like" rhythm — while its plainness can also nudge the LLM judge upward. A genuinely human, crafted text can therefore skew toward AI; the length gate usually rescues it into `uncertain`, but a longer repetitive poem might not.

- **Adversarial AI defeats both signals cheaply.** An LLM told to "vary your sentence length and write casually" beats burstiness *and* the stylometric judge from opposite directions. This is a stylometric detector, not a watermark or provenance certificate — it measures how text *reads*, which is exactly the thing a determined user can change.

## Spec reflection

**One way the spec helped.** Writing `planning.md` *before* any code — specifically the §2 decision to compute confidence as a separate number from the AI-likelihood, with explicit thresholds (`0.35`/`0.65` on score, `0.50` on confidence) — meant the scoring function had a concrete contract to implement against. When I generated the scoring code, I could check it line-by-line against the spec and immediately see that a clear AI paragraph was landing in `uncertain` because the *length gate*, not the signals, was crushing confidence. Without the spec's separation of score vs. confidence, that bug would have looked like "the signals are bad" instead of "one calibration constant is wrong."

**One way the implementation diverged.** The spec's AI Tool Plan said to build **burstiness first** in M3 and the LLM signal in M4. I flipped the order — LLM signal first. The reason was concrete: M3's acceptance test is a 2-sentence input, on which burstiness is pure noise (it needs ≥5 sentences), so building it first would have produced nothing inspectable, defeating the milestone's "get one signal producing a result you can inspect" goal. The LLM judge gives a meaningful, discriminating result even on short text, and building the risky external integration first de-risked the project. The signal *definitions* never changed — only the build order — and I recorded the divergence and its rationale back into `planning.md` so the doc stayed the source of truth.

## AI usage

I used an AI coding tool to generate implementation code from `planning.md` sections, reviewing and correcting each output rather than pasting it blindly. Three concrete instances:

1. **Stylometric signal + Flask skeleton (M3).** I gave the AI the §1 detection-signals section and the architecture diagram and asked for a `POST /submit` skeleton plus the Groq signal function. It produced a working function, but the first draft's prompt would happily flag formal/non-native prose as AI. I **overrode the prompt** to add an explicit instruction not to penalize formality or non-native English and to stay near 0.5 when unsure — directly addressing the bias I'd flagged in §2 — and I added the `ok:false` failure path so a dead API call can't be mistaken for a "human" verdict.

2. **Confidence scoring (M4).** I asked the AI to implement the `combine()` function from the §2 formula. The code matched the formula, but my own test inputs exposed that the *constant* `LENGTH_FULL_AT = 8` was wrong: a clearly-AI 3-sentence paragraph scored `C = 0.33` and fell into `uncertain`. I traced it (signals agreed; the length gate was the culprit) and **revised the constant to 5**, which let clear cases classify while still keeping short formal-human text in `uncertain`. The AI implemented the spec faithfully; the spec's *constant* needed calibration, which only real testing surfaced.

3. **Label generator + appeal endpoint (M5).** I gave the AI the §3 label variants and §4 appeals workflow and asked for `generate_label()` and `POST /appeal`. I verified the generated label text matched the exact strings I'd written in the spec, and I checked that the appeal **appends** to the audit log rather than overwriting the original decision — the tamper-evident property the whole design rests on — before accepting it.

## Portfolio walkthrough

A short (~2 min) screen recording giving a quick end-to-end tour. Suggested run-of-show (the detailed evidence already lives in this README):

1. **Start the app**, hit `GET /health` to show Groq is wired.
2. **Submit casual human text** → show `likely_human`, high confidence (~0.84).
3. **Submit AI-flavored text** → show `likely_ai`.
4. **Submit the formal-human "monetary policy" passage** → show it lands `uncertain` (C ~0.30), and explain *why*: the two signals disagree, so the system abstains instead of falsely accusing a formal/non-native writer. This is the design's whole point.
5. **Appeal that submission** with the non-native-speaker reasoning → `201 under_review`.
6. **`GET /log`** → show the appeal appended beside the preserved original decision.
7. Optional: paste the 12-request rate-limit loop to show `200×10` then `429`.

> _Recording link: <add your video URL here>._

## Setup & run

```bash
pip install -r requirements.txt        # flask, flask-limiter, groq, python-dotenv
echo "GROQ_API_KEY=your_key_here" > .env   # never commit this; it is gitignored
python app.py                          # serves on http://127.0.0.1:5000
```

Example:

```bash
curl -s -X POST http://localhost:5000/submit \
  -H "Content-Type: application/json" \
  -d '{"text": "ok so i finally tried that ramen place downtown and honestly? underwhelming.", "creator_id": "test-user-1"}' | python -m json.tool
```

## Project layout

| File | Role |
|---|---|
| [app.py](app.py) | Flask app: `/submit`, `/appeal`, `/log`, `/health`; validation + rate limiting |
| [signals.py](signals.py) | Signal 1 (burstiness) and Signal 2 (stylometric Groq judge) |
| [scoring.py](scoring.py) | Confidence scoring, attribution bands, label generation |
| [db.py](db.py) | SQLite: `submissions`, append-only `audit_log`, `appeals` |
| [planning.md](planning.md) | Full design: signals, uncertainty, labels, appeals, edge cases, diagram |
