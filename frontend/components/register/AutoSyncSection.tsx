'use client';
import { AUTO_SYNC_REASON_COPY, AutoSync } from '@/lib/register';
import { safeText } from '@/lib/sanitize';

export type AutoSyncAction = 'on' | 'off' | 'resume';

function reasonText(code: string): string {
  return `${AUTO_SYNC_REASON_COPY[code] ?? code} (${code})`;
}

/**
 * PR-SYNC-2 — the Register page's Auto sync control: On / Off, a pause with
 * its exact next step and Resume, what the next scheduled tick would decide,
 * and the last tick. Everything shown is the server's (`autosync.view`);
 * the switch is recorded under the open conversation, like a sync.
 */
export function AutoSyncSection({ autoSync, busy, hasConversation, onSwitch }: {
  autoSync: AutoSync;
  busy: boolean;
  hasConversation: boolean;
  onSwitch?: (action: AutoSyncAction) => void;
}) {
  const { lastTick } = autoSync;
  const disabled = busy || !hasConversation || !onSwitch;
  return (
    <div className="register-auto-sync" aria-label="Auto sync">
      <p>
        <strong>Auto sync: {autoSync.enabled ? (autoSync.pausedReason ? 'Paused' : 'On') : 'Off'}</strong>
        {' '}— next tick: {autoSync.next.decision} ({reasonText(autoSync.next.reason)}).
      </p>
      {autoSync.pausedReason && (
        <p className="alert" role="alert" aria-label="Auto sync paused">
          Paused{autoSync.pausedAt ? ` since ${safeText(autoSync.pausedAt)}` : ''}: {reasonText(autoSync.pausedReason)}.
          {autoSync.nextStep && <> Next step: {safeText(autoSync.nextStep)}</>}
        </p>
      )}
      {autoSync.enabled && autoSync.schedulerStale && (
        <p className="alert" role="alert">
          Scheduler not ticking: no tick for over 2 hours while Auto sync is on. Check the Cloud Scheduler job
          (bash scripts/ops/setup-register-scheduler.sh --check).
        </p>
      )}
      {autoSync.dbWarning && (
        <p className="note" aria-label="Database warning">
          The database is at 90% of its capacity threshold or more: plan Register retention (vacuum-full) before
          Auto sync pauses itself.
        </p>
      )}
      <p className="muted" aria-label="Last tick">
        {lastTick
          ? <>Last tick {safeText(lastTick.at)}: {lastTick.decision} ({reasonText(lastTick.reason)}),
            backlog {safeText(lastTick.backlog)}, database {safeText(lastTick.dbMb)} MB.</>
          : 'No tick yet.'}
        {(autoSync.consecutiveThrottles > 0 || autoSync.consecutiveFailures > 0)
          && ` Throttled in a row: ${autoSync.consecutiveThrottles}; failed in a row: ${autoSync.consecutiveFailures}.`}
      </p>
      <div className="button-row">
        <button type="button" className="button button--quiet" disabled={disabled}
          onClick={() => onSwitch?.(autoSync.enabled ? 'off' : 'on')}>
          {autoSync.enabled ? 'Turn auto sync off' : 'Turn auto sync on'}
        </button>
        {autoSync.pausedReason && (
          <button type="button" className="button button--primary" disabled={disabled}
            onClick={() => onSwitch?.('resume')}>
            Resume
          </button>
        )}
      </div>
    </div>
  );
}
