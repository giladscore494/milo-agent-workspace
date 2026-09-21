# Evidence authority and CURRENT verdicts

**Classification: `COMPLETED_IN_CODE`.** Implemented and tested in this
repository. No promotion flag is enabled by this work, no paid call is made by
it, and no production row is written by it.

This document states one rule and three consequences of it:

> A verified fact rests on **durable located evidence**, and it is verified
> **now** — not because a model said so, and not because a `verified` row
> exists somewhere in an append-only history.

---

## 1. The canonical evidence model

There is exactly one evidence model in this repository, and both engines use
it. It was built for Swarm V2 (R3/R4) and nothing beside it was added:

| Primitive | Relation | What it is |
| --- | --- | --- |
| `VersionedEvidenceSource` | `public.sources` | Source metadata **plus the version it was read at** (`source_version_kind` / `source_version_id`) |
| `FocusedEvidenceFragment` | `public.source_evidence_fragments` | Bounded evidence text at an **exact locator**, typed `verbatim_excerpt` or `structured_projection` |
| `StructuredEvidenceFact` → `ClaimCreate` | `public.claims` | field + value + unit + closed identity dimensions, carrying the locator it was read from |
| `VerificationVerdict` | `public.claim_verdicts` | One decision, its mode, its contract version |
| `SupportLink` | `public.claim_verdict_supports` | The exact durable fragments a decision rests on — ids and hashes, never text |
| `ConflictResolution` | `public.conflict_resolutions` | One typed, append-only decision per contradicting scope |

The chain a verified fact is held to, end to end:

```
field/value  ->  claim  ->  source  ->  version + locator  ->  durable fragment
                   |
                   +--->  CURRENT verdict  ->  support links
```

Everything on that line is written by trusted server code through
lease-guarded, idempotent RPCs. A model may *propose* a value or *classify* a
record; it never names an evidence identity, a source version, a locator or a
verdict.

---

## 2. The CURRENT verdict rule

`public.claim_verdicts` is append-only — a verdict is an audit record, and
rewriting one would destroy the history it exists for. But **append-only
history is not current truth**:

```
t0  verdict: verified      (the register said 1798 cc)
t1  verdict: rejected      (a re-verification found 1600 cc)

exists(verdict = 'verified')   ->   true, forever
```

Every consumer of verified evidence asked that existence question. One
deterministic resolution replaces it, in Python
(`backend/engines/swarm_v2/current_verdict.py`) and in SQL
(`supabase/migrations/20260921000100_current_verdict_authority.sql`), stating
the same rule:

1. the claim row must still be **active**, else `invalidated`;
2. a **resolved** conflict naming it among the superseded claims → `superseded`;
3. an **unresolved** conflict covering it → `contested`;
4. otherwise the current verdict is the latest row by `created_at`, then — for
   rows written in the same instant — a **non-`verified` verdict ahead of a
   `verified` one**, then the greatest id. That middle term is the fail-closed
   tiebreak: `created_at` is the transaction clock, so two verdicts of one
   transaction are indistinguishable in time and must not be resolved by chance;
5. no verdict row → `unverified`;
6. a current `rejected` / `needs_review` verdict is exactly that;
7. a current `verified` verdict citing **no durable support row**, or stating no
   evidence-bearing mode and contract version → `unsupported`;
8. and only then → `supported`.

`supported` is the only state that authorizes anything. A consumer must also
check that the verdict **it cited** is the row that state names
(`CurrentVerdict.authorizes`): a citation of a superseded `verified` is refused
even when the claim is verified now by a different verdict.

Because every durable verdict write is idempotent on an identity derived from
the verdict's own content, an **exact replay adds no row** — so replay can
never produce two contradictory current answers.

### Reading it

| Layer | Entry point |
| --- | --- |
| Python (pure) | `resolve_current_verdict` / `resolve_current_verdicts` |
| Repository | `Repository.claim_current_verdict_states(run_id, claim_ids, limit=…)` |
| SQL | `public.claim_current_verdict_state(uuid)`, `public.claim_current_verdict_states(uuid, uuid[], integer)` |
| In-process verdict objects | `current_verdict_by_claim` — the product builder, the correction planner and the checkpoint all index through it |

---

## 3. V1 evidence flow

Before this work, V1's `source_verifier` was handed the **names** of the fields
a record filled in, and its conclusion lived only in a JSON document supported
by URLs the same model wrote down.

```
technical record (observed)          model verifier (proposes / classifies)
        |                                          |
        v                                          v
  V1EvidenceAuthority  -- deterministic rules -->  verdict  (downwards only)
        |
        +--> VersionedEvidenceSource   (version = content_sha256 of the record)
        +--> FocusedEvidenceFragment   (structured_projection at record_field)
        +--> StructuredEvidenceFact    (field + value + unit + identity)
        +--> VerificationVerdict       (deterministic_structured, with support)
        |
        v
  CURRENT verdict resolution  ->  what the final document may call `verified`
```

Implemented in `backend/engines/vehicle_catalog_v1/evidence_authority.py`,
wired by `backend/worker/main.py` on the run's own lease.

### What a `verified` V1 field requires

Four things, and each one of them was a way `verified` used to fail **open**:

1. the verdict **and** its durable support links persisted successfully;
2. the **authoritative** current-state read succeeded — no repository, no such
   read, no lease, a failed read or a malformed answer all demote the field.
   "We could not tell" is never read as "still verified";
3. `CurrentVerdict.state == supported`;
4. the current `verdict_id` is **exactly** the verdict that pass settled. A
   supported state naming another row is history or current-state drift, and
   the fact being written rests on the row that was settled and nothing else.

