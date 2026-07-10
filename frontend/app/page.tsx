'use client';
import { useCallback, useEffect, useState } from 'react';
import { api } from '@/lib/api';
import { getCurrentSession, onAuthStateChange, signInWithSupabase, signOutFromSupabase, SupabaseSession } from '@/lib/supabaseClient';
import { initialWorkspaceState } from '@/lib/runReducer';
import { AgentState, Conversation, InternetPolicy, Project } from '@/lib/types';
import { redactSecrets, safeText } from '@/lib/sanitize';

const internetLabels: InternetPolicy[] = ['forbidden','allowed','required','conditional','requested','approved','denied','active'];
const HARDENING_NOTE = 'Workflow proposal, run, retry, resume, cancel, worker, Kimi and tool-grant controls are disabled during the auth hardening stage.';

export default function WorkspacePage() {
  const [mobileOpen, setMobileOpen] = useState(false);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [authError, setAuthError] = useState('');
  const [session, setSession] = useState<SupabaseSession | null>();

  const [projects, setProjects] = useState<Project[]>();
  const [projectsError, setProjectsError] = useState('');
  const [selectedProject, setSelectedProject] = useState<Project>();

  const [conversationTitle, setConversationTitle] = useState('');
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversation, setActiveConversation] = useState<Conversation>();
  const [conversationError, setConversationError] = useState('');
  const [creatingConversation, setCreatingConversation] = useState(false);

  const [tab, setTab] = useState('Agents');
  // No run can be started while execution surfaces are disabled, so the run
  // console renders the empty reducer state instead of subscribing anywhere.
  const state = initialWorkspaceState;
  const agents = Object.values(state.agents);

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
        setProjectsError(error instanceof Error ? error.message : 'Failed to load projects.');
      });
  }, []);

  useEffect(() => {
    if (!session) {
      setProjects(undefined);
      setProjectsError('');
      setSelectedProject(undefined);
      setConversations([]);
      setActiveConversation(undefined);
      return;
    }
    loadProjects();
  }, [session, loadProjects]);

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
    setConversationError('');
    setMobileOpen(false);
  }

  async function createConversation() {
    if (!selectedProject || creatingConversation) return;
    setConversationError('');
    setCreatingConversation(true);
    try {
      const conversation = await api.createConversation(selectedProject.id, conversationTitle.trim() || undefined);
      setConversations(previous => [conversation, ...previous]);
      setActiveConversation(conversation);
      setConversationTitle('');
    } catch (error) {
      setConversationError(error instanceof Error ? error.message : 'Failed to create the conversation.');
    } finally {
      setCreatingConversation(false);
    }
  }

  if (session === undefined) return <main className="auth-only"><p>Restoring your MILO session…</p></main>;
  if (!session) return (
    <main className="auth-only">
      <h1>MILO</h1>
      <p>Sign in to access the authenticated workspace. Run, proposal, worker, Kimi, event and cancellation controls are disabled during auth hardening.</p>
      <input aria-label="Email" value={email} onChange={e => setEmail(e.target.value)} placeholder="Email"/>
      <input aria-label="Password" type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="Password"/>
      <button onClick={login}>Login</button>
      {authError && <p role="alert">{authError}</p>}
    </main>
  );

  const projectsLoading = projects === undefined;

  return (
    <main className="shell">
      <section className="auth-panel">
        <b>{session.user?.email ?? 'Authenticated'}</b>
        <button onClick={logout}>Logout</button>
        <small>{HARDENING_NOTE}</small>
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
          {conversations.length === 0 && <p>{selectedProject ? 'No conversations yet in this session.' : 'Select a project to start a conversation.'}</p>}
          {conversations.map(conversation => (
            <button key={conversation.id} className={`nav-card ${activeConversation?.id === conversation.id ? 'active' : ''}`} onClick={() => setActiveConversation(conversation)}>
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
          <span className="badge idle">read-only • execution disabled</span>
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
          <article className="proposal">
            <h3>Workflow proposal</h3>
            <p>{HARDENING_NOTE}</p>
          </article>
          <article className="run-card">
            <h3>Live run</h3>
            <p>No active run. Run creation and execution control are disabled until a separately approved execution stage.</p>
          </article>
          <article className="message assistant">
            <b>Live event stream</b>
            <p>No events. Realtime and polling stay disabled while execution surfaces are off.</p>
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
  return <span className={`internet ${policy}`}>{policy} internet — {reason || 'policy visible'}</span>;
}

function Inspector({ tab, agents, state }: { tab: string; agents: AgentState[]; state: typeof initialWorkspaceState }) {
  if (tab === 'Agents') return <>{agents.length === 0 && <p>No agents are running.</p>}{internetLabels.map(p => <InternetBadge key={p} policy={p}/>)}</>;
  if (tab === 'Workflow') return <p>No workflow activity. Runs are disabled during the auth hardening stage.</p>;
  if (tab === 'Sources') return <p>No sources recorded.</p>;
  if (tab === 'Claims') return <pre>{JSON.stringify(state.claims, null, 2)}</pre>;
  if (tab === 'Conflicts') return <pre>{JSON.stringify(state.conflicts, null, 2)}</pre>;
  if (tab === 'Costs') return <pre>{JSON.stringify({ tokens: state.tokens, cost: state.cost }, null, 2)}</pre>;
  return <pre>{JSON.stringify(redactSecrets({ events: state.events, checkpoints: state.checkpoints, validationErrors: state.validationErrors, rawErrors: state.rawErrors }), null, 2)}</pre>;
}
