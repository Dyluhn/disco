# Source-Accuracy Testing — Grounding Verdict Fairness Baseline

**Date:** 2026-06-17
**Author:** Claude (independent adjudication)
**Question (Dylan):** How accurate is the citation / source-to-report
supported-vs-unsupported grading? Run 2–3 reports, check the accuracy grading,
then the reasoning, then determine if it graded fairly.

**TL;DR:** Across 3 fresh reports (132 graded claims) the verdicts are **fair
~88% of the time** *as a relevance signal*. But the headline finding is
architectural, not statistical: **the grader is not an entailment checker — it
is a relevance cross-encoder (`bge-reranker-base`).** "Supported" means *the
cited passage is on-topic for this claim*, **not** *the passage proves this
claim*. That distinction is the single most important thing to understand before
trusting the badge. The error pattern is mostly **over-crediting** precise
claims on topical match, with a secondary **under-crediting** bug on sentence
fragments and a handful of broken citation links.

---

## 1. How the grading actually works (verified in code)

- Each report sentence that ends in a `[[passage_id]]` citation becomes a
  *claim*. Uncited sentences are never graded (`streaming._verify_claims`,
  regex `_CLUSTER`).
- For each claim, the **premise** = the concatenated text of its cited
  passage(s); the **hypothesis** = the claim sentence.
- The verdict comes from `grounding.py:20-23`:
  `entail → supported`, `neutral → weak`, `contradict → unsupported`.
- **The "NLI" is `BAAI/bge-reranker-base`** — a *relevance* cross-encoder reused
  as an entailment proxy, sigmoid-squashed and thresholded
  (`local_encoders.py`): **score ≥ 0.5 → supported, ≤ 0.1 → unsupported, else
  weak**. (Confirmed active at runtime; `disco-config.json` has
  `encoders.remote: false`, so the in-process FastEmbed verifier is used, not the
  remote `bge-reranker-v2-m3` sidecar named in config.)
- **Per-claim verdicts are NOT persisted** — the report stores only a roll-up
  (`confidence` + `unsupported_count`). The grades here were faithfully
  *recomputed* by re-running the exact pipeline over the stored section markdown
  and cited-passage store (`scripts/extract_grounding.py`). Validation: the
  recomputed `unsupported_count` matched the report's stored count exactly on the
  reference report — so the recomputation reproduces the live grading.

### Why this matters: relevance ≠ entailment
A relevance scorer cannot tell apart:
- a claim the passage **states/entails** (true "support"), vs
- a claim that is merely **on the same topic** as the passage (false support), vs
- a claim the passage **contradicts** vs one that is simply **irrelevant** (both
  score low → both land in "unsupported", with no way to distinguish refutation
  from off-topic).

So **"unsupported" does not mean "the source refutes this"** — it means "this
passage isn't a relevant match for this sentence." And **"supported" does not
mean "this claim is true"** — only that the cited passage is topically close.

---

## 2. Method

- 3 fresh Deep Research reports run live on the `gpt-oss-120b` driver, chosen for
  different grounding difficulty:
  1. **HTTP/2 vs HTTP/3** (technical, well-documented) — 46 claims
  2. **Intermittent fasting vs calorie restriction** (contested clinical
     evidence) — 36 claims
  3. **Open-source vector databases** (product comparison, fuzzy) — 50 claims
- For every graded claim I extracted `{claim, verdict, entailment_score, full
  cited-passage text}` and **independently adjudicated** each grade against the
  **full** passage text (not a truncated view — truncation flipped 2 of my early
  calls, so full text is essential).
- Each grade was labelled **FAIR** (verdict matches whether the passage
  substantiates the claim), **OVER-CREDIT** (supported, but the passage doesn't
  state the claim's actual content), or **UNDER-CREDIT** (weak/unsupported, but
  the passage does substantiate it).
- Adjudication axis is *grounding fairness* — does the verdict reflect whether
  the **cited passage** backs the claim. (It is **not** a fact-check of whether
  the claim is true in the world; that's a separate, harder question the system
  does not attempt.)

---

