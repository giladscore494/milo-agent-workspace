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
| API: view, request, approval, rejection | `backend/catalog/register/service.py`, `backend/main.py` |
| Normalisation job mode | `backend/catalog/operator_capture.py` (`--normalisation-claim`), `backend/capture_invocation.py` (`normalisation_job_arguments`, `NO_OVERRIDES`) |
| The job: create / remove, posture | `scripts/catalog/government-production-capture.sh --ensure-normalisation-job`, `scripts/deploy/website-execution-activate.sh`, `scripts/deploy/deployment-contract.sh` (`milo_normalisation_*`) |
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

**Where it runs.** The call runs in its **own small job**, the normalisation job (`CLOUD_RUN_NORMALISATION_JOB`, default `<capture job>-normalisation`): the capture job's definition, run as the **worker identity**, and the only job that binds the provider key and the quota store. The capture job and the capture identity never hold the key (`iam:capture-cannot-read-provider-key` stays PASS). The call runs under an operator capture run, which is the lease and the budget anchor the gateway's caps need. It is never a product run: no Commander, no run creation, no Arm.

**Executed exactly as defined -- no overrides.** The job holds the provider key, so nobody may change what it runs: an override (`--args`, `--command`, env) would let whoever controls the caller run `python -c ...` with the key.
- The job's **whole invocation is its definition** (`government-production-capture.sh --ensure-normalisation-job`): `python -m backend.catalog.operator_capture --execute ... --normalisation-claim`, with `MILO_ENABLE_MANUFACTURER_NORMALISATION_JOB=true` baked in. It names no run and no proposal.
- The API executes it with an **empty** `jobs.run` body (`capture_invocation.NO_OVERRIDES`). Before that, it has recorded the proposal's run on its request group.
- The job **claims** its work from the database. `requested_manufacturer_normalization()` gives the newest request, while it is still `requested`, has a run and its trigger did not fail (a request nobody is waiting for is never picked up later); none, and the job refuses (`CAPTURE_NORMALISATION_NOTHING_REQUESTED`) and starts nothing. `claim_manufacturer_normalization(proposal, run, worker, lease)` then, under the proposal lock, re-checks that request and takes the run's lease through the existing `claim_run_lease` CAS. A second execution finds the lease held (`CATALOG_NORMALIZATION_ALREADY_CLAIMED`) or the proposal answered (`CATALOG_NORMALIZATION_NOT_REQUESTED`). The outcome is recorded under that lease (`assert_worker_lease`).
- The API identity holds **`roles/run.jobsExecutor`** on that job (`run.jobs.run`; its read of the job for the release check, `run.jobs.get`, comes from `roles/run.viewer`, which the executor roles do not carry), **never** a role carrying `run.jobs.runWithOverrides` -- or `run.jobs.update`, `run.jobs.create`, `run.jobs.replace`, `run.jobs.setIamPolicy`, which would let it rewrite the job's definition and then run it plainly -- not on the job, not on the project. (Bindings through groups, domains or an inherited folder or organisation policy are not read: the check reads the job's and the project's own policies.) The enable path reads that back from IAM itself (`gcloud iam roles describe` of every role the API holds there) and unwinds otherwise; `production-preflight.sh` (`iam:api-cannot-override-normalisation-job`) and `production-verify.sh` (`NORMALISATION_JOB_OVERRIDES`, part of `CODE_DEPLOYED`) report BLOCKED while the job exists and any such role is held, or any policy or role cannot be read.

**Cost.** The call goes through the same `ModelGateway`, `BudgetTracker` and provider authority a worker uses, bounded by **the deployment's RuntimePolicy caps** (`resolve_runtime_policy(paid=True)`: the worker's applied caps, copied onto the job; a missing cap refuses the call, `NORMALIZATION_POLICY_REFUSED`):
- the per-run cap
- the daily user and daily project budgets, checked in Python from the ledger before the call and reserved through `reserve_model_call_budget`

**Names are data.** The prompt tells the model to treat every tozar, plant and model name as a string to group and to ignore any instruction inside one.

**Kill switch (decision 33).** The job exists only while the stage is on, and deleting it is the kill switch (below). It is not `MILO_ENABLE_PAID_EXECUTION`, so the call is allowed while paid runs are off.

