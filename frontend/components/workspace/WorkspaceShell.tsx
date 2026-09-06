'use client';
import { ReactNode, useEffect } from 'react';

export const SIDEBAR_ID = 'workspace-sidebar';
export const INSPECTOR_ID = 'workspace-inspector';

export type WorkspaceShellProps = {
  sidebar: ReactNode;
  inspector: ReactNode;
  children: ReactNode;
  sidebarOpen: boolean;
  inspectorOpen: boolean;
  onSidebarOpenChange: (open: boolean) => void;
  onInspectorOpenChange: (open: boolean) => void;
};

/**
 * Three-zone workspace layout.
 *
 * Desktop keeps both rails mounted beside a dominant centre column. Below the
 * desktop breakpoint the rails become two independent drawers: each keeps its
 * own open state, so opening one never changes the other. The shell owns no
 * workspace data — it only positions the regions it is handed.
 */
export function WorkspaceShell({
  sidebar,
  inspector,
  children,
  sidebarOpen,
  inspectorOpen,
  onSidebarOpenChange,
  onInspectorOpenChange,
}: WorkspaceShellProps) {
  useEffect(() => {
    if (!sidebarOpen && !inspectorOpen) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== 'Escape') return;
      // Close the drawer layered on top first; the other one keeps its state.
      if (inspectorOpen) onInspectorOpenChange(false);
      else onSidebarOpenChange(false);
    }
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [sidebarOpen, inspectorOpen, onSidebarOpenChange, onInspectorOpenChange]);

  return (
    <div className="shell">
      <header className="shell-bar">
        <button
          type="button"
          className="icon-button"
          aria-label="Workspace navigation"
          aria-expanded={sidebarOpen}
          aria-controls={SIDEBAR_ID}
          onClick={() => onSidebarOpenChange(!sidebarOpen)}
        >
          <span aria-hidden="true">☰</span>
          <span className="icon-button-text">Workspace</span>
        </button>
        <span className="shell-bar-brand">MILO</span>
        <button
          type="button"
          className="icon-button"
          aria-label="Run inspector"
          aria-expanded={inspectorOpen}
          aria-controls={INSPECTOR_ID}
          onClick={() => onInspectorOpenChange(!inspectorOpen)}
        >
          <span className="icon-button-text">Inspector</span>
          <span aria-hidden="true">◧</span>
        </button>
      </header>

      {(sidebarOpen || inspectorOpen) && (
        <div
          className="shell-scrim"
          aria-hidden="true"
          onClick={() => {
            onSidebarOpenChange(false);
            onInspectorOpenChange(false);
          }}
        />
      )}

      <aside
        id={SIDEBAR_ID}
        className="rail rail--sidebar"
        data-open={sidebarOpen}
        aria-label="Projects and conversations"
      >
        <div className="rail-close">
          <button type="button" className="icon-button" aria-label="Close projects and conversations" onClick={() => onSidebarOpenChange(false)}>
            <span aria-hidden="true">×</span>
          </button>
        </div>
        {sidebar}
      </aside>

      <main className="center">{children}</main>

      <aside
        id={INSPECTOR_ID}
        className="rail rail--inspector"
        data-open={inspectorOpen}
        aria-label="Run inspector"
      >
        <div className="rail-close">
          <button type="button" className="icon-button" aria-label="Close inspector panel" onClick={() => onInspectorOpenChange(false)}>
            <span aria-hidden="true">×</span>
          </button>
        </div>
        {inspector}
      </aside>
    </div>
  );
}
