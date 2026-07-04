# Mock end-to-end verification

1. Create a project/conversation through the API.
2. Create a workflow proposal and verify typed task spec, critic output, internet policies, estimates, and deliverables.
3. Approve the proposal.
4. Start a run and assert the API returns `202` immediately with a queued run id.
5. Verify a run invocation row/event exists and the worker can claim the run with `RUN_ID`.
6. Simulate browser close/reopen by rebuilding UI state from `/runs/{run_id}` and `/runs/{run_id}/events`.
7. Use mocked internet/tool grants only; record sources, claims, conflicts, and final assembly.
8. Save a checkpoint, restart the worker with the same `RUN_ID`, and verify resume.
9. Verify final status is `completed` or `partial_success` with linked sources and unresolved conflicts marked for review.
