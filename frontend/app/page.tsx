'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api, executionUiEnabled, newIdempotencyKey } from '@/lib/api';
import { getCurrentSession, onAuthStateChange, signInWithSupabase, signOutFromSupabase, SupabaseSession } from '@/lib/supabaseClient';
import { initialWorkspaceState } from '@/lib/runReducer';
import { useRunRealtime } from '@/lib/useRunRealtime';
import { AgentState, Conversation, InternetPolicy, Project, Proposal } from '@/lib/types';
import { redactSecrets, safeText } from '@/lib/sanitize';

const internetLabels: InternetPolicy[] = ['forbidden','allowed','required','conditional','requested','approved','denied','active'];
const HARDENING_NOTE = 'Execution controls are hidden: the execution UI flag is off. Backend execution flags and authorization stay authoritative either way.';
const TERMINAL_STATES = new Set(['completed','partial_success','failed','cancelled','timed_out','budget_exhausted']);

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

export default function WorkspacePage() {
  const executionUi = executionUiEnabled();
  const [mobileOpen, setMobileOpen] = useState(false);
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

  const [tab, setTab] = useState('Agents');
  const { state, mode } = useRunRealtime(executionUi ? activeRunId : undefined);
  const agents = Object.values(state.agents);
  const runStatus = state.run?.status;
  const launchState = state.run?.launch_state;
  const launchReconciliationRequired = state.run?.launch_reconciliation_required;
  const runIsTerminal = !!runStatus && TERMINAL_STATES.has(runStatus);

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
    setMobileOpen(false);
    loadConversations(project);
  }

  function selectConversation(conversation: Conversation) {
    setActiveConversation(conversation);
    setRunError('');
    setCancelError('');
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

  if (session === undefined) return <main className="auth-only"><p>Restoring your MILO session…</p></main>;
  if (!session) return (
    <main className="auth-only">
      <h1>MILO</h1>
      <p>Sign in to access the authenticated workspace.</p>
      <input aria-label="Email" value={email} onChange={e => setEmail(e.target.value)} placeholder="Email"/>
      <input aria-label="Password" type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="Password"/>
      <button onClick={login}>Login</button>
      {authError && <p role="alert">{authError}</p>}
    </main>
  );

  const projectsLoading = projects === undefined;
  const conversationsLoading = selectedProject !== undefined && conversations === undefined;

  return (
    <main className="shell">
      <section className="auth-panel">
        <b>{session.user?.email ?? 'Authenticated'}</b>
        <button onClick={logout}>Logout</button>
        {!executionUi && <small>{HARDENING_NOTE}</small>}
      </section>
      <button className="mobile-menu" onClick={() => setMobileOpen(true)}>☰ Workspace</button>
      <aside className={`sidebar ${mobileOpen ? 'open' : ''}`}>
        <button className="close" onClick={() => setMobileOpen(false)}>×</button>
        <h1>MILO</h1>
        <section>
          <h2>Projects</h2>
          {projectsLoading && <p>Loading your projects…</p>}
          {projectsError && <div role="alert"><p>{safeText(projectsError)}</p><button onClick={loadProjects}>Retry loading projects</button></div>}
          {!projectsLoading && !projectsError && projects.length === 0 && <p>No projects are assigned to your account yet. Ask an operator to add your project membership.</p>}
          {!projectsLoading && projects.map(project => (
            <button key={project.id} className={`nav-card ${selectedProject?.id === project.id ? 'active' : ''}`} onClick={() => selectProject(project)}>
              <span>{safeText(project.name)}</span>
              <small>{safeText(project.slug)}</small>
            </button>
          ))}
        </section>
        <section>
          <h2>Conversations</h2>
          {conversationsLoading && <p>Loading conversations…</p>}
          {!selectedProject && <p>Select a project to start a conversation.</p>}
          {selectedProject && !conversationsLoading && (conversations?.length ?? 0) === 0 && <p>No conversations yet in this project.</p>}
          {(conversations ?? []).map(conversation => (
            <button key={conversation.id} className={`nav-card ${activeConversation?.id === conversation.id ? 'active' : ''}`} onClick={() => selectConversation(conversation)}>
              <span>{safeText(conversation.title || 'Untitled conversation')}</span>
            </button>
          ))}
        </section>
      </aside>
      <section className="chat">
        <header>
          <div>
            <p className="eyebrow">Backend-authoritative agent workspace</p>
            <h2>{selectedProject ? safeText(selectedProject.name) : 'Select a project to begin'}</h2>
          </div>
          <span className="badge idle">{executionUi ? `execution UI enabled • backend flags authoritative` : 'read-only • execution disabled'}</span>
        </header>
        <div className="messages">
          {selectedProject ? (
            <article className="message user">
              <b>New conversation in {safeText(selectedProject.name)}</b>
              {selectedProject.description && <p>{safeText(selectedProject.description)}</p>}
              <input aria-label="Conversation title" value={conversationTitle} onChange={e => setConversationTitle(e.target.value)} placeholder="Conversation title (optional)"/>
              <button className="primary" onClick={createConversation} disabled={creatingConversation}>{creatingConversation ? 'Creating conversation…' : 'New conversation'}</button>
              {conversationError && <p role="alert">{safeText(conversationError)}</p>}
            </article>
          ) : (
            <article className="message user"><b>No project selected</b><p>Choose one of your authorized projects from the sidebar. Projects are loaded through the authenticated gateway; membership is enforced server-side.</p></article>
          )}
          {activeConversation && (
            <article className="message assistant">
              <b>Conversation</b>
              <p>{safeText(activeConversation.title || 'Untitled conversation')}</p>
              <small>ID {safeText(activeConversation.id)} • project {safeText(activeConversation.project_id)}</small>
            </article>
          )}

          {executionUi && selectedProject ? (
            <article className="proposal">
              <h3>Workflow proposal</h3>
              <textarea aria-label="Proposal request" value={proposalRequest} onChange={e => setProposalRequest(e.target.value)} placeholder="Describe the workflow you need…"/>
              <div className="grid">
                <button className="primary" onClick={generateProposal} disabled={proposalBusy || !proposalRequest.trim()}>{proposalBusy ? 'Working…' : 'Generate proposal'}</button>
                {proposal && <button onClick={reviseProposal} disabled={proposalBusy || !proposalRequest.trim()}>Revise with new request</button>}
              </div>
              {proposalError && <p role="alert">{safeText(proposalError)}</p>}
              {proposal && (
                <div>
                  <p><span className={`badge ${proposal.status}`}>{safeText(proposal.status)}</span></p>
                  <p>{safeText(proposal.user_request)}</p>
                  {(proposal.draft?.agents ?? []).map((agent: any) => (
                    <p key={agent.key ?? agent.role}>
                      <b>{safeText(agent.role ?? agent.key)}</b>{' '}
                      <InternetBadge policy={(agent.internet_policy ?? 'conditional') as InternetPolicy} reason={agent.internet_reason}/>
                    </p>
                  ))}
                  <pre>{JSON.stringify(redactSecrets({ steps: proposal.draft?.workflow ?? proposal.draft?.steps ?? [], budget: proposal.estimates, critiques: proposal.critiques }), null, 2)}</pre>
                  <div className="grid">
                    <button className="primary" onClick={() => decideProposal('approve')} disabled={proposalBusy || proposal.status !== 'approved'}>Approve</button>
                    <button onClick={() => decideProposal('reject')} disabled={proposalBusy}>Reject</button>
                  </div>
                </div>
              )}
            </article>
          ) : (
            <article className="proposal">
              <h3>Workflow proposal</h3>
              <p>{HARDENING_NOTE}</p>
            </article>
          )}

          {executionUi && activeConversation ? (
            <article className="run-card">
              <h3>Run</h3>
              <textarea aria-label="Task content" value={taskContent} onChange={e => setTaskContent(e.target.value)} placeholder="Describe the task for this run…"/>
              <button className="primary" onClick={startRun} disabled={submittingRun || !taskContent.trim()}>{submittingRun ? 'Sending…' : 'Send task'}</button>
              {runError && <p role="alert">{safeText(runError)}</p>}
              {activeRunId && (
                <dl>
                  <dt>Run</dt><dd>{safeText(activeRunId)}</dd>
                  <dt>Status</dt><dd>{safeText(runStatus ?? 'loading…')}</dd>
                  <dt>Phase</dt><dd>{safeText(state.currentPhase)}</dd>
                  <dt>Connection</dt><dd>{mode === 'reconnecting' ? 'reconnecting…' : mode}</dd>
                </dl>
              )}
              {activeRunId && !runIsTerminal && !confirmingCancel && (
                <button onClick={() => setConfirmingCancel(true)}>Cancel run</button>
              )}
              {activeRunId && confirmingCancel && (
                <div>
                  <input aria-label="Cancellation reason" value={cancelReason} onChange={e => setCancelReason(e.target.value)} placeholder="Reason (optional)"/>
                  <button className="primary" onClick={confirmCancelRun}>Confirm cancellation</button>
                  <button onClick={() => setConfirmingCancel(false)}>Keep running</button>
                </div>
              )}
              {cancelError && <p role="alert">{safeText(cancelError)}</p>}
              {runIsTerminal && <p>Run finished with status <b>{safeText(runStatus)}</b>.</p>}
              {launchState && (
                <p>
                  Launch state <b>{safeText(launchState)}</b>
                  {launchReconciliationRequired ? ' — reconciliation required.' : '.'}
                </p>
              )}
            </article>
          ) : (
            <article className="run-card">
              <h3>Live run</h3>
              <p>{executionUi ? 'Select or create a conversation to start a run.' : 'No active run. Run creation and execution control are disabled until a separately approved execution stage.'}</p>
            </article>
          )}

          <article className="message assistant">
            <b>Live event stream</b>
            {state.events.length === 0 && <p>{executionUi ? 'No events yet.' : 'No events. Realtime and polling stay disabled while execution surfaces are off.'}</p>}
            {state.events.slice(-50).map(event => (
              <div className="event" key={event.id}>
                <small>{safeText(event.event_type)}</small>
                <span>{safeText(event.agent ?? '')}</span>
                <small>{safeText(event.phase ?? '')}</small>
                <p>{safeText(event.message ?? '')}</p>
              </div>
            ))}
          </article>
          <article className="artifacts">
            <b>Final artifacts</b>
            <pre>{JSON.stringify(redactSecrets(state.run?.output ?? {}), null, 2)}</pre>
          </article>
        </div>
      </section>
      <aside className="inspector">
        <nav>{['Agents','Workflow','Sources','Claims','Conflicts','Costs','Developer'].map(t => <button className={tab === t ? 'selected' : ''} onClick={() => setTab(t)} key={t}>{t}</button>)}</nav>
        <Inspector tab={tab} agents={agents} state={state}/>
      </aside>
    </main>
  );
}

