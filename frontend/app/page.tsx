'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { api, executionUiEnabled, newIdempotencyKey } from '@/lib/api';
import { safeErrorText } from '@/lib/errorText';
import {
  INITIAL_WORKSPACE_SCOPE,
  WorkspaceScope,
  nextSessionScope,
  ownsConversation,
  ownsProject,
  ownsRun,
  ownsSession,
  withConversation,
  withProject,
  withRun,
} from '@/lib/ownership';
import { getCurrentSession, onAuthStateChange, signInWithSupabase, signOutFromSupabase, SupabaseSession } from '@/lib/supabaseClient';
import { isTerminalRunStatus, isPartialSuccessRunStatus } from '@/lib/runStatus';
import { useRunRealtime } from '@/lib/useRunRealtime';
import { Conversation, Project, Proposal } from '@/lib/types';
import { AuthScreen, SessionRestoreScreen } from '@/components/auth/AuthScreen';
import { ConversationView } from '@/components/conversation/ConversationView';
import { TaskComposer } from '@/components/conversation/TaskComposer';
import { InspectorTab, RunInspector } from '@/components/inspector/RunInspector';
import { WorkflowProposalPanel } from '@/components/proposals/WorkflowProposalPanel';
import { FinalResultPanel } from '@/components/result/FinalResultPanel';
import { CurrentRunPanel } from '@/components/run/CurrentRunPanel';
import { RunOutputPanel } from '@/components/run/RunOutputPanel';
import { SwarmRunCard } from '@/components/swarm/SwarmRunCard';
import { WorkspaceShell } from '@/components/workspace/WorkspaceShell';
import { WorkspaceSidebar } from '@/components/workspace/WorkspaceSidebar';

const HARDENING_NOTE = 'Execution controls are hidden: the execution UI flag is off. Backend execution flags and authorization stay authoritative either way.';

/**
 * Shown when a stored run id does not survive verification against the server.
 * Static and authored here: it says what happened without repeating anything
 * the server said about a run this conversation may not own.
 */
const STORED_RUN_REJECTED = 'The run stored for this conversation could not be verified as belonging to it and was cleared.';

const ACTIVE_RUN_KEY_PREFIX = 'milo.activeRun.';

/** Sentinel for "this page has not observed an authenticated identity yet". */
const NO_SESSION_YET = Symbol('no session observed yet');

function activeRunStorageKey(conversationId: string): string {
  return `${ACTIVE_RUN_KEY_PREFIX}${conversationId}`;
}

function readStoredRunId(conversationId?: string): string | undefined {
  if (!conversationId || typeof window === 'undefined') return undefined;
  try {
    return window.sessionStorage.getItem(activeRunStorageKey(conversationId)) ?? undefined;
  } catch {
    return undefined;
  }
}

function storeRunId(conversationId: string, runId?: string) {
  if (typeof window === 'undefined') return;
  try {
    if (runId) window.sessionStorage.setItem(activeRunStorageKey(conversationId), runId);
    else window.sessionStorage.removeItem(activeRunStorageKey(conversationId));
  } catch {
    // Storage may be unavailable (private mode); polling still works in-page.
  }
}

/**
 * Drop every stored active-run id.
 *
 * Session storage survives a sign-out, and a run id sitting under a
 * conversation key is browser-held state about work the signed-out user
 * started. Clearing it is what makes "sign-out removes rendered workspace
 * data" true of the browser and not only of the React tree.
 */
function clearStoredRunIds() {
  if (typeof window === 'undefined') return;
  try {
    const keys: string[] = [];
    for (let index = 0; index < window.sessionStorage.length; index += 1) {
      const key = window.sessionStorage.key(index);
      if (key?.startsWith(ACTIVE_RUN_KEY_PREFIX)) keys.push(key);
    }
    for (const key of keys) window.sessionStorage.removeItem(key);
  } catch {
    // Storage may be unavailable (private mode); nothing was stored either.
  }
}

