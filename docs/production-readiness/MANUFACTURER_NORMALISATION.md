# Manufacturer normalisation (PR-D3)

Owner decisions 2, 14, 15 and 33. The register's own `tozar` strings **never
change** (decision 2). A normalisation maps each EXACT source tozar to a
canonical manufacturer. It is versioned and append-only, and every entry keeps
its provenance: a code-owned rule, or the model proposal it came from, plus
who approved it and when. The Register page and the catalog browser show the
canonical name beside the exact source name. Every read, filter and **Add to
plan** keeps using the exact tozar; the Mapping Plan contract is unchanged.

| Piece | Where |
|---|---|
| Rules, output contract, the one call, view helpers | `backend/catalog/register/normalization.py` |
| API: view, request, approval | `backend/catalog/register/service.py`, `backend/main.py` |
| Capture job mode | `backend/catalog/operator_capture.py` (`--normalisation-proposal-id`), `backend/capture_invocation.py` |
| Tables and RPCs | `supabase/migrations/20261003000100_catalog_manufacturer_normalization.sql` |
| UI | `frontend/components/register/NormalisationSection.tsx`, `frontend/lib/normalisation.ts` |

## 1. Deterministic pass (no model, $0)

Code-owned rules. The database accepts only these rule ids.

| Rule | Joins |
|---|---|
| `R1_SPELLING` | Names equal up to case, whitespace and punctuation: NFKC, case-folded, every separator, punctuation mark and control character removed. |
| `R2_TOZERET_CD` | Captured tozars whose served variants state the same non-empty set of `tozeret_cd` codes. The register supports this only once both tozars are captured and built. |

- Only unmapped names are grouped.
- The canonical name is the member with the most rows.
- An R1 group is high confidence. An R2 group is low confidence and is approved alone, because one maker's codes can carry two brands.
- A rule group is a pending proposal like the model's: it becomes active only when the owner approves it.

## 2. The one K3 call (decision 14)

**Normalise manufacturers** on the Register page makes one call.

**Input:** every source name still unmapped, each with:
- its row count
- up to three `tozeret_nm`
- up to three sample `kinuy_mishari`

**The call:** `kimi-k3`, role `normaliser` / `manufacturers`. It is low effort, capped at 16,000 output tokens, with a 300 s deadline. It has no tools and no internet.

**Strict JSON contract:**

```json
{"groups": [{"canonical": "...", "members": ["exact input names"], "confidence": "high|low", "reason": "..."}]}
```

The server validates the answer, in Python and again in the database (`catalog_normalization_groups_valid`):
- Every member is an input name: `NORMALIZATION_MEMBER_INVENTED` otherwise.
- No name appears twice: `NORMALIZATION_MEMBER_DUPLICATED`.
- The keys, the confidence and the text bounds are exact: `NORMALIZATION_OUTPUT_SHAPE_INVALID`.
- The answer is JSON: `NORMALIZATION_OUTPUT_NOT_JSON`.
- No control character or lone surrogate, which the database's JSON cannot store: `NORMALIZATION_OUTPUT_SHAPE_INVALID`.

Anything else is recorded as a refused proposal with that code. A budget refusal is recorded as `NORMALIZATION_BUDGET_REFUSED`, any other failure as `NORMALIZATION_MODEL_FAILED`.

**Where it runs.** The call runs in the **existing capture job**, under an operator capture run. That run is the lease and the budget anchor that the gateway's caps need. It is never a product run: no Commander, no run creation, no Arm.

**Cost.** The call goes through the same `ModelGateway`, `BudgetTracker` and provider authority a worker uses, bounded by the reviewed envelope:
- $3.00 per run
- the $10 daily user budget and the $10 daily project budget, reserved through `reserve_model_call_budget`

**Kill switch (decision 33).** The budget's kill switch is the per-execution switch `MILO_ENABLE_MANUFACTURER_NORMALISATION_JOB`, which only the API's invocation sets. It is not `MILO_ENABLE_PAID_EXECUTION`, so the call is allowed while paid runs are off.

**One at a time.** A proposal's request, trigger and liveness record is a register capture group of kind `normalisation`. A second press while one is live answers 200 and starts nothing.

## 3. Approval (decision 15)

Only a project **owner** presses the button (it spends the owner's daily budget) and approves (`project_members.role`); anyone else gets `CATALOG_NORMALIZATION_OWNER_ONLY`.

A model group that the active mapping already holds is no longer pending.

- **Together:** every high-confidence group that conflicts with nothing can be approved in one call.
- **Alone:** a low-confidence group, or a conflicting one, is approved on its own. A group conflicts when it re-maps an active name to another canonical name, or shares a member with another pending group.
- **Exactly as proposed:** the server accepts a group only when it matches one of its own pending groups exactly (canonical name, members, rule or proposal): `CATALOG_NORMALIZATION_APPROVAL_INVALID` otherwise.
- **The database re-checks it:**
  - a model entry must be a group of a `proposed` proposal
  - a rule entry must name a code-owned rule
  - every name must be a tozar of the latest directory
- **Versioning:** an approval creates the next version, which is the whole active mapping. It is a compare-and-set on the active version number: `CATALOG_NORMALIZATION_VERSION_STALE` otherwise.

## The flag

| Flag | Where | Scope |
|---|---|---|
| `MILO_ENABLE_MANUFACTURER_NORMALISATION` | API | Stage A pinned off. The button. The view and approvals ride the Register page's flag, `MILO_ENABLE_REGISTER_CAPTURE`. |
| `MILO_ENABLE_MANUFACTURER_NORMALISATION_JOB` | Capture job, per execution | Pinned off on the job. |

While the stage is on, the capture job carries the provider key (`KIMI_API_KEY`) and the shared quota store (`UPSTASH_REDIS_REST_*`) as secret bindings. The capture identity has `roles/secretmanager.secretAccessor` on exactly those three secrets. That grant is a standing one: the capture identity falls back to the worker's, which needs the key. The job's bindings go away in three ways:
- every capture-job ensure (`--set-secrets`) re-creates the job without them
- a Stage A deploy
- the kill switch, which removes `KIMI_API_KEY` from the capture job and reads it back

## Operator steps

1. **Apply the migration** `20261003000100_catalog_manufacturer_normalization.sql` with the release.
2. **Turn it on:** Actions -> **Website stage** -> `stage = manufacturer-normalisation`, or `bash scripts/ops/website-stage.sh --stage manufacturer-normalisation`. It runs three steps:
   - it ensures the capture job
   - it re-applies the Register page (register-capture)
   - it binds the key and the store on the capture job and sets the API flag, reading back that run creation and paid execution are OFF

   It is never part of `all`: it binds a paid provider key, so it is always its own decision.
3. **After a deploy:** dispatch **Deploy** with `restore_website_stage = manufacturer-normalisation`, or re-run step 2.
4. **Use it:** open **Register**, press **Normalise manufacturers**, reload after the job ends, and approve.

A read-only role created after the migration gets no reads. Grant them the same way as PR-D1's roles:

```sql
grant select on public.catalog_manufacturer_normalization_proposals,
  public.catalog_manufacturer_normalization_versions, public.catalog_manufacturer_normalization_entries to <role>;
grant execute on function public.catalog_normalization_groups_valid(jsonb,jsonb),
  public.catalog_manufacturer_normalization_current(), public.catalog_manufacturer_evidence() to <role>;
```
