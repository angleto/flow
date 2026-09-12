"""WS-EVAL T2: deterministic adversarial queries + gold from the T1 registry
(protocol note 0cb0dda0 §3, task 4a2670ac).

Consumes the :mod:`eval_workspace` fact registry (ground truth by
construction) and emits per-category query records whose gold is RESOLVED,
never annotated. The panel obligations are code, not prose:

- **Category partition** (one fact never feeds overlapping categories):
  ``distributed`` > temporal pair (``freshness`` current-gold +
  ``as_of_previous`` stale-gold, the ANTI-RECENCY case a recency prior must
  fail) > ``collision`` (a same-attribute sibling fact exists in the corpus,
  emitted as tracked distractors) > ``single_fact``. ``multi_hop`` /
  ``impossible`` / ``perimeter`` / ``erasure`` are built from relations,
  schema gaps and sampling, not from the partition.
- **Fan-out competitivo** (§3): multi-hop chains are only emitted when the
  intermediate hop has >=2 plausible branches; the observed fan-out is
  recorded on the record and asserted in tests.
- **Impossible = near-miss** (§3): existing entity, schema attribute with NO
  fact of ANY category in the registry (verified against the registry, not
  against templates).
- **Vocabulary mismatch**: question frames and attribute phrasings are
  DISJOINT from T1's realization templates (tested by set intersection), and
  a pre-registered fraction of queries is generated in the OTHER language
  than the gold unit.
- **Hardness measured, not declared** (§1.2): :func:`compute_hardness` scores
  gold vs near-miss distractor units on both channels (lexical ts_rank,
  dense cosine) against a REAL ingested workspace and reports the
  pre-registered competitive fraction. CI asserts the machinery; the >=50%
  validity check gates real corpora at T5.
- **Human anchor** (§3): :func:`export_reviewer_pack` gives a reviewer the
  REGISTRY only (never corpus text); :func:`import_human_queries` resolves
  human-written queries through the same gold resolution as templated ones.
- **Generation matrix** (§1.6): every record carries provider/model/prompt
  fields; the deterministic layer is ``template-v1``, the LLM paraphrase
  seam (:class:`Paraphraser`) is pluggable and NEVER called in CI.

Erasure records only mark the DIRECT source to erase and the queries that
must degrade; the via-derived cascade (humus atoms, KG provenance) is
emitted by T3's scenario runner once derivatives exist -- the linkage fields
(``erase_unit_id`` / ``degrades_after_erase``) are the contract it consumes.
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import json
import random
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mycelium_core.embedder import EmbedResult, EmbedSide
from mycelium_core.models.memory_blob import MemoryBlob
from mycelium_core.services.eval_workspace import (
    _ANAPHORIC_FRAMES,
    _DISTRIBUTED_A_FRAMES,
    _DISTRIBUTED_B_FRAMES,
    _FACT_FRAMES,
    _FILLER,
    _SCHEMA,
    Fact,
    IngestResult,
    Workspace,
    resolve_unit_blobs,
)

GENERATION_MATRIX_QUERIES_V1 = {
    "provider": "deterministic",
    "model": "template-v1",
    "temperature": None,
    "prompt": "wseval-queries-v1",
}

# Pre-registered quotas (§3). Changing them is a protocol change (T5 freeze).
MAX_QUERIES_PER_FACT = 2
SECOND_PHRASING_FRACTION = 0.25
INVERT_LANG_FRACTION = 0.50
ERASURE_FRACTION = 0.15
PERIMETER_FRACTION = 0.15
IMPOSSIBLE_PER_ENTITY_MAX = 1
HARDNESS_MIN_COMPETITIVE_FRACTION = 0.50
_MULTIHOP_MIN_FANOUT = 2

# ── question surface forms (MUST stay disjoint from T1's declaratives) ──────

_Q_FRAMES = {
    "it": (
        "Qual è {qlabel} di {e}?",
        "Mi ricordi {qlabel} di {e}?",
        "Che {qlabel} risulta per {e}?",
        "Sapresti dirmi {qlabel} di {e}?",
    ),
    "en": (
        "What is {qlabel} of {e}?",
        "Can you remind me of {qlabel} for {e}?",
        "Which {qlabel} is on record for {e}?",
        "Do we know {qlabel} of {e}?",
    ),
}
_Q_ASOF_FRAMES = {
    "it": (
        "Qual era {qlabel} di {e} prima dell'ultimo aggiornamento?",
        "Che valore aveva {qlabel} di {e} in precedenza?",
    ),
    "en": (
        "What was {qlabel} of {e} before the latest update?",
        "Which previous value did {qlabel} of {e} have?",
    ),
}
_Q_ASOF_DATED_FRAMES = {
    "it": (
        "Qual era {qlabel} di {e} alla data {d}?",
        "Al {d}, che {qlabel} risultava per {e}?",
    ),
    "en": (
        "What was {qlabel} of {e} as of {d}?",
        "As of {d}, which {qlabel} was on record for {e}?",
    ),
}
_Q_HOP2_FRAMES = {
    "it": ("Qual è {qlabel2} del referente del progetto {p}?",),
    "en": ("What is {qlabel2} of the lead of project {p}?",),
}
_Q_HOP3_FRAMES = {
    "it": ("Qual è {qlabel2} del referente del progetto il cui {qanchor} è {vanchor}?",),
    "en": ("What is {qlabel2} of the lead of the project whose {qanchor} is {vanchor}?",),
}

# Question-side attribute phrasings: deliberately NOT the schema labels used
# by T1's realizations (vocabulary mismatch §3). Fallback = schema label.
_Q_LABELS: dict[tuple[str, str], tuple[str, str]] = {
    ("person", "tariffa"): ("il costo orario", "the hourly cost"),
    ("person", "citta"): ("la sede di lavoro", "the work location"),
    ("person", "telefono"): ("il recapito telefonico", "the phone contact"),
    ("person", "ruolo"): ("la mansione", "the job function"),
    ("project", "scadenza"): ("la data di consegna", "the delivery date"),
    ("project", "budget"): ("la disponibilità economica", "the allocated funds"),
    ("project", "repository"): ("l'indirizzo del codice sorgente", "the source code address"),
    ("project", "database"): ("il motore dati", "the data engine"),
    ("project", "referente"): ("la persona di riferimento", "the point of contact"),
    ("client", "partita_iva"): ("il codice fiscale IVA", "the VAT registration"),
    ("client", "rinnovo"): ("la data di rinnovo dell'accordo", "the agreement renewal date"),
    ("client", "pagamento"): ("i termini di saldo", "the settlement terms"),
    ("system", "versione"): ("la release installata", "the installed release"),
    ("system", "porta"): ("la porta di ascolto", "the listening port"),
    ("system", "endpoint"): ("l'indirizzo di servizio", "the service address"),
    ("system", "backup"): ("la pianificazione dei salvataggi", "the save schedule"),
}


def _qlabel(fact_etype: str, attribute: str, lang: str) -> str:
    pair = _Q_LABELS.get((fact_etype, attribute))
    if pair is not None:
        return pair[0] if lang == "it" else pair[1]
    labels = _SCHEMA[fact_etype][attribute]
    return labels[0] if lang == "it" else labels[1]


def t2_template_strings() -> set[str]:
    """Every surface template T2 can emit (for the disjointness test)."""
    out: set[str] = set()
    for frames in (_Q_FRAMES, _Q_ASOF_FRAMES, _Q_ASOF_DATED_FRAMES, _Q_HOP2_FRAMES, _Q_HOP3_FRAMES):
        for langset in frames.values():
            out.update(langset)
    for pair in _Q_LABELS.values():
        out.update(pair)
    return out


def t1_template_strings() -> set[str]:
    out: set[str] = set()
    for frames in (
        _FACT_FRAMES,
        _ANAPHORIC_FRAMES,
        _DISTRIBUTED_A_FRAMES,
        _DISTRIBUTED_B_FRAMES,
        _FILLER,
    ):
        for langset in frames.values():
            out.update(langset)
    return out


# ── records ─────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class QueryRecord:
    query_id: str
    category: str
    query_text: str
    lang: str
    fact_id: str | None
    gold_unit_ids: list[str]
    distractor_unit_ids: list[str] = dataclasses.field(default_factory=list)
    hop_unit_ids: list[str] = dataclasses.field(default_factory=list)
    fan_out: int | None = None
    as_of_date: str | None = None  # date pinned by an as_of_previous query (A3)
    expected_empty: bool = False
    home_project: str | None = None
    context_project: str | None = None
    erase_unit_id: str | None = None
    degrades_after_erase: bool = False
    lang_inverted: bool = False
    generation: dict[str, Any] = dataclasses.field(
        default_factory=lambda: dict(GENERATION_MATRIX_QUERIES_V1)
    )


class Paraphraser(Protocol):
    """LLM paraphrase seam (§3). Implementations rewrite ``query_text``
    WITHOUT touching gold/category fields and must stamp their generation
    matrix. Never called in CI."""

    async def paraphrase(self, record: QueryRecord) -> QueryRecord: ...


class NoopParaphraser:
    async def paraphrase(self, record: QueryRecord) -> QueryRecord:
        return record


class LLMParaphraser:
    """Config-driven paraphraser: ``complete`` is a caller-wired async
    ``prompt -> text`` (the house LLM resolver lives at the call site, so
    this module never imports provider code)."""

    _PROMPT = (
        "Riscrivi la domanda seguente con parole diverse, stessa lingua, "
        "senza cambiare il significato né i nomi propri. Rispondi SOLO con "
        "la domanda riscritta.\n\n{q}"
    )

    def __init__(
        self,
        complete: Callable[[str], Awaitable[str]],
        *,
        provider: str,
        model: str,
        temperature: float | None = None,
    ) -> None:
        self._complete = complete
        self._matrix = {
            "provider": provider,
            "model": model,
            "temperature": temperature,
            "prompt": "wseval-paraphrase-v1",
        }

    async def paraphrase(self, record: QueryRecord) -> QueryRecord:
        rewritten = (await self._complete(self._PROMPT.format(q=record.query_text))).strip()
        if rewritten:
            record.query_text = rewritten
            record.generation = dict(self._matrix)
        return record


# ── query assembly ──────────────────────────────────────────────────────────


def _query_lang(rng: random.Random, fact_lang: str) -> tuple[str, bool]:
    if rng.random() < INVERT_LANG_FRACTION:
        return ("en" if fact_lang == "it" else "it"), True
    return fact_lang, False


def _direct_question(
    rng: random.Random, fact: Fact, lang: str, frame_idx: int | None = None
) -> str:
    frames = _Q_FRAMES[lang]
    frame = frames[frame_idx % len(frames)] if frame_idx is not None else rng.choice(frames)
    return frame.format(qlabel=_qlabel(fact.entity_type, fact.attribute, lang), e=fact.entity_name)


def build_queries(ws: Workspace, *, seed: int) -> list[QueryRecord]:
    """Assemble the full adversarial query set from the registry. Fully
    determined by ``seed`` (independent from the corpus seed: the protocol's
    generator/query separation)."""
    rng = random.Random(seed)  # noqa: S311 - deterministic assembly, not crypto
    records: list[QueryRecord] = []
    qn = 0

    def _qid() -> str:
        nonlocal qn
        qn += 1
        return f"q-{qn:05d}"

    gold = [f for f in ws.facts if f.queryable and f.gold_unit_ids]
    realized_pairs = {(f.entity_id, f.attribute) for f in ws.facts}
    units_by_id = {u.unit_id: u for u in ws.units}
    projects = sorted({u.project for u in ws.units})

    # Resolve the multi-hop chains up front (project --referente--> person
    # --attr--> value) so their target facts can be kept OUT of the
    # perimeter/erasure reservation: a person-attribute fact used by a 2-hop
    # AND a 3-hop query already fills its 2-query budget, and a perimeter/
    # erasure record on the same fact would be silently trimmed by the cap.
    referente = [
        f
        for f in ws.facts
        if f.entity_type == "project" and f.attribute == "referente" and f.gold_unit_ids
    ]
    person_by_name: dict[str, list[Fact]] = {}
    for f in ws.facts:
        if f.entity_type == "person" and f.gold_unit_ids:
            person_by_name.setdefault(f.entity_name, []).append(f)
    chains: list[tuple[Fact, Fact]] = []
    for rf in referente:
        person_facts = [p for p in person_by_name.get(rf.value, []) if p.attribute != "ruolo"]
        if len(person_facts) >= _MULTIHOP_MIN_FANOUT:
            chains.append((rf, sorted(person_facts, key=lambda p: p.fact_id)[0]))
    multihop_active = len(chains) >= _MULTIHOP_MIN_FANOUT
    multihop_target_ids = {pf.fact_id for _rf, pf in chains} if multihop_active else set()

    # Perimeter/erasure facts are RESERVED up front: their scenario record is
    # the fact's second (and last) query, so the per-fact cap (§3) never
    # silently starves the two categories. Only direct note-backed gold that is
    # NOT a multi-hop target qualifies (the erase target must be a single note;
    # the perimeter gold must live in exactly one project).
    direct_pool = [
        f
        for f in gold
        if not f.distributed
        and not (f.old_value and f.stale_unit_id)
        and f.fact_id not in multihop_target_ids
        and units_by_id[f.gold_unit_ids[0]].unit_kind == "note"
    ]
    rng.shuffle(direct_pool)
    n_per = max(1, round(len(direct_pool) * PERIMETER_FRACTION)) if len(direct_pool) >= 2 else 0
    n_era = max(1, round(len(direct_pool) * ERASURE_FRACTION)) if len(direct_pool) >= 2 else 0
    perimeter_facts = [f for f in direct_pool[:n_per]]
    erasure_facts = [f for f in direct_pool[n_per : n_per + n_era]]
    reserved_ids = {f.fact_id for f in perimeter_facts} | {f.fact_id for f in erasure_facts}

    def _sibling_units(f: Fact) -> list[str]:
        out: list[str] = []
        for other in ws.facts:
            if (
                other.attribute == f.attribute
                and other.entity_id != f.entity_id
                and other.entity_type == f.entity_type
                and other.gold_unit_ids
            ):
                out.extend(other.gold_unit_ids[:1])
        return out[:4]

    # Multi-hop emission (chains resolved above). Placed BEFORE the per-fact
    # partition so a person-attribute fact used by a 2-hop AND a 3-hop query
    # fills its 2-query budget here and its direct query is the one trimmed by
    # the cap (§3) -- otherwise a fired second phrasing upstream could evict
    # the multi-hop pair.
    if multihop_active:
        fan_out = len(chains)
        for rf, pf in chains:
            lang, inverted = _query_lang(rng, pf.lang)
            records.append(
                QueryRecord(
                    query_id=_qid(),
                    category="multi_hop_2",
                    query_text=rng.choice(_Q_HOP2_FRAMES[lang]).format(
                        qlabel2=_qlabel("person", pf.attribute, lang), p=rf.entity_name
                    ),
                    lang=lang,
                    fact_id=pf.fact_id,
                    gold_unit_ids=pf.gold_unit_ids[:1],
                    hop_unit_ids=rf.gold_unit_ids[:1],
                    fan_out=fan_out,
                    lang_inverted=inverted,
                )
            )
            # 3-hop: anchor the project by ANOTHER of its attributes.
            anchors = [
                f
                for f in ws.facts
                if f.entity_id == rf.entity_id
                and f.attribute not in ("referente",)
                and f.gold_unit_ids
            ]
            if not anchors:
                continue
            anchor = sorted(anchors, key=lambda a: a.fact_id)[0]
            lang3, inverted3 = _query_lang(rng, pf.lang)
            records.append(
                QueryRecord(
                    query_id=_qid(),
                    category="multi_hop_3",
                    query_text=rng.choice(_Q_HOP3_FRAMES[lang3]).format(
                        qlabel2=_qlabel("person", pf.attribute, lang3),
                        qanchor=_qlabel("project", anchor.attribute, lang3),
                        vanchor=anchor.value,
                    ),
                    lang=lang3,
                    fact_id=pf.fact_id,
                    gold_unit_ids=pf.gold_unit_ids[:1],
                    hop_unit_ids=[*anchor.gold_unit_ids[:1], *rf.gold_unit_ids[:1]],
                    fan_out=fan_out,
                    lang_inverted=inverted3,
                )
            )

    # Partition: distributed > temporal pair > collision > single_fact.
    for f in gold:
        lang, inverted = _query_lang(rng, f.lang)
        if f.distributed:
            records.append(
                QueryRecord(
                    query_id=_qid(),
                    category="distributed",
                    query_text=_direct_question(rng, f, lang),
                    lang=lang,
                    fact_id=f.fact_id,
                    # Contract: recall counts if ANY carrying unit is found;
                    # the FULL set is emitted so T4 can also score joint
                    # coverage of the A/B sides.
                    gold_unit_ids=list(f.gold_unit_ids),
                    distractor_unit_ids=_sibling_units(f),
                    lang_inverted=inverted,
                )
            )
            continue
        if f.old_value and f.stale_unit_id:
            records.append(
                QueryRecord(
                    query_id=_qid(),
                    category="freshness",
                    query_text=_direct_question(rng, f, lang),
                    lang=lang,
                    fact_id=f.fact_id,
                    gold_unit_ids=f.gold_unit_ids[:1],
                    distractor_unit_ids=[f.stale_unit_id],
                    lang_inverted=inverted,
                )
            )
            lang2, inverted2 = _query_lang(rng, f.lang)
            # Date-pinned when the registry carries validity dates (A3): the
            # pin sits strictly between the old and new effective dates, so
            # the version valid AT the pin is the OLD one. Falls back to the
            # relative "before the latest update" phrasing when undated.
            if f.as_of_pin:
                qtext = rng.choice(_Q_ASOF_DATED_FRAMES[lang2]).format(
                    qlabel=_qlabel(f.entity_type, f.attribute, lang2),
                    e=f.entity_name,
                    d=f.as_of_pin,
                )
            else:
                qtext = rng.choice(_Q_ASOF_FRAMES[lang2]).format(
                    qlabel=_qlabel(f.entity_type, f.attribute, lang2), e=f.entity_name
                )
            records.append(
                QueryRecord(
                    query_id=_qid(),
                    category="as_of_previous",
                    query_text=qtext,
                    lang=lang2,
                    fact_id=f.fact_id,
                    # ANTI-RECENCY: the gold is the OLDER unit; the fresh one
                    # is the tracked distractor a recency prior would serve.
                    gold_unit_ids=[f.stale_unit_id],
                    distractor_unit_ids=f.gold_unit_ids[:1],
                    as_of_date=f.as_of_pin,
                    lang_inverted=inverted2,
                )
            )
            continue
        siblings = _sibling_units(f)
        category = "collision" if siblings else "single_fact"
        records.append(
            QueryRecord(
                query_id=_qid(),
                category=category,
                query_text=_direct_question(rng, f, lang),
                lang=lang,
                fact_id=f.fact_id,
                gold_unit_ids=f.gold_unit_ids[:1],
                distractor_unit_ids=siblings,
                lang_inverted=inverted,
            )
        )
        if f.fact_id not in reserved_ids and rng.random() < SECOND_PHRASING_FRACTION:
            lang2, inverted2 = _query_lang(rng, f.lang)
            records.append(
                QueryRecord(
                    query_id=_qid(),
                    category=category,
                    query_text=_direct_question(rng, f, lang2, frame_idx=rng.randrange(97)),
                    lang=lang2,
                    fact_id=f.fact_id,
                    gold_unit_ids=f.gold_unit_ids[:1],
                    distractor_unit_ids=siblings,
                    lang_inverted=inverted2,
                )
            )

    # Impossible near-miss: existing QUERYABLE entity, schema attribute with
    # no fact of ANY category in the registry (checked against the registry).
    for ent in ws.entities:
        if not ent.queryable:
            continue
        missing = [
            a for a in sorted(_SCHEMA[ent.entity_type]) if (ent.entity_id, a) not in realized_pairs
        ]
        for attribute in missing[:IMPOSSIBLE_PER_ENTITY_MAX]:
            lang = "it" if rng.random() < 0.5 else "en"
            ghost = Fact(
                fact_id=f"none-{ent.entity_id}-{attribute}",
                entity_id=ent.entity_id,
                entity_name=ent.name,
                entity_type=ent.entity_type,
                attribute=attribute,
                value="",
                lang=lang,
                category="impossible",
                queryable=False,
            )
            records.append(
                QueryRecord(
                    query_id=_qid(),
                    category="impossible",
                    query_text=_direct_question(rng, ghost, lang),
                    lang=lang,
                    fact_id=None,
                    gold_unit_ids=[],
                    expected_empty=True,
                )
            )

    # Perimeter + erasure: the disjoint samples reserved before the partition
    # loop (their second-phrasing slot was withheld, so the cap holds).
    for f in perimeter_facts:
        home = units_by_id[f.gold_unit_ids[0]].project
        others = [p for p in projects if p != home]
        if not others:
            continue
        lang, inverted = _query_lang(rng, f.lang)
        records.append(
            QueryRecord(
                query_id=_qid(),
                category="perimeter",
                query_text=_direct_question(rng, f, lang),
                lang=lang,
                fact_id=f.fact_id,
                gold_unit_ids=f.gold_unit_ids[:1],
                expected_empty=True,
                home_project=home,
                context_project=rng.choice(others),
                lang_inverted=inverted,
            )
        )
    for f in erasure_facts:
        lang, inverted = _query_lang(rng, f.lang)
        records.append(
            QueryRecord(
                query_id=_qid(),
                category="erasure",
                query_text=_direct_question(rng, f, lang),
                lang=lang,
                fact_id=f.fact_id,
                gold_unit_ids=f.gold_unit_ids[:1],
                erase_unit_id=f.gold_unit_ids[0],
                degrades_after_erase=True,
                lang_inverted=inverted,
            )
        )

    # Cap guard (§3): a fact may back at most MAX_QUERIES_PER_FACT records
    # (the temporal pair and the second phrasing sit exactly at the cap; the
    # erasure/perimeter samples reuse the SAME record budget, so trim).
    seen: dict[str, int] = {}
    capped: list[QueryRecord] = []
    for r in records:
        if r.fact_id is None:
            capped.append(r)
            continue
        seen[r.fact_id] = seen.get(r.fact_id, 0) + 1
        if seen[r.fact_id] <= MAX_QUERIES_PER_FACT:
            capped.append(r)
    return capped


# ── artifacts ───────────────────────────────────────────────────────────────


def write_query_artifacts(records: Sequence[QueryRecord], out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "queries.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(dataclasses.asdict(r), ensure_ascii=False, sort_keys=True) + "\n")
    counts: dict[str, int] = {}
    for r in records:
        counts[r.category] = counts.get(r.category, 0) + 1
    manifest = {
        "generator": "wseval-t2",
        "generation_matrix": GENERATION_MATRIX_QUERIES_V1,
        "counts": dict(sorted(counts.items())),
        "total": len(records),
        "lang_inverted": sum(1 for r in records if r.lang_inverted),
        "quotas": {
            "max_queries_per_fact": MAX_QUERIES_PER_FACT,
            "invert_lang_fraction": INVERT_LANG_FRACTION,
            "second_phrasing_fraction": SECOND_PHRASING_FRACTION,
            "perimeter_fraction": PERIMETER_FRACTION,
            "erasure_fraction": ERASURE_FRACTION,
            "hardness_min_competitive_fraction": HARDNESS_MIN_COMPETITIVE_FRACTION,
        },
        "sha256": {"queries.jsonl": hashlib.sha256(path.read_bytes()).hexdigest()},
    }
    (out_dir / "queries_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


# ── human anchor (§3) ───────────────────────────────────────────────────────


def export_reviewer_pack(ws: Workspace, out_path: Path, *, seed: int, max_facts: int = 60) -> int:
    """Reviewer pack for the HUMAN query author: registry facts only (entity,
    attribute labels, value, category) -- never a corpus sentence, so the
    reviewer cannot copy the realization's surface form."""
    rng = random.Random(seed)  # noqa: S311
    gold = [f for f in ws.facts if f.queryable and f.gold_unit_ids]
    sample = rng.sample(gold, k=min(max_facts, len(gold)))
    lines = [
        "# WS-EVAL — pacchetto per query umane",
        "",
        "Scrivi UNA domanda per ogni fatto, con parole tue (qualunque lingua).",
        "Non hai accesso al corpus: solo il registro qui sotto.",
        'Compila `human_queries.jsonl`: {"fact_id": ..., "query_text": ..., "lang": ...}',
        "",
    ]
    for f in sample:
        label_it, label_en = _SCHEMA[f.entity_type][f.attribute][0:2]
        lines.append(
            f"- `{f.fact_id}` — {f.entity_type} **{f.entity_name}**, "
            f"attributo: {label_it} / {label_en}, valore: `{f.value}`"
            + (" (fatto DISTRIBUITO su più unità)" if f.distributed else "")
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(sample)


def import_human_queries(path: Path, ws: Workspace) -> list[QueryRecord]:
    """Human-written queries -> records through the SAME gold resolution as
    templated ones. Accepts JSONL or CSV with fact_id / query_text / lang."""
    facts_by_id = {f.fact_id: f for f in ws.facts}
    rows: list[dict[str, str]] = []
    if path.suffix == ".csv":
        with path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    else:
        with path.open(encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
    records: list[QueryRecord] = []
    for i, row in enumerate(rows, start=1):
        fact = facts_by_id.get(str(row["fact_id"]))
        if fact is None or not fact.queryable:
            raise ValueError(f"human query {i}: unknown or non-gold fact_id {row['fact_id']!r}")
        records.append(
            QueryRecord(
                query_id=f"hq-{i:05d}",
                category="human_anchor",
                query_text=str(row["query_text"]),
                lang=str(row.get("lang") or fact.lang),
                fact_id=fact.fact_id,
                gold_unit_ids=list(fact.gold_unit_ids)
                if fact.distributed
                else fact.gold_unit_ids[:1],
                generation={
                    "provider": "human",
                    "model": "human",
                    "temperature": None,
                    "prompt": "reviewer-pack-v1",
                },
            )
        )
    return records


# ── hardness gate (§1.2) ────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class HardnessRow:
    query_id: str
    gold_lex: float
    gold_dense: float
    best_distractor_lex: float
    best_distractor_dense: float
    distractor_beats_gold: bool
    distractor_in_lexical_top10: bool
    competitive: bool


@dataclasses.dataclass(frozen=True)
class HardnessReport:
    rows: list[HardnessRow]
    skipped_non_note: int
    fraction_competitive: float
    threshold: float
    passes: bool


_LEX_SCORE_SQL = text(
    """
    SELECT COALESCE(MAX(GREATEST(
        ts_rank(fts, plainto_tsquery('simple', :q)),
        ts_rank(fts_lang, plainto_tsquery(fts_language::regconfig, :q))
    )), 0.0)
    FROM memory_blobs
    WHERE org_id = :org AND id = ANY(:ids)
    """
)
_LEX_TOP10_SQL = text(
    """
    SELECT id FROM memory_blobs
    WHERE org_id = :org
    ORDER BY GREATEST(
        ts_rank(fts, plainto_tsquery('simple', :q)),
        ts_rank(fts_lang, plainto_tsquery(fts_language::regconfig, :q))
    ) DESC, id
    LIMIT 10
    """
)


async def _lex_score(session: AsyncSession, org_id: Any, ids: list[Any], q: str) -> float:
    if not ids:
        return 0.0
    got = await session.execute(_LEX_SCORE_SQL, {"org": str(org_id), "ids": ids, "q": q})
    return float(got.scalar_one())


async def _dense_score(
    session: AsyncSession, org_id: Any, ids: list[Any], qvec: EmbedResult
) -> float:
    """Best cosine over the unit's blobs; ``max_inner_product`` returns the
    NEGATED inner product and vectors are L2-normalized, so cosine = -distance
    (same convention as the semantic stage)."""
    if not ids:
        return 0.0
    from sqlalchemy import func, select

    dist = MemoryBlob.embedding.max_inner_product(qvec.vector)
    got = await session.execute(
        select(func.min(dist)).where(
            MemoryBlob.org_id == org_id,
            MemoryBlob.id.in_(ids),
            MemoryBlob.embedding.is_not(None),
        )
    )
    val = got.scalar_one()
    return -float(val) if val is not None else 0.0


async def compute_hardness(
    session: AsyncSession,
    *,
    org_id: Any,
    ingest: IngestResult,
    records: Sequence[QueryRecord],
    embedder: Any,
) -> HardnessReport:
    """Score gold vs near-miss distractors per channel over a REAL ingested
    workspace. ``competitive`` = the best distractor beats the gold on >=1
    channel OR enters the lexical top-10. The pre-registered >=50% validity
    check is REPORTED here and enforced at T5 on real corpora (CI asserts the
    machinery on tiny workspaces where the threshold is meaningless)."""
    rows: list[HardnessRow] = []
    skipped = 0

    async def _unit_blobs(unit_id: str) -> list[Any]:
        meta = ingest.units.get(unit_id)
        if meta is None or meta.get("kind") != "note":
            return []
        import uuid as _uuid

        return await resolve_unit_blobs(session, org_id=org_id, note_id=_uuid.UUID(meta["note_id"]))

    for r in records:
        if not r.distractor_unit_ids or not r.gold_unit_ids:
            continue
        gold_blobs = [b for uid in r.gold_unit_ids for b in await _unit_blobs(uid)]
        dist_blobs_by_unit = [await _unit_blobs(uid) for uid in r.distractor_unit_ids]
        dist_blobs = [b for blobs in dist_blobs_by_unit for b in blobs]
        if not gold_blobs or not dist_blobs:
            skipped += 1
            continue
        qvec = await embedder.embed(r.query_text, side=EmbedSide.query)
        gold_lex = await _lex_score(session, org_id, gold_blobs, r.query_text)
        gold_dense = await _dense_score(session, org_id, gold_blobs, qvec)
        d_lex = await _lex_score(session, org_id, dist_blobs, r.query_text)
        d_dense = await _dense_score(session, org_id, dist_blobs, qvec)
        top10 = {
            row[0]
            for row in (
                await session.execute(_LEX_TOP10_SQL, {"org": str(org_id), "q": r.query_text})
            ).all()
        }
        in_top10 = any(b in top10 for b in dist_blobs)
        beats = d_lex > gold_lex or d_dense > gold_dense
        rows.append(
            HardnessRow(
                query_id=r.query_id,
                gold_lex=round(gold_lex, 6),
                gold_dense=round(gold_dense, 6),
                best_distractor_lex=round(d_lex, 6),
                best_distractor_dense=round(d_dense, 6),
                distractor_beats_gold=beats,
                distractor_in_lexical_top10=in_top10,
                competitive=beats or in_top10,
            )
        )
    frac = (sum(1 for x in rows if x.competitive) / len(rows)) if rows else 0.0
    return HardnessReport(
        rows=rows,
        skipped_non_note=skipped,
        fraction_competitive=round(frac, 4),
        threshold=HARDNESS_MIN_COMPETITIVE_FRACTION,
        passes=frac >= HARDNESS_MIN_COMPETITIVE_FRACTION,
    )
