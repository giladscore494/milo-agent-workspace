'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api, executionUiEnabled, newIdempotencyKey } from '@/lib/api';
import { getCurrentSession, onAuthStateChange, signInWithSupabase, signOutFromSupabase, SupabaseSession } from '@/lib/supabaseClient';
import { isTerminalRunStatus, isPartialSuccessRunStatus } from '@/lib/runStatus';
import { useRunRealtime } from '@/lib/useRunRealtime';
import { Conversation, Project, Proposal } from '@/lib/types';
import { AuthScreen, SessionRestoreScreen } from '@/components/auth/AuthScreen';
import { ConversationView } from '@/components/conversation/ConversationView';
import { TaskComposer } from '@/components/conversation/TaskComposer';
import { InspectorTab, RunInspector } from '@/components/inspector/RunInspector';
import { WorkflowProposalPanel } from '@/components/proposals/WorkflowProposalPanel';
import { CurrentRunPanel } from '@/components/run/CurrentRunPanel';
import { RunOutputPanel } from '@/components/run/RunOutputPanel';
import { SwarmRunCard } from '@/components/swarm/SwarmRunCard';
import { WorkspaceShell } from '@/components/workspace/WorkspaceShell';
import { WorkspaceSidebar } from '@/components/workspace/WorkspaceSidebar';

const HARDENING_NOTE = 'Execution controls are hidden: the execution UI flag is off. Backend execution flags and authorization stay authoritative either way.';

function activeRunStorageKey(conversationId: string): string {
  return `milo.activeRun.${conversationId}`;
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

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) return `${error.message} (${error.code})`;
  return error instanceof Error ? error.message : fallback;
}

/**
 * Orchestration boundary for the workspace.
 *
 * Every API call, every piece of session, project, conversation, proposal and
 * run state, the idempotency-key lifetime, the active-run session storage and
 * the polling hook live here. The components below are presentational: they
 * receive typed props and report intent back through callbacks, so no state
 * has a second owner.
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
  // The project's trusted workflow_key selects V2 vs V1 presentation; the
  // frontend never guesses the engine from event shapes.
  const { state, mode, swarm } = useRunRealtime(executionUi ? activeRunId : undefined, selectedProject?.workflow_key);
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

  useEffect(() => {
    let mounted = true;
    getCurrentSession().then(current => { if (mounted) setSession(current); }).catch(() => { if (mounted) setSession(null); });
    const unsubscribe = onAuthStateChange(next => { if (mounted) setSession(next); });
    return () => { mounted = false; unsubscribe(); };
  }, []);

  const loadProjects = useCallback(() => {
    setProjects(undefined);
    setProjectsError('');
    api.projects()
      .then(setProjects)
      .catch(error => {
        setProjects([]);
        setProjectsError(errorMessage(error, 'Failed to load projects.'));
      });
  }, []);

  useEffect(() => {
    if (!session) {
      setProjects(undefined);
      setProjectsError('');
      setSelectedProject(undefined);
      setConversations(undefined);
      setActiveConversation(undefined);
      setActiveRunId(undefined);
      setProposal(undefined);
      return;
    }
    loadProjects();
  }, [session, loadProjects]);

  const loadConversations = useCallback((project: Project) => {
    setConversations(undefined);
    setConversationError('');
    api.conversations(project.id)
      .then(setConversations)
      .catch(error => {
        setConversations([]);
        setConversationError(errorMessage(error, 'Failed to load conversations.'));
      });
  }, []);

  async function login() {
    setAuthError('');
    try {
      setSession(await signInWithSupabase(email, password));
    } catch (error) {
      setSession(null);
      setAuthError(error instanceof Error ? error.message : 'Authentication failed.');
    }
  }

  async function logout() {
    setAuthError('');
    await signOutFromSupabase();
    setSession(null);
  }

  function selectProject(project: Project) {
    setSelectedProject(project);
    setActiveConversation(undefined);
    setActiveRunId(undefined);
    setProposal(undefined);
    setConversationError('');
    setSidebarOpen(false);
    loadConversations(project);
  }

  function selectConversation(conversation: Conversation) {
    setActiveConversation(conversation);
    setRunError('');
    setCancelError('');
    setSidebarOpen(false);
    // Reopen an existing run after refresh or navigation.
    setActiveRunId(readStoredRunId(conversation.id));
  }

  async function createConversation() {
    if (!selectedProject || creatingConversation) return;
    setConversationError('');
    setCreatingConversation(true);
    try {
      const conversation = await api.createConversation(selectedProject.id, conversationTitle.trim() || undefined);
      setConversations(previous => [conversation, ...(previous ?? [])]);
      selectConversation(conversation);
      setConversationTitle('');
    } catch (error) {
      setConversationError(errorMessage(error, 'Failed to create the conversation.'));
    } finally {
      setCreatingConversation(false);
    }
  }

  async function generateProposal() {
    if (!selectedProject || proposalBusy || !proposalRequest.trim()) return;
    setProposalBusy(true);
    setProposalError('');
    try {
      setProposal(await api.createProposal(selectedProject.id, proposalRequest.trim()));
    } catch (error) {
      setProposalError(errorMessage(error, 'Proposal creation failed.'));
    } finally {
      setProposalBusy(false);
    }
  }

  async function decideProposal(decision: 'approve' | 'reject') {
    if (!proposal || proposalBusy) return;
    setProposalBusy(true);
    setProposalError('');
    try {
      setProposal(await api.decideProposal(proposal.id, decision));
    } catch (error) {
      setProposalError(errorMessage(error, `Proposal ${decision} failed.`));
    } finally {
      setProposalBusy(false);
    }
  }

  async function reviseProposal() {
    if (!proposal || proposalBusy || !proposalRequest.trim()) return;
    setProposalBusy(true);
    setProposalError('');
    try {
      setProposal(await api.reviseProposal(proposal.id, proposalRequest.trim()));
    } catch (error) {
      setProposalError(errorMessage(error, 'Proposal revision failed.'));
    } finally {
      setProposalBusy(false);
    }
  }

  async function startRun() {
    if (!activeConversation || submittingRun || !taskContent.trim()) return;
    setSubmittingRun(true);
    setRunError('');
    // One key per logical submission: a retry after failure reuses it, so
    // the backend returns the original run instead of creating a duplicate.
    idempotencyKey.current ??= newIdempotencyKey();
    try {
      const created = await api.startRun(activeConversation.id, taskContent.trim(), idempotencyKey.current);
      idempotencyKey.current = undefined;
      storeRunId(activeConversation.id, created.run_id);
      setActiveRunId(created.run_id);
      setTaskContent('');
    } catch (error) {
      setRunError(errorMessage(error, 'Run creation failed.'));
    } finally {
      setSubmittingRun(false);
    }
  }

  async function confirmCancelRun() {
    if (!activeRunId) return;
    setCancelError('');
    try {
      await api.cancel(activeRunId, cancelReason.trim() || undefined);
      setConfirmingCancel(false);
      setCancelReason('');
    } catch (error) {
      setCancelError(errorMessage(error, 'Cancellation failed.'));
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
          onRetryProjects={loadProjects}
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
        <RunOutputPanel visible={executionUi && activeRunId !== undefined} output={state.run?.output} />
      </ConversationView>
    </WorkspaceShell>
  );
}
