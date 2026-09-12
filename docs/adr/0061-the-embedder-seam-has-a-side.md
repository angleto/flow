# ADR-0061: The embedder seam has a side, and the prefix comes from the checkpoint

Status: Accepted (2026-09-12)
Relates to: ADR-0012 (embedder abstraction), ADR-0005 (single embedding
store), ADR-0030 (changing the model means re-embedding the corpus),
ADR-0060 (the fleet dim is schema).

## Context

`Embedder.embed(text)` was symmetric: one text in, one vector out, with no
notion of what the text was for. That matches bge-m3, the incumbent, which
embeds a question and a document the same way.

It does not match the class of models that has been published since 2025.
Instruction-tuned retrieval models (Qwen3-Embedding, E5-instruct, and most
of what tops the retrieval leaderboards) are trained with an instruction
prefix on the QUERY and nothing on the document. Run symmetrically they do
not fail: they return vectors of the right width, from a model outside the
configuration it was trained for, at a few points of recall. Nothing
downstream can see it.

That is not a hypothetical. The 2026-07-03 embedder round scored
Qwen3-Embedding-0.6B and multilingual-e5-large-instruct without their
prefixes and concluded "no promotion" — a conclusion about a configuration
nobody would ship.

The correction was first written inside the benchmark: a `PrefixedEmbedder`
wrapper, one instance per side, installed around the ingest phase and then
around the scoring phase through the process-global override. Three things
were wrong with it. It measured a shape production could not reproduce, so
a winning candidate would have had to be measured again after adoption. It
was a second implementation of something production would need anyway. And
it rested on an undocumented discipline — the phases run in sequence, the
override is global, nothing runs concurrently — that no test held: the day
someone parallelised the instances, the round would embed documents with the
query prefix and print a plausible, wrong table.

## Decision

**The side is part of the seam.** `EmbedSide` (`query` | `document`) is a
required keyword argument on `Embedder.embed`, on `embed_batch`, and on both
implementations. It has no default: a call site that has not decided which
side it is on has a bug that only a benchmark would ever surface, so the
type checker asks the question instead, at every call site at once, and a
new one cannot be written without answering it.

**The prefix is read from the checkpoint, not configured beside it.**
`LocalEmbedder` resolves the instruction from the model's own
`config_sentence_transformers.json` (sentence-transformers exposes it as
`model.prompts`) and passes `prompt_name` to `encode`. A prefix written by
hand next to a model id is a second copy of something the model already
states, and the two drift the first time a checkpoint is republished — at
which point the model is again running outside its training configuration,
which is the exact failure this is here to prevent. The round's spec file
therefore no longer carries prefixes, and the retired key is REFUSED rather
than ignored, so a spec written against the old shape stops the run.

`HostedEmbedder` takes its prefixes as constructor arguments, empty by
default: an `/v1/embeddings` endpoint serves a model without exposing its
sentence-transformers configuration, so there the instruction is knowable
only from the model card.

**The benchmark got smaller.** One embedder per candidate instead of two,
no wrapper, no per-phase swap, and the round reports the prefix each
checkpoint actually declared — "the instruction-tuned model ran
instruction-tuned" being a claim about what happened, not an assumption.

## Consequences

- What the embedder round measures is now the shape a promoted candidate
  would ship in. A winner does not have to be re-measured after adoption.
- An instruction-tuned model becomes adoptable without further work. That
  was previously a cost to be counted in the evidence for adopting one.
- Every embedder double in the suite implements the side. The shared
  `FakeEmbedder` ignores it deliberately and says so: it stands in for
  bge-m3, which is symmetric, and a fake that returned different vectors per
  side would break every retrieval test for a reason no production embedder
  has.
- A wrong side is now a wrong ARGUMENT rather than a missing capability, so
  it is reviewable at the call site and catchable by a test. The batched
  path took the side and dropped it in the first draft of this change; the
  test written for that case is what found it.

## Alternatives rejected

**Default `side=document`.** Every existing call site would have kept
compiling, and the four query sites would have stayed silently wrong — the
exact bug, preserved, with a type annotation on top.

**Two methods, `embed_query` and `embed_document`.** The same information,
spread across two members that every implementation and every double must
keep in agreement, and impossible to pass through a wrapper generically
(the timeout wrappers in `note_search` and `task_search` forward the side
today in one line each).

**Leave the asymmetry in the benchmark.** It measures something we cannot
ship, duplicates what production needs, and carries a latent trap that the
next person to touch the bench would have had to rediscover.