## 3. Per-report results

### Report 1 — HTTP/2 vs HTTP/3 (46 claims)
Verdicts: **37 supported / 7 weak / 2 unsupported.**
My adjudication: **~40 fair, ~5 over-credit, ~1 under-credit (≈87% fair).**

- **Correctly supported (verbatim/clear):** #1 (TLS mandatory in QUIC — the
  passage is a literal comparison table), #5 (2-3 RTT → 1 RTT — verbatim), #18
  (Cloudflare HPACK 53%/1.4% — verbatim), #44/#45 (UDP firewall blocking —
  verbatim). These are exactly what grounding should reward.
- **Over-credits (supported on topical match, claim content absent from the full
  passage):**
  - #4 (0.974) — "connection IDs and **stateless reset** … single packet signals
    closure … UDP sockets released immediately." The two cited passages are about
    HOL blocking and QUIC-needs-TLS; **neither mentions connection teardown,
    stateless reset, or socket release.**
  - #30 (1.000) — "implementations often treat priority as a **hint rather than a
    guarantee**." Cited passage only says "HTTP/3 retains prioritization" —
    nothing about hint-vs-guarantee.
  - #37 (0.975) — "QUIC encrypts transport metadata … limiting **traffic-analysis
    attacks**." Cited passage is a generic protocol-overview intro; the
    traffic-analysis claim is absent.
  - #24, #35 (partial) — the *general* claim is supported but the *specifics*
    (priority weights 1-256; "50-70%" latency reduction) aren't in the passage.
    #35's primary cited passage is literally a **website nav menu**
    ("Industry / Role / CIO / ITOps…"); it scored 0.962 because the *second*
    passage is topically about TLS 1.3 performance.
- **Correction worth noting:** #6/#7 (FEC + "50% faster recovery") looked like
  over-credits on a truncated view but are **FAIR** — the full passage does say
  "forward error correction … recover up to 50% faster." (Aside: real QUIC
  dropped FEC, so the *claim* is dubious — but the **source says it**, so
  "supported" is the correct grounding verdict. Grounding measures
  source-support, not world-truth. Working as intended.)
- **Unsupported were fair:** #16, #29 cite passages that genuinely don't cover
  the claim (an HPACK passage for a HOL-liability claim; a generic TCP passage
  for a QUIC-flow-control-tuning claim). Correctly flagged.

### Report 2 — Intermittent fasting vs calorie restriction (36 claims)
Verdicts: **32 supported / 1 weak / 3 unsupported.**
My adjudication: **~32 fair, ~3 over-credit, 0 under-credit (≈89% fair).**
**Best-behaved report.**

- **Strong positive:** every hard quantitative claim is correctly supported
  against a verbatim passage — #0 (4:3 trial lost more weight), #3 (7.6% vs 5%,
  ~17 lb), #5 (19% vs 30% dropout), #1 ("99 RCTs in *The BMJ* … about as
  effective"), #2 (Cochrane 22 RCTs "nearly identical"), #23 (DCR adherence
  "decreases markedly over time"). This is exactly the behaviour you want on a
  contested topic — the numbers and study conclusions are tied to real sources.
- **All 3 "unsupported" are editorial *synthesis* sentences** ("this pattern
  suggests…", "underscores the importance of pairing IF with behavioral
  support…", "the convergence on these definitions enables…") that genuinely
  aren't in any cited passage. **Correctly flagged** — this is the feature
  working: the model's own connective/interpretive prose is honestly marked as
  not-source-backed.
- **Over-credits:** #16 (0.877, "heterogeneous risk-of-bias scores" cited to an
  obesity-prevalence *intro*) and #18 (0.990, "rarely reported adherence metrics
  or ITT analyses" cited to a passage about exercise modalities / adverse
  effects). Topical match, claim content absent.
- **Telling inconsistency:** #16 (supported 0.877) and #17 (unsupported 0.029)
  cite the **same** obesity-intro passage — one passed, one failed, for similarly
  unsupported claims. Pure scorer noise at the boundary.

