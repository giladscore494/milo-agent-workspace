'use client';
import { safeText } from '@/lib/sanitize';
import { Conversation, Project } from '@/lib/types';

export type WorkspaceSidebarProps = {
  userEmail?: string;
  executionUi: boolean;
  hardeningNote: string;
  projects?: Project[];
  projectsError: string;
  selectedProjectId?: string;
  onSelectProject: (project: Project) => void;
  onRetryProjects: () => void;
  conversations?: Conversation[];
  conversationsLoading: boolean;
  conversationError: string;
  activeConversationId?: string;
  onSelectConversation: (conversation: Conversation) => void;
  conversationTitle: string;
  onConversationTitleChange: (value: string) => void;
  onCreateConversation: () => void;
  creatingConversation: boolean;
  onLogout: () => void;
};

/**
 * Left workspace rail: identity, projects, conversations, the new-conversation
 * action and the authenticated-user controls. It renders state it is given and
 * reports intent upwards; the page keeps every fetch and every piece of state.
 */
export function WorkspaceSidebar({
  userEmail,
  executionUi,
  hardeningNote,
  projects,
  projectsError,
  selectedProjectId,
  onSelectProject,
  onRetryProjects,
  conversations,
  conversationsLoading,
  conversationError,
  activeConversationId,
  onSelectConversation,
  conversationTitle,
  onConversationTitleChange,
  onCreateConversation,
  creatingConversation,
  onLogout,
}: WorkspaceSidebarProps) {
  const projectsLoading = projects === undefined;
  const projectSelected = selectedProjectId !== undefined;

  return (
    <div className="sidebar">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">◆</span>
        <div>
          <h1 className="brand-name">MILO</h1>
          <p className="eyebrow">Agentic workspace</p>
        </div>
      </div>

      <nav className="sidebar-section" aria-label="Projects">
        <h2 className="section-title">Projects</h2>
        {projectsLoading && <p className="muted">Loading your projects…</p>}
        {projectsError && (
          <div className="alert" role="alert">
            <p>{safeText(projectsError)}</p>
            <button type="button" className="button button--quiet" onClick={onRetryProjects}>Retry loading projects</button>
          </div>
        )}
        {!projectsLoading && !projectsError && projects.length === 0 && (
          <p className="muted">No projects are assigned to your account yet. Ask an operator to add your project membership.</p>
        )}
        {!projectsLoading && projects.map((project) => (
          <button
            key={project.id}
            type="button"
            className="nav-item"
            aria-current={selectedProjectId === project.id ? 'true' : undefined}
            onClick={() => onSelectProject(project)}
          >
            <span className="nav-item-title">{safeText(project.name)}</span>
            <span className="nav-item-meta">{safeText(project.slug)}</span>
          </button>
        ))}
      </nav>

      <nav className="sidebar-section" aria-label="Conversations">
        <h2 className="section-title">Conversations</h2>
        {conversationsLoading && <p className="muted">Loading conversations…</p>}
        {!projectSelected && <p className="muted">Select a project to start a conversation.</p>}
        {projectSelected && !conversationsLoading && (conversations?.length ?? 0) === 0 && (
          <p className="muted">No conversations yet in this project.</p>
        )}
        {projectSelected && (
          <div className="create-conversation">
            <div className="field">
              <label className="field-label sr-only" htmlFor="conversation-title">Conversation title</label>
              <input
                id="conversation-title"
                value={conversationTitle}
                onChange={(event) => onConversationTitleChange(event.target.value)}
                placeholder="Conversation title (optional)"
              />
            </div>
            <button type="button" className="button button--primary button--block" onClick={onCreateConversation} disabled={creatingConversation}>
              {creatingConversation ? 'Creating conversation…' : 'New conversation'}
            </button>
            {conversationError && <p className="alert" role="alert">{safeText(conversationError)}</p>}
          </div>
        )}
        {(conversations ?? []).map((conversation) => (
          <button
            key={conversation.id}
            type="button"
            className="nav-item"
            aria-current={activeConversationId === conversation.id ? 'true' : undefined}
            onClick={() => onSelectConversation(conversation)}
          >
            <span className="nav-item-title">{safeText(conversation.title || 'Untitled conversation')}</span>
          </button>
        ))}
      </nav>

      <div className="sidebar-footer">
        <p className="account"><b>{safeText(userEmail ?? 'Authenticated')}</b></p>
        <button type="button" className="button button--quiet button--block" onClick={onLogout}>Logout</button>
        {!executionUi && <p className="note">{hardeningNote}</p>}
      </div>
    </div>
  );
}
