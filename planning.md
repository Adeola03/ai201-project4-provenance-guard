# Provenance Guard — Planning & Specification

This document is written **before** implementation. It is the contract every
piece of code implements against, and the reference I hand to AI tools when
generating implementation code in Milestones 3–5.

---

## Milestone 1 — Architecture Narrative

### The path of a single piece of text

A creator (or a platform integration acting on their behalf) sends a piece of
text to `POST /submit`. Here is everything it touches, in order:

1. **Rate limiter** (`flask-limiter`) — gates the request. If the caller has
   exceeded the per-minute or per-day budget, the text never reaches detection
   and the caller gets `429 Too Many Requests`.
2. **Submission endpoint** (`app.py`) — validates that `text` is present and
   non-trivial (≥ a minimum word count), assigns a `content_id`, and records
   the creator id.
3. **Signal 1 — Stylometric heuristics** (`detection.py`, pure Python) —
   computes measurable statistical properties of the text (sentence-length
   variance / "burstiness", type-token ratio, punctuation density, etc.) and
   maps them to a probability `p_style` that the text is AI-generated.
4. **Signal 2 — LLM classifier** (`detection.py`, Groq `llama-3.3-70b-versatile`)
   — asks a model to holistically judge whether the text reads as human- or
   AI-written and to return a probability `p_llm` plus a one-line rationale. If
   the API call fails, the pipeline degrades gracefully to stylometric-only with
   a flagged, reduced confidence.
5. **Confidence scoring** (`detection.py`) — combines `p_style` and `p_llm` into
   a single `p_ai` via a weighted average, then derives a **confidence** (how
   sure we are of the call we made) and a **classification**
   (`likely_ai` / `likely_human` / `uncertain`).
6. **Transparency label** (`labels.py`) — turns the classification + confidence
   into one of three plain-language label variants with the real percentage
   filled in. This is the human-readable artifact a reader would see.
7. **Store + audit log** (`store.py`) — persists the full content record and
   appends a structured audit entry (signals, scores, confidence, label,
   status). Everything is written to JSON so it survives restarts.
8. **Response** — the endpoint returns the `content_id`, classification,
   `p_ai`, confidence, the label (variant + exact text), and the raw signal
   breakdown so the result is inspectable, not a black box.

### The path of an appeal

A creator who disagrees sends `POST /appeal` with the `content_id` and their
reasoning. The endpoint looks up the original record, flips its status to
`under_review`, and appends an `appeal` entry to the audit log **alongside the
original decision** (the appeal does not erase or overwrite the original — it
annotates it). A human reviewer can later read the queue via `GET /appeals`.
Automated re-classification is intentionally out of scope; a human decides.

### The false-positive scenario (drives Milestone 2 decisions)

A human poet writes in a spare, repetitive, low-vocabulary style. The
stylometric signal sees low burstiness and a low type-token ratio — both of
which correlate with AI text — and pushes `p_style` high. This is exactly the
failure mode the design must absorb:

- The **LLM signal** is an independent check; if it reads the poem as human, the
  combined `p_ai` lands in the middle band and the system returns **uncertain**
  rather than a confident (wrong) "AI" verdict.
- The **confidence score** narrows toward 0.5 when the two signals disagree, so
  the label honestly communicates doubt instead of false certainty.
- The **appeal workflow** is the human's recourse: they contest, the status
  becomes `under_review`, and the disagreement is logged for a reviewer.

This is why the system must never return a binary flip at 0.5, why signals must
be genuinely independent, and why "uncertain" is a first-class outcome.

---

## Architecture

### Submission flow

```
                         429 if over budget
                        ┌──────────────────┐
   raw text             │                  │
 ─────────────►  [ Rate Limiter ]  ──► [ POST /submit ]
                                              │ raw text
                                              ▼
                                    ┌───────────────────────┐
                                    │  Detection pipeline    │
                          raw text  │                        │
                          ┌─────────┤  Signal 1: Stylometry  │── p_style (0–1)
                          │         │  (pure Python)         │      │
                          │         └───────────────────────┘      │
                          │         ┌───────────────────────┐      │
                          └────────►│  Signal 2: LLM (Groq)  │── p_llm (0–1) + rationale
                                    └───────────────────────┘      │
                                              │ p_style, p_llm      │
                                              ▼                     │
                                    [ Confidence scoring ] ◄────────┘
                                              │ p_ai, confidence, classification
                                              ▼
                                    [ Transparency label ]  ── variant + exact text
                                              │
                                              ▼
                                    [ Store + Audit log ]  ── append structured entry
                                              │
                                              ▼
                                    JSON response to caller
```

### Appeal flow

```
  content_id + reasoning
 ──────────────────────►  [ POST /appeal ]
                                │ look up original record
                                ▼
                      [ status → "under_review" ]
                                │ original decision + appeal reasoning
                                ▼
                      [ Audit log: append "appeal" entry ]
                                │
                                ▼
                      JSON response (status, queue position)
                                                  ▲
                          GET /appeals  ──────────┘  (human reviewer queue)
```

