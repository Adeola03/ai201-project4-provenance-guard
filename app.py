"""Provenance Guard — Flask API for AI-content attribution.

Endpoints
  POST /submit    submit text for attribution analysis (rate limited)
  POST /appeal    creator contests a classification -> status "under_review"
  GET  /log       structured audit log (every decision + appeal)
  GET  /appeals   reviewer queue of items currently under review
  GET  /content/<id>  fetch a single content record
  GET  /          service info / health

See planning.md and README.md for the design.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import detection
import store
from labels import build_label

load_dotenv()

app = Flask(__name__)

# --- Rate limiting ---------------------------------------------------------
# /submit triggers a paid, latency-bound LLM call per request, so it is the
# endpoint worth protecting. See README "Rate Limiting" for the reasoning.
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],  # only the routes we explicitly decorate are limited
    storage_uri="memory://",
)

MIN_WORDS = 20  # below this we refuse: too little text to analyze meaningfully

TEXT_SNIPPET_LEN = 280


def _snippet(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TEXT_SNIPPET_LEN else text[:TEXT_SNIPPET_LEN] + "…"


@app.get("/")
def index():
    return jsonify(
        service="Provenance Guard",
        description="Multi-signal AI-content attribution with transparency labels.",
        endpoints={
            "POST /submit": "Analyze text. Body: {text, creator_id?}",
            "POST /appeal": "Contest a result. Body: {content_id, creator_id, creator_reasoning}",
            "GET /log": "Structured audit log",
            "GET /appeals": "Reviewer queue (status=under_review)",
            "GET /content/<id>": "Fetch one content record",
        },
        llm_available=bool(os.environ.get("GROQ_API_KEY")),
    )


@app.post("/submit")
@limiter.limit("10 per minute; 100 per day")
def submit():
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    creator_id = (body.get("creator_id") or "anonymous").strip()

    if not text:
        return jsonify(error="`text` is required"), 400
    if len(text.split()) < MIN_WORDS:
        return jsonify(
            error=f"`text` must be at least {MIN_WORDS} words to analyze"
        ), 400

    result = detection.analyze(text)
    label = build_label(result["classification"], result["confidence"])

    content_id = store.next_content_id()
    record = {
        "content_id": content_id,
        "creator_id": creator_id,
        "text": text,
        "text_snippet": _snippet(text),
        "status": "classified",
        "classification": result["classification"],
        "p_ai": result["p_ai"],
        "confidence": result["confidence"],
        "degraded": result["degraded"],
        "label": label,
        "signals": result["signals"],
        "appeals": [],
    }
    store.save_content(record)

    store.append_audit(
        {
            "event": "classification",
            "content_id": content_id,
            "creator_id": creator_id,
            "text_snippet": record["text_snippet"],
            "classification": result["classification"],
            "p_ai": result["p_ai"],
            "confidence": result["confidence"],
            "degraded": result["degraded"],
            "signals_used": ["stylometry", "llm"],
            "signal_detail": {
                "p_style": result["signals"]["stylometry"]["p_style"],
                "p_llm": result["signals"]["llm"]["p_llm"],
                "llm_available": result["signals"]["llm"]["available"],
                "llm_rationale": result["signals"]["llm"]["rationale"],
            },
            "label_variant": label["variant"],
            "label_text": label["text"],
        }
    )

    return jsonify(
        content_id=content_id,
        classification=result["classification"],
        p_ai=result["p_ai"],
        confidence=result["confidence"],
        degraded=result["degraded"],
        label=label,
        signals=result["signals"],
    ), 201


@app.post("/appeal")
def appeal():
    body = request.get_json(silent=True) or {}
    content_id = (body.get("content_id") or "").strip()
    creator_id = (body.get("creator_id") or "anonymous").strip()
    # Accept `creator_reasoning` (the documented field) or `reasoning` as a fallback.
    reasoning = (body.get("creator_reasoning") or body.get("reasoning") or "").strip()

    if not content_id or not reasoning:
        return jsonify(
            error="`content_id` and `creator_reasoning` are required"
        ), 400

    record = store.get_content(content_id)
    if record is None:
        return jsonify(error=f"no content with id {content_id}"), 404

    appeal_entry = {
        "creator_id": creator_id,
        "appeal_reasoning": reasoning,
        "original_classification": record["classification"],
        "original_confidence": record["confidence"],
        "original_p_ai": record["p_ai"],
    }
    appeals = record.get("appeals", [])
    appeals.append(appeal_entry)
    store.update_content(content_id, status="under_review", appeals=appeals)

    logged = store.append_audit(
        {
            "event": "appeal",
            "content_id": content_id,
            "creator_id": creator_id,
            "status": "under_review",
            "appeal_reasoning": reasoning,
            "original_classification": record["classification"],
            "original_confidence": record["confidence"],
            "original_p_ai": record["p_ai"],
        }
    )

    queue = store.content_under_review()
    position = next(
        (i + 1 for i, r in enumerate(queue) if r["content_id"] == content_id),
        len(queue),
    )

    return jsonify(
        content_id=content_id,
        status="under_review",
        message="Your appeal was received. A human reviewer will assess it.",
        queue_position=position,
        queue_size=len(queue),
        logged_seq=logged["seq"],
    ), 200


@app.get("/log")
def log():
    return jsonify(count=len(store.read_audit()), entries=store.read_audit())


@app.get("/appeals")
def appeals_queue():
    queue = store.content_under_review()
    view = [
        {
            "content_id": r["content_id"],
            "creator_id": r["creator_id"],
            "text_snippet": r["text_snippet"],
            "original_classification": r["classification"],
            "original_confidence": r["confidence"],
            "original_p_ai": r["p_ai"],
            "signals": r["signals"],
            "appeals": r.get("appeals", []),
        }
        for r in queue
    ]
    return jsonify(count=len(view), queue=view)


@app.get("/content/<content_id>")
def content(content_id: str):
    record = store.get_content(content_id)
    if record is None:
        return jsonify(error=f"no content with id {content_id}"), 404
    return jsonify(record)


@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify(
        error="rate limit exceeded",
        detail=str(e.description),
        hint="The /submit endpoint is limited to protect the LLM backend.",
    ), 429


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
