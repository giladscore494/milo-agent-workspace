'use client';
import { ReactNode, useEffect, useRef } from 'react';

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
 *
 * A closed drawer is `visibility: hidden`, so it leaves the tab order and the
 * accessibility tree rather than sitting off-screen still reachable. That makes
 * focus the shell's responsibility at both ends: opening a drawer moves focus
 * into it, and closing one gives focus back to the control that opened it
 * instead of dropping it into a region the viewer can no longer see. Focus is
 * only taken when it is inside the drawer that is closing or when the document
 * has none at all, so a click that lands somewhere else is never overridden.
 *
 * On desktop neither `open` flag ever transitions — the rails are always
 * visible and the toggles are not rendered — so none of this fires there.
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
  const sidebarToggleRef = useRef<HTMLButtonElement | null>(null);
  const inspectorToggleRef = useRef<HTMLButtonElement | null>(null);
  const sidebarCloseRef = useRef<HTMLButtonElement | null>(null);
  const inspectorCloseRef = useRef<HTMLButtonElement | null>(null);
  const sidebarRef = useRef<HTMLElement | null>(null);
  const inspectorRef = useRef<HTMLElement | null>(null);
  const wasSidebarOpen = useRef(sidebarOpen);
  const wasInspectorOpen = useRef(inspectorOpen);

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

  useEffect(() => {
    const sidebarOpened = sidebarOpen && !wasSidebarOpen.current;
    const sidebarClosed = !sidebarOpen && wasSidebarOpen.current;
    const inspectorOpened = inspectorOpen && !wasInspectorOpen.current;
    const inspectorClosed = !inspectorOpen && wasInspectorOpen.current;
    wasSidebarOpen.current = sidebarOpen;
    wasInspectorOpen.current = inspectorOpen;

    // Focus belongs to whoever has it. It is only moved out of a drawer that is
    // closing, or claimed when the document has none.
    function shouldReclaim(drawer: HTMLElement | null): boolean {
      if (typeof document === 'undefined') return false;
      const active = document.activeElement;
      if (active === null || active === document.body) return true;
      return drawer !== null && drawer.contains(active);
    }

    // Opening wins over closing, and the inspector is the drawer layered on
    // top — the same precedence Escape uses above.
    if (inspectorOpened) { inspectorCloseRef.current?.focus(); return; }
    if (sidebarOpened) { sidebarCloseRef.current?.focus(); return; }
    if (inspectorClosed && shouldReclaim(inspectorRef.current)) {
      inspectorToggleRef.current?.focus();
      return;
    }
    if (sidebarClosed && shouldReclaim(sidebarRef.current)) sidebarToggleRef.current?.focus();
  }, [sidebarOpen, inspectorOpen]);

  return (
    <div className="shell">
      <header className="shell-bar">
        <button
          ref={sidebarToggleRef}
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
          ref={inspectorToggleRef}
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
        ref={sidebarRef}
        id={SIDEBAR_ID}
        className="rail rail--sidebar"
        data-open={sidebarOpen}
        aria-label="Projects and conversations"
      >
        <div className="rail-close">
          <button ref={sidebarCloseRef} type="button" className="icon-button" aria-label="Close projects and conversations" onClick={() => onSidebarOpenChange(false)}>
            <span aria-hidden="true">×</span>
          </button>
        </div>
        {sidebar}
      </aside>

      <main className="center">{children}</main>

      <aside
        ref={inspectorRef}
        id={INSPECTOR_ID}
        className="rail rail--inspector"
        data-open={inspectorOpen}
        aria-label="Run inspector"
      >
        <div className="rail-close">
          <button ref={inspectorCloseRef} type="button" className="icon-button" aria-label="Close inspector panel" onClick={() => onInspectorOpenChange(false)}>
            <span aria-hidden="true">×</span>
          </button>
        </div>
        {inspector}
      </aside>
    </div>
  );
}
