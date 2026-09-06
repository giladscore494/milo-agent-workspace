'use client';
import { ReactNode } from 'react';
import { safeText } from '@/lib/sanitize';
import { Conversation, Project } from '@/lib/types';

export type ConversationViewProps = {
  executionUi: boolean;
  project?: Project;
  conversation?: Conversation;
  /** Secondary panels (proposal, current run, output) rendered under the thread. */
  children?: ReactNode;
  /** Composer pinned below the scrolling conversation. */
  composer: ReactNode;
};

/**
 * The centre column and the primary product surface: conversation header,
 * conversation content, the secondary run panels and the pinned composer.
 */
export function ConversationView({ executionUi, project, conversation, children, composer }: ConversationViewProps) {
  return (
    <div className="conversation">
      <header className="conversation-header">
        <div className="conversation-heading">
          <p className="eyebrow">Backend-authoritative agent workspace</p>
          <h2 className="conversation-title">{project ? safeText(project.name) : 'Select a project to begin'}</h2>
          {conversation && <p className="conversation-subtitle">{safeText(conversation.title || 'Untitled conversation')}</p>}
        </div>
        <span className={`badge ${executionUi ? 'is-status-active' : 'is-status-pending'}`}>
          {executionUi ? 'execution UI enabled • backend flags authoritative' : 'read-only • execution disabled'}
        </span>
      </header>

      <div className="conversation-scroll">
        {!project && (
          <article className="message message--system">
            <b>No project selected</b>
            <p>Choose one of your authorized projects from the workspace navigation. Projects are loaded through the authenticated gateway; membership is enforced server-side.</p>
          </article>
        )}

        {project && !conversation && (
          <article className="message message--system">
            <b>No conversation selected</b>
            {project.description && <p>{safeText(project.description)}</p>}
            <p className="muted">Open a conversation from the workspace navigation, or start a new one there.</p>
          </article>
        )}

        {conversation && (
          <article className="message message--assistant">
            <b>Conversation</b>
            <small className="identifier">ID {safeText(conversation.id)} • project {safeText(conversation.project_id)}</small>
          </article>
        )}

        {children}
      </div>

      {composer}
    </div>
  );
}
