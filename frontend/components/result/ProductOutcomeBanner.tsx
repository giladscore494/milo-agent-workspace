'use client';
import { safeText } from '@/lib/sanitize';
import { ProductOutcome, describeProductOutcome } from '@/lib/productOutcome';

export type ProductOutcomeBannerProps = {
  /** The parsed canonical outcome, or undefined when the run states none. */
  outcome?: ProductOutcome;
  /** True once the run is terminal: only then is "no verdict recorded" a fact. */
  terminal: boolean;
};

/**
 * The canonical verdict line: what the finalizer recorded about the product.
 *
 * It renders `run.product_outcome` — the record the canonical finalizer wrote
 * in the same transaction as the terminal status — and never anything
 * inferred from the payload. Semantic status, usability, coverage and the
 * allowlisted blocking codes are all static vocabulary; the payload reference
 * is a digest and a size, never content. A terminal run with no recorded
 * verdict (a cancellation, a timeout, a historical run) says exactly that.
 */
export function ProductOutcomeBanner({ outcome, terminal }: ProductOutcomeBannerProps) {
  if (!terminal) return null;
  if (!outcome) {
    return (
      <div className="final-result-banner product-outcome" data-tone="neutral" role="status">
        <p className="final-result-outcome">
          <span className="final-result-symbol" aria-hidden="true">–</span>
          <span className="final-result-label">No canonical verdict recorded</span>
        </p>
        <p className="final-result-summary">
          The run is terminal but the finalizer recorded no ProductOutcome for it — a cancellation, a timeout, a budget stop or a run finalized before canonical finalization. Nothing is inferred from the payload.
        </p>
      </div>
    );
  }
  const described = describeProductOutcome(outcome);
  const ratio = outcome.coverage.ratio;
  return (
    <div className="final-result-banner product-outcome" data-tone={described.tone} role="status" data-testid="product-outcome">
      <p className="final-result-outcome">
        <span className="final-result-symbol" aria-hidden="true">{described.symbol}</span>
        <span className="final-result-label">Canonical verdict: {described.label}</span>
      </p>
      <p className="final-result-summary">{described.summary}</p>
      <dl className="run-facts product-outcome-facts">
        <div className="run-fact"><dt>Usability</dt><dd>{safeText(outcome.usability)}</dd></div>
        <div className="run-fact"><dt>Coverage</dt><dd>{outcome.coverage.produced} produced · {outcome.coverage.outstanding} outstanding{ratio !== undefined ? ` · ${Math.round(ratio * 100)}%` : ''}</dd></div>
        <div className="run-fact"><dt>Engine</dt><dd className="identifier">{safeText(outcome.engine)}</dd></div>
        <div className="run-fact"><dt>Payload</dt><dd>{outcome.payload.present ? `recorded${outcome.payload.byteSize !== undefined ? ` · ${outcome.payload.byteSize} bytes` : ''}` : 'none recorded'}</dd></div>
      </dl>
      {outcome.blocking.length > 0 && (
        <ul className="product-outcome-blocking" aria-label="Blocking items">
          {outcome.blocking.map((item) => (
            <li key={item.code}>
              <span className="identifier">{item.code}</span> — {item.count} {item.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
