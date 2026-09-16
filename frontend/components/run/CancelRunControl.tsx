'use client';
import { KeyboardEvent, RefObject, useEffect, useRef } from 'react';
import { safeText } from '@/lib/sanitize';

export type CancelRunControlProps = {
  /** Distinguishes the two run surfaces so their input ids never collide. */
  idPrefix: string;
  /**
   * The run is still cancellable. When this goes false the whole control is
   * withdrawn, which is the terminal-state case the focus handling below is
   * written for.
   */
  available: boolean;
  confirming: boolean;
  reason: string;
  error: string;
  onReasonChange: (value: string) => void;
  onRequestCancel: () => void;
  onConfirm: () => void;
  onKeepRunning: () => void;
  /**
   * Where focus goes when the control is withdrawn from under it — normally the
   * surface's own heading, so a keyboard user lands on the panel that replaced
   * the control rather than at the top of the document.
   */
  focusFallbackRef?: RefObject<HTMLElement | null>;
};

/**
 * Cancelling a run: the request, the confirmation and the focus contract.
 *
 * Both run surfaces had this markup, character for character, and both were
 * missing the same thing. A confirmation that appears without taking focus is
 * one a keyboard user has to go looking for; a confirmation that disappears
 * without giving focus back leaves them at the top of the document; and a
 * control withdrawn because the run reached a terminal state takes the focused
 * element with it. So the behaviour lives here once, and both surfaces get it:
 *
 *  - opening the confirmation moves focus to the reason field, which is where
 *    the interaction continues;
 *  - Escape is equivalent to "Keep running" — the non-destructive choice, as a
 *    dismissal always must be — and closing either way returns focus to the
 *    "Cancel run" button that opened it;
 *  - when the run goes terminal while focus is inside the control, focus moves
 *    to the surface heading instead of being dropped.
 *
 * Focus is only ever taken when the document has NONE — the browser parks focus
 * on `<body>` when the focused element is removed — so a user who has already
 * clicked somewhere else is never yanked back.
 *
 * This is deliberately NOT a modal. It is an inline confirmation inside a panel
 * that keeps updating around it: the run is still executing, its status still
 * changes, and trapping focus or marking the rest of the page inert would hide
 * that. It therefore carries a labelled group, not `role="dialog"`, because
 * claiming dialog semantics without dialog behaviour is worse than claiming
 * neither.
 */
export function CancelRunControl({
  idPrefix,
  available,
  confirming,
  reason,
  error,
  onReasonChange,
  onRequestCancel,
  onConfirm,
  onKeepRunning,
  focusFallbackRef,
}: CancelRunControlProps) {
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const reasonRef = useRef<HTMLInputElement | null>(null);
  const wasConfirming = useRef(confirming);
  const wasAvailable = useRef(available);

  useEffect(() => {
    const opened = confirming && !wasConfirming.current;
    const closed = !confirming && wasConfirming.current;
    const withdrawn = !available && wasAvailable.current;
    wasConfirming.current = confirming;
    wasAvailable.current = available;

    // Only act when the document has no focus of its own to speak of.
    const unfocused = typeof document !== 'undefined'
      && (document.activeElement === null || document.activeElement === document.body);

    if (opened) {
      reasonRef.current?.focus();
      return;
    }
    if (withdrawn) {
      if (unfocused) focusFallbackRef?.current?.focus();
      return;
    }
    if (closed && unfocused) triggerRef.current?.focus();
  }, [available, confirming, focusFallbackRef]);

  function onConfirmKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== 'Escape') return;
    // Stop here: the workspace shell also closes a drawer on Escape, and one
    // key press must dismiss one thing.
    event.stopPropagation();
    onKeepRunning();
  }

  return (
    <>
      {available && !confirming && (
        <button ref={triggerRef} type="button" className="button button--quiet" onClick={onRequestCancel}>
          Cancel run
        </button>
      )}
      {available && confirming && (
        <div className="cancel-form" role="group" aria-label="Confirm run cancellation" onKeyDown={onConfirmKeyDown}>
          <div className="field">
            <label className="field-label sr-only" htmlFor={`${idPrefix}-cancel-reason`}>Cancellation reason</label>
            <input
              ref={reasonRef}
              id={`${idPrefix}-cancel-reason`}
              value={reason}
              onChange={(event) => onReasonChange(event.target.value)}
              placeholder="Reason (optional)"
            />
          </div>
          <div className="button-row">
            <button type="button" className="button button--danger" onClick={onConfirm}>Confirm cancellation</button>
            <button type="button" className="button button--quiet" onClick={onKeepRunning}>Keep running</button>
          </div>
        </div>
      )}
      {error && <p className="alert" role="alert">{safeText(error)}</p>}
    </>
  );
}