**One at a time, never twice for the same question.** A proposal's request, trigger and liveness record is a register capture group of kind `normalisation` (`request_manufacturer_normalization`, in the database):
- A second press while one is live answers 200 and starts nothing (`existing`).
- When the newest `proposed` proposal was made from the **same input** (`input_sha256`), it is answered again (`reused`) and no model call starts.
- A new request waits out a **cooldown** of 600 s after the last one (`REQUEST_COOLDOWN_SECONDS`; the API answers 429 `CATALOG_NORMALIZATION_COOLDOWN`) -- except after a request whose trigger failed, which started nothing and spent nothing.
- A source name carrying a format character (below) is not sent: the answer could not name it. R1 still joins it (its key drops the character).
- More unmapped names than one call may carry (2,000) is refused (422 `CATALOG_NORMALIZATION_INPUT_TOO_LARGE`), never silently cut; each evidence string is at most 120 characters (the database refuses more).

**A proposal moves once.** A trigger lets a proposal be born only `requested` with no outcome, and then allows only `requested -> proposed` (its groups valid against its own input, checked again) or `requested -> refused`; nothing else of a proposal is ever updated, so validated groups cannot be rewritten below the RPCs (`CATALOG_NORMALIZATION_PROPOSAL_IMMUTABLE`). Rule provenance (`R1_SPELLING`, `R2_TOZERET_CD`) is trusted from the service layer: the database checks the rule id and that every name is in the latest directory, but does not recompute the rule (R1 is Unicode NFKC plus case folding, which SQL cannot restate exactly).

**Format characters.** A proposed canonical name or member carrying a Unicode format character (category Cf: bidi overrides and isolates, zero-width characters, the BOM, tag characters) is refused, in Python and in the database (`catalog_normalization_has_format_char`); a rule group's canonical name is always a member without one.

