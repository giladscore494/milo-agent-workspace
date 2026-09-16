# Frontend acceptance matrix — Stage F5

**Tested tree:** `ad50a466aa2dd81d13f3435d2ac552719245fdec`
**Base:** `0fdab212ce3c6277fa6d88502a4213401e1a3bcc` (merge of PR #89, Stage F4)

Every local verification result recorded here was executed against that tree.
Commits after it in the F5 pull request change documentation only, which is
checkable from the diff; CI results on the exact pull-request head are recorded
in the pull-request description.

This document closes the F1–F5 frontend track. It is the frontend counterpart of
[FINAL_ACCEPTANCE.md](FINAL_ACCEPTANCE.md) and uses a classification set of its
own, because "is this behaviour proven" and "is this service configured" are
different questions:

| Classification | Meaning |
| --- | --- |
| `IMPLEMENTED_AND_PROVEN` | the production path exists and an automated test exercises **that path**, not a re-implementation of it |
| `IMPLEMENTED_TEST_GAP` | the production path exists; the evidence does not fully cover it, and the gap is stated |
| `CONFIRMED_DEFECT` | reproduced; the fix and the test that fails without it are named |
| `REQUIRES_MANUAL_OPERATOR_CONFIGURATION` | only a human with real production access can confirm it; no fixture can |
| `INTENTIONALLY_DEFERRED` | deliberately not built, with the reason and the condition to revisit |
| `OUT_OF_SCOPE` | belongs to another stage |

## What a fixture can and cannot prove

Everything below marked `IMPLEMENTED_AND_PROVEN` is proven **against an isolated
stack**: a mock Supabase auth server, a `MemoryRepository`, an in-process worker
with mocked model adapters, and the real FastAPI app with its real authorization,
execution flags, idempotency and identity verification. No paid model call, no
live source capture, no production Supabase and no Cloud Run job is reachable
from it.

That stack proves **behaviour**. It proves nothing about live production
configuration — Cloud Run privacy, the real Vercel environment, Secret Manager
IAM, or the production Supabase project. Those rows are
`REQUIRES_MANUAL_OPERATOR_CONFIGURATION` and stay that way until the
authenticated read-only audit in [SMOKE_TESTING.md](SMOKE_TESTING.md) is run
against the real project by an operator.

---

## A. Authentication and authorization

| # | Requirement | Classification | Production path | Automated evidence | Limitation |
| --- | --- | --- | --- | --- | --- |
| A1 | Sign-in, session restoration, sign-out | `IMPLEMENTED_AND_PROVEN` | `lib/supabaseClient.ts`, `app/page.tsx` session effect | E2E 1–3, 27; `tests/workspace.test.tsx` (restore screen, login-only UI) | mock Supabase auth server |
| A2 | Sign-out removes rendered workspace data **and** invalidates in-flight client state | `IMPLEMENTED_AND_PROVEN` (was `CONFIRMED_DEFECT`) | `app/page.tsx` `logout()` + session effect, `clearStoredRunIds`, `nextSessionScope` | `tests/stateOwnership.test.tsx` "projects answering after sign-out…", "one user's projects never render for the user who replaced them"; E2E 27, 27b | — |
| A3 | Another user's project, conversation and run are inaccessible | `IMPLEMENTED_AND_PROVEN` | `backend/auth.py` membership; gateway identity headers built server-side | E2E 5, 6, 10, 17 | authorization is enforced server-side; the browser is not the boundary |
| A4 | Browser-supplied identity headers remain untrusted | `IMPLEMENTED_AND_PROVEN` | `app/api/gateway/[...path]/route.ts` builds `Headers` from scratch | E2E 17 (spoofed `x-milo-auth-*` returns the token owner's memberships); `tests/gatewayRoute.test.ts` | — |
| A5 | The gateway validates Supabase access tokens server-side | `IMPLEMENTED_AND_PROVEN` | `lib/server/supabaseAuth.ts` | `tests/gatewayRoute.test.ts`; E2E identity spec | token validation calls the mock auth server |
| A6 | The browser calls only the gateway, never a privileged backend URL | `IMPLEMENTED_AND_PROVEN` | `lib/api.ts` (`const API = '/api/gateway'`); `CLOUD_RUN_API_URL` is read only in `lib/server/` | `npm run test:secrets` (the variable name is forbidden in any client chunk); `tests/secretBundleCheck.test.ts` | — |
| A7 | Cloud Run API privacy is an authenticated server-side boundary | `REQUIRES_MANUAL_OPERATOR_CONFIGURATION` | `lib/server/cloudRunAuth.ts` → WIF → `X-Milo-Gateway-Token`; `backend/gateway_auth.py` fail-closed 503 | E2E identity spec proves a browser token and a worker identity are both rejected as gateway identities, with verification ACTIVE in both stacks | **the live Cloud Run ingress setting is not proven by any test here.** `scripts/release/check-gcp-resources.sh` against the real project is the only evidence |
| A8 | Service-role, provider and worker credentials never reach browser code, HTML or bundles | `IMPLEMENTED_AND_PROVEN` | `frontend/scripts/no-secret-bundle-check.mjs`; `scripts/check_unsafe_defaults.py` | `npm run test:secrets` (source **and** built bundle); `tests/secretBundleCheck.test.ts` (9 staged failures); E2E 28 (served page + **every** served script) | scans the artifacts this repository produces; a credential injected by a platform at deploy time is operator territory |
| A9 | Only approved `NEXT_PUBLIC_*` variables enter the browser | `IMPLEMENTED_AND_PROVEN` (new) | `APPROVED_PUBLIC_VARS` in `frontend/scripts/no-secret-bundle-check.mjs`, mirroring [ENVIRONMENT_MATRIX.md](ENVIRONMENT_MATRIX.md) | `tests/secretBundleCheck.test.ts`; E2E 28 | the allowlist is repository-side; adding a variable in Vercel without adding it to code changes nothing in the bundle |

**A2 — the defect, exactly.** `loadProjects` applied whatever
`api.projects()` resolved with, whenever it resolved. A response in flight when
the user signed out — or when a different user signed in — was written into the
page it landed on. Reproduced in `tests/stateOwnership.test.tsx`; both tests
fail against `0fdab21`.

**A2 — the second defect, found by review of the first fix.** Guarding what
RENDERS is not the same as guarding what is WRITTEN. `startRun` called
`storeRunId` and only then checked ownership, so a run-creation response
belonging to a signed-out session put a key back into `sessionStorage` that
sign-out had just removed — browser state outliving the session that produced
it, which is precisely what A2 requires not to happen. Reproduced on `ae2ce45`:
with Alice's request held open, signing out cleared
`milo.activeRun.<conversation>`; resolving the request restored Alice's run id
to it.

Ownership is now checked BEFORE any durable browser-side write, and the three
outcomes are separated: a response from a replaced or signed-out session stores
nothing and activates nothing; a response in the same session but a different
conversation is stored under the conversation it actually belongs to and is
never activated under the one on screen; only a response that still owns the
conversation becomes the active run.

**A2 — and the pending state that crossed with it.** `creatingConversation`,
`proposalBusy` and `submittingRun` were booleans cleared by an unconditional
`finally`. Two failures follow, and clearing them on sign-out fixes only one:
a replacement session INHERITED the busy flag (reproduced: after signing out
mid-request and signing in as another user, "New conversation" read
"Creating conversation…" and was disabled), and a superseded request settling
CLEARED its successor's. Each flag now holds which request is in flight
(`beginPending`/`settlePending` in `lib/ownership.ts`), so settling compares
identity and a session change simply drops the token. The idempotency key is
scoped the same way — to one session, one conversation and one content — so it
is reused for a genuine retry and never inherited by a different submission.

---

## B. Run lifecycle

| # | Requirement | Classification | Production path | Automated evidence | Limitation |
| --- | --- | --- | --- | --- | --- |
| B1 | Idempotent run creation | `IMPLEMENTED_AND_PROVEN` | `lib/api.ts` `idempotency_key`; `app/page.tsx` keeps the key across a failure; `backend/main.py` `_create_and_launch_run` | E2E 12+13, 14, 15; `tests/workspace.test.tsx` "prevents double submission" | — |
| B2 | Launch states are distinct from run terminal states | `IMPLEMENTED_AND_PROVEN` | `lib/types.ts` `LaunchState` vs `lib/runStatus.ts`; `components/run/LaunchStateNote.tsx` | `tests/accessibility.test.tsx` (launch note vs verdict); E2E F5-L1 (`launch_state=launch_unknown` while `status=queued`) | — |
| B3 | Polling is the supported path; Realtime remains deferred | `IMPLEMENTED_AND_PROVEN` | `lib/useRunRealtime.ts` polls; Realtime is not wired | `tests/realtimeDisabled.test.ts`; `tests/swarmV2Polling.test.ts` | — |
| B4 | A temporary reconnect preserves already-rendered state | `IMPLEMENTED_AND_PROVEN` | `useRunRealtime` backoff + `reconnecting` mode; the reducer is never reset on failure | `tests/swarmV2Polling.test.ts` O and P | — |
| B5 | Cancellation requested is not displayed as terminal cancellation | `IMPLEMENTED_AND_PROVEN` | `components/swarm/SwarmRunCard.tsx` (`cancellationRequested`), `lib/runStatus.ts` | `tests/swarmRunCard.test.tsx`; E2E 22 | — |
| B6 | Refresh and navigation reconstruct the same authorized run | `IMPLEMENTED_AND_PROVEN` | `app/page.tsx` `readStoredRunId` + hook verification | E2E 21, F4-5; `tests/finalResultRouting.test.tsx` 6, 6b | — |
| B7 | All six terminal states are represented correctly | `IMPLEMENTED_AND_PROVEN` | `lib/runStatus.ts` (single mirror of `backend/runtime.py` TERMINAL_STATES) | `completed` E2E 20+26 / F4-1; `partial_success` **E2E 25b** (new); `failed` E2E 25; `cancelled` E2E 22; `timed_out` E2E 24; `budget_exhausted` E2E 23; plus `tests/runIsolation.test.ts` "stops polling on every terminal state" | each terminal state is produced by the mocked worker through the real transition and outcome code, not written into the row by the test |
| B8 | `launch_unknown` is visible as requiring reconciliation and never implies automatic retry | `IMPLEMENTED_AND_PROVEN` (was `CONFIRMED_DEFECT`) | `components/run/LaunchStateNote.tsx`; `backend/main.py` `JobLaunchUncertain` handling | **E2E F5-L1, F5-L2** (new); `tests/accessibility.test.tsx` announcement tests | — |
| B9 | Automatic `launch_unknown` reconciliation | `INTENTIONALLY_DEFERRED` | — | — | guessing wrong means a double execution and a double spend. Operator-resolved with `scripts/release/reconcile-launch-unknown.sh`; condition to revisit is recorded in [FINAL_ACCEPTANCE.md](FINAL_ACCEPTANCE.md) |

**B8 — what the new E2E proves.** A test-only launcher seam raises the
**production** `JobLaunchUncertain` exception; every downstream step is
production code. `frontend/e2e/enabled.launch-unknown.spec.ts` asserts:

* the run is parked with `launch_state = launch_unknown` and `status = queued`;
* the user receives HTTP 502 `JOB_LAUNCH_UNKNOWN` with no traceback, no Cloud Run
  hostname, no bearer token and no key-shaped value, and the launch exception
  itself is stripped from the browser response;
* an explicit idempotent replay returns the **same** run id, twice;
* the launcher was invoked exactly once — exactly one `launch_failed` event,
  zero `run_created`, zero `run_started`, still true after a delay;
* the UI states that reconciliation is required, that the run will **not** be
  relaunched automatically, and that nothing on the screen retries it;
* no browser timer, polling path or API path relaunches it.

**B8 — the defect, exactly.** `launch_unknown` rendered as a muted
footnote reading "reconciliation required" and said nothing about retry, on a
screen whose other affordance is a retryable submit button.

---

## C. Swarm execution

| # | Requirement | Classification | Production path | Automated evidence | Limitation |
| --- | --- | --- | --- | --- | --- |
| C1 | Logical tasks are never presented as agents | `IMPLEMENTED_AND_PROVEN` | `lib/swarmReducer.ts` keys tasks by `payload.task_id`; there is no agent registry for Swarm V2 | `tests/swarmV2Reducer.test.ts`, `tests/swarmRunCard.test.tsx` | — |
| C2 | Tool calls, output repairs and verifier batches never increase task count | `IMPLEMENTED_AND_PROVEN` | `lib/swarmReducer.ts` counters on an existing task | `tests/swarmV2Reducer.test.ts`, `tests/swarmV2ViewModel.test.ts` | — |
| C3 | Model-call count comes only from authoritative `run.usage` | `IMPLEMENTED_AND_PROVEN` | `lib/runUsage.ts`; nothing sums events | `tests/runUsageContract.test.ts`, `tests/swarmV2ViewModel.test.ts` | — |
| C4 | Unknown event types fail safely | `IMPLEMENTED_AND_PROVEN` (was `CONFIRMED_DEFECT`) | `lib/eventVocabulary.ts` decides what each type owns; `lib/runReducer.ts` gates the whole V1 projection on it; `lib/swarmReducer.ts` folds the swarm slice | **`tests/eventProjection.test.ts`** — through `reduceRunEvent` AND `useRunRealtime`; plus `tests/swarmV2Reducer.test.ts` | — |
| C5 | Unknown or hostile events cannot manufacture trusted lifecycle transitions, tasks, agents or usage | `IMPLEMENTED_AND_PROVEN` (was `CONFIRMED_DEFECT`) | same, plus sticky terminal phases and non-regressing task status | **`tests/eventProjection.test.ts`** (10 of its 15 cases fail against `ae2ce45`), `tests/swarmV2Reducer.test.ts`, `tests/displaySecurity.test.tsx` | — |
| C6 | Event ordering, de-duplication and PostgreSQL bigint ids remain lossless | `IMPLEMENTED_AND_PROVEN` | `lib/losslessJson.ts`, `lib/eventId.ts`, `lib/runReducer.ts` | `tests/eventId.test.ts`, `tests/swarmV2Reducer.test.ts`, `tests/swarmV2Polling.test.ts` | — |
| C7 | Late events or responses from an earlier run cannot mutate the active run | `IMPLEMENTED_AND_PROVEN` (was `IMPLEMENTED_TEST_GAP`) | generation counter **plus** `runBelongsToScope` / `eventBelongsToRun` in `lib/useRunRealtime.ts` | `tests/runIsolation.test.ts` (4 new cases); `tests/ownership.test.ts`; E2E "switching runs does not retain stale state" | — |

**C7 — what was missing.** The generation counter answered "is this answer for
the run the hook is on". It could not answer "is this answer that run": the
fetched row was dispatched without checking `run.id`, and events were folded
without checking `event.run_id`. Both are now checked before anything is
dispatched.

**C4/C5 — the defect the first F5 revision missed, exactly.** The evidence
pointed at `reduceSwarmEvent`, which is not the production path.
`reduceRunEvent` wraps it and runs the V1 projection — agents, lifecycle phase,
progress, sources, claims, conflicts, event-derived spend — on the event's
FIELDS before anything asked whether its TYPE was recognised. Reproduced on
`ae2ce45`: a single event of type `future_unknown_failed_signal` carrying
`agent: "invented-agent"`, `phase: "completed"` and
`payload: {tokens: 999999, cost_usd: 1234}` produced an agent named
`invented-agent` with status `failed` (picked out of a substring of the
invented type's own name), a `completed` phase, and those exact spend totals.
A recognised `task_started` carrying a hostile `agent` field likewise created a
V1 agent, though Swarm V2 has no agent concept.

Recognition now comes first and is by exact type, never substring.
`lib/eventVocabulary.ts` mirrors `EVENT_TYPES` from `backend/runtime.py` — the
set the API itself validates worker-written events against — and splits it into
what each type owns: the V1 projection, the agent registry (the V1 set minus the
run-level types), and the event-derived spend telemetry. A Swarm V2 type never
owns any of them. An unrecognised type stays visible as developer telemetry (the
raw event stream and `swarm.unknownEventTypes`) and touches nothing else, so a
new backend event type is inert in the browser until it is added on purpose.

---

## D. Final Result and state ownership

### D.1 Merged F4 contracts, re-run

All of F4 is preserved and re-verified on this tree. `RunOutputPanel` still has
a zero-line diff from `0fdab21`; `redactSecrets` is byte-identical.

| Contract | Classification | Evidence |
| --- | --- | --- |
| No raw Swarm V2 product JSON | `IMPLEMENTED_AND_PROVEN` | `scripts/static-ui-check.mjs` forbids `JSON.stringify`/`dangerouslySetInnerHTML` under `components/result`; `tests/staticUiCheck.test.ts`; E2E F4-1 |
| Fail-closed invalid payload handling | `IMPLEMENTED_AND_PROVEN` | `tests/finalResult.test.ts` (87 cases), `tests/finalResultRouting.test.tsx` 7 |
| Complete provenance requirements | `IMPLEMENTED_AND_PROVEN` | `tests/finalResult.test.ts` strict-provenance cases; fixtures generated through the real `EvidenceReference` + `FinalBuilder` |
| Secret redaction across scalar and structured values | `IMPLEMENTED_AND_PROVEN` | `tests/redactSecretText.test.ts`, `tests/finalResultPanel.test.tsx` DOM sweep |
| JSON-only value handling | `IMPLEMENTED_AND_PROVEN` | `tests/finalResult.test.ts` (`VALUE_NOT_JSON`, `undefined` in all three positions) |
| Correct partial-result copy with no review items | `IMPLEMENTED_AND_PROVEN` | E2E F4-4b, F4-4c; four backend-generated partial fixtures |
| Distinct usable / partial / not-found / no-usable-result states | `IMPLEMENTED_AND_PROVEN` | `tests/finalResultPanel.test.tsx`, E2E F4-1/3/4 |
| No success presentation for invalid or contradictory payloads | `IMPLEMENTED_AND_PROVEN` | `tests/finalResult.test.ts` status/kind pairing and terminal-status mismatch cases |
| Deterministic reconstruction after refresh | `IMPLEMENTED_AND_PROVEN` | E2E F4-5 (identical `innerHTML`), `tests/finalResultRouting.test.tsx` 6/6b |
| V1 keeps its existing sanitized output path | `IMPLEMENTED_AND_PROVEN` | E2E F4-8; `tests/displaySecurity.test.tsx`; `RunOutputPanel` unchanged |

**`not_found`.** `IMPLEMENTED_AND_PROVEN` as a **parser and rendering contract**;
it remains **unreachable in production** because no registered tool returns
`TRUSTED_SOURCE_NO_MATCH` (`docs/catalog-pr3-swarm-and-promotion.md` §10). F5
tests the contract and deliberately invents no producer and changes no tool
registration.

### D.2 Client state-ownership audit

Each risk the F5 brief names, audited against the pre-F5 tree.

| # | Risk | Finding | Classification | Fix | Failing-first evidence |
| --- | --- | --- | --- | --- | --- |
| D-1 | A stored run id trusted merely because it was stored under a conversation key | **present** | `CONFIRMED_DEFECT` | the hook verifies the row; the page clears the id and says so | `tests/stateOwnership.test.tsx` "a stored run belonging to another conversation…" |
| D-2 | A fetched run not matched against both the requested id and the selected conversation | **present** | `CONFIRMED_DEFECT` | `runBelongsToScope` before any dispatch | `tests/runIsolation.test.ts` (2 cases), `tests/stateOwnership.test.tsx` (2 cases) |
| D-3 | Events not checked against the active run | **present** | `CONFIRMED_DEFECT` | `eventBelongsToRun` at ingest | `tests/runIsolation.test.ts` "never folds an event that names another run" |
| D-4 | A mismatched, unauthorized or malformed stored run rendering under the wrong conversation | **present** | `CONFIRMED_DEFECT` | nothing dispatched, polling stopped, id cleared from session storage, one authored sentence shown | `tests/stateOwnership.test.tsx` (both refusal cases) |
| D-5 | Late async responses crossing session, project, conversation or run boundaries | **present, seven paths** | `CONFIRMED_DEFECT` | `lib/ownership.ts` scope captured per request | the seven cases below |
| D-6 | A durable browser-side write (session storage) performed BEFORE the ownership check | **present** | `CONFIRMED_DEFECT` (found reviewing the D-5 fix) | ownership checked first; store-under-its-own-conversation and activate-here separated | `tests/stateOwnership.test.tsx` "a run created after sign-out is neither stored nor rendered", "…for the previous user…" |
| D-7 | Busy flags crossing a session boundary in both directions | **present** | `CONFIRMED_DEFECT` (same review) | `beginPending`/`settlePending`: the flag holds WHICH request is in flight | `tests/stateOwnership.test.tsx` "a replacement session starts unblocked…", "an old request settling cannot clear the newer owner's busy state" |
| D-8 | An idempotency key outliving its logical submission | **present** | `CONFIRMED_DEFECT` (same review) | the key is scoped to (session, conversation, content) | `tests/stateOwnership.test.tsx` "the idempotency key is scoped to its submission" (3 cases) |

**The seven delayed-response races.** Each is reproduced by holding the request
open, changing the selection, then resolving it. All eleven tests in
`frontend/tests/stateOwnership.test.tsx` fail against `0fdab21` and pass here.

| Race | Test |
| --- | --- |
| project A request completing after project B was selected | "project A conversations arriving after project B was selected are dropped" (and its failure twin) |
| conversation list A completing after another project was selected | same pair |
| conversation creation completing after the project changed | "a conversation created for project A is not added to, or selected in, project B" |
| run creation for conversation A completing after conversation B was selected | "a run created for conversation A never becomes the active run of conversation B" (and its failure twin) |
| run A polling completing after run B became active | `tests/runIsolation.test.ts` "ignores a late response from the previous run" + the two new row-verification cases |
| outstanding requests completing after sign-out or session/user replacement | "projects answering after sign-out…", "one user's projects never render for the user who replaced them" |
| proposal responses completing after their owning project changed | "a proposal answering after its project changed is dropped", "a proposal decision answering after its project changed is dropped" |

**Mechanism.** `lib/ownership.ts` — an explicit four-level scope
(`session ⊃ project ⊃ conversation ⊃ run`) captured before each request and
compared when it resolves. Not React unmounting, not visual hiding. The session
counter always advances, so an answer in flight across a sign-out is dropped
even when the same user signs back in.

---

## E. Browser-bundle and display security

| # | Requirement | Classification | Production path | Automated evidence | Limitation |
| --- | --- | --- | --- | --- | --- |
| E1 | No `dangerouslySetInnerHTML` or equivalent unsafe rendering path | `IMPLEMENTED_AND_PROVEN` | none exists; `scripts/static-ui-check.mjs` fails the build on one under `components/result` | `tests/staticUiCheck.test.ts`; repository-wide absence | the construct guard is scoped to the final-result surface; the rest is proven by absence |
| E2 | React escaping is not mistaken for credential redaction | `IMPLEMENTED_AND_PROVEN` (new) | `lib/sanitize.ts` keeps `safeText` and `redactSecretText` separate | `tests/displaySecurity.test.tsx` "escaping is not redaction" — asserts `safeText` prints a credential **unchanged** | — |
| E3 | User-visible errors cannot expose provider messages, stack traces, internal URLs, tokens, authorization headers or secret-shaped values | `IMPLEMENTED_AND_PROVEN` (was `CONFIRMED_DEFECT`) | `lib/errorText.ts` — a CLOSED policy: no upstream text is ever rendered, only copy authored there for a classification allowlisted **by value**, or the caller's own fallback. `lib/supabaseClient.ts` raises `AuthFailure` instead of the SDK's message | `tests/errorText.test.ts` (14 cases incl. the six upstream strings the review named), `tests/supabaseClient.test.ts`, `tests/workspace.test.tsx`, E2E 2 and F5-L2 | a classification the product has authored no copy for degrades to the caller's fallback — deliberate, and the reason adding one is an edit beside the copy a user reads |
| E4 | Gateway and API errors retain safe actionable meaning through allowlisted messages/codes | `IMPLEMENTED_AND_PROVEN` | `ERROR_COPY` in `lib/errorText.ts` — one authored sentence per allowlisted classification, plus the code as the support handle (suppressed for `HTTP_*`, which is a label `lib/api.ts` synthesises rather than a code anyone can look up) | `tests/errorText.test.ts` (`EXECUTION_SURFACE_DISABLED`, `JOB_LAUNCH_UNKNOWN` vs `JOB_LAUNCH_FAILED`, idempotency, concurrency, budget, rate-limit and not-found cases); `tests/workspace.test.tsx`; E2E F5-L2 | a SCREAMING_SNAKE shape is not a classification: `REPOSITORY_ERROR` and every `CATALOG_*` code earn the caller's fallback |
| E5 | Hostile project names, conversation titles, events, errors, task fields, result values and provenance remain inert text | `IMPLEMENTED_AND_PROVEN` | `safeText` at every render site; the closed F4 contract for result values | `tests/displaySecurity.test.tsx` (no element, no attribute created); `tests/swarmRunCard.test.tsx`; `tests/finalResultPanel.test.tsx` hostile payloads | — |
| E6 | Only approved `NEXT_PUBLIC_*` variables enter the browser | `IMPLEMENTED_AND_PROVEN` (new) | see A9 | `tests/secretBundleCheck.test.ts`; E2E 28 | — |
| E7 | Service-role/provider/worker secrets absent from source bundles **and served pages** | `IMPLEMENTED_AND_PROVEN` (was `IMPLEMENTED_TEST_GAP`) | `frontend/scripts/no-secret-bundle-check.mjs` now scans `.next*/static` as well as source | `npm run test:secrets`; `tests/secretBundleCheck.test.ts`; E2E 28 scans **every** served script (it previously stopped after ten) | — |
| E8 | Inspector and legacy JSON surfaces remain redacted | `IMPLEMENTED_AND_PROVEN` (was `IMPLEMENTED_TEST_GAP`) | `components/inspector/RunInspector.tsx` and `components/run/RunOutputPanel.tsx` use `redactSecrets` | `tests/displaySecurity.test.tsx` (Claims and Developer tabs, V1 output panel) | `redactSecrets` is pattern-based; an unrecognised credential format is not redacted, which is why the typed contract exists beside it |

### The four defences, and why none substitutes for another

| Defence | Stops | Does nothing about |
| --- | --- | --- |
| HTML/text escaping (`safeText`) | markup becoming markup | a credential; an internal URL; a stack frame |
| structured validation (`parseFinalResult`) | unknown shapes reaching a product surface | anything inside a string the contract allows |
| credential redaction (`redactSecretText`, `redactSecrets`) | credential-shaped substrings being printed | markup; hostnames; operational prose |
| safe error classification (`safeErrorText`) | operational text reaching the screen at all | anything not routed through it |

`tests/displaySecurity.test.tsx` asserts the distinction directly: `safeText`
returns a credential **unchanged**, and `redactSecretText` returns hostile markup
**unchanged**. If either ever stops being true, the two boundaries have been
silently merged.

**E3 — the defect the first F5 revision missed, exactly.** The first version of
`lib/errorText.ts` asked whether a message LOOKED unsafe — credential, URL,
stack frame, markup — and displayed anything that did not. That inverts the
burden. Reproduced on `ae2ce45`:

```
safeErrorText(new ApiError(502, "REPOSITORY_ERROR",
  "OpenRouter upstream quota exhausted for provider account"), "Run creation failed.")
→ "OpenRouter upstream quota exhausted for provider account (REPOSITORY_ERROR)"
```

`Moonshot request rejected by upstream` and `PostgREST connection pool
exhausted` reached the screen the same way. All three pass a "looks harmless"
test and all three tell a user about infrastructure they do not operate, in
words nobody in this repository wrote.

The policy is now closed. `ApiError.message` and `Error.message` are never
rendered; a classification is matched **by value** against `ERROR_COPY` and the
sentence the user reads is authored there. `EXECUTION_SURFACE_DISABLED`,
`JOB_LAUNCH_UNKNOWN`, `JOB_LAUNCH_FAILED`, idempotency, concurrency, budget,
rate-limit, lifecycle and not-found cases each keep a distinct, actionable
meaning; everything else — including every `CATALOG_*` code and
`REPOSITORY_ERROR` — degrades to the caller's own static sentence. Supabase is
covered at its source: `signInWithSupabase` raises an `AuthFailure` built from
the SDK error's HTTP **status** only, so "that email and password combination
was not accepted" is our sentence rather than the SDK's.

**Sentinels.** Every credential-shaped value in the test suite is assembled at
runtime (`frontend/tests/secretSentinels.ts`). No literal that would trip
`scripts/secret_scan.py` or GitHub push protection is committed.

---

## F. Accessibility and responsive behaviour

| # | Requirement | Classification | Production path | Automated evidence | Limitation |
| --- | --- | --- | --- | --- | --- |
| F1 | Full keyboard navigation | `IMPLEMENTED_AND_PROVEN` | native controls throughout; `RunInspector` arrow/Home/End tablist; native `<details>` for provenance | `tests/swarmRunCard.test.tsx`, E2E F4-7 | — |
| F2 | Visible and logical focus | `IMPLEMENTED_AND_PROVEN` | `:focus-visible` in `app/styles.css`; no positive `tabIndex` anywhere | `npm run build` + `app/styles.css`; focus order follows DOM order | contrast of the focus ring is not machine-checked |
| F3 | Cancellation confirmation receives focus when opened | `IMPLEMENTED_AND_PROVEN` (was `CONFIRMED_DEFECT`) | `components/run/CancelRunControl.tsx` | `tests/accessibility.test.tsx` "moves focus into the confirmation when it opens" | — |
| F4 | Escape or "Keep running" closes it and returns focus to its trigger | `IMPLEMENTED_AND_PROVEN` (was `CONFIRMED_DEFECT`) | same | `tests/accessibility.test.tsx` (3 cases, incl. "does not also close an open drawer") | — |
| F5 | Terminal-state changes do not leave focus trapped in removed controls | `IMPLEMENTED_AND_PROVEN` (was `CONFIRMED_DEFECT`) | same, `focusFallbackRef` → the surface heading | `tests/accessibility.test.tsx` "a terminal state that withdraws the control does not strand focus" | — |
| F6 | Mobile drawers receive and restore focus; Escape closes the active drawer | `IMPLEMENTED_AND_PROVEN` (was `CONFIRMED_DEFECT`) | `components/workspace/WorkspaceShell.tsx` | `tests/accessibility.test.tsx` (4 cases) | — |
| F7 | Modal behaviour, if used, has correct focus containment and semantics | `IMPLEMENTED_AND_PROVEN` | **there is no modal.** The cancellation confirmation is a labelled `role="group"` inside a panel that keeps updating | `tests/accessibility.test.tsx` "is a labelled group, not a dialog it does not behave like" | deliberate: claiming `role="dialog"` without trapping focus or marking the page inert would be worse than claiming neither |
| F8 | `aria-live` limited to meaningful lifecycle announcements | `IMPLEMENTED_AND_PROVEN` | one `aria-live="polite"` on the Swarm lifecycle block; `role="status"` on the outcome banner and the `launch_unknown` note | `tests/accessibility.test.tsx` "an ordinary run announces nothing at all on the V1 surface" | — |
| F9 | Status meaning is not colour-only | `IMPLEMENTED_AND_PROVEN` | symbol + text label everywhere; `data-tone` only colours | `tests/accessibility.test.tsx`, `tests/finalResultPanel.test.tsx` | — |
| F10 | Headings and landmark labels remain coherent | `IMPLEMENTED_AND_PROVEN` | labelled `<nav>`/`<aside>`/`<main>`/`<section>`; heading levels descend without skipping | `tests/finalResultPanel.test.tsx`, `tests/workspace.test.tsx` | — |
| F11 | Desktop and phone-width layouts have no horizontal document overflow | `IMPLEMENTED_AND_PROVEN` | `app/styles.css` fluid grids | E2E F4-6 measures `scrollWidth - clientWidth` at 375px | measured on the final-result route at one phone width |
| F12 | Long run ids, event ids, field names, codes and structured values wrap | `IMPLEMENTED_AND_PROVEN` | `overflow-wrap: anywhere` on every identifier/value class | E2E F4-6; `app/styles.css` | — |

**F3–F6 — the defects, exactly.** The confirmation markup existed in both run
surfaces, character for character, with no focus handling at either end: opening
it left focus on the page, closing it left focus nowhere, and a run reaching a
terminal state removed the focused element outright. The drawers had the same
shape of problem. Twelve of the fifteen tests in
`frontend/tests/accessibility.test.tsx` fail against `0fdab21`.

---

## G. Regression and terminal-state evidence

| # | Requirement | Classification | Evidence |
| --- | --- | --- | --- |
| G1 | The isolated Playwright suite covers every terminal state | `IMPLEMENTED_AND_PROVEN` | see B7 — all six, each produced through the real transition and outcome code |
| G2 | Real production state transitions and outcome builders via test-only adapters | `IMPLEMENTED_AND_PROVEN` | `backend/testing/e2e_app.py` uses the shipped `FinalBuilder`, `finalize_product_outcome`, `durable_run_status`, `BudgetTracker` and now the production `JobLaunchUncertain` |
| G3 | No paid model call, live data source, production Supabase or Cloud Run job | `IMPLEMENTED_AND_PROVEN` | in-process worker, mocked model adapters, `MemoryRepository`, mock auth server; `JOB_LAUNCHER=disabled` in both stacks |
| G4 | V1 routing and output preserved | `IMPLEMENTED_AND_PROVEN` | E2E F4-8; `tests/finalResultRouting.test.tsx` 2/3; `RunOutputPanel` zero-line diff |
| G5 | Stage C lifecycle, safety flags and provider boundaries preserved | `IMPLEMENTED_AND_PROVEN` | backend suite 2597 passed / 1 skipped; `scripts/check_unsafe_defaults.py`; `scripts/release/production-readiness.sh` |
| G6 | Gateway allowlists preserved | `IMPLEMENTED_AND_PROVEN` | `tests/gatewayPolicy.test.ts`, `tests/gatewayRoute.test.ts`, E2E 8/11/16 |
| G7 | Worker/gateway identity separation preserved | `IMPLEMENTED_AND_PROVEN` | E2E identity spec (4 cases); `backend/production_config.py` `SHARED_GATEWAY_WORKER_IDENTITY` |
| G8 | Disabled-by-default execution posture preserved | `IMPLEMENTED_AND_PROVEN` | the whole DISABLED Playwright stack; `scripts/check_unsafe_defaults.py` |
| G9 | Cancellation and idempotency behaviour preserved | `IMPLEMENTED_AND_PROVEN` | E2E 12–15, 22; `tests/workspace.test.tsx` |
| G10 | F4 Final Result behaviour preserved | `IMPLEMENTED_AND_PROVEN` | section D.1 |

---

## Local verification results

Executed on `ad50a466aa2dd81d13f3435d2ac552719245fdec`. Local unless marked.
Backend commands need `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` set to the
same offline placeholders CI uses, or `backend.config.Settings` refuses to load.

| Command | Exit | Result |
| --- | --- | --- |
| `python -m pytest -q tests --ignore=MILO-main-original/MILO-main/test_websearch.py` | 0 | **2597 passed, 1 skipped** |
| `python scripts/check_migrations.py` | 0 | passed (static text validation only) |
| `python scripts/secret_scan.py` | 0 | passed |
| `python scripts/check_unsafe_defaults.py` | 0 | passed |
| `python scripts/release/validate_production_manifest.py --manifest config/production.example.yaml --mode plan` | 0 | passed |
| `bash scripts/release/production-readiness.sh` | 0 | `RESULT: OK` (external checks report MANUAL without operator arguments, by design) |
| `docker build -f Dockerfile.api -t milo-agent-api:f5 .` | 1 | **not run** — no Docker daemon in this environment (see below) |
| `docker build -f Dockerfile.worker -t milo-agent-worker:f5 .` | — | **not run** — same |
| `npm ci` (frontend) | 0 | clean install |
| `npm run build` | 0 | production build |
| `npx tsc --noEmit` | 0 | passed |
| `npm test -- --run` | 0 | **501 passed** (401 at base; +100) |
| `npm run test:static` | 0 | passed |
| `MILO_REQUIRE_BUNDLE_SCAN=1 npm run test:secrets` | 0 | source **and** bundle scanned |
| `npx playwright test` | 0 | **47 passed** (43 at base; +4) |

**Not run locally, and why.**

* Both Docker builds — this environment has no Docker daemon
  (`/var/run/docker.sock` does not exist); the attempt exited 1 with
  `failed to connect to the docker API`, which is an environment failure and not
  a build result. F5 changes neither `Dockerfile.api`, `Dockerfile.worker` nor
  `backend/requirements.txt` (verified empty diff against `origin/main`), and CI
  builds both images on the exact pull-request head.
* `MILO_REQUIRE_PG_TESTS=1 pytest -q tests/test_migrations_postgres.py` — F5 adds
  no migration and touches no backend contract, so the PostgreSQL suite has
  nothing new to cover. It runs in CI on the exact pull-request head, where it is
  configured to fail on any skip.
* ShellCheck — the binary is not installed in this environment. No shell script
  was modified by F5, and CI runs it on the exact head.
* `MILO-main-original/MILO-main/test_websearch.py` — forbidden by `AGENTS.md`
  (it makes real provider calls). Never run.

A check that did not run is not green. The rows above say which is which.

---

## Known limitations

1. **Live production posture is not proven here.** Cloud Run ingress, the real
   Vercel environment, Secret Manager IAM and the production Supabase project are
   `REQUIRES_MANUAL_OPERATOR_CONFIGURATION`. The isolated stack cannot confirm
   any of them, and this document does not claim it does.
2. **The E2E stack is a stand-in.** Mock Supabase auth, `MemoryRepository`,
   in-process worker, mocked model adapters. Authorization, execution flags,
   idempotency, gateway identity verification, the outcome contract and the
   launch lifecycle are production code inside it; the storage engine, the
   transport and the model are not.
3. **Redaction is pattern-based.** A credential format not in `SECRET_PATTERNS`
   is not redacted. That is why the typed F4 contract exists beside it and why
   neither is described as sufficient alone.
4. **Error presentation is a closed list.** A classification the product has
   authored no copy for degrades to the caller's static fallback, so a genuinely
   useful upstream message that is not on the list is not shown. Adding one is a
   deliberate edit in `lib/errorText.ts`, beside the sentence a user will read.
5. **`not_found` has no production producer.** Parser and rendering only.
6. **Horizontal-overflow measurement covers one phone width (375px)** on the
   final-result route, not every route at every width.
7. **Focus-ring contrast is not machine-checked**; `:focus-visible` presence and
   focus ORDER are.
8. **The bundle scan covers artifacts this repository builds.** A credential
   injected by a platform at deploy time is operator territory
   ([SMOKE_TESTING.md](SMOKE_TESTING.md)).
9. **The `NEXT_PUBLIC_*` allowlist is repository-side.** Setting a variable in
   Vercel that no code reads changes nothing in the bundle and is not detected
   here.
10. **Python 3.11 locally, 3.12 in CI.** The backend suite passed on both; CI is
    the authority.

## Intentionally deferred

| Item | Reason | Safe current behaviour | Condition to revisit |
| --- | --- | --- | --- |
| Automatic `launch_unknown` reconciliation | a wrong guess is a double execution and a double spend | the run parks, the UI says so, an operator resolves it | a trustworthy Cloud Run execution-API signal plus operator sign-off ([FINAL_ACCEPTANCE.md](FINAL_ACCEPTANCE.md)) |
| Supabase Realtime as the primary event channel | polling is sufficient and simpler to reason about | polling, with Realtime unwired | scale need plus a Realtime authorization review |
| A production producer for `not_found` | no registered tool returns `TRUSTED_SOURCE_NO_MATCH` | the contract is parsed and rendered; nothing emits it | a tool whose contract genuinely includes it |
| `role="dialog"` for the cancellation confirmation | it does not trap focus or mark the page inert, and should not — the run keeps updating behind it | a labelled `role="group"` with a real focus contract | a genuinely modal interaction, if one is ever needed |
| Gap Audit | a separate stage | — | after F5 is merged |

No critical TODO is hidden in a code comment. The limitations above are the
complete list.
