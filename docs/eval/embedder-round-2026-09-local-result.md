# Embedder round, local arm: result (2026-09-12)

The run of `docs/eval/embedder-round-2026-09-local.json`, its verdict, and the
two things it settles that the table does not say out loud. LoCoMo, 2
conversations, k=10, 230 paired questions; the pre-registration and the
promotion rule are in the spec file and were fixed before the run.

## What ran

```
EMBEDDER ROUND  dataset=locomo  k=10  instances=2  scored=230  baseline=bge-m3

candidate                dim  trunc  prefix  recall@10     MRR  tok/query  verdict
bge-m3                  1024      -       -      0.726   0.492        468  BASELINE
qwen3-emb-0.6B          1024      -   query      0.739   0.480        467  no

Paired against bge-m3 (exact McNemar; cluster-bootstrap CI on ΔMRR, clusters = instance):
system                    n   recall  Δrecall  McNemar p     MRR    ΔMRR            Δ95%CI
qwen3-emb-0.6B          230    0.739   +0.013     0.7283   0.480  -0.012 [-0.019,+0.001]

single-hop Δrecall: -0.026 over 114 questions
```

Verdict: **no promotion**. bge-m3 stays the local tier's embedder. The rule
stopped at its first clause (MRR did not improve), and every later clause
would have stopped it too: single-hop went down, and p=0.73 puts the recall
delta nowhere near the 0.05 threshold.

## The two things this settles

**1. The July round's method was wrong, and it cost Qwen3 about what you
would expect.** July measured Qwen3-Embedding-0.6B at 0.713 recall, WITHOUT
its instruction prefix, because the `Embedder` seam had no way to express
one. The same model with the prefix its own checkpoint declares scores
0.739. So the missing prefix was worth roughly +0.026 of recall to that
model, which is twice the gap the round was trying to resolve, and every
instruction-tuned candidate July scored was scored low for the same reason.
That was the open methodological question; it is now closed, and the answer
is that the method mattered.

**2. It did not change the conclusion.** Corrected, Qwen3-0.6B still loses on
MRR and on single-hop, and its recall gain is inside the noise. bge-m3 was
not winning July's round by accident of measurement. Two independent reasons
to believe the two models are simply in the same band: the COLM 2026
comparison puts them at 44.1 against 44.3 on retrieval, and this round cannot
distinguish them on 230 paired questions.

Worth stating plainly because it is the less comfortable half: the correction
made the round MORE certain of the incumbent, not less.

## Incidental evidence

- **The harness reproduces July's baseline exactly.** bge-m3 at 0.726 recall
  here, 0.726 in the 2026-07-03 round (nota 3276b266 §4). Same dataset, same
  two conversations, a rewritten scoring path and a rebuilt embedder seam in
  between. A number that survives that much change underneath it is a number
  the harness is measuring rather than producing.
- **The query/document asymmetry works end to end against a real
  checkpoint.** The prefix in the table was read from
  `config_sentence_transformers.json`, not configured: `LocalEmbedder`
  resolved it, applied it to the query side only, and the round reported what
  was applied. Nothing in the run declared it.
- **`tok/query` is flat** (468 against 467), so the two candidates were
  served the same retrieval budget and the recall difference is not a
  context-size artifact.

## What this does NOT say

The declared limit of the pre-registration stands: LoCoMo is English
conversational memory, and this workload is mixed Italian and English with
proper nouns and entity codes. This round decides the local tier on a public
benchmark; it says nothing about Italian.

It also says nothing about the 4-8B band, which is not adoptable in the local
tier at any score (~16GB and ~30GB of weights against a backend pod already
OOMKilled by bge-m3, task 91c36656). That band is the hosted arm,
`embedder-round-2026-09-hosted.json`, and it has not run.

## Reproducing it

Never against production: the run creates workspaces and writes blobs.

```
docker run -d --rm --name round-db -e POSTGRES_USER=mycelium \
  -e POSTGRES_PASSWORD=mycelium -e POSTGRES_DB=mycelium \
  -p 5457:5432 pgvector/pgvector:pg16
# deploy/local/bootstrap_roles.sql, then alembic upgrade head, then:
uv run --extra local-embedder python scripts/eval_public_bench.py \
  --dataset locomo --path <datasets>/locomo10.json --limit-instances 2 \
  --embedders docs/eval/embedder-round-2026-09-local.json
```

The script needs the application secrets (`MYCELIUM_JWT_SECRET`,
`MYCELIUM_SECRET_KEY`, `MYCELIUM_ISSUER_KEY_PEPPER`) that the test conftest
sets for the suite and that a plain script run does not inherit. Both
checkpoints come from the local HuggingFace cache; `HF_HUB_OFFLINE=1` keeps a
pinned revision from being re-resolved over the network.