**Narrative.** The submission flow takes raw text, runs it through two
independent signals (structural stylometry and semantic LLM judgment), combines
them into a calibrated `p_ai` and a confidence, renders a plain-language label,
and logs the whole decision before responding. The appeal flow lets a creator
contest a decision: it never re-runs detection automatically — it flips status to
`under_review` and logs the contest next to the original decision for a human.

---

## Milestone 2 — Specification (the five questions)

### 1. Detection signals

Two **genuinely independent** signals — one structural, one semantic:

#### Signal 1 — Stylometric heuristics (pure Python, `p_style`)
**Measures:** statistical regularity of the prose. Sub-metrics:
- **Burstiness** — coefficient of variation of sentence lengths. Human writing
  mixes long and short sentences; AI tends toward uniform mid-length sentences.
- **Type-token ratio (TTR)** — vocabulary diversity (unique words / total
  words). AI text often reuses a smaller, "safe" vocabulary.
- **Punctuation density** — punctuation marks per word. Humans use dashes,
  semicolons, parentheses, ellipses idiosyncratically; AI is more even.
- **Repetition** — fraction of repeated bigrams. AI loops on phrasings.

**Why it differs human vs AI:** AI decoding (especially with low temperature)
optimizes for locally-likely tokens, which produces lower variance, lower
lexical diversity, and smoother punctuation than spontaneous human writing.

**Output:** each sub-metric is normalized to a 0–1 "AI-likeness" partial score;
their weighted mean is `p_style ∈ [0, 1]`. Deterministic and explainable.

**Blind spot:** it is purely surface statistics. Heavily-edited human prose,
formal/technical writing, very short texts, poetry, and lists all look
"uniform" and inflate `p_style` — false positives. It also has no idea what the
text *means*, so AI text deliberately written with varied sentence length fools
it.

#### Signal 2 — LLM classifier (Groq `llama-3.3-70b-versatile`, `p_llm`)
**Measures:** holistic semantic + stylistic coherence — does this *read* like a
person wrote it? Tone shifts, lived-in specificity, argumentative idiosyncrasy
vs. the hedged, balanced, "as an overview" register of generated text.

**Why it differs:** captures meaning-level cues stylometry can't — generic
framing, suspiciously even-handed structure, absence of genuine voice.

**Output:** the model is prompted to return strict JSON: `{"p_ai": 0–1,
"rationale": "..."}`. We parse `p_ai` as `p_llm` and keep the rationale for the
audit log.

**Blind spot:** non-deterministic and can be confidently wrong; vulnerable to
adversarial/edited text; reflects the model's biases (e.g. flags non-native
English or very polished writing as AI). It is also a dependency that can fail
or rate-limit — so the pipeline must degrade gracefully.

#### Combining them
```
p_ai = W_STYLE * p_style + W_LLM * p_llm        # W_STYLE = 0.30, W_LLM = 0.70
```
The LLM gets more weight because, empirically, it is the stronger and
better-separated signal (≈0.05–0.20 on human text, ≈0.85–0.90 on AI text),
while stylometry is weaker and noisier — but stylometry keeps a real 30% vote so
a single bad LLM call can't dominate, it tempers the LLM toward "uncertain" when
the two disagree, and it is the sole fallback if the LLM is down. If the LLM call
fails, `p_ai = p_style` and the result is marked `degraded` with a confidence
penalty.

### 2. Uncertainty representation

`p_ai` is the probability the text is AI-generated. From it we derive two
reader-facing numbers:

- **direction** = `ai` if `p_ai ≥ 0.5` else `human`.
- **confidence** = `max(p_ai, 1 − p_ai)` → ranges **0.5–1.0**, the probability
  mass behind whichever call we made.

**What a confidence of 0.6 means:** the system leans one way but is barely past a
coin flip — explicitly *not* sure. It maps to the **uncertain** label.

**Mapping raw outputs to a calibrated score:** the weighted blend pulls the
combined value toward 0.5 whenever the two independent signals disagree, which is
exactly when we *should* be unsure. We additionally apply a `−0.1` confidence
penalty in `degraded` (LLM-down) mode, and cap confidence at 0.82 for very short
texts (< 40 words) where the statistics are too noisy to be sure. We validate
that the score is meaningful — not just a number — with a calibration script
(`calibrate.py`) over labeled clearly-human / clearly-AI / ambiguous samples,
checking that clear cases land at the extremes and ambiguous ones land in the
middle.

**Thresholds (single source of truth, lives in `detection.py`):** with
`confidence = max(p_ai, 1 − p_ai)`, the threshold is symmetric in `p_ai`:

| Condition                                | Classification | Label variant         |
|------------------------------------------|----------------|-----------------------|
| `p_ai ≥ 0.65` (confidence ≥ 0.65, AI)    | `likely_ai`    | High-confidence AI    |
| `p_ai ≤ 0.35` (confidence ≥ 0.65, human) | `likely_human` | High-confidence human |
| `0.35 < p_ai < 0.65` (confidence < 0.65) | `uncertain`    | Uncertain             |

