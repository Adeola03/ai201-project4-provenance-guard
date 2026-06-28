"""Transparency label generation.

Turns a (classification, confidence) pair into the exact plain-language text a
reader would see on the platform. Three variants, defined verbatim in
planning.md: high-confidence AI, high-confidence human, and uncertain.
"""

from __future__ import annotations

_TEMPLATES = {
    "likely_ai": (
        "🤖 Likely AI-Generated — Our analysis indicates this text was most "
        "likely produced with the help of an AI system (about {pct}% "
        "confidence). This is an automated estimate, not a certainty. If you "
        "created this and disagree, you can appeal and a human will review it."
    ),
    "likely_human": (
        "✍️ Likely Human-Written — Our analysis found no strong signs of AI "
        "generation in this text (about {pct}% confidence). This is an "
        "automated estimate, not a guarantee of authorship."
    ),
    "uncertain": (
        "❓ Attribution Uncertain — Our analysis could not confidently tell "
        "whether this text was written by a person or an AI (only about {pct}% "
        "confidence either way). Please treat its origin as unverified."
    ),
}

_HEADLINES = {
    "likely_ai": "Likely AI-Generated",
    "likely_human": "Likely Human-Written",
    "uncertain": "Attribution Uncertain",
}


def build_label(classification: str, confidence: float) -> dict:
    """Return {variant, headline, text} for a classification + confidence.

    confidence is a 0-1 float; it is rendered as a whole percent in the text.
    """
    if classification not in _TEMPLATES:
        raise ValueError(f"unknown classification: {classification!r}")
    pct = round(confidence * 100)
    return {
        "variant": classification,
        "headline": _HEADLINES[classification],
        "text": _TEMPLATES[classification].format(pct=pct),
    }
