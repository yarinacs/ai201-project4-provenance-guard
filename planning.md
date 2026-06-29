# Provenance Guard — Planning

**Project:** A service that takes a piece of text, estimates how likely it is to be AI-generated, and returns an *honest, appealable transparency label* — not a verdict. The point is not to "catch AI"; it is to attach a calibrated, contestable provenance signal to text and keep a tamper-evident record of every decision.

**One-line goal:** Given raw text, produce a confidence-scored provenance label backed by two independent detection signals, logged immutably, and reversible through an appeal — so that being *wrong* is a recoverable event, not a permanent accusation.

> This is a design document written before any application code. The numbers, thresholds, and label strings here are the contract the implementation must match. If I change a signal, a threshold, or an endpoint shape later, I update this file *first*, then the code. This file is also my primary prompting artifact — Milestones 3–5 feed sections of it (plus the diagram) to an AI tool, so it is written to be concrete enough to implement against.

---

## 0. The seven required features

1. **Submission + detection** — accept text, run detection.
2. **Detection signal #1** — an independent measurement of one property of the text.
3. **Detection signal #2** — a *second, mechanistically different* measurement.
4. **Confidence scoring** — combine the two signals into one score *and* an explicit uncertainty.
5. **Transparency label** — a human-readable label that exposes the uncertainty instead of hiding it.
6. **Audit log** — an append-only record of every decision and every appeal.
7. **Appeal mechanism** — a path for a creator to contest a label, which itself gets logged.

Rate limiting (`flask-limiter`) wraps all of the above. The rest of this document makes these seven connect *at the seams*, which is where feature-by-feature builds break.

---

## 1. Question 1 — Detection signals

I committed to **one cheap deterministic structural signal** and **one expensive semantic model-based signal**. They are paired on purpose: they fail in *different* ways, so agreement is meaningful and disagreement is itself the most useful output. Two signals of the same kind (e.g. two statistical metrics) would share blind spots and give false confidence.

Both signals emit a normalized **AI-likelihood in `[0, 1]`** (0 = looks human, 1 = looks AI) so the scorer can combine them on one scale.

### Signal 1 — Burstiness (sentence-length variability)

- **Measures:** variation in sentence length across the text — concretely the **coefficient of variation** `CV = stdev(sentence_lengths) / mean(sentence_lengths)`.
- **Why it separates human/AI:** humans write bursty — a long winding clause, then a three-word punch. Autoregressive models regress to an even cadence, so sentences cluster around one length. Low CV ≈ AI-like.
- **Output shape:**
  ```json
  { "score": 0.78, "stats": { "n_sentences": 12, "mean_len": 17.4, "stdev_len": 5.1, "cv": 0.29 } }
  ```
- **Raw→score mapping (calibration):** `cv ≥ 0.70` → `0.0` (very human), `cv ≤ 0.30` → `1.0` (very AI), linear between:
  `score_b = clamp((0.70 - cv) / (0.70 - 0.30), 0, 1)`. Reference values 0.70/0.30 are starting points to be tuned in M4 against a small labeled set; the *form* of the mapping is fixed here.
- **Blind spot:** meaning-blind. Breaks on short text (<5 sentences = noise), on naturally-uniform human genres (legal, technical, instructions, lists) which read as AI-like — a **major false-positive source** — and on AI told to "vary your sentence length." It measures rhythm, not authorship.

### Signal 2 — Stylometric LLM judge (Groq `llama-3.3-70b`)

- **Measures:** discourse-level hallmarks a statistic can't see — formulaic transitions ("Moreover," "In conclusion"), suspiciously even hedging, polished-but-generic register, absence of specific lived detail.
- **Why it separates human/AI:** these patterns emerge from instruction-tuned generation (the tidy "assistant voice"). A capable model reading at the discourse level can name them.
- **Output shape:** the model is prompted to return strict JSON, which I normalize to:
  ```json
  { "score": 0.15, "rationale": "specific lived detail; varied register; idiosyncratic asides", "ok": true }
  ```
  `score` is the model's 0–1 AI-likelihood (already on the target scale). `ok:false` means the call failed/timed out → treated as **no signal**, not "human."
