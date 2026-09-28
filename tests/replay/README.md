# Production replay fixtures

Every directory here is ONE real production Swarm V2 run, recorded so the
whole engine can be run offline against what the models actually produced.
`tests/test_replay_runs.py` replays each one on every PR, in the existing
offline CI job (`pytest -q -rs tests`), so a shape a production model already
produced can never fail a run again without a test failing first.

## What a replay runs

`tests/replay_harness.py` runs the REAL `ModelGateway`, `Commander` +
`PlanValidator` (with the reviewed production plan limits), executor,
`GenericWorker`, `ToolRegistry`, trusted Government evidence mapper and
lease-guarded `EvidenceBoard`, `Verifier`, `FinalBuilder` and vehicle-result
assembler. Only two things are recorded:

* **the provider** -- `ReplayProviderAdapter` stands where `ProviderAdapter`
  stands and answers each call with the next recorded completion for that
  role, phase and task. A call the recording does not hold is a
  `ReplayDivergence` (it is never answered);
* **the Government tool** -- `ReplayGovernmentTool` keeps the real tool's
  descriptor and answers the recorded result for the call's operation and
  resolved arguments; the sink then checks the answer went to the recorded
  `task_id`/`call_id`. Each answer is also cross-checked against the real tool
  reading the fixture's own register rows (same match count, same rows, same
  identity projection).

Every catalog read goes through a spy that refuses the whole-snapshot
projections. The replay runs with ONE logical worker, so tool answers are
matched deterministically.

## The format: `<run>/manifest.json`, `"format": "replay/1"`

| key | what |
|---|---|
| `run_id`, `description`, `models`, `objective` | identity; `models` = the commander and worker model names |
| `preparation` | the run's preparation record: pinned snapshot + queue (becomes the Commander's `government_work` context) |
| `commander` | the Commander's completions IN ORDER, each `{phase: planning\|replanning, content, finish_reason}` |
| `workers` | `task_id -> [attempt 1, attempt 2]`, each `{content, finish_reason}` |
| `verifier` | the verifier's completions in order (`phase: verification`) |
| `tool_results` | `{task_id, call_id, tool, operation, arguments, result}` per call |
| `snapshot_rows` | the raw register rows -- ONLY those the tool results name |
| `provenance` | per artifact id (`commander[0]`, `workers.t01[0]`, `tool_results.t01/c1`, `snapshot_rows.37350`, `preparation`, ...): `{kind: captured\|reconstructed, source}` |
| `expected` | the outcome on the CURRENT code (see below) |

`content` is always the provider's text, inert, BEFORE any validation.

`expected.terminal` is `result` (the run finalized: `status`, `result_kind`,
`vehicles`, `unresolved_groups`, `needs_review`, `summary`), `failed` (the
engine refused the run with its own static code, e.g. a plan refused twice --
`failure.code`), or `unrecorded_call` (the current code asks for a completion
production never produced -- e.g. the Commander repair of a plan the firewall
now refuses; the call is named). `model_calls`, `retry_reasons` and `unconsumed` (recorded
completions / tool results the current code never reached) are always stated.
`note` is free text and is not compared.

A fixture is **captured** only when every artifact came from
`scripts/export_replay_capture.py`. The fixtures committed with PR-Y, and
29eb076c (PR-EV), are **reconstructed**: no raw provider output of them exists
offline, so each was written by `tests/replay/reconstruct.py` from the durable
facts the repository records about the run, with stand-in register rows, and each artifact says so.
`python tests/replay/reconstruct.py --check` proves none was edited by hand.

A **derived** fixture (`aa63369b-v4`) is a recording with ONE stated change:
the aa63369b plan text with only `register_meta.evidence.minimum_sources`
changed from 1 to 0, so the plan passes PR-V's V4 firewall. Its changed
artifact's provenance says `derived from aa63369b (V4)`; every other artifact
is aa63369b's, unchanged, and a test reverses the substitution byte for byte.
The original `aa63369b` is kept as recorded; on the current code its plan is
refused and the replay stops at the Commander repair production never made.

A second derived fixture, `29eb076c-ev`, keeps the recorded 29eb076c (T batch)
plan -- which PR-EV's firewall refuses with `EVIDENCE_FIELD_NOT_PRODUCIBLE` --
and adds the ONE repair the firewall asks for: the same plan text with every
task's `evidence.required_fields` changed from the resolve_variant output keys
(`resolved`, `ambiguous`, `match_count`) to `["trim", "official_model_code"]`.
Its `commander[1]` provenance says `derived from 29eb076c (EV)`, every other
artifact is 29eb076c's, and a test reverses the substitution byte for byte.

The reconstructed tool results predate PR-V's `match_mode`; `reconstruct.py`
confirms each is an `exact` match under the current tool and writes it as the
run recorded it (without the field). A recording that states `match_mode` is
cross-checked against it.

## Sanitization

Every fixture must pass `backend.replay_capture.sanitization_findings`: no
secret or credential-shaped text, no user/owner/lease/token keys, no e-mail
address, no URL outside `data.gov.il`, and no UUID other than the run's and the
register's data identifiers. `tests/test_replay_format.py` runs it over every
committed fixture; the export refuses to write anything that fails it.

## Adding a run

1. Execute the run with the capture on. `MILO_CAPTURE_REPLAY` is default off
   and pinned `false` by every deploy script (enforced by
   `scripts/check_unsafe_defaults.py`); turning it on for one worker execution
   is an explicit operator decision, e.g.
   `gcloud run jobs execute <worker job> --update-env-vars MILO_CAPTURE_REPLAY=<on> ...`.
   With it on, the worker keeps the run's completions (answer `content` and
   `finish_reason` only, never reasoning) and tool results, bounded, on the
   run's own checkpoints (`artifacts.replay_capture`; `run_checkpoints` is
   service-only). A run that fails -- at planning or after its first engine
   checkpoint -- keeps its capture in one final `replay_capture` checkpoint.
   Every checkpoint of a prepared run, that one included, carries the run's OWN
   preparation record (`artifacts.government`); the export reads both from the
   same latest checkpoint and refuses when the record is missing or a recorded
   tool result names another snapshot.
2. Export it (read-only; service credentials in the environment):

   ```
   python scripts/export_replay_capture.py --run-id <run uuid> \
       --out tests/replay/<first 8 hex of the run id> --write-expected
   ```

   The export refuses a missing or truncated capture and anything the
   sanitizer flags. The run's objective text is never exported.
3. Review `expected` -- `--write-expected` records what the CURRENT code does
   with the recording; every later PR is held to it -- and commit.

When a replay stops matching, the code changed how it treats something a
model really produced. Fix the code, or, when the change is intended, update
`expected` in the same PR and say why (for a reconstructed fixture, in
`reconstruct.py`, then regenerate).
