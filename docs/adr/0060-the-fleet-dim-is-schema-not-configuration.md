# ADR-0060: The fleet embedding dim is schema, and truncation is declared

Status: Accepted (2026-09-12)
Relates to: ADR-0005 (single embedding store on pgvector), ADR-0012
(embedder abstraction), ADR-0030 (embedding model migration: changing the
model means re-embedding the corpus).

## Context

Every embedder in the fleet, local or hosted, must emit exactly the width
of the column it writes to. That invariant was expressed four times and
enforced in none of them: `Settings.embed_dim`, `models.memory_blob.EMBED_DIM`,
`models.adjudication.EMBED_DIM` (carrying the comment "must match the fleet
embedding dim"), and the `vector(1024)` of the migration DDL. Four numbers
held equal by three comments.

Being a setting made it worse than a duplicate. `MYCELIUM_EMBED_DIM=768`
was an expressible configuration with no possible correct meaning: the
embedders would coerce every vector to 768 floats while the column stayed
at 1024, so every write would fail at the driver, three modules from the
line that chose the width. The dim cannot vary at boot, because changing
it is a drop-and-rebuild of the column plus a re-embedding of the whole
corpus (ADR-0030).

The second half of the context is newer. Until 2026-09-11 `LocalEmbedder`
returned whatever its checkpoint emitted, so the local tier could host
exactly one model — the default bge-m3, natively at the fleet width — and
any other candidate failed at its first write. Applying the hosted side's
`_truncate_normalize` to the local side removed that wall, and introduced
a quieter one: truncation is the Matryoshka procedure, principled only for
an MRL-trained checkpoint, and applied to any other model it returns a
vector of exactly the right shape whose content is worse. A loud failure
had been turned into a silent degradation for that entire class of model.

## Decision

**One constant, in a leaf module.** `mycelium_core.embed_dims` holds
`EMBED_DIM` and `EMBED_DIM_HOSTED`; `config`, the models and the services
all read them, and the two `Settings` fields are gone. A leaf module
rather than either existing home because `config` is read before any model
is imported and the models do not read configuration, so neither can
import the other.

**The migration keeps its literal, and a test ties the two together.** A
migration is a historical record and must not change meaning when a
constant does, so `core/tests/test_embed_dim_drift.py` reads the width the
live column actually declares — on `memory_blobs`, on every one of its
partitions, and on `adjudication_steps` — and compares it with the
constants. The DDL is checked as it exists, not as its source says it
should.

**Truncation is declared, never inferred.** `LocalEmbedder` refuses at
load time a checkpoint wider than the fleet dim that has not declared
Matryoshka training (`MYCELIUM_EMBED_MODEL_IS_MRL` for the deployment's
own model, `mrl=` per candidate in the embedder round). A narrower
checkpoint is refused whatever it declares: a short vector cannot be
padded faithfully. The refusal is at load rather than at the first encode
because it is a property of the configuration and not of the text, and the
message names the model, both widths and the knob, since it will be read
on a machine that is not the one that chose the model.

Default false, which asks nothing of the incumbent: bge-m3 is natively at
the fleet width, never truncated, and has no MRL claim to make — it does
not document any.

## Consequences

- The fleet dim can no longer be set to a value the column will refuse.
  A stale `MYCELIUM_EMBED_DIM` in an environment is now inert rather than
  destructive; it was never set in any deployment manifest.
- Changing the fleet width is now visibly what it always was: a migration,
  a constant and a corpus re-embedding, moving together, with the drift
  test failing until they do.
- A wider local model needs one deliberate declaration before it will
  load. That is the cost of the guarantee, and it is paid by whoever
  already had to read the model card to choose the model.
- bge-m3 not being MRL has a consequence worth stating, because it is not
  obvious and it constrains a decision that looked open: lowering the
  fleet dim below 1024 (to admit the 768-native models the COLM 2026
  comparison favours) would not shrink the incumbent, it would disqualify
  it. There is no principled truncation of bge-m3.
- `write_blob` logs the embed failure it swallows. The degradation to
  keyword-only is deliberate (an optional extra must not break memory) but
  it was invisible from the outside, and a refused width now lands there.

## Alternatives rejected

**Keep the setting, add a startup check that it equals the constant.**
A setting whose only valid value is the constant is not configuration, it
is a question with one answer that an operator can still get wrong. The
check would also arrive at startup, after the value had already been read
by anything that ran earlier.

**Infer MRL from the checkpoint.** There is nothing to read: MRL is a
property of how a model was trained, not of what it emits, and a
`config_sentence_transformers.json` does not carry it. Inferring it from
a dimension list on the model card would mean parsing prose into a
guarantee.

**Refuse a model that does not report its width.** The gate reads a
dimension the model volunteers. A stand-in that does not implement the
accessor is not thereby suspect — what it emits is still checked at the
write — and refusing it would turn the gate into a second, stricter
definition of what an embedder is.
