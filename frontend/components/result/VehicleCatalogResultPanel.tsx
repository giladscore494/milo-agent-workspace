'use client';
import { ProductOutcomeBanner } from '@/components/result/ProductOutcomeBanner';
import { ProductOutcome } from '@/lib/productOutcome';
import { isTerminalRunStatus, terminalRunStatusLabel } from '@/lib/runStatus';
import { safeText } from '@/lib/sanitize';
import { PollingMode } from '@/lib/useRunRealtime';
import {
  V1Model,
  VehicleCatalogResult,
  dataDepthLabel,
  parseVehicleCatalogResult,
  verificationLabel,
} from '@/lib/vehicleCatalogResult';

export type VehicleCatalogResultPanelProps = {
  /** Decided by the caller from the run's IMMUTABLE identity only. */
  visible: boolean;
  runId?: string;
  runStatus?: string;
  connection: PollingMode;
  /** The durable `run.output`, untouched. */
  output?: Record<string, unknown>;
  /** The canonical verdict from `run.product_outcome`, already parsed. */
  outcome?: ProductOutcome;
};

/**
 * The vehicle_catalog_v1 FINAL RESULT surface — the typed product, not a dump.
 *
 * Everything rendered comes from `parseVehicleCatalogResult`, a closed reader
 * of the engine's deterministic final document, so a key the contract does not
 * name never reaches the screen and free text is bounded before `safeText`.
 * There is no `JSON.stringify` of durable data here: the raw payload lives in
 * the Inspector's Developer tab as telemetry, and this surface is the product.
 *
 * The verdict line above the models is the canonical ProductOutcome the
 * finalizer recorded. The document's own `status` is shown as a fact the
 * engine declared, never as the verdict.
 */
export function VehicleCatalogResultPanel({ visible, runId, runStatus, connection, output, outcome }: VehicleCatalogResultPanelProps) {
  if (!visible) return null;
  return (
    <section className="panel final-result" aria-labelledby="vehicle-result-title">
      <header className="final-result-head">
        <div>
          <h3 className="panel-title" id="vehicle-result-title">Final result</h3>
          <p className="eyebrow">Vehicle Catalog V1 product result</p>
        </div>
      </header>
      <VehicleCatalogBody runId={runId} runStatus={runStatus} connection={connection} output={output} outcome={outcome} />
    </section>
  );
}

function Note({ tone = 'neutral', symbol, label, detail }: { tone?: 'neutral' | 'negative'; symbol: string; label: string; detail: string }) {
  return (
    <div className="final-result-banner" data-tone={tone} role="status">
      <p className="final-result-outcome">
        <span className="final-result-symbol" aria-hidden="true">{symbol}</span>
        <span className="final-result-label">{label}</span>
      </p>
      <p className="final-result-summary">{detail}</p>
    </div>
  );
}

function VehicleCatalogBody({ runId, runStatus, connection, output, outcome }: Omit<VehicleCatalogResultPanelProps, 'visible'>) {
  if (!runId || runStatus === undefined) {
    return <Note symbol="…" label="Loading" detail={runId ? 'Reading the run. The result appears once the run has been read.' : 'No run is selected. Start a task to produce a result.'} />;
  }
  if (!isTerminalRunStatus(runStatus)) {
    return (
      <Note symbol="…" label="Not finished" detail={connection === 'reconnecting'
        ? 'The run has not finished and the last status check did not get through. Retrying — no result is available yet.'
        : 'The run has not finished. The final result appears once it reaches a terminal state.'} />
    );
  }
  const producesResult = runStatus === 'completed' || runStatus === 'partial_success';
  const parsed = parseVehicleCatalogResult(output);
  if (!producesResult && parsed.state === 'absent') {
    return (
      <>
        <ProductOutcomeBanner outcome={outcome} terminal />
        <Note tone="negative" symbol="×" label={`Run ${terminalRunStatusLabel(runStatus).toLowerCase()}`} detail="The run ended without producing a product result. There is nothing to report for it." />
      </>
    );
  }
  if (parsed.state === 'absent') {
    return (
      <>
        <ProductOutcomeBanner outcome={outcome} terminal />
        <Note tone="negative" symbol="∅" label="No result recorded" detail="The run reached a terminal state but the backend recorded no result payload for it. Nothing is being inferred from that absence." />
      </>
    );
  }
  if (parsed.state === 'invalid') {
    return (
      <>
        <ProductOutcomeBanner outcome={outcome} terminal />
        <div className="final-result-banner" data-tone="negative" role="status">
          <p className="final-result-outcome">
            <span className="final-result-symbol" aria-hidden="true">×</span>
            <span className="final-result-label">Result unavailable</span>
          </p>
          <p className="final-result-summary">The recorded payload is not a vehicle catalog document, so it is not being displayed as one. Technical detail is in the Inspector.</p>
          <p className="final-result-code">Reason <span className="identifier">{safeText(parsed.code)}</span></p>
        </div>
      </>
    );
  }
  return (
    <>
      <ProductOutcomeBanner outcome={outcome} terminal />
      <CatalogBody result={parsed.result} />
    </>
  );
}