/**
 * Orchestration boundary for the workspace.
 *
 * Every API call, every piece of session, project, conversation, proposal and
 * run state, the idempotency-key lifetime, the active-run session storage and
 * the polling hook live here. The components below are presentational: they
 * receive typed props and report intent back through callbacks, so no state
 * has a second owner.
 *
 * Because this component stays mounted across every switch, unmounting is not
 * the boundary that keeps one selection's data out of another's. `scope`
 * (lib/ownership.ts) is: it holds the session, project, conversation and run
 * this page is showing RIGHT NOW, it is updated synchronously in the handler
 * that causes the switch rather than after a render, and every asynchronous
 * result is applied only if the scope it was issued under still owns the state
 * it would write. A dropped answer is dropped silently — nobody is looking at
 * the selection it belonged to.
 */
export default function WorkspacePage() {
  const executionUi = executionUiEnabled();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [authError, setAuthError] = useState('');
  const [session, setSession] = useState<SupabaseSession | null>();

  const [projects, setProjects] = useState<Project[]>();
  const [projectsError, setProjectsError] = useState('');
  const [selectedProject, setSelectedProject] = useState<Project>();

  const [conversationTitle, setConversationTitle] = useState('');
  const [conversations, setConversations] = useState<Conversation[]>();
  const [activeConversation, setActiveConversation] = useState<Conversation>();
  const [conversationError, setConversationError] = useState('');
  const [creatingConversation, setCreatingConversation] = useState(false);

  const [proposalOpen, setProposalOpen] = useState(false);
  const [proposalRequest, setProposalRequest] = useState('');
  const [proposal, setProposal] = useState<Proposal>();
  const [proposalError, setProposalError] = useState('');
  const [proposalBusy, setProposalBusy] = useState(false);

  const [taskContent, setTaskContent] = useState('');
  const [runError, setRunError] = useState('');
  const [submittingRun, setSubmittingRun] = useState(false);
  const [activeRunId, setActiveRunId] = useState<string>();
  const idempotencyKey = useRef<string>();

  const [cancelReason, setCancelReason] = useState('');
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  const [cancelError, setCancelError] = useState('');

  const [tab, setTab] = useState<InspectorTab>('Agents');

  // The live ownership scope. Updated synchronously by the handlers below, so
  // it always describes what is selected now rather than what React last
  // rendered; a response that resolves after a switch compares against this.
  const scope = useRef<WorkspaceScope>(INITIAL_WORKSPACE_SCOPE);
  // The authenticated identity the page last held. `NO_SESSION_YET` is the
  // "never observed" state and is deliberately distinct from `null`
  // (signed out), so the first session a page load sees is not mistaken for a
  // user swap.
  const lastSessionIdentity = useRef<string | null | typeof NO_SESSION_YET>(NO_SESSION_YET);

  /** Change the active run and keep the scope's run level in step. */
  const changeActiveRun = useCallback((runId?: string) => {
    scope.current = withRun(scope.current, runId);
    setActiveRunId(runId);
  }, []);

  /**
   * The polling hook refused the stored run: it is not that run, or not this
   * conversation's run. Clear the stored id so a refresh cannot re-open it,
   * drop it from the page, and say so in one authored sentence.
   */
  const onRunRejected = useCallback((rejectedRunId: string) => {
    const conversationId = scope.current.conversationId;
    if (scope.current.runId !== rejectedRunId) return; // already moved on
    if (conversationId) storeRunId(conversationId, undefined);
    changeActiveRun(undefined);
    setRunError(STORED_RUN_REJECTED);
  }, [changeActiveRun]);

  // The project's trusted workflow_key selects V2 vs V1 presentation; the
  // frontend never guesses the engine from event shapes. The conversation id
  // is handed down as the run's expected owner, so a run row that belongs to
  // another conversation is refused before anything is rendered.
  const { state, mode, swarm } = useRunRealtime(
    executionUi ? activeRunId : undefined,
    selectedProject?.workflow_key,
    activeConversation?.id,
    onRunRejected,
  );
  const agents = Object.values(state.agents);
  const runStatus = state.run?.status;
  const launchState = state.run?.launch_state;
  const launchReconciliationRequired = state.run?.launch_reconciliation_required;
  const runIsTerminal = isTerminalRunStatus(runStatus);
  // Exactly one run surface renders at a time. A Swarm V2 project with a live
  // run gets the dedicated card; every other case — Swarm V1, no run yet, the
  // execution UI switched off — keeps the existing CurrentRunPanel path.
  const swarmCardRunId =
    swarm.isSwarmV2 && executionUi && activeConversation !== undefined ? activeRunId : undefined;
  // The Final Result surface is the product answer and is shown for Swarm V2
  // only. It is gated exactly like the run card above — same trusted
  // workflow_key, same execution flag, same conversation requirement — and
  // stays mounted for the whole run, so its loading, not-finished, absent and
  // invalid states are visible rather than appearing from nowhere.
  const showFinalResult =
    swarm.isSwarmV2 && executionUi && activeConversation !== undefined && activeRunId !== undefined;

  useEffect(() => {
    let mounted = true;
    getCurrentSession().then(current => { if (mounted) setSession(current); }).catch(() => { if (mounted) setSession(null); });
    const unsubscribe = onAuthStateChange(next => { if (mounted) setSession(next); });
    return () => { mounted = false; unsubscribe(); };
  }, []);

  const loadProjects = useCallback((owner: WorkspaceScope) => {
    setProjects(undefined);
    setProjectsError('');
    api.projects()
      .then(list => {
        if (!ownsSession(owner, scope.current)) return; // another session owns the page now
        setProjects(list);
      })
      .catch(error => {
        if (!ownsSession(owner, scope.current)) return;
        setProjects([]);
        setProjectsError(safeErrorText(error, 'Failed to load projects.'));
      });
  }, []);

  useEffect(() => {
    // `undefined` is "still restoring", not "signed out": nothing is rendered
    // and nothing is in flight, so there is nothing to invalidate or clear —
    // and clearing here would destroy the stored run a refresh exists to
    // reopen.
    if (session === undefined) return;

    // Identity, not object identity. Supabase hands back a NEW session object
    // on every token refresh, and treating that as a session replacement would
    // discard requests in flight for a user who never changed.
    const identity = session ? session.user?.id ?? '' : null;
    const identityKnown = lastSessionIdentity.current !== NO_SESSION_YET;
    const identityChanged = identityKnown && lastSessionIdentity.current !== identity;
    lastSessionIdentity.current = identity;

    if (!session || identityChanged) {
      // Sign-out, expiry or a different user: every request in flight under
      // the previous session is invalidated before any state is touched, the
      // rendered workspace is emptied, and the browser's own record of which
      // run each conversation was on goes with it.
      scope.current = nextSessionScope(scope.current, session?.user?.id);
      clearStoredRunIds();
      setProjects(undefined);
      setProjectsError('');
      setSelectedProject(undefined);
      setConversations(undefined);
      setActiveConversation(undefined);
      setActiveRunId(undefined);
      setProposal(undefined);
      setProposalError('');
      setConversationError('');
      setRunError('');
      setCancelError('');
      setTaskContent('');
      setConfirmingCancel(false);
      setCancelReason('');
      idempotencyKey.current = undefined;
      if (!session) return;
    }
    loadProjects(scope.current);
  }, [session, loadProjects]);

  const loadConversations = useCallback((project: Project, owner: WorkspaceScope) => {
    setConversations(undefined);
    setConversationError('');
    api.conversations(project.id)
      .then(list => {
        if (!ownsProject(owner, scope.current)) return; // another project is selected now
        setConversations(list);
      })
      .catch(error => {
        if (!ownsProject(owner, scope.current)) return;
        setConversations([]);
        setConversationError(safeErrorText(error, 'Failed to load conversations.'));
      });
  }, []);

  async function login() {
    setAuthError('');
    try {
      setSession(await signInWithSupabase(email, password));
    } catch (error) {
      setSession(null);
      setAuthError(safeErrorText(error, 'Authentication failed.'));
    }
  }

  async function logout() {
    setAuthError('');
    // Invalidate BEFORE awaiting the provider: from this line on, every
    // response already on the wire belongs to a session that no longer owns
    // this page, and the stored run ids go with it.
    scope.current = nextSessionScope(scope.current, undefined);
    clearStoredRunIds();
    await signOutFromSupabase();
    setSession(null);
  }

  function selectProject(project: Project) {
    const owner = withProject(scope.current, project.id);
    scope.current = owner;
    setSelectedProject(project);
    setActiveConversation(undefined);
    setActiveRunId(undefined);
    setProposal(undefined);
    setProposalError('');
    setConversationError('');
    setRunError('');
    setCancelError('');
    setSidebarOpen(false);
    loadConversations(project, owner);
  }

  function selectConversation(conversation: Conversation) {
    scope.current = withConversation(scope.current, conversation.id);
    setActiveConversation(conversation);
    setRunError('');
    setCancelError('');
    setSidebarOpen(false);
    // Reopen an existing run after refresh or navigation. The stored id is a
    // request, not a fact: the polling hook verifies the run it names against
    // this conversation before anything is rendered.
    changeActiveRun(readStoredRunId(conversation.id));
  }

  async function createConversation() {
    if (!selectedProject || creatingConversation) return;
    const owner = scope.current;
    setConversationError('');
    setCreatingConversation(true);
    try {
      const conversation = await api.createConversation(selectedProject.id, conversationTitle.trim() || undefined);
      // The conversation was created and is safe on the server. It simply does
      // not belong in a project the user has since moved away from.
      if (!ownsProject(owner, scope.current)) return;
      setConversations(previous => [conversation, ...(previous ?? [])]);
      selectConversation(conversation);
      setConversationTitle('');
    } catch (error) {
      if (!ownsProject(owner, scope.current)) return;
      setConversationError(safeErrorText(error, 'Failed to create the conversation.'));
    } finally {
      setCreatingConversation(false);
    }
  }

  /**
   * A proposal belongs to the project it was raised for. Every proposal
   * response — creation, revision and decision alike — is therefore applied
   * only while that project is still selected.
   */
  async function runProposalRequest(request: Promise<Proposal>, fallback: string) {
    const owner = scope.current;
    setProposalBusy(true);
    setProposalError('');
    try {
      const next = await request;
      if (!ownsProject(owner, scope.current)) return;
      setProposal(next);
    } catch (error) {
      if (!ownsProject(owner, scope.current)) return;
      setProposalError(safeErrorText(error, fallback));
    } finally {
      setProposalBusy(false);
    }
  }

  async function generateProposal() {
    if (!selectedProject || proposalBusy || !proposalRequest.trim()) return;
    await runProposalRequest(
      api.createProposal(selectedProject.id, proposalRequest.trim()),
      'Proposal creation failed.',
    );
  }

  async function decideProposal(decision: 'approve' | 'reject') {
    if (!proposal || proposalBusy) return;
    await runProposalRequest(api.decideProposal(proposal.id, decision), `Proposal ${decision} failed.`);
  }

  async function reviseProposal() {
    if (!proposal || proposalBusy || !proposalRequest.trim()) return;
    await runProposalRequest(
      api.reviseProposal(proposal.id, proposalRequest.trim()),
      'Proposal revision failed.',
    );
  }

  async function startRun() {
    if (!activeConversation || submittingRun || !taskContent.trim()) return;
    const owner = scope.current;
    const conversationId = activeConversation.id;
    setSubmittingRun(true);
    setRunError('');
    // One key per logical submission: a retry after failure reuses it, so
    // the backend returns the original run instead of creating a duplicate.
    idempotencyKey.current ??= newIdempotencyKey();
    try {
      const created = await api.startRun(conversationId, taskContent.trim(), idempotencyKey.current);
      idempotencyKey.current = undefined;
      // The run exists and belongs to the conversation it was created for, so
      // it is stored under THAT key whichever conversation is selected now.
      storeRunId(conversationId, created.run_id);
      if (!ownsConversation(owner, scope.current)) return;
      changeActiveRun(created.run_id);
      setTaskContent('');
    } catch (error) {
      if (!ownsConversation(owner, scope.current)) return;
      setRunError(safeErrorText(error, 'Run creation failed.'));
    } finally {
      setSubmittingRun(false);
    }
  }

  async function confirmCancelRun() {
    if (!activeRunId) return;
    const owner = scope.current;
    setCancelError('');
    try {
      await api.cancel(activeRunId, cancelReason.trim() || undefined);
      if (!ownsRun(owner, scope.current)) return;
      setConfirmingCancel(false);
      setCancelReason('');
    } catch (error) {
      if (!ownsRun(owner, scope.current)) return;
      setCancelError(safeErrorText(error, 'Cancellation failed.'));
    }
  }

  if (session === undefined) return <SessionRestoreScreen/>;
  if (!session) return (
    <AuthScreen
      email={email}
      password={password}
      error={authError}
      onEmailChange={setEmail}
      onPasswordChange={setPassword}
      onSubmit={login}
    />
  );

  return (
    <WorkspaceShell
      sidebarOpen={sidebarOpen}
      inspectorOpen={inspectorOpen}
      onSidebarOpenChange={setSidebarOpen}
      onInspectorOpenChange={setInspectorOpen}
      sidebar={
        <WorkspaceSidebar
          userEmail={session.user?.email}
          executionUi={executionUi}
          hardeningNote={HARDENING_NOTE}
          projects={projects}
          projectsError={projectsError}
          selectedProjectId={selectedProject?.id}
          onSelectProject={selectProject}
          onRetryProjects={() => loadProjects(scope.current)}
          conversations={conversations}
          conversationsLoading={selectedProject !== undefined && conversations === undefined}
          conversationError={conversationError}
          activeConversationId={activeConversation?.id}
          onSelectConversation={selectConversation}
          conversationTitle={conversationTitle}
          onConversationTitleChange={setConversationTitle}
          onCreateConversation={createConversation}
          creatingConversation={creatingConversation}
          onLogout={logout}
        />
      }
      inspector={
        <RunInspector
          executionUi={executionUi}
          tab={tab}
          onTabChange={setTab}
          agents={agents}
          state={state}
          swarm={swarm}
        />
      }
    >
      <ConversationView
        executionUi={executionUi}
        project={selectedProject}
        conversation={activeConversation}
        composer={
          <TaskComposer
            executionUi={executionUi}
            hasConversation={activeConversation !== undefined}
            content={taskContent}
            onContentChange={setTaskContent}
            onSubmit={startRun}
            submitting={submittingRun}
            error={runError}
          />
        }
      >
        <WorkflowProposalPanel
          executionUi={executionUi}
          hasProject={selectedProject !== undefined}
          hardeningNote={HARDENING_NOTE}
          open={proposalOpen}
          onOpenChange={setProposalOpen}
          request={proposalRequest}
          onRequestChange={setProposalRequest}
          proposal={proposal}
          error={proposalError}
          busy={proposalBusy}
          onGenerate={generateProposal}
          onRevise={reviseProposal}
          onDecide={decideProposal}
        />
        {swarmCardRunId ? (
          <SwarmRunCard
            swarm={swarm}
            runId={swarmCardRunId}
            connection={mode}
            launchState={launchState}
            launchReconciliationRequired={launchReconciliationRequired}
            confirmingCancel={confirmingCancel}
            cancelReason={cancelReason}
            cancelError={cancelError}
            onCancelReasonChange={setCancelReason}
            onRequestCancel={() => setConfirmingCancel(true)}
            onConfirmCancel={confirmCancelRun}
            onKeepRunning={() => setConfirmingCancel(false)}
          />
        ) : (
          <CurrentRunPanel
            executionUi={executionUi}
            hasConversation={activeConversation !== undefined}
            runId={activeRunId}
            runStatus={runStatus}
            phase={state.currentPhase}
            connection={mode}
            isTerminal={runIsTerminal}
            isPartialSuccess={isPartialSuccessRunStatus(runStatus)}
            launchState={launchState}
            launchReconciliationRequired={launchReconciliationRequired}
            confirmingCancel={confirmingCancel}
            cancelReason={cancelReason}
            cancelError={cancelError}
            onCancelReasonChange={setCancelReason}
            onRequestCancel={() => setConfirmingCancel(true)}
            onConfirmCancel={confirmCancelRun}
            onKeepRunning={() => setConfirmingCancel(false)}
          />
        )}
        {/* Two surfaces, never both. Swarm V2 gets the typed final-result
            contract; every other workflow keeps the existing sanitized-output
            path unchanged. The choice comes from the project's trusted
            workflow_key (via swarm.isSwarmV2), never from the payload. */}
        <FinalResultPanel
          visible={showFinalResult}
          runId={activeRunId}
          runStatus={runStatus}
          connection={mode}
          output={state.run?.output}
        />
        <RunOutputPanel visible={executionUi && activeRunId !== undefined && !swarm.isSwarmV2} output={state.run?.output} />
      </ConversationView>
    </WorkspaceShell>
  );
}