Anything else demotes the field to `needs_review` with a static reason naming
which requirement failed — `V1_EVIDENCE_VERDICT_NOT_DURABLE`,
`V1_EVIDENCE_CURRENT_STATE_UNAVAILABLE`, `V1_EVIDENCE_CURRENT_STATE_DRIFT`,
`V1_EVIDENCE_NOT_CURRENTLY_SUPPORTED`. The research run carries on either way;
a **lost lease** still escapes as infrastructure, from the bundle write, the
verdict write and the current-state read alike.

> ⚠️ **Rollout consequence, by design.** The authoritative read is
> `public.claim_current_verdict_states`, which `20260921000100` creates. Until
> that migration is applied, the read fails and **every V1 field resolves to
> `needs_review`** — the fail-closed direction, and the one an operator should
> expect between deploying this code and applying the migration. Apply the
> migration first, or accept that V1 verifies nothing in that window.

**The four deterministic rules** that decide a field in the first place,
applied over durable evidence, never by a prompt:

1. **Located support** — the value must be readable at an exact locator of a
   versioned record, with exactly one durable fragment at that locator;
2. **Value identity** — two records of one run stating different quantities for
   one field in one identity scope contradict each other, and both are
   rejected (the merge used to pick the higher confidence);
3. **Market evidence** — in a market whose policy requires Israeli evidence, the
   deterministic `source_policy` classification of the record's own URLs gates
   the verdict, not only a note on the final document;
4. **The model's classification, downwards only.**

The verifier prompt now carries the **actual values** (`compact_verifier_input`
emits `fields` as field → bounded value), scoped to the models of its own chunk
so the added detail is paid for rather than added on top.

**A model-reported URL is source metadata, never evidence.** A record that
states a source and no usable value produces no fragment, no claim and no
verdict — so there is nothing for a `verified` to attach to, and the model is
taken to `needs_review`.

**V1 evidence can never be promoted.** It records its own `tool_operation`,
which is not the one `catalog_run_pending_promotions` matches on. That is a
property of the data, not a flag.

---

## 4. Promotion gate changes

Catalog promotion consumes the current supported state and nothing else. Four
layers, each of which independently refuses a stale historical verification:

| Layer | Refusal |
| --- | --- |
| `public.catalog_run_pending_promotions` | resolves the current state and keeps only `supported`; the `verdict_id` it returns is the current one |
| `CatalogPromotionPipeline` | re-resolves the state between the read and the write, links only currently supported claims, and cites the current verdict id |
| `field_evidence_for` | `CATALOG_PROMOTION_EVIDENCE_UNSUPPORTED` / `CATALOG_PROMOTION_EVIDENCE_NOT_CURRENT` |
| PostgreSQL triggers | `catalog_candidate_evidence_links_current_verdict` (a link may not cite a non-current verdict) and `catalog_canonical_field_provenance_current_verdict` (a canonical fact may not be written from one) |

The last row is the one that matters most: a link created while its verdict was
current, followed by a newer invalidation, is exactly the window a link-time
check cannot close.

**Promotion stays default OFF.** `MILO_ENABLE_CATALOG_EXECUTION`,
`MILO_ENABLE_GOVERNMENT_CATALOG_READ` and `MILO_ENABLE_CATALOG_PROMOTION` are
unchanged, all three are required for promotion, and unset/unrecognised is off
(`backend/catalog/execution.py`, `scripts/check_unsafe_defaults.py`).

---

## 5. Migration and rollback

`20260921000100_current_verdict_authority.sql` is additive and forward-only: it
creates five functions, three indexes and two triggers, rewrites one read
function, and re-asserts service-only ACLs. **No verdict already durable is
changed** — only which of them is read as current. There is no backfill.

**ORDER MATTERS on a re-run.** `20260916120000_catalog_field_level_promotion.sql`
defines `catalog_run_pending_promotions` with the old "a verified verdict
exists" join. Migrations apply in filename sequence, so the shipped end state is
the R5 one; an operator who re-runs `20260916120000` afterwards must re-apply
`20260921000100` last. It is rerun-safe, and
`test_reapplying_the_catalog_promotion_migration_needs_this_one_again` states
the hazard.

**Rollback** is forward-safe: re-applying `20260916120000` restores the previous
read, and dropping the two triggers restores the previous gates. Doing so
re-opens the stale-verification window, so it is an incident action rather than
a routine one.

---

## 6. Where it is proven

| Property | Test |
| --- | --- |
| A verdict that could not be persisted leaves the field unverified | `tests/test_v1_evidence_authority.py` §2b |
| A failed or unavailable current-state read never verifies | `tests/test_v1_evidence_authority.py` §2b |
| A supported state naming another verdict never verifies | `tests/test_v1_evidence_authority.py` §2b |
| Only the exact current supported verdict verifies | `tests/test_v1_evidence_authority.py` §2b |
| The rule, every state and the fail-closed tiebreak | `tests/test_current_verdict_authority.py` |
| The same rule in PostgreSQL, incl. a real same-transaction tie | `tests/test_migrations_postgres.py` |
| V1 verifies actual field/value identity | `tests/test_v1_evidence_authority.py` §1 |
| A model-only URL never becomes durable verified evidence | `tests/test_v1_evidence_authority.py` §2 |
| Durable V1 evidence survives checkpoint/resume | `tests/test_v1_evidence_authority.py` §4 |
| Older verified + newer invalid ⇒ not verified | both modules |
| Duplicate/idempotent evidence ⇒ one current truth | both modules |
| Promotion refuses stale historical verification | `tests/test_catalog_pr3_swarm_promotion.py` §12, `tests/test_migrations_postgres.py` |
| Promotion succeeds only from current, supported, provenance-complete evidence | `tests/test_catalog_pr3_swarm_promotion.py` §12 |
| V2 evidence behaviour unchanged | `tests/test_current_verdict_authority.py` §5 and the existing R3/R4 suites |