### Report 3 — Open-source vector databases (50 claims)
Verdicts: **37 supported / 6 weak / 7 unsupported.**
My adjudication: **~44 fair, ~2 over-credit, ~3 under-credit + 2 broken-citation
(≈88% fair).** Most revealing report — shows both error directions and a strong
positive.

- **Strong positive — real discrimination:** #29/#31/#34 (supported 0.99+) and
  #30 (unsupported 0.010) all cite the **same** in-memory-vs-disk passage. The
  grader supported the three claims that match the passage's content
  (vertical scaling, persistence, cost) and failed the one whose specific content
  (buffer-pool / disk-I/O-scheduling CPU cost) isn't there. That's not luck —
  it's discriminating within one source.
- **Over-credits:** #22 (1.000, "hybrid search / scalar filtering" cited to a
  passage that only lists Milvus's distance *metrics*), #35 (0.877, TiDB
  "LangChain/LlamaIndex connectors … only solution that…" cited to a generic
  evaluation-criteria passage).
- **Under-credit (the segmentation bug):** #1 scored **0.001 / unsupported**
  despite its cited passage **literally listing the top-7** ("The top 7 vector
  databases in 2026 are Chroma, Pinecone, Weaviate, Faiss, Qdrant, Milvus, pgvector").
  Why it failed: the claim is a sentence **fragment** —
  ";the DataCamp overview also flags **them** among the top-seven…" — and once
  the segmenter splits it, "them" has no referent, so the reranker sees a vague
  fragment and scores it near-zero. Fragmented/pronoun-led claims are
  systematically under-credited.
- **Broken citations:** #45 and #46 scored **0.000 with NO resolved passage** —
  their `[[id]]` markers don't resolve to any passage in the report's store
  (yet #45 quotes text — "proportion of time … close to 100%" — that *is* present
  in passage `45f796_p2`, used by #44/#49). So a real citation links to the wrong
  / missing id, and the claim is graded against an empty premise. This is a
  pipeline data-integrity bug, not just a scorer issue.

---

## 4. Cross-report baseline

| Report | Claims | supported / weak / unsup | My fairness adjudication |
|---|---|---|---|
| HTTP/2 vs HTTP/3 | 46 | 37 / 7 / 2 | ~87% fair (≈5 over, ≈1 under) |
| Intermittent fasting | 36 | 32 / 1 / 3 | ~89% fair (≈3 over, 0 under) |
| Vector databases | 50 | 37 / 6 / 7 | ~88% fair (≈2 over, ≈3 under, 2 broken-cite) |
| **Total** | **132** | **106 / 14 / 12** | **≈88% fair** |

(Reference 4th report — "accuracy/latency for small LMs", 48 claims, 28/10/10 —
showed the same shape, including a precise numeric claim correctly failed when
cited to bibliographic boilerplate.)

**Verdict-mix observation:** ~80% of all claims are graded "supported." Given
the grader is relevance-based and over-credits, the true "the source substantiates
this exact claim" rate is **lower** than 80% — my adjudication suggests roughly
**8–12% of "supported" grades are over-credited** (topically relevant, specific
content absent).

---

## 5. Did it grade fairly? — Findings

1. **Fair as a *relevance* signal (~88%).** When a claim closely tracks its
   cited passage (verbatim numbers, definitions, direct quotes), the grade is
   reliably "supported" with high confidence (0.95–1.0). When the passage is
   clearly off-topic or boilerplate, it's reliably "unsupported" (<0.05). The
   middle band (0.1–0.5) is noisy.
2. **It is NOT an entailment/fact verdict.** "Supported" over-promises: it reads
   to a user as "the source proves this," but it only means "the cited passage is
   topically relevant." This is the biggest fairness gap — not that the grades
   are wrong, but that the **label implies more than the model measures.**
3. **Dominant error = over-crediting precise claims.** Specific numbers,
   mechanisms, and named features get "supported" when cited to a topically-related
   passage that doesn't actually state them (#4, #30, #37, #16, #18, #22, #35).
   The model rewards aboutness, not assertion.
4. **Secondary error = under-crediting fragments.** Sentence fragments and
   pronoun-led claims ("; the … overview also flags **them** …") score
   artificially low because segmentation strips their referent (#1). This
   penalises perfectly well-sourced claims for a formatting reason.
5. **No contradiction detection.** "Unsupported" conflates "refuted" and
   "irrelevant." On a contested topic (fasting) this means a claim the literature
   *disagrees with* and a claim that's merely *off-topic for its citation* get the
   same badge.
6. **Citation-resolution bugs exist** (#45/#46): some `[[id]]` markers don't
   resolve to the passage store and are graded against nothing → 0.000. Worth a
   data-integrity fix independent of the scorer.
7. **Genuine positives:** correct discrimination within a single shared passage
   (#29-34 vs #30); honest flagging of the model's own synthesis/connective prose
   as unsupported (Report 2's 3 unsupported); correctly grading source-support
   even when the source itself is wrong (#6/#7 FEC).

---

## 6. Recommendations (for a future fix pass — not yet implemented)

- **Re-label the UI honestly.** "Supported" → consider "Relevant source" / "On-topic
  citation," or keep "supported" but add a tooltip: *"the cited passage is
  topically matched; this is a relevance check, not a fact-check."* This closes
  the over-promise gap (#2 above) at zero model cost.
- **Use a real entailment model for the verdict** (the config already names
  `bge-reranker-v2-m3` on the remote path; a true NLI head — e.g. an mDeBERTa-MNLI
  — would let "unsupported" actually mean *refuted/unstated* and enable true
  contradiction surfacing). Relevance reranker → recall; NLI → the verdict.
- **Fix claim segmentation for fragments** — stitch leading-`;`/pronoun fragments
  to their antecedent sentence before grading (kills the #1 under-credit class).
- **Fix the broken `[[id]]` resolution** (#45/#46) so claims are never
  graded against an empty premise; surface unresolved citations distinctly
  ("citation not found") rather than as "weak."
- **Persist per-claim verdicts + scores** in the report event so this audit
  doesn't require recomputation and the UI hover-card can show the real score.
- **Calibrate the thresholds** — the 0.5/0.1 cutoffs produce a noisy 0.1–0.5
  "weak" band; consider tuning against a small labelled set.

---

## 7. Can "iterative mode" work? (re-research unsupported sections until supported)

The dream: use the grounding verdicts as a control signal — scrap weak/unsupported
sections, re-research, repeat until supported. To test whether *any* available
grader can be that gate, I re-graded the **same 130 claims** with two more graders
and spot-checked a third.

### Three-grader comparison
| Grader | What it is | supported / weak / unsup (of 130) |
|---|---|---|
| **bge-reranker-base** (current) | relevance cross-encoder | **106 / 12 / 12** |
| **mDeBERTa-MNLI** (the `:8092` sidecar) | true 3-way NLI | **11 / 116 / 3** |
| **LLM-judge** (gpt-oss-120b) | spot-checked 8 disputed claims | nuanced (see below) |

- **Reranker vs true NLI agree only 18%.** 93 of the reranker's "supported"
  became "neutral/weak" under mDeBERTa; **zero** went the other way.
- **Neither embedding model is a usable convergence gate:**
  - Reranker **over-credits** (80% "supported") → an iterative loop would
    **stop too early**, declaring victory while claims are only topically grounded.
  - mDeBERTa **under-credits** — it marks only 8% supported and calls a clean
    paraphrase ("encryption mandatory in QUIC, TLS built-in") *neutral*. It is a
    280 M sentence-pair model that truncates at 512 tokens, so it is the wrong
    tool for passage-length, multi-part, paraphrased research claims. An iterative
    loop gated on it would **never converge** — it would burn all iterations with
    ~92% of claims still "weak."

### The LLM-judge is the viable gate
Spot-checked the 8 claims where the embedding graders failed (premise = cited
passage, claim = the sentence; gpt-oss-120b, strict 3-way SUPPORTED/PARTIAL/UNSUPPORTED):

| Case (independent adjudication) | reranker | mDeBERTa | LLM-judge |
|---|---|---|---|
| HTTP #1 genuine support | supported | weak | **SUPPORTED** ✓ |
| HTTP #4 over-credit (stateless reset) | supported | weak | **UNSUPPORTED** ✓ |
| HTTP #30 over-credit (priority=hint) | supported | weak | **UNSUPPORTED** ✓ |
| HTTP #37 over-credit (traffic analysis) | supported | weak | **UNSUPPORTED** ✓ |
| FAST #18 over-credit (ITT/adherence) | supported | weak | **UNSUPPORTED** ✓ |
| VDB #1 under-credit (top-7 *fragment*) | unsupported | weak | **PARTIAL** ✓ |
| FAST #3 genuine support | supported | weak | PARTIAL (≈ok) |
| VDB #22 over-credit (hybrid search) | supported | supported | SUPPORTED (miss) |

The LLM-judge **matched the independent human-style adjudication on ~7/8** — it
caught the over-credits the reranker waved through, rescued the fragment the
reranker wrongly failed, and expresses a **PARTIAL** middle state the embedding
models structurally can't. (Caveat: gpt-oss-120b is a reasoning model — it needs a
generous `max_tokens` and robust verdict parsing; an 80-token cap returned empty
content. One case leaked its reasoning instead of a clean tag.)

### Verdict on iterative mode — VIABLE, with the right gate and loop shape
1. **Don't gate on an embedding NLI/reranker.** The reranker stops too early; the
   sidecar NLI never converges. This is *why* the naive "loop until all supported"
   would have failed — not because the idea is wrong, but because the signal was.
2. **Use an LLM-judge for the per-claim support verdict** (claim × cited passage →
   SUPPORTED / PARTIAL / UNSUPPORTED + reason). It handles paraphrase, multi-part
   claims, and distinguishes "irrelevant" from "contradicted." The driver LLM is
   already in the loop; one judge call per claim (batchable per section) is cheap
   next to a re-research iteration.
3. **Loop shape that dodges the convergence trap:**
   - *Rank and target* — re-research the lowest-scoring sections, not "everything
     not green." Use the reranker (cheap) to rank, the LLM-judge (accurate) to gate.
   - *Cap iterations* (≤5, as specced) and accept "improved," not "perfect."
   - *Converge* when the section has no UNSUPPORTED claims (PARTIAL allowed), or
     iterations exhausted — never require 100% SUPPORTED (no grader supports that).
   - *Re-research targets the claim's gap*: an UNSUPPORTED verdict + the judge's
     reason ("source never mentions X") is a ready-made focused search query.
4. **Bonus:** the same LLM-judge replaces the misleading relevance badge in §6's
   recommendations — one mechanism fixes both the display honesty and the loop gate.

**Bottom line for the product idea:** iterative-research-until-supported is
buildable and would genuinely raise grounding quality — but its engine is an
**LLM-judge**, not the embedding grounding model. The current supported/unsupported
verdicts are fine for *ranking* what to fix; they are not trustworthy enough to be
the *gate* that decides "done."

---

## Appendix — Reproduce

```bash
cd /var/home/dylan/projects/disco
PYTHONPATH=packages/core/src:packages/retrieval/src:packages/tools/src:packages/agent-server/src:packages/app-server/src \
  .venv/bin/python3 scripts/extract_grounding.py <conversation_id>
```

Reports used (disco.db, surface=deep_research, FINISHED):
- HTTP/2 vs HTTP/3 — `conv_e4b68b3d0901417d946377820bfd0c3b`
- Intermittent fasting — `conv_2118d9f5cb5d46cdaf96e26dd6dd74cb`
- Vector databases — `conv_3741f59ee54d402b81567fc914235508`
- Reference (small-LM accuracy/latency) — `conv_92f032c16ee8450dbd2ea6ace944988a`

Grader: in-process `FastEmbedNLIVerifier` = `BAAI/bge-reranker-base`,
thresholds entail≥0.5 / contradict≤0.1 (`packages/retrieval/src/disco/retrieval/
grounding.py`, `local_encoders.py`).
