# Frontend pre-release UI verification

A short, executable pass over the deployed workspace before a release is
accepted. It complements — and never replaces —
[SMOKE_TESTING.md](SMOKE_TESTING.md), which covers the API and gateway from the
outside. Everything here is done by a human in a browser, because that is the
part no fixture can stand in for.

**Scope.** Read-only by default. The only writes are creating a conversation and
— when and only when a stage has authorized run creation — creating and
cancelling runs in an operator-controlled test project. Nothing here authorizes
a paid run, a deployment, a migration or a flag change.

**Before you start.** Record the release SHA, the environment (staging or
production), the browser and the operator identity. Every step below is PASS,
FAIL or MANUAL/N-A; a step you did not perform is not a pass.

---

## 0. Stop conditions

Stop and do not accept the release if any of these is true. Each is a failure of
an invariant, not a cosmetic problem.

* a service-role, provider or worker credential is visible anywhere in the page,
  a script, a network response or the console;
* the browser makes a request to anything other than the app's own origin and
  the configured Supabase URL;
* another user's project, conversation or run is reachable;
* an execution control is present while its stage is supposed to be disabled;
* a `launch_unknown` run shows any sign of being retried;
* a run reports `completed` while its result surface says the outcome is
  partial, invalid or absent;
* a conversation shows a run, an output or an event that belongs to another
  conversation.

**Rollback.** Execution flags off first, then
`vercel promote <previous deployment>` for the frontend; the API and worker
follow [ROLLBACK.md](ROLLBACK.md). The frontend is stateless, so promoting the
previous deployment is sufficient and reversible.

---

## 1. Disabled default posture

1. Open the workspace signed out. Only the sign-in screen is present.
2. Sign in. With `NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI` unset, confirm there is
   **no** task composer, no "Send task", no "Cancel run", no proposal actions.
3. The hardening note is shown: execution controls are hidden and the backend
   flags remain authoritative.

Expected: a read-only workspace. If any execution control is present while the
flag is off, stop.

## 2. Authentication and session

1. Sign in with a valid account; the workspace loads.
2. Refresh the page: the session is restored without signing in again, and the
   conversation you were on reopens the same run.
3. Sign in with a wrong password: the error names the failure and nothing else —
   no URL, no stack, no token.
4. Sign out: the workspace disappears, the sign-in screen returns, and a refresh
   does not bring back projects, conversations or a run.
5. In DevTools → Application → Session storage, confirm **no** `milo.activeRun.*`
   key remains after sign-out.

## 3. Project and conversation isolation

1. With two operator accounts, confirm each sees only its own projects.
2. Switch projects quickly, twice in a row, on a slow connection (DevTools →
   Network → throttle). The conversation list must always match the project
   shown — never the previous one.
3. Create a conversation, then immediately switch projects. The new conversation
   must not appear in, or be opened under, the other project.
4. Paste another user's project or conversation id into the URL/API: the answer
   is 403/404 and no data.

## 4. Run creation and idempotency

*Only in a stage where run creation is authorized.*

1. Send a task. Exactly one run is created.
2. Double-click "Send task": still exactly one run.
3. Confirm the run id shown matches the run row the API returns.

## 5. Refresh, reconnect and navigation

1. With a run in progress, refresh. The same run reopens with its state intact.
2. Interrupt the network briefly. The surface shows "Reconnecting…" and does
   **not** clear anything already rendered. Restore the network: polling resumes
   from where it was and no event is duplicated.
3. Switch to another conversation and back. Nothing from the first run appears
   under the second.

## 6. Cancellation

1. Start a long run and press "Cancel run".
2. Focus lands in the reason field. Press **Escape**: the confirmation closes,
   the run is **not** cancelled, and focus returns to "Cancel run".
3. Open it again and press "Keep running": same result.
4. Open it again, give a reason, confirm. The surface says cancellation was
   requested and **does not** call the run cancelled until the backend reports
   the terminal state.
5. When it goes terminal, confirm the status reads `cancelled`.

## 7. Terminal states

Drive or observe each of the six and confirm the displayed status is the durable
one, verbatim:

`completed` · `partial_success` · `failed` · `cancelled` · `timed_out` ·
`budget_exhausted`

`partial_success` must never read as completed: the surface says in words that
work remains outstanding.

## 8. `launch_unknown`

If a run parks with `launch_unknown`:

1. The screen states that reconciliation is required, that the run will **not**
   be relaunched automatically, and that nothing on the screen retries it.
2. Watch it for a minute. No new worker execution appears, no new run is created
   and no event other than the single launch failure is appended.
3. Resolve it only with `scripts/release/reconcile-launch-unknown.sh` (list mode
   first). Never by pressing send again expecting a new run — a replay
   deliberately returns the **same** run.

## 9. Final Result variants

For a Swarm V2 project, confirm each variant reads honestly:

| Variant | The surface must |
| --- | --- |
| usable result | show the verified fields; list every value when a field has more than one and choose none |
| partial result | say it is **not** a completed result, and group outstanding items by what they are |
| partial with no itemized rows | say plainly that no itemized entries exist, and infer nothing about why |
| no usable result | report empty as empty, never as success |
| no payload recorded | say so, distinctly from an invalid one |
| invalid payload | say the result is unavailable, and show none of the payload |

Then refresh: the surface must rebuild identically from the durable run output.

Confirm the raw-JSON panel is absent for Swarm V2, and present and unchanged for
`vehicle_catalog_v1`.

## 10. Keyboard and focus

1. Tab through the whole workspace. Focus is always visible and follows reading
   order; nothing is reachable that is not visible.
2. Open the provenance disclosure with Enter alone.
3. Open a drawer on a narrow window: focus moves into it, Escape closes it, and
   focus returns to the control that opened it.
4. With a screen reader, confirm the only announcements are lifecycle ones —
   never a counter, a task row or a polling tick.
5. Confirm no status is conveyed by colour alone.

## 11. Mobile layout

At 375px width:

1. No horizontal document scroll on any surface.
2. Long run ids, event ids, field names and codes wrap and stay readable.
3. Both drawers open, close and restore focus.

## 12. Browser and bundle inspection

1. DevTools → Network, hard reload. Scan the document and **every** script for:
   `sb_secret_`, `sk-`, `-----BEGIN`, `service_role`, `SUPABASE_SERVICE_ROLE_KEY`,
   `SUPABASE_SECRET_KEY`, `KIMI_API_KEY`, `MOONSHOT_API_KEY`,
   `UPSTASH_REDIS_REST_TOKEN`, `CLOUD_RUN_API_URL`. None may appear.
2. Confirm the only `NEXT_PUBLIC_*` values present are
   `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` and
   `NEXT_PUBLIC_MILO_ENABLE_EXECUTION_UI`
   ([ENVIRONMENT_MATRIX.md](ENVIRONMENT_MATRIX.md)).
3. Confirm every workspace request goes to `/api/gateway/*` on the app's own
   origin, plus Supabase auth. No request reaches a Cloud Run URL from the
   browser.
4. Trigger an error (sign in wrongly, or act while a surface is disabled) and
   confirm the message carries no URL, stack, token or key-shaped value — a
   sentence and a code.

> The repository's own equivalents run in CI: `npm run test:secrets` with
> `MILO_REQUIRE_BUNDLE_SCAN=1` scans the built bundle, and the Playwright suite
> scans every script the running server serves. This step is the deployed
> counterpart, which is the only place a platform-injected value would show up.

---

## Recording the result

Record, with no secret values: release SHA, environment, browser, operator, the
outcome of each section, and any FAIL with what was observed. Attach it beside
the release record. A release is not accepted while any section 0 stop condition
holds.