**Accepted, pre-existing risk (tracked as PR-KEY).** The provider key can still be reached outside this job, independent of normalisation:
- the worker identity holds a **standing** `roles/secretmanager.secretAccessor` on `KIMI_API_KEY` (the Stage 2 worker reads it; nothing revokes it at Stage A);
- every deploy grants the API identity `roles/run.jobsExecutorWithOverrides` on the **worker** job (`scripts/deploy/cloud-run.sh`, the launcher's `RUN_ID` override), and the worker job runs as that identity;
- so a compromised API can run the worker job with an override and read the key through the worker identity -- whether or not normalisation is on.

The checks above cover the normalisation job only. PR-KEY (after the catalog release) closes it: a dedicated normalisation service account holding the key's accessor only while the stage is on; the worker's accessor granted at Arm and revoked, with read-back, at Stage A, in the kill switch and in Deploy's reset; preflight BLOCKED while the worker identity reads the key unarmed; and a check of `iam.serviceAccountTokenCreator` / `iam.serviceAccountUser` (actAs) on the worker identity.

## 3. Approval (decision 15)

Only a project **owner** presses the button (it spends the owner's daily budget) and approves (`project_members.role`); anyone else gets `CATALOG_NORMALIZATION_OWNER_ONLY`.

The mapping is **deployment-wide** (one register, one canonical name per tozar): an owner of any project on this deployment approves or rejects for every project. That fits the single-tenant deployment this is built for; a multi-tenant one would need an operator role instead.

A model group that the active mapping already holds is no longer pending.

- **Together:** every high-confidence group of two or more names that conflicts with nothing can be approved in one call. A single-name group (it re-names one source) is always approved on its own.
- **Alone:** a low-confidence group, or a conflicting one, is approved on its own. A group conflicts when it re-maps an active name to another canonical name, or shares a member with another pending group.
- **Exactly as proposed:** the server accepts a group only when it matches one of its own pending groups exactly (canonical name, members, rule or proposal): `CATALOG_NORMALIZATION_APPROVAL_INVALID` otherwise.
- **The database re-checks it:**
  - a model entry must be a group of a `proposed` proposal
  - a rule entry must name a code-owned rule
  - every name must be a tozar of the latest directory
- **Reject:** any pending group can be rejected (owner only, the same exact-match rule, `CATALOG_NORMALIZATION_REJECTION_INVALID` otherwise). A rejection is an append-only record; the group is no longer pending.
- **Versioning:** an approval creates the next version, which is the whole active mapping. It is a compare-and-set on the active version number: `CATALOG_NORMALIZATION_VERSION_STALE` otherwise.

## The flag

| Flag | Where | Scope |
|---|---|---|
| `MILO_ENABLE_MANUFACTURER_NORMALISATION` | API | Stage A pinned off. The button. The view and approvals ride the Register page's flag, `MILO_ENABLE_REGISTER_CAPTURE`. |
| `MILO_ENABLE_MANUFACTURER_NORMALISATION_JOB` | Normalisation job | Baked **on** into the normalisation job's own definition (never an override); pinned off on the capture job. |

The normalisation job exists exactly while the stage is on: it carries the provider key (`KIMI_API_KEY`) and the shared quota store (`UPSTASH_REDIS_REST_*`) as secret references, read by the worker identity. `production-preflight.sh` (`cloud-run:normalisation-job`) and `production-verify.sh` (`NORMALISATION_JOB`, part of `CODE_DEPLOYED`) report it: present with the flag on PASS, absent with the flag off PASS, anything else BLOCKED. It is deleted, its absence read back, and any role the capture identity holds on the provider key or the quota store revoked and read back, by:
- `website-execution-activate.sh --remove-manufacturer-normalisation` (the API flag off first; every step runs even if one fails). It needs only `GCP_PROJECT_ID`, `GCP_REGION` and `CLOUD_RUN_API_SERVICE` from the configuration -- no gateway, worker or policy value -- and a project with no API service has no flag to turn off.
- **Actions -> Website stage -> `stage = normalisation-off`** (`website-stage.sh --stage normalisation-off`): that same path, on its own
- every Stage A deploy (`deploy.sh` step 6): that same path; any failure fails the step and stops the deploy
- the kill switch (step 6)

The Register page and the catalog browser only label source names with the mapping: when it cannot be read they show the exact source names instead of failing.

`production-preflight.sh` also holds, in every state:
- `iam:capture-cannot-read-provider-key`: the capture identity holds **no role at all** (a conditional binding included) on the provider key or either quota-store secret; a policy that cannot be read is BLOCKED, never a pass.
- `iam:no-project-level-secret-access`: neither the capture nor the API identity holds `roles/secretmanager.secretAccessor`, `secretmanager.admin`, `editor` or `owner` on the project (each would reach every secret); an unreadable project policy is BLOCKED.

## Operator steps

1. **Apply the migration** `20261003000100_catalog_manufacturer_normalization.sql` with the release.
2. **Turn it on:** Actions -> **Website stage** -> `stage = manufacturer-normalisation`, or `bash scripts/ops/website-stage.sh --stage manufacturer-normalisation`. It runs three steps:
   - it ensures the capture job
   - it re-applies the Register page (register-capture)
   - it creates the normalisation job as the worker identity with the worker's caps, the key and the store (read back), proves the capture job binds no key and the capture identity reads none, binds the API identity's read on the job, and sets the API flag, reading back that run creation and paid execution are OFF

   The worker identity must already read the provider key and the quota store (as for Stage 2).

   It is never part of `all`: it binds a paid provider key, so it is always its own decision.
3. **After a deploy:** a Stage A deploy deletes the job; dispatch **Deploy** with `restore_website_stage = manufacturer-normalisation`, or re-run step 2.
4. **Use it:** open **Register**, press **Normalise manufacturers**, reload after the job ends, and approve or reject.
5. **Turn it off:** Actions -> **Website stage** -> `stage = normalisation-off` (or `bash scripts/ops/website-stage.sh --stage normalisation-off`, or the kill switch).

A read-only role created after the migration gets no reads. Grant them the same way as PR-D1's roles:

```sql
grant select on public.catalog_manufacturer_normalization_proposals,
  public.catalog_manufacturer_normalization_versions, public.catalog_manufacturer_normalization_entries,
  public.catalog_manufacturer_normalization_rejections to <role>;
grant execute on function public.catalog_normalization_groups_valid(jsonb,jsonb),
  public.catalog_normalization_has_format_char(text),
  public.catalog_manufacturer_normalization_current(), public.catalog_manufacturer_evidence() to <role>;
```