function InternetBadge({ policy, reason }: { policy: InternetPolicy; reason?: string }) {
  return <span className={`internet ${policy}`}>{policy} internet — {safeText(reason || 'policy visible')}</span>;
}

function Inspector({ tab, agents, state }: { tab: string; agents: AgentState[]; state: typeof initialWorkspaceState }) {
  if (tab === 'Agents') return <>{agents.length === 0 && <p>No agents are running.</p>}{agents.map(agent => (
    <div className="agent-card" key={agent.name}>
      <b>{safeText(agent.name)}</b> <span className={`badge ${agent.status}`}>{safeText(agent.status)}</span>
      <p>{safeText(agent.currentTask ?? agent.responsibility)}</p>
      <InternetBadge policy={agent.internet} reason={agent.internetReason}/>
    </div>
  ))}{agents.length === 0 && internetLabels.map(p => <InternetBadge key={p} policy={p}/>)}</>;
  if (tab === 'Workflow') return state.events.length === 0 ? <p>No workflow activity yet.</p> : <pre>{JSON.stringify(redactSecrets({ phase: state.currentPhase, progress: state.progress, checkpoints: state.checkpoints.length }), null, 2)}</pre>;
  if (tab === 'Sources') return state.sources.length === 0 ? <p>No sources recorded.</p> : <>{state.sources.map(source => <div className="source" key={source.id}><b>{safeText(source.title)}</b><small> {safeText(source.domain)} • {safeText(source.source_strength)}</small></div>)}</>;
  if (tab === 'Claims') return <pre>{JSON.stringify(redactSecrets(state.claims), null, 2)}</pre>;
  if (tab === 'Conflicts') return <pre>{JSON.stringify(redactSecrets(state.conflicts), null, 2)}</pre>;
  if (tab === 'Costs') return <pre>{JSON.stringify({ tokens: state.tokens, cost: state.cost, usage: redactSecrets((state.run as any)?.usage ?? {}) }, null, 2)}</pre>;
  return <pre>{JSON.stringify(redactSecrets({ events: state.events.length, checkpoints: state.checkpoints, validationErrors: state.validationErrors, rawErrors: state.rawErrors }), null, 2)}</pre>;
}