function CatalogBody({ result }: { result: VehicleCatalogResult }) {
  const depth = dataDepthLabel(result.dataDepth);
  return (
    <>
      <dl className="run-facts vehicle-result-facts">
        <div className="run-fact"><dt>Manufacturer</dt><dd>{safeText(result.manufacturer)}</dd></div>
        <div className="run-fact"><dt>Market</dt><dd>{safeText(result.market)}</dd></div>
        <div className="run-fact"><dt>Period</dt><dd>{safeText(result.period)}</dd></div>
        <div className="run-fact"><dt>Engine declared</dt><dd className="identifier">{safeText(result.declaredStatus)}</dd></div>
      </dl>
      {result.summary && <p className="vehicle-result-summary">{safeText(result.summary)}</p>}

      <section className="final-result-section" aria-labelledby="vehicle-models-title">
        <h4 className="section-title" id="vehicle-models-title">Models ({result.models.length})</h4>
        <p className="note">
          {result.counts.verified} verified · {result.counts.needsReview} awaiting review · {result.counts.rejected} rejected
          {result.counts.failedAgents > 0 ? ` · ${result.counts.failedAgents} failed agents` : ''}
        </p>
        {result.models.length === 0 ? (
          <p className="muted">No models were catalogued.</p>
        ) : (
          <ul className="vehicle-models">
            {result.models.map((model, index) => <ModelRow key={`${model.name}-${index}`} model={model} />)}
          </ul>
        )}
      </section>

      <section className="final-result-section" aria-labelledby="vehicle-quality-title">
        <h4 className="section-title" id="vehicle-quality-title">Pipeline quality</h4>
        <dl className="run-facts vehicle-quality">
          {result.quality.map((stage) => (
            <div className="run-fact" key={stage.stage} data-status={stage.status}>
              <dt>{stage.label}</dt><dd>{safeText(stage.status)}</dd>
            </div>
          ))}
          {depth && <div className="run-fact"><dt>Data depth</dt><dd>{depth}</dd></div>}
        </dl>
      </section>
    </>
  );
}

function ModelRow({ model }: { model: V1Model }) {
  return (
    <li className="vehicle-model" data-verification={model.verification}>
      <div className="vehicle-model-head">
        <span className="vehicle-model-name">{safeText(model.name)}</span>
        {model.nameHe && <span className="vehicle-model-he" lang="he" dir="rtl">{safeText(model.nameHe)}</span>}
        <span className="badge vehicle-model-verdict">{verificationLabel(model.verification)}</span>
      </div>
      <dl className="vehicle-model-fields">
        {model.fields.map((field) => (
          <div className="vehicle-model-field" key={field.key}>
            <dt>{field.label}</dt><dd>{safeText(field.value)}</dd>
          </div>
        ))}
        {model.confidence && <div className="vehicle-model-field"><dt>Confidence</dt><dd>{model.confidence}</dd></div>}
        {model.sourceStrength && <div className="vehicle-model-field"><dt>Source strength</dt><dd>{safeText(model.sourceStrength)}</dd></div>}
      </dl>
      {model.sources.length > 0 && (
        <details className="final-result-provenance">
          <summary>{model.sources.length === 1 ? '1 source' : `${model.sources.length} sources`}</summary>
          <ul className="vehicle-model-sources">
            {model.sources.map((source) => (
              <li key={source.url}><a href={source.url} rel="noreferrer noopener" target="_blank">{safeText(source.host)}</a></li>
            ))}
          </ul>
        </details>
      )}
    </li>
  );
}
