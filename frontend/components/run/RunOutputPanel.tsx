'use client';
import { redactSecrets } from '@/lib/sanitize';

export type RunOutputPanelProps = {
  visible: boolean;
  output?: Record<string, unknown>;
};

/**
 * Transitional sanitized-output surface. It shows exactly what the backend
 * recorded, redacted, and says so plainly when nothing was recorded: an empty
 * output is never dressed up as a product result. The F4 final-result contract
 * replaces this panel.
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
