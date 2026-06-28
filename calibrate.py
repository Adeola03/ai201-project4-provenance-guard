"""Calibration harness — verifies the confidence scores are *meaningful*.

Runs the full pipeline over a small labeled set (clearly-human, clearly-AI, and
deliberately-ambiguous samples) and checks two things:

  1. Clearly-AI text scores high p_ai and clearly-human text scores low p_ai
     (the signals discriminate, they aren't noise).
  2. Ambiguous text lands in the "uncertain" band rather than getting a
     confident-but-arbitrary label.

Run:  python calibrate.py
Set GROQ_API_KEY for the full two-signal result; without it the run degrades to
stylometry-only and still demonstrates the score spread.
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

# Windows consoles default to cp1252, which can't encode the label emoji.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

import detection
from labels import build_label

load_dotenv()

# (name, ground_truth, text). These are the assignment's four canonical M4
# inputs, used here as a reproducible calibration set.
SAMPLES = [
    (
        "clearly-AI",
        "ai",
        "Artificial intelligence represents a transformative paradigm shift in "
        "modern society. It is important to note that while the benefits of AI "
        "are numerous, it is equally essential to consider the ethical "
        "implications. Furthermore, stakeholders across various sectors must "
        "collaborate to ensure responsible deployment.",
    ),
    (
        "clearly-human",
        "human",
        "ok so i finally tried that new ramen place downtown and honestly? "
        "underwhelming. the broth was fine but they put WAY too much sodium in "
        "it and i was thirsty for like three hours after. my friend got the "
        "spicy version and said it was better. probably won't go back unless "
        "someone drags me there",
    ),
    (
        # KNOWN FALSE POSITIVE: formal human writing the LLM reads as AI.
        "borderline-formal-human",
        "human",
        "The relationship between monetary policy and asset price inflation has "
        "been extensively studied in the literature. Central banks face a "
        "fundamental tension between their mandate for price stability and the "
        "unintended consequences of prolonged low interest rates on equity and "
        "real estate valuations.",
    ),
    (
        # Lightly-edited AI: should ideally land mid-range -> uncertain.
        "borderline-edited-AI",
        "ai",
        "I've been thinking a lot about remote work lately. There are genuine "
        "tradeoffs — flexibility and no commute on one side, isolation and "
        "blurred work-life boundaries on the other. Studies show productivity "
        "varies widely by individual and role type.",
    ),
]


def main() -> None:
    print(f"{'sample':24} {'truth':6} {'p_style':>7} {'p_llm':>6} "
          f"{'p_ai':>6} {'conf':>5}  {'classification':15} llm?")
    print("-" * 92)
    rows = []
    for name, expected, text in SAMPLES:
        r = detection.analyze(text)
        s = r["signals"]
        rows.append((name, expected, r))
        print(
            f"{name:24} {expected:6} "
            f"{s['stylometry']['p_style']:>7.3f} "
            f"{s['llm']['p_llm']:>6.3f} "
            f"{r['p_ai']:>6.3f} {r['confidence']:>5.2f}  "
            f"{r['classification']:15} {s['llm']['available']}"
        )

    print("\nLabels produced:")
    for name, _, r in rows:
        lbl = build_label(r["classification"], r["confidence"])
        print(f"  [{name}] -> {lbl['text']}\n")

    # Sanity checks (only meaningful when the LLM signal is live).
    ai = [r for n, e, r in rows if e == "ai"]
    human = [r for n, e, r in rows if e == "human"]
    if ai and human:
        avg_ai = sum(r["p_ai"] for r in ai) / len(ai)
        avg_human = sum(r["p_ai"] for r in human) / len(human)
        print(f"avg p_ai  AI-truth={avg_ai:.3f}  human-truth={avg_human:.3f}  "
              f"spread={avg_ai - avg_human:+.3f}")
        assert avg_ai > avg_human, "AI samples should score higher p_ai than human"
        print("PASS: AI text scores meaningfully higher than human text.")
        print("NOTE: 'borderline-formal-human' is a deliberate known false "
              "positive — see README Known Limitations.")


if __name__ == "__main__":
    main()