- **Blind spot:** it is itself a miscalibrated detector; it is **biased against non-native English and formal human prose** (the second major false-positive source — an *equity* problem); it is defeated by AI told to "write casually"; and it is non-deterministic and can fail.

**Why this pairing:** Signal 1 is fast/free/reproducible/meaning-blind; Signal 2 is slow/costly/fallible/meaning-aware. Blind spots barely overlap — *except* both are unreliable on very short text, which is exactly why **length gates confidence** (§2).

---

## 2. Question 2 — Uncertainty representation

The scorer computes **two separate numbers** from the two signal scores: a *combined score* (how AI-like) and a *confidence* (how much to trust that score). Keeping them separate is the whole project — a text can be "70% AI-like" with **low confidence**, and that must produce a cautious label, never an accusation.

### Combined score `S ∈ [0,1]`
Weighted blend, semantic signal weighted higher because Signal 1's false-positive rate on uniform human genres is high:
```
S = 0.35 * score_b + 0.65 * score_s        (when Signal 2 ok)
S = score_b                                 (when Signal 2 failed; confidence is then penalized)
```
Weights 0.35/0.65 are M4-tunable; the form is fixed.

### Confidence `C ∈ [0,1]`
`C` is the product of three factors, each in `[0,1]`:
```
agreement = 1 - |score_b - score_s|                     # 1.0 when signals match, →0 as they diverge
length    = clamp(n_sentences / 5, 0.30, 1.0)           # short text caps confidence low (M4-calibrated: 8→5)
available = 1.0 if Signal 2 ok else 0.60                # lean on Signal 1 alone, and say so
C = agreement * length * available
```
(When Signal 2 failed, `agreement` is dropped from the product since there's nothing to agree with; `C = length * 0.60`.)

### What a confidence number *means* (the "explain 0.62 to a non-technical user" test)

| Confidence | Plain-English meaning shown/served to users |
|---|---|
| `C ≥ 0.75` | "The two checks strongly agree and the text was long enough to judge. Treat this as a firm read." |
| `0.50 ≤ C < 0.75` | "The two checks mostly agree but not fully, or the text is on the short side. Treat this as a working guess, not a finding." (a **0.62** lands here) |
| `C < 0.50` | "The checks disagree, the text is too short, or one check failed. We can't make a reliable call." |

### Thresholds — likely human / uncertain / likely AI
The final label is decided by **both** `S` and `C` (not a binary flip at 0.5):

```
if   C < 0.50:                     label = UNCERTAIN      # low confidence always collapses to uncertain
elif S >= 0.65:                    label = LIKELY_AI
elif S <= 0.35:                    label = LIKELY_HUMAN
else:  # 0.35 < S < 0.65           label = UNCERTAIN      # the muddy middle is uncertain even at high C
```

So there are two distinct ways to land in UNCERTAIN — the score is in the middle, *or* confidence is low — and the band boundaries (0.35 / 0.65 on score, 0.50 on confidence) are the calibration knobs.

---

## 3. Question 3 — Transparency label design (the three variants)

Three canonical variants. Each is served with the `combined_score`, `confidence`, both signal sub-scores, the `submission_id`, and a fixed probabilistic-and-appealable note. **No variant asserts authorship as fact** — "likely," "appears," "can't tell" are load-bearing.

**Variant A — LIKELY_AI** (`C ≥ 0.50` and `S ≥ 0.65`)
> **Likely AI-generated** *(confidence {C})*
> Both of our checks point toward machine generation. This is a probabilistic assessment, not proof — if this is your own writing, you can appeal it with submission id `{id}`.

**Variant B — LIKELY_HUMAN** (`C ≥ 0.50` and `S ≤ 0.35`)
> **Likely human-written** *(confidence {C})*
> Both of our checks point toward a human author. This is a probabilistic assessment, not proof. Submission id `{id}`.

**Variant C — UNCERTAIN** (`C < 0.50`, *or* `0.35 < S < 0.65`)
> **Inconclusive — we can't make a reliable call** *(confidence {C})*
> Our two checks disagree, the text is too short to judge, or one check was unavailable. We are deliberately not labeling this as AI or human. Submission id `{id}`.

> Design note: the *uncertain* variant is the one that protects people. A naive 0.5 flip would force every borderline case into "AI" or "human"; the explicit third variant is what lets the system say "I don't know" instead of guessing about a real person's work.

---

## 4. Question 4 — Appeals workflow

- **Who can appeal:** the creator/submitter of a scored text. They identify the case by quoting its `submission_id` (returned at submit time). No accounts in this build — possession of the `submission_id` is the credential.
- **What they provide:** `{ "submission_id", "reason" }` — the id plus a free-text reason ("this is my own writing; I'm a non-native English speaker").
- **What the system does on receipt** (`POST /appeal`):
  1. Validate the `submission_id` exists → `404` if not.
  2. Create an `appeals` row: `appeal_id`, `submission_id`, `created_at`, `reason`, `status = "under_review"`.
  3. Flip the submission's `status` from `scored` → `appealed`. **The original decision is never overwritten.**
  4. Write an `audit_log` entry `event_type = "appeal"` *beside* the original `decision` entry.
  5. Return `201 { appeal_id, submission_id, status: "under_review" }`.
- **What a human reviewer sees** when they open the appeal queue (`GET /appeals?status=under_review`, an admin read view): a list, oldest-first, each row showing — `appeal_id`, `submission_id`, `created_at`, the **creator's reason**, the **original label + combined_score + confidence**, and **both signal sub-scores with their rationale/stats**. That is everything needed to judge the appeal: what the system decided, *why* (the two signals), and what the creator says is wrong. A reviewer action (uphold/overturn) is a stretch feature; the M5 scope is queue + status, with the resolution slot already in the schema.

---

## 5. Question 5 — Anticipated edge cases (specific)

1. **The repetitive simple-vocabulary poem.** A villanelle or a children's-style poem repeats lines and uses short, even, plain sentences. Signal 1 sees low CV → scores it *AI-like*; Signal 2 may read the plainness as "generic register." Both can push a genuinely human, *crafted* text toward LIKELY_AI. Mitigation: the form of the failure is exactly why the UNCERTAIN band and the appeal exist — but I'll also note in M4 that very short line-based text trips the length gate, capping confidence and pushing it to UNCERTAIN rather than a false accusation.

2. **The formal, non-native English business letter (Maria).** Uniform sentence structure (Signal 1 → AI-like) plus "elevated register / formulaic transitions" (Signal 2 → AI-like, the known equity bias). Here **both signals agree and are both wrong**, which normally *raises* confidence — the worst case. Scoring can't save it; the appeal is the load-bearing correction, and the label's probabilistic wording keeps it from being a flat accusation in the meantime.

3. **(Bonus) Heavily AI-edited human text / "humanized" AI.** Mixed-authorship and AI told to write casually defeat both signals from opposite directions. Correct behavior is to land in UNCERTAIN rather than pretend precision — the system should under-claim, not over-claim.

The throughline: every edge case here is a **false-positive against a real person**, and the design's answer is the same each time — surface uncertainty (UNCERTAIN band), never assert authorship (probabilistic wording), and make the decision reversible (appeal + append-only log).

---

## Architecture

The submission flow: a creator POSTs raw text to `/submit`; after rate-limit and validation it runs **two independent signals** (local burstiness + a Groq stylometric judge), whose scores feed a **confidence scorer** that emits a combined AI-likelihood *and* a separate confidence, which a **label generator** maps to one of three transparency labels before the decision is written to an append-only **SQLite audit log** and returned with its `submission_id`. The appeal flow: a creator POSTs that `submission_id` plus a reason to `/appeal`, which creates an appeal row, flips the submission's status to `appealed`, and **appends** the challenge to the audit log *beside* the original decision rather than overwriting it — so the record always shows both what was decided and that it was contested.

```
SUBMISSION FLOW
===============

  Creator
     │  raw text  { "text": "..." }
     ▼
┌─────────────────┐   request ok?
│  Flask app      │──────────────► Rate limiter (flask-limiter) ──► [429 if over quota]
│  POST /submit   │
└────────┬────────┘
         │  raw text
         ▼
   Input validator ──► [400 if missing/short]  ──┐ (too-short flag travels onward)
         │  validated text                       │
         ▼                                        │
   ┌─────────────── Detection layer ───────────┐ │
   │   raw text                  raw text       │ │
   │      ▼                         ▼           │ │
   │  Signal 1:                Signal 2:        │ │
   │  Burstiness               Stylometric LLM  │ │
   │  (local Python)           (Groq llama-3.3) │ │
   │      │                         │           │ │
   │  score_b ∈[0,1]           score_s ∈[0,1]   │ │
   │  + stats                  + rationale,ok   │ │
   └──────┼─────────────────────────┼──────────┘ │
          └───────────┬──────────────┘  short-text flag
                      ▼                  ◄──────────┘
              Confidence scorer
                      │  combined_score S + confidence C
                      ▼
              Label generator   (S,C → LIKELY_AI | LIKELY_HUMAN | UNCERTAIN)
                      │  label text + C + both signal scores
                      ▼
              Audit log (SQLite, append-only)
                      │  creates submission_id; writes 'decision' row
                      ▼
              Response builder
                      │  JSON { submission_id, label, confidence, combined_score, signals, explanation }
                      ▼
                   Creator


APPEAL FLOW
===========

  Creator
     │  { "submission_id", "reason" }
     ▼
┌─────────────────┐
│  Flask app      │──► Rate limiter ──► [429]
│  POST /appeal   │
└────────┬────────┘
         │  submission_id + reason
         ▼
   Lookup submission ──► [404 if unknown]
         │  valid submission_id
         ▼
   Status updater
         │  set submission.status = "appealed"; create appeal row (status="under_review")
         ▼
   Audit log (SQLite, append-only)
         │  writes 'appeal' entry BESIDE original 'decision' (never overwrites it)
         ▼
   Response builder
         │  JSON { appeal_id, submission_id, status: "under_review" }
         ▼
      Creator
```

**Key invariant:** the audit log is the only writer of `submission_id`, and the appeal flow *appends* rather than mutating — so the record always shows what was decided *and* that it was contested. That single property is what makes the system accountable rather than merely automated.

### Data model (SQLite, append-only by convention)
- **`submissions`**: `id` (uuid PK), `created_at`, `text_hash`, `score_b`, `score_s`, `combined_score`, `confidence`, `label`, `status` (`scored`|`appealed`).
- **`appeals`**: `id` (uuid PK), `submission_id` (FK), `created_at`, `reason`, `status` (`under_review`|`upheld`|`overturned`), `resolution` (nullable, for the stretch reviewer action).
- **`audit_log`**: `id`, `created_at`, `submission_id`, `event_type` (`decision`|`appeal`), `payload` (JSON snapshot). No code path issues `UPDATE`/`DELETE` against this table.

### API surface
| Endpoint | Accepts | Returns |
|---|---|---|
| `POST /submit` | `{text}` | `200` decision (id, label, combined_score, confidence, signals, explanation); `400`/`429`; `503`→Signal-1-only result with lowered confidence |
| `POST /appeal` | `{submission_id, reason}` | `201 {appeal_id, submission_id, status}`; `400`/`404`/`429` |
| `GET /result/{id}` | — | `200` decision + current `status`; `404` |
| `GET /audit/{id}` | — | `200 {submission, appeals[], log[]}`; `404` |
| `GET /appeals?status=` | — | `200` reviewer queue (admin); appeal rows + original decision + signals |
| `GET /health` | — | `200 {status, groq}` |

---

## AI Tool Plan

I generate code milestone-by-milestone, feeding the AI tool only the relevant spec sections plus the Architecture diagram each time, then verifying outputs *before* wiring them together. The diagram travels into all three.

> **Build-order note (decided at M3):** the two signals are built in the *opposite* order from their §1 numbering. The **stylometric LLM judge (Signal 2) is built first in M3**, and **burstiness (Signal 1) is built in M4**. Reasons: (a) the M3 acceptance test is a 2-sentence input, on which burstiness is pure noise (<5 sentences) and produces nothing inspectable; (b) the LLM signal yields a real, discriminating attribution on short text, satisfying "get one signal producing a result you can inspect"; (c) it de-risks the external Groq integration early. Signal *identities/definitions* are unchanged — only the build sequence flipped.

### M3 — submission endpoint + first signal (built: stylometric)
- **Spec I provide:** §1 (Detection signals — esp. Signal 2's output shape + the strict-JSON Groq contract, `ok:false` handling), the **Architecture** diagram, and the `POST /submit` row of the API table.
- **Ask the tool to generate:** a Flask app skeleton with `POST /submit` (validation + `400`/`429` handling, `flask-limiter` wired) and a standalone `stylometric_signal(text) -> {score, rationale, ok}` function calling Groq `llama-3.3-70b-versatile` with a JSON-only prompt, `temperature=0`, a timeout, and a failure path.
- **Verify:** call `stylometric_signal()` directly on hand-picked inputs (a personal human paragraph, an AI-flavored paragraph, a short stub) and confirm the score discriminates and `ok` flips on failure — *before* wiring it in. Then curl `/submit` for shape. **(Done — human 0.2 / AI 0.8 / stub 0.6; log shows all three attributions.)**

### M4 — second signal + confidence scoring (built: burstiness)
- **Spec I provide:** §1 (Signal 1 output shape + `cv`→`score_b` mapping), §2 (combined-score formula, the 3-factor confidence formula, the band thresholds), the diagram.
- **Ask the tool to generate:** a `burstiness(text) -> {score, stats}` function implementing the exact CV mapping, plus a `score(score_b, score_s, n_sentences, ok) -> {combined_score, confidence}` function implementing §2 exactly (replacing the M3 `placeholder_confidence`).
- **Verify:** call `burstiness()` on a bursty paragraph, a uniform/listy paragraph, and a 2-sentence stub; confirm `score_b` moves the right way and short text flags low `n_sentences`. Then run clearly-AI vs clearly-human samples end-to-end and confirm combined scores **vary meaningfully** (not stuck near 0.5); feed a disagreement case and confirm `confidence` drops; force Signal-2 `ok:false` and confirm fallback with penalized confidence.

### M5 — production layer (labels + appeals)
- **Spec I provide:** §3 (the three exact label variants + their `S`/`C` conditions), §4 (appeals workflow: status changes, what's logged, reviewer-queue fields), the data model, the diagram.
- **Ask the tool to generate:** a `label(S, C) -> variant` generator emitting the three exact strings, the SQLite schema + append-only audit writes, `POST /appeal`, and `GET /appeals` reviewer queue.
- **Verify:** craft inputs that reach **all three** label variants (high-C AI, high-C human, and both routes into UNCERTAIN — low C *and* mid-S); file an appeal and confirm `status` flips `scored`→`appealed`, an `appeal` row + audit entry are written, and the original `decision` row is untouched.

---

## Checkpoint status
- [x] Q1 Detection signals — 2 signals, what each measures, output shapes, combination formula (§1, §2).
- [x] Q2 Uncertainty — what 0.6 means, raw→calibrated mapping, the human/uncertain/AI thresholds (§2).
- [x] Q3 Transparency labels — three exact variants written out (§3).
- [x] Q4 Appeals — who, what info, status changes, logging, reviewer-queue view (§4).
- [x] Q5 Edge cases — repetitive poem + non-native formal letter (+ bonus) (§5).
- [x] Architecture section with the M1 diagram + 2–3 sentence narrative.
- [x] AI Tool Plan covering M3/M4/M5 with sections, requests, verification.
- [x] Labels are score-band driven (0.35/0.65 + C<0.50), not a binary flip at 0.5.
