"""Confidence scoring + attribution/label mapping (planning.md §2/§3).

Two separate numbers from the two signal scores:
  - combined_score S : how AI-like (0 human .. 1 AI)
  - confidence     C : how much to trust S

The attribution is decided by BOTH S and C — low confidence always collapses to
"uncertain", and the muddy middle of S is "uncertain" even at high confidence —
so the labels are score-band driven, never a binary flip at 0.5.
"""

# Machine-readable attribution enum -> human-readable headline (planning.md §3).
LABELS = {
    "likely_ai": "Likely AI-generated",
    "likely_human": "Likely human-written",
    "uncertain": "Inconclusive — we can't make a reliable call",
}

# Per-variant body text (planning.md §3). No variant asserts authorship as fact.
_LABEL_DETAIL = {
    "likely_ai": (
        "Both of our checks point toward machine generation. This is a "
        "probabilistic assessment, not proof — if this is your own writing, you "
        "can appeal it with content id {cid}."
    ),
    "likely_human": (
        "Both of our checks point toward a human author. This is a probabilistic "
        "assessment, not proof. Content id {cid}."
    ),
    "uncertain": (
        "Our two checks disagree, the text is too short to judge, or one check "
        "was unavailable. We are deliberately not labeling this as AI or human. "
        "Content id {cid}."
    ),
}


def generate_label(attribution: str, confidence: float, content_id: str) -> dict:
    """Map an attribution + confidence to the transparency label (planning.md §3).

    Returns {"headline", "detail"}. The headline carries the confidence so the
    served label text changes with the score, never a fixed string.
    """
    return {
        "headline": f"{LABELS[attribution]} (confidence {confidence})",
        "detail": _LABEL_DETAIL[attribution].format(cid=content_id),
    }

# Combined-score weights (semantic signal weighted higher; planning.md §2).
W_BURSTINESS = 0.35
W_STYLOMETRIC = 0.65

# Confidence factors.
LENGTH_FULL_AT = 5       # n_sentences at which the length factor reaches 1.0
# (M4-calibrated from 8 -> 5: at 8, clear 3-sentence paragraphs were gated to
#  "uncertain"; 5 lets clear cases through while still keeping short formal-human
#  text appropriately uncertain. ~5 sentences is also where burstiness stabilizes.)
LENGTH_FLOOR = 0.30      # minimum length factor (very short text)
UNAVAILABLE_PENALTY = 0.60  # confidence multiplier when Signal 2 failed

# Band thresholds (planning.md §2).
AI_THRESHOLD = 0.65
HUMAN_THRESHOLD = 0.35
CONFIDENCE_FLOOR = 0.50  # below this, attribution is always "uncertain"


def combine(score_b: float, score_s: float, n_sentences: int, ok: bool) -> dict:
    """Combine the two signals into {combined_score, confidence}.

    `ok` is whether Signal 2 (stylometric) succeeded. When it failed we fall back
    to Signal 1 alone and penalize confidence rather than inventing agreement.
    """
    length = max(LENGTH_FLOOR, min(1.0, n_sentences / LENGTH_FULL_AT))

    if ok:
        S = W_BURSTINESS * score_b + W_STYLOMETRIC * score_s
        agreement = 1.0 - abs(score_b - score_s)
        C = agreement * length * 1.0
    else:
        S = score_b
        C = length * UNAVAILABLE_PENALTY

    return {"combined_score": round(S, 4), "confidence": round(C, 4)}


def attribution_from_scores(combined_score: float, confidence: float) -> str:
    """Map (S, C) to the three-way attribution enum (planning.md §2)."""
    if confidence < CONFIDENCE_FLOOR:
        return "uncertain"
    if combined_score >= AI_THRESHOLD:
        return "likely_ai"
    if combined_score <= HUMAN_THRESHOLD:
        return "likely_human"
    return "uncertain"
