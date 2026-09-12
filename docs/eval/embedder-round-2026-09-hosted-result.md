# Embedder round, hosted arm: result (2026-09-12)

The run of `docs/eval/embedder-round-2026-09-hosted.json`: Qwen3-Embedding-8B,
served by Scaleway, against the local incumbent, both at the fleet width of
1024. LoCoMo, 2 conversations, k=10, 230 paired questions. The
pre-registration and the promotion rule were fixed before the run.

## What ran

```
candidate                dim  trunc  prefix  recall@10     MRR  tok/query  verdict
bge-m3                  1024      -       -      0.726   0.494        468  BASELINE
qwen3-emb-8B-hosted        ?      -   query      0.726   0.512        484  no

Paired against bge-m3:
system                    n   recall  Δrecall  McNemar p     MRR    ΔMRR            Δ95%CI
qwen3-emb-8B-hosted     230    0.726   +0.000     1.0000   0.512  +0.018 [-0.003,+0.055]

single-hop Δrecall: +0.026 over 114 questions
```

Verdict: **no promotion**. bge-m3 stays the local tier's embedder.

## What the numbers say, which is not what the paper led us to expect

**Recall is identical. Not close: equal.** 0.726 against 0.726, Δ = +0.000,
McNemar p = 1.0000. That is not a model failing to help; it is a model whose
discordant pairs cancel exactly. The 8B finds things bge-m3 misses and misses
things bge-m3 finds, in equal number, and at k=10 the two cover the same
share of the corpus.

**What it does buy is ORDERING.** MRR +0.018 and single-hop recall +0.026,
with the pooled recall flat, is the signature of a model that puts the right
passage higher inside a set it was already retrieving. The MRR interval
[-0.003, +0.055] contains zero, so even that is not established on 230
questions.

**This is the opposite shape of what the COLM 2026 paper measures.** It gives
the 8B band +11.7 on MTEB(LLM) retrieval; here, on conversational memory at
k=10, the same band buys nothing in coverage. Either the gain does not
transfer from MTEB's task mix to this one, or it lives at a k this workload
does not use. The paper's number is not wrong; it is about a different
question than the one production asks.

**And that redirects the next step.** If the 4-8B band's contribution is
ordering rather than coverage, then the lever for it is the reranker (task
f0d24fdb), which reorders a shortlist the incumbent already retrieved, for
the cost of a cross-encoder pass on ten candidates. Re-embedding the whole
corpus through a hosted 8B to buy ordering would be paying the expensive
price for the cheap half of the problem.

## Cost

~46k input tokens for the candidate's whole pass (788 document embeds + 230
query embeds, the queries carrying a 25-token instruction prefix). At
Scaleway's EUR 0.10 per million that is under half a cent, and inside the
1M-token free tier. It is an estimate, not a measurement: `usage_record` is
empty because the metering seam records for billable workspaces and the
bench's throwaway orgs are not billable.

Only the candidate went over the network. The incumbent ran locally, as it
does in production, and cost nothing.

## Three operational facts the run established

- **The base URL must carry the project id.** `https://api.scaleway.ai/v1/...`
  answers 403 `insufficient permissions` even for a key whose default project
  is the intended one; `https://api.scaleway.ai/<project-id>/v1/...` works.
  The `scaleway_base_url` default is the path-less form.
- **`GenerativeApisModelAccess` is not enough**, despite reading as if it
  were ("Query Generative APIs Models and Deployments"): both `/v1/models`
  and `/v1/embeddings` refuse under it. `GenerativeApisFullAccess` is the
  one.
- **The endpoint does not apply Qwen3's instruction prefix for you.** The
  same query bare and prefixed came back at 7 against 25 tokens, cosine
  0.705. So the prefix declared in the spec is ours to apply, it is applied
  exactly once, and the pre-registration's open question is closed. This
  mattered: without it the candidate would have been measured outside its
  training configuration, which is precisely the defect that made the July
  round unusable.

## Taken together with the local arm

Two arms, two verdicts, one conclusion. bge-m3 survives a comparison with a
model thirteen times its size at the same width, with the instruction prefix
applied correctly in both. The 0.6B candidate gains recall inside the noise
and loses MRR; the 8B gains MRR inside the noise and gains no recall at all.

The declared limit stands for both: LoCoMo is English conversational memory,
and this workload is mixed Italian and English with proper nouns and entity
codes. Neither arm says anything about Italian.
