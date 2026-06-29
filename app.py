"""Provenance Guard — Flask app (Milestone 3).

POST /submit  -> run signal 1 (stylometric LLM), return content_id + attribution.
GET  /log     -> recent audit-log entries.
GET  /health  -> liveness + whether a Groq key is configured.

The full submission/appeal architecture lives in planning.md (## Architecture).
"""

import datetime
import hashlib
import os
import uuid

from dotenv import load_dotenv

load_dotenv()  # must run before signals.py reads GROQ_API_KEY

from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import db
from scoring import attribution_from_scores, combine, generate_label
from signals import burstiness, stylometric_signal

app = Flask(__name__)
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

db.init_db()


def _utc_now_iso() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@app.post("/submit")
@limiter.limit("10 per minute;100 per day")
def submit():
    data = request.get_json(silent=True) or {}
    text = data.get("text")
    creator_id = data.get("creator_id")

    if not isinstance(text, str) or not text.strip():
        return jsonify({"error": "'text' is required and must be a non-empty string"}), 400
    if not isinstance(creator_id, str) or not creator_id.strip():
        return jsonify({"error": "'creator_id' is required and must be a string"}), 400

    content_id = str(uuid.uuid4())

    # --- Signal 1: burstiness (local) --- Signal 2: stylometric LLM (Groq) ---
    burst = burstiness(text)
    sig = stylometric_signal(text)
    score_b = burst["score"]
    score_s = sig["score"]
    n_sentences = burst["stats"]["n_sentences"]

    # --- Confidence scoring (planning.md §2) ---
    scored = combine(score_b, score_s, n_sentences, sig["ok"])
    combined_score = scored["combined_score"]
    confidence = scored["confidence"]
    attribution = attribution_from_scores(combined_score, confidence)
    label = generate_label(attribution, confidence, content_id)
    timestamp = _utc_now_iso()

    # --- Audit entry: both individual signal scores + combined result ---
    entry = {
        "content_id": content_id,
        "creator_id": creator_id,
        "timestamp": timestamp,
        "attribution": attribution,
        "confidence": confidence,
        "combined_score": combined_score,
        "burstiness_score": score_b,
        "llm_score": round(score_s, 4),
        "signal2_ok": sig["ok"],
        "status": "classified",
    }

    db.write_submission(
        {
            "id": content_id,
            "created_at": timestamp,
            "creator_id": creator_id,
            "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "score_b": score_b,
            "score_s": round(score_s, 4),
            "combined_score": combined_score,
            "confidence": confidence,
            "attribution": attribution,
            "label": label["headline"],
            "status": "classified",
        }
    )
    db.write_audit(content_id, "decision", entry)

    return jsonify(
        {
            "content_id": content_id,
            "attribution": attribution,
            "label": label["headline"],
            "label_detail": label["detail"],
            "confidence": confidence,
            "combined_score": combined_score,
            "signals": {
                "burstiness": {"score": score_b, "stats": burst["stats"]},
                "stylometric": {
                    "score": round(score_s, 4),
                    "rationale": sig["rationale"],
                    "ok": sig["ok"],
                },
            },
            "status": "classified",
        }
    )


@app.post("/appeal")
@limiter.limit("10 per minute;50 per day")
def appeal():
    data = request.get_json(silent=True) or {}
    content_id = data.get("content_id")
    creator_reasoning = data.get("creator_reasoning")

    if not isinstance(content_id, str) or not content_id.strip():
        return jsonify({"error": "'content_id' is required and must be a string"}), 400
    if not isinstance(creator_reasoning, str) or not creator_reasoning.strip():
        return jsonify({"error": "'creator_reasoning' is required and must be a string"}), 400

    submission = db.get_submission(content_id)
    if submission is None:
        return jsonify({"error": f"unknown content_id: {content_id}"}), 404

    appeal_id = str(uuid.uuid4())
    timestamp = _utc_now_iso()

    # Mutate the submission's status; the original decision in audit_log is untouched.
    db.update_submission_status(content_id, "under_review")
    db.write_appeal(
        {
            "id": appeal_id,
            "submission_id": content_id,
            "created_at": timestamp,
            "reason": creator_reasoning,
            "status": "under_review",
            "resolution": None,
        }
    )

    # Append the appeal to the audit log BESIDE the original decision.
    entry = {
        "content_id": content_id,
        "appeal_id": appeal_id,
        "creator_id": submission["creator_id"],
        "timestamp": timestamp,
        "event_type": "appeal",
        "status": "under_review",
        "appeal_reasoning": creator_reasoning,
        "original_attribution": submission["attribution"],
        "original_confidence": submission["confidence"],
    }
    db.write_audit(content_id, "appeal", entry)

    return (
        jsonify(
            {
                "appeal_id": appeal_id,
                "content_id": content_id,
                "status": "under_review",
                "message": (
                    "Appeal received and logged. The original decision is "
                    "preserved alongside this appeal for review."
                ),
            }
        ),
        201,
    )


@app.get("/log")
def log():
    return jsonify({"entries": db.get_log(limit=50)})


@app.get("/health")
def health():
    return jsonify({"status": "ok", "groq": bool(os.environ.get("GROQ_API_KEY"))})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
