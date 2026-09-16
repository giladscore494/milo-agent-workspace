'use client';
import { redactSecrets } from '@/lib/sanitize';

export type RunOutputPanelProps = {
  visible: boolean;
  output?: Record<string, unknown>;
};

/**
 * The sanitized-output surface for workflows that have no typed product
 * contract — V1 and anything else that is not Swarm V2.
 *
 * It shows exactly what the backend recorded, redacted, and says so plainly
 * when nothing was recorded: an empty output is never dressed up as a product
 * result. Swarm V2 no longer reaches this panel; it has a closed, typed
 * contract and its own product surface (components/result/FinalResultPanel).
 * The caller decides between the two from the project's trusted workflow_key,
 * so this path is unchanged for every workflow that used it before.
 */
export function RunOutputPanel({ visible, output }: RunOutputPanelProps) {
  if (!visible) return null;
  const hasOutput = output !== undefined && output !== null && Object.keys(output).length > 0;
  return (
    <section className="panel panel--quiet">
      <h3 className="panel-title">Final artifacts</h3>
      {hasOutput ? (
        <pre className="code-block">{JSON.stringify(redactSecrets(output), null, 2)}</pre>
      ) : (
        <p className="muted">The backend has recorded no output payload for this run.</p>
      )}
    </section>
  );
}
