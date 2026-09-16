'use client';
import { safeText } from '@/lib/sanitize';
import { LaunchState } from '@/lib/types';

export type LaunchStateNoteProps = {
  launchState?: LaunchState;
  launchReconciliationRequired?: boolean;
  /** `note` sits in the Swarm card footer; `muted` is the V1 panel's paragraph. */
  tone: 'note' | 'muted';
};

/**
 * What the worker launch did, and — when it is unknown — what happens next.
 *
 * `launch_unknown` is the one launch state that is not merely informational.
 * The API reached it because a launch request may or may not have started a
 * worker, so it parked the run and refused to launch again
 * (`backend/main.py`, `JobLaunchUncertain`). Resolving it is an operator action
 * against Cloud Run execution logs
 * (`scripts/release/reconcile-launch-unknown.sh`); automatic reconciliation is
 * `INTENTIONALLY_DEFERRED` precisely because guessing wrong means a double
 * execution and a double spend.
 *
 * A user reading this screen must therefore not be left expecting a retry. The
 * note says, in words, that the run is parked, that it will not be relaunched
 * automatically, and that an operator has to reconcile it. It is a
 * `role="status"` because it is a state the run arrived in, not an error the
 * user caused and not something they can act on themselves.
 *
 * The launch state itself is a closed backend enum and the browser never sees
 * the launch exception — `_safe_run_response` strips it and exposes only the
 * classification and this flag.
 */
export function LaunchStateNote({ launchState, launchReconciliationRequired, tone }: LaunchStateNoteProps) {
  if (!launchState) return null;
  // The Swarm card keeps this inline in its footer; the V1 panel gives it a
  // paragraph of its own. Same words either way.
  const Wrapper = tone === 'note' ? 'span' : 'p';
  const className = tone === 'note' ? 'note' : 'muted';

  if (!launchReconciliationRequired) {
    return (
      <Wrapper className={className}>
        {tone === 'note' ? 'Launch ' : 'Launch state '}
        <b>{safeText(launchState)}</b>
        {tone === 'note' ? '' : '.'}
      </Wrapper>
    );
  }
  return (
    <Wrapper className={`${className} launch-unknown`} role="status">
      Launch outcome unknown (<b>{safeText(launchState)}</b>) — operator reconciliation required.
      The run is parked and will <b>not</b> be relaunched automatically; nothing on this screen
      retries it.
    </Wrapper>
  );
}