So a 0.95 confidence is a decisive label; a 0.58 confidence is "uncertain" — a
meaningfully different outcome, never a hard flip at 0.5. Empirically (see
`calibrate.py`), clearly-human text lands ~0.89–0.93 confidence, clearly-AI text
~0.70–0.77, and text where the two signals disagree lands ~0.58–0.60 →
uncertain. The 0.65 threshold was chosen from this measured spread so all three
variants are robustly reachable rather than perched on a boundary.

### 3. Transparency label design (exact text)

Each label embeds the real confidence as a whole percent (`round(confidence*100)`).

**High-confidence AI** (`likely_ai`):
> 🤖 **Likely AI-Generated** — Our analysis indicates this text was most likely
> produced with the help of an AI system (about **{pct}%** confidence). This is
> an automated estimate, not a certainty. If you created this and disagree, you
> can appeal and a human will review it.

**High-confidence human** (`likely_human`):
> ✍️ **Likely Human-Written** — Our analysis found no strong signs of AI
> generation in this text (about **{pct}%** confidence). This is an automated
> estimate, not a guarantee of authorship.

**Uncertain** (`uncertain`):
> ❓ **Attribution Uncertain** — Our analysis could not confidently tell whether
> this text was written by a person or an AI (only about **{pct}%** confidence
> either way). Please treat its origin as unverified.

### 4. Appeals workflow

- **Who:** the content's creator (identified by the `creator_id` they supplied
  at submission; enforced loosely for this prototype by matching the id).
- **What they provide:** the `content_id` and free-text `reasoning` explaining
  why they believe the classification is wrong.
- **What the system does:** looks up the original record; if found, sets its
  `status` from `classified` → `under_review`, stores the appeal (reasoning +
  timestamp + appellant) on the record, and appends an `appeal`-type entry to
  the audit log that references the original `p_ai`, confidence, and
  classification. No re-classification happens automatically.
- **What a reviewer sees:** `GET /appeals` returns the queue of `under_review`
  items, each showing the original text, the original decision and signals, and
  the creator's stated reasoning — enough to make a human judgment.

### 5. Anticipated edge cases (where the system handles content poorly)

1. **Sparse, repetitive poetry.** A minimalist poem ("so much depends / upon …")
   has low sentence-length variance, tiny vocabulary, and sparse punctuation —
   the stylometric signal reads it as AI. The LLM signal and the wide uncertain
   band are the mitigations, but this is a known false-positive risk and a prime
   appeal candidate.
2. **Very short submissions.** Under ~40 words, every statistic is noise (a
   2-sentence text has almost no measurable burstiness). We enforce a minimum
   word count and, below a comfortable threshold, cap confidence so the system
   refuses to be sure about a sample that's too small to judge.
3. *(also handled)* **Heavily human-edited AI text / AI-written-to-look-human.**
   Both signals can be defeated by editing; we accept this and lean on
   "uncertain" + appeals rather than pretending to certainty.

---

## AI Tool Plan

How I use this spec to drive AI code generation across M3–M5.

### M3 — Submission endpoint + first signal
- **Spec sections provided:** "Detection signals → Signal 1 (Stylometry)" + the
  Architecture diagram + the API contract.
- **Ask for:** a Flask app skeleton with `POST /submit` (validation, id
  assignment) and a pure-Python `stylometric_signal(text) -> {p_style, metrics}`
  function.
- **Verify:** call `stylometric_signal` directly on a handful of clearly-human
  and clearly-AI paragraphs in a scratch script *before* wiring it into the
  endpoint; confirm AI-ish text scores higher than human-ish text.

### M4 — Second signal + confidence scoring
- **Spec sections provided:** "Detection signals → Signal 2 (LLM)" +
  "Uncertainty representation" (thresholds table) + the diagram.
- **Ask for:** `llm_signal(text) -> {p_llm, rationale}` (strict-JSON Groq call
  with graceful failure) and `combine(p_style, p_llm) -> {p_ai, confidence,
  classification}` implementing the thresholds table.
- **Verify:** run `calibrate.py` over labeled samples — confirm clear cases land
  near 0/1 confidence-extremes and ambiguous text lands in the uncertain band,
  i.e. scores vary *meaningfully* between clearly-AI and clearly-human text.

### M5 — Production layer (labels, appeals, logging, rate limiting)
- **Spec sections provided:** "Transparency label design" (the three exact
  variants) + "Appeals workflow" + the diagram.
- **Ask for:** `build_label(classification, confidence) -> {variant, text}`, the
  `POST /appeal` endpoint, JSON-backed audit logging, and `flask-limiter` config.
- **Verify:** submit crafted inputs that reach all three label variants; submit
  an appeal and confirm status becomes `under_review` and an `appeal` entry is
  logged; hammer `/submit` to confirm the limiter returns `429`.

> Stretch features: update this section before starting any of them. (Current
> build targets required features M1–M5 only.)
