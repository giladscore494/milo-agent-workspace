import { describe, expect, it } from 'vitest';
import {
  INITIAL_WORKSPACE_SCOPE,
  eventBelongsToRun,
  nextSessionScope,
  ownsConversation,
  ownsProject,
  ownsRun,
  ownsSession,
  runBelongsToScope,
  withConversation,
  withProject,
  withRun,
} from '../lib/ownership';

const USER = 'aaaaaaaa-1111-4111-8111-000000000001';
const OTHER_USER = 'aaaaaaaa-1111-4111-8111-000000000002';
const PROJECT_A = 'bbbbbbbb-1111-4111-8111-00000000000a';
const PROJECT_B = 'bbbbbbbb-1111-4111-8111-00000000000b';
const CONVO_A = 'cccccccc-1111-4111-8111-00000000000a';
const CONVO_B = 'cccccccc-1111-4111-8111-00000000000b';
const RUN_A = 'dddddddd-1111-4111-8111-00000000000a';
const RUN_B = 'dddddddd-1111-4111-8111-00000000000b';

function fullScope() {
  return withRun(
    withConversation(withProject(nextSessionScope(INITIAL_WORKSPACE_SCOPE, USER), PROJECT_A), CONVO_A),
    RUN_A,
  );
}

describe('workspace ownership scope', () => {
  it('a session replacement can never compare equal to the session before it', () => {
    const first = nextSessionScope(INITIAL_WORKSPACE_SCOPE, USER);
    // The SAME user signing back in is still a different session: an answer in
    // flight across the sign-out belongs to neither.
    const second = nextSessionScope(first, USER);
    expect(second.session).toBeGreaterThan(first.session);
    expect(ownsSession(first, second)).toBe(false);
    expect(ownsSession(first, first)).toBe(true);
  });

  it('a different user never owns the previous user scope', () => {
    const mine = nextSessionScope(INITIAL_WORKSPACE_SCOPE, USER);
    const theirs = { ...mine, userId: OTHER_USER };
    expect(ownsSession(mine, theirs)).toBe(false);
  });

  it('narrowing a level clears every level below it', () => {
    const scope = fullScope();
    const movedProject = withProject(scope, PROJECT_B);
    expect(movedProject.conversationId).toBeUndefined();
    expect(movedProject.runId).toBeUndefined();

    const movedConversation = withConversation(scope, CONVO_B);
    expect(movedConversation.projectId).toBe(PROJECT_A);
    expect(movedConversation.runId).toBeUndefined();
  });

  it('ownership is nested: a change at any level invalidates every level below', () => {
    const scope = fullScope();
    expect(ownsRun(scope, scope)).toBe(true);

    const otherRun = withRun(scope, RUN_B);
    expect(ownsRun(scope, otherRun)).toBe(false);
    expect(ownsConversation(scope, otherRun)).toBe(true); // same conversation

    const otherConversation = withConversation(scope, CONVO_B);
    expect(ownsConversation(scope, otherConversation)).toBe(false);
    expect(ownsProject(scope, otherConversation)).toBe(true); // same project

    const otherProject = withProject(scope, PROJECT_B);
    expect(ownsProject(scope, otherProject)).toBe(false);
    expect(ownsSession(scope, otherProject)).toBe(true); // same session

    const otherSession = nextSessionScope(scope, USER);
    expect(ownsSession(scope, otherSession)).toBe(false);
  });

  it('a no-longer-selected project is not owned even after returning to it', () => {
    // Re-selecting A after B does restore ownership — the scope describes what
    // is selected now, and A's data is A's data. What must not survive is a
    // response captured under a scope that has since been REPLACED wholesale,
    // which is what the session counter covers above.
    const scope = withProject(nextSessionScope(INITIAL_WORKSPACE_SCOPE, USER), PROJECT_A);
    const away = withProject(scope, PROJECT_B);
    expect(ownsProject(scope, away)).toBe(false);
    expect(ownsProject(scope, withProject(away, PROJECT_A))).toBe(true);
  });
});

describe('run rows are verified, not assumed', () => {
  const run = { id: RUN_A, conversation_id: CONVO_A, status: 'running' };

  it('accepts the run that was asked for in the conversation that is selected', () => {
    expect(runBelongsToScope(run, RUN_A, CONVO_A)).toBe(true);
  });

  it('refuses a response carrying a different run id', () => {
    expect(runBelongsToScope({ ...run, id: RUN_B }, RUN_A, CONVO_A)).toBe(false);
  });

  it('refuses a run that belongs to another conversation', () => {
    // The stored id said "this conversation's run". The server says otherwise,
    // and the server is the only one that knows.
    expect(runBelongsToScope({ ...run, conversation_id: CONVO_B }, RUN_A, CONVO_A)).toBe(false);
  });

  it('refuses a malformed or missing run row', () => {
    expect(runBelongsToScope(undefined, RUN_A, CONVO_A)).toBe(false);
    expect(runBelongsToScope(null, RUN_A, CONVO_A)).toBe(false);
    expect(runBelongsToScope({}, RUN_A, CONVO_A)).toBe(false);
    expect(runBelongsToScope({ id: 42 as unknown as string, conversation_id: CONVO_A }, RUN_A, CONVO_A)).toBe(false);
    expect(runBelongsToScope({ id: RUN_A, conversation_id: 7 as unknown as string }, RUN_A, CONVO_A)).toBe(false);
  });

  it('still checks the run id when no conversation is supplied', () => {
    expect(runBelongsToScope(run, RUN_A)).toBe(true);
    expect(runBelongsToScope({ ...run, id: RUN_B }, RUN_A)).toBe(false);
  });
});

describe('events are verified against the run being rendered', () => {
  it('accepts an event carrying the active run id', () => {
    expect(eventBelongsToRun({ run_id: RUN_A }, RUN_A)).toBe(true);
  });

  it('refuses an event naming another run, or naming none', () => {
    expect(eventBelongsToRun({ run_id: RUN_B }, RUN_A)).toBe(false);
    expect(eventBelongsToRun({}, RUN_A)).toBe(false);
    expect(eventBelongsToRun({ run_id: 5 as unknown as string }, RUN_A)).toBe(false);
  });
});
