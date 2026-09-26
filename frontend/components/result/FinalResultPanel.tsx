'use client';
import { ProductOutcomeBanner } from '@/components/result/ProductOutcomeBanner';
import { ProductOutcome } from '@/lib/productOutcome';
import { safeText } from '@/lib/sanitize';
import {
  DisplayValue,
  FinalResult,
  FinalResultParse,
  ProvenanceReference,
  REVIEW_GROUPS,
  ReviewItem,
  ReviewItemKind,
  UnresolvedGroupView,
  VehicleIdentity,
  VehicleResult,
  VehicleView,
  VerifiedField,
  describeOutcome,
  describeReviewCode,
  parseFinalResult,
} from '@/lib/finalResult';
import { isTerminalRunStatus, terminalRunStatusLabel } from '@/lib/runStatus';
import { PollingMode } from '@/lib/useRunRealtime';

export type FinalResultPanelProps = {
  /**
   * Whether this surface applies at all. The caller decides from the RUN'S OWN
   * IMMUTABLE IDENTITY, never from the payload and never from what the project
   * says today: a V1 run keeps its existing path, a run whose identity cannot
   * be trusted gets a bounded alert instead of a surface, and nothing in
   * `output` may route a run here.
   */
  visible: boolean;
  runId?: string;
  /** The durable run status, from the run row. */
  runStatus?: string;
  connection: PollingMode;
  /** The durable `run.output`, untouched. This component never mutates it. */
  output?: Record<string, unknown>;
  /**
   * The canonical ProductOutcome the finalizer recorded (`run.product_outcome`,
   * parsed by lib/productOutcome.ts). Shown once, above the product, as the
   * verdict; the payload below is what that verdict is about. Optional so the
   * surface renders exactly as before when a caller states none.
   */
  outcome?: ProductOutcome;
};

/**
 * The Swarm V2 FINAL RESULT surface — the product answer, and only that.
 *
 * It is deliberately separate from `SwarmRunCard`, which reports EXECUTION.
 * Nothing here describes how the run went: no stage track, no task rows, no
 * model-call count, no verifier batch progress, no launch state. Execution
 * telemetry is never presented as the result, and a run that merely finished
 * is never dressed up as an answer.
 *
 * Everything rendered comes from `parseFinalResult`, so the closed contract in
 * lib/finalResult.ts is the only thing that can reach the screen. There is no
 * `JSON.stringify` of durable data anywhere in this file, no walk over keys
 * the contract does not name, and no `dangerouslySetInnerHTML` — free-form
 * durable text goes through `safeText`, and the contract carries no HTML to
 * begin with. Technical detail stays in the Inspector.
 *
 * Five outcomes are kept visibly distinct, because they mean different things:
 * a usable result, a partial result WITH what is outstanding, an empty result,
 * a confirmed not-found, and a payload that cannot be trusted at all.
 */
export function FinalResultPanel({ visible, runId, runStatus, connection, output, outcome }: FinalResultPanelProps) {
  if (!visible) return null;

  return (
    <section className="panel final-result" aria-labelledby="final-result-title">
      <header className="final-result-head">
        <div>
          <h3 className="panel-title" id="final-result-title">Final result</h3>
          <p className="eyebrow">Swarm V2 product result</p>
        </div>
      </header>
      {outcome && isTerminalRunStatus(runStatus) && <ProductOutcomeBanner outcome={outcome} terminal />}
      <FinalResultBody runId={runId} runStatus={runStatus} connection={connection} output={output} />
    </section>
  );
}

function FinalResultBody({
  runId,
  runStatus,
  connection,
  output,
}: Omit<FinalResultPanelProps, 'visible'>) {
  // No run selected, or the run row has not arrived yet. "Not loaded" is not
  // "nothing was produced", so it never borrows either of those messages.
  if (!runId || runStatus === undefined) {
    return (
      <StatusNote
        symbol="…"
        label="Loading"
        detail={
          runId
            ? 'Reading the run. The result appears once the run has been read.'
            : 'No run is selected. Start a task to produce a result.'
        }
      />
    );
  }

  if (!isTerminalRunStatus(runStatus)) {
    // Still executing. A delayed poll is called out separately: a slow refresh
    // is a connection fact, not evidence that the run produced nothing.
    return (
      <StatusNote
        symbol="…"
        label="Not finished"
        detail={
          connection === 'reconnecting'
            ? 'The run has not finished and the last status check did not get through. Retrying — no result is available yet.'
            : 'The run has not finished. The final result appears once it reaches a terminal state.'
        }
      />
    );
  }

  // A run that failed, was cancelled, timed out or exhausted its budget reaches
  // no product outcome at all. Saying so plainly is the truthful answer; it is
  // neither an empty result nor a broken payload.
  const producesResult = runStatus === 'completed' || runStatus === 'partial_success';
  if (!producesResult && (output === undefined || output === null || Object.keys(output).length === 0)) {
    return (
      <StatusNote
        tone="negative"
        symbol="×"
        label={`Run ${terminalRunStatusLabel(runStatus).toLowerCase()}`}
        detail="The run ended without producing a product result. There is nothing to report for it."
      />
    );
  }

  const parsed: FinalResultParse = parseFinalResult(output, { runStatus });

  if (parsed.state === 'absent') {
    return (
      <StatusNote
        tone="negative"
        symbol="∅"
        label="No result recorded"
        detail="The run reached a terminal state but the backend recorded no result payload for it. Nothing is being inferred from that absence."
      />
    );
  }

  if (parsed.state === 'invalid') {
    // Fail-closed: a payload that cannot be trusted is shown as unusable, with
    // a static reason code. The payload itself is never rendered — displaying
    // it is precisely what a hostile or corrupted output would want.
    return (
      <StatusNote
        tone="negative"
        symbol="×"
        label="Result unavailable"
        detail="The recorded result did not satisfy the product contract, so it is not being displayed as a result. Technical detail is in the Inspector."
      >
        <p className="final-result-code">
          Reason <span className="identifier">{safeText(parsed.code)}</span>
        </p>
      </StatusNote>
    );
  }

  return <ResultBody result={parsed.result} />;
}

function ResultBody({ result }: { result: FinalResult }) {
  const outcome = describeOutcome(result.kind);
  const outstanding = result.review.filter((item) => item.kind !== 'empty_marker').length;

  return (
    <>
      {/* The outcome is announced once, as text. The symbol and the label both
          carry the meaning, so it survives with no colour and no stylesheet. */}
      <div className="final-result-banner" data-tone={outcome.tone} role="status">
        <p className="final-result-outcome">
          <span className="final-result-symbol" aria-hidden="true">{outcome.symbol}</span>
          <span className="final-result-label">{outcome.label}</span>
        </p>
        <p className="final-result-summary">{outcome.summary}</p>
      </div>

      {result.fields.length > 0 ? (
        <section className="final-result-section" aria-labelledby="final-result-fields-title">
          <h4 className="section-title" id="final-result-fields-title">Verified fields</h4>
          {result.multiValuedFieldCount > 0 && (
            <p className="note">
              {result.multiValuedFieldCount === 1
                ? 'One field was verified with more than one value. Every value is listed; none was selected.'
                : `${result.multiValuedFieldCount} fields were verified with more than one value. Every value is listed; none was selected.`}
            </p>
          )}
          <dl className="final-result-fields">
            {result.fields.map((field) => (
              <FieldEntry key={field.key} field={field} />
            ))}
          </dl>
        </section>
      ) : (
        <p className="muted">
          {result.kind === 'not_found'
            ? 'No fields are reported, which is the point of a confirmed negative.'
            : 'No field was verified, so no value is reported.'}
        </p>
      )}

      {result.vehicleResult && <VehicleSection view={result.vehicleResult} />}

      {/* A backend-VALID partial result can carry no itemized review rows at
          all: a rejected verdict, an unverified claim and a bare conflict each
          make a run partial without writing one. The payload does not say
          WHICH of those happened, so neither does this — naming a cause here
          would be the inference the next sentence promises not to make. */}
      {result.kind === 'partial_result' && outstanding === 0 && (
        <section className="final-result-section" aria-labelledby="final-result-noitems-title">
          <h4 className="section-title" id="final-result-noitems-title">Outstanding items</h4>
          <p className="note">
            The recorded outcome says the run left work unfinished, but it contains no itemized
            entries. This view does not infer the missing reason, task or claim.
          </p>
        </section>
      )}

      {outstanding > 0 && (
        <section className="final-result-section" aria-labelledby="final-result-review-title">
          <h4 className="section-title" id="final-result-review-title">
            Outstanding items ({outstanding})
          </h4>
          {REVIEW_GROUPS.map((group) => (
            <ReviewGroup
              key={group.kind}
              title={group.title}
              note={group.note}
              kind={group.kind}
              items={result.review.filter((item) => item.kind === group.kind)}
            />
          ))}
        </section>
      )}
    </>
  );
}

/**
 * Phase 2: one row per register row a task RESOLVED, then every unresolved
 * candidate in its own group. Identity line, verified field count and a
 * review badge only; the values themselves stay in the Inspector and in the
 * verified fields above. An unresolved candidate is never listed as a vehicle.
 */
function VehicleSection({ view }: { view: VehicleResult }) {
  return (
    <section className="final-result-section" aria-labelledby="final-result-vehicles-title">
      <h4 className="section-title" id="final-result-vehicles-title">
        Vehicles ({view.vehicles.length})
      </h4>
      {view.vehicles.length === 0 ? (
        <p className="muted">No register row was resolved to exactly one vehicle.</p>
      ) : (
        <ul className="final-result-review-items">
          {view.vehicles.map((vehicle) => (
            <li className="final-result-review-item" key={vehicle.key}>
              <VehicleRow vehicle={vehicle} />
            </li>
          ))}
        </ul>
      )}
      {view.unresolvedGroups.length > 0 && (
        <div className="final-result-review-group" data-kind="unresolved">
          <h5 className="final-result-review-title">
            Unresolved candidates ({view.unresolvedGroups.length})
          </h5>
          <p className="note">
            The register did not settle these on one row. They are not vehicles and carry no values.
          </p>
          <ul className="final-result-review-items">
            {view.unresolvedGroups.map((group, index) => (
              <li className="final-result-review-item" key={`${group.outcome}-${index}`}>
                <UnresolvedRow group={group} />
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function identityLine(identity: VehicleIdentity): string {
  const head = [identity.manufacturer, identity.commercialModel, identity.modelYear]
    .filter((part): part is string => part !== undefined)
    .join(' ');
  return [head, identity.trim, identity.officialModelCode]
    .filter((part): part is string => part !== undefined && part.length > 0)
    .join(' · ') || 'Identity not stated';
}

function VehicleRow({ vehicle }: { vehicle: VehicleView }) {
  const verified = vehicle.verifiedFieldCount;
  return (
    <>
      <span className="final-result-review-subject">{safeText(identityLine(vehicle.identity))}</span>
      <span className="final-result-review-reason">
        {verified === 1 ? '1 verified field' : `${verified} verified fields`}
      </span>
      {vehicle.review.length > 0 ? (
        <span className="final-result-review-reason" data-review="true">
          Needs review ({vehicle.review.length})
        </span>
      ) : (
        <span className="final-result-review-reason" data-review="false">No review items</span>
      )}
      <span className="final-result-review-reason">
        Register record <span className="identifier">{safeText(vehicle.key)}</span>
      </span>
    </>
  );
}

function UnresolvedRow({ group }: { group: UnresolvedGroupView }) {
  const ambiguous = group.outcome === 'unresolved_ambiguous';
  return (
    <>
      <span className="final-result-review-subject">{safeText(identityLine(group.identity))}</span>
      <span className="final-result-review-reason">
        {ambiguous
          ? `Ambiguous — matches ${group.recordIds.length} register rows`
          : 'Not found in the register'}
      </span>
      {group.recordIds.length > 0 && (
        <span className="final-result-review-reason">
          Register records <span className="identifier">{safeText(group.recordIds.join(', '))}</span>
        </span>
      )}
      <span className="final-result-review-reason">
        Tasks <span className="identifier">{safeText(group.taskIds.join(', '))}</span>
      </span>
    </>
  );
}

function FieldEntry({ field }: { field: VerifiedField }) {
  const multiple = field.values.length > 1;
  return (
    <div className="final-result-field" data-multiple={multiple}>
      <dt className="final-result-field-name">
        {safeText(field.label)}
        <span className="identifier final-result-field-key">{safeText(field.key)}</span>
      </dt>
      <dd className="final-result-field-values">
        {multiple && (
          <p className="final-result-multiple">
            {field.values.length} verified values — none was chosen.
          </p>
        )}
        <ul className="final-result-values">
          {field.values.map((entry, index) => (
            // Two durable rows may legitimately carry the same value, so the
            // index is the only stable identity for a rendered row here.
            <li className="final-result-value" key={`${field.key}-${index}`}>
              <ValueText value={entry.value} />
              <Provenance provenance={entry.provenance} />
            </li>
          ))}
        </ul>
      </dd>
    </div>
  );
}

/**
 * A durable value, rendered safely and within the parser's declared bounds.
 *
 * The parser has already refused anything the durable contract cannot carry
 * and has already applied the depth and breadth bounds, so this function only
 * renders what it is handed. Every string — a record key included — goes
 * through `safeText`, and a bound that bit is stated rather than hidden: a
 * reader is always told when they are not seeing the whole value.
 */
function ValueText({ value }: { value: DisplayValue }) {
  if (value.display === 'text') return <span className="final-result-value-text">{safeText(value.text)}</span>;
  if (value.display === 'empty') return <span className="muted">No value recorded</span>;
  if (value.display === 'depth_bounded') {
    return <span className="muted">Nested further than this view shows — see the Inspector for technical detail.</span>;
  }
  if (value.display === 'list') {
    return (
      <>
        <ul className="final-result-value-list">
          {value.items.map((item, index) => (
            // Repeated values are legitimate, so position is the only stable
            // identity for a rendered row.
            <li key={index}><ValueText value={item} /></li>
          ))}
        </ul>
        <HiddenCount hidden={value.hidden} noun="entries" />
      </>
    );
  }
  return (
    <>
      <dl className="final-result-value-record">
        {value.entries.map((entry, index) => (
          <div className="final-result-value-record-row" key={`${entry.key}-${index}`}>
            <dt>{safeText(entry.key)}</dt>
            <dd><ValueText value={entry.value} /></dd>
          </div>
        ))}
      </dl>
      <HiddenCount hidden={value.hidden} noun="keys" />
    </>
  );
}

/** States a breadth bound that bit. Silence here would misreport the value. */
function HiddenCount({ hidden, noun }: { hidden: number; noun: string }) {
  if (hidden <= 0) return null;
  return (
    <p className="note">
      {hidden} further {noun} are not shown here — see the Inspector for technical detail.
    </p>
  );
}

/**
 * Safe, public provenance: WHICH durable rows stand behind a value.
 *
 * Identifiers and declared scope only. No fragment text, no locator, no
 * source version, no hash, no confidence score — none of which the contract
 * carries in the first place. Collapsed by default so provenance never
 * competes with the answer, and a native `<details>` keeps it fully keyboard
 * operable with no custom key handling.
 */
function Provenance({ provenance }: { provenance?: ProvenanceReference }) {
  if (!provenance) return null;
  const rows: [string, string][] = [];
  if (provenance.sourceId) rows.push(['Source', provenance.sourceId]);
  if (provenance.taskId) rows.push(['Task', provenance.taskId]);
  if (provenance.claimId) rows.push(['Claim', provenance.claimId]);
  if (provenance.entity) rows.push(['Entity', provenance.entity]);
  if (provenance.geography) rows.push(['Geography', provenance.geography]);
  if (provenance.market) rows.push(['Market', provenance.market]);
  if (rows.length === 0) return null;

  return (
    <details className="final-result-provenance">
      <summary>Provenance</summary>
      <dl className="final-result-provenance-list">
        {rows.map(([label, value]) => (
          <div className="final-result-provenance-row" key={label}>
            <dt>{label}</dt>
            <dd><span className="identifier">{safeText(value)}</span></dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

function ReviewGroup({
  title,
  note,
  kind,
  items,
}: {
  title: string;
  note: string;
  kind: ReviewItemKind;
  items: ReviewItem[];
}) {
  if (items.length === 0) return null;
  return (
    <div className="final-result-review-group" data-kind={kind}>
      <h5 className="final-result-review-title">{title} ({items.length})</h5>
      <p className="note">{note}</p>
      <ul className="final-result-review-items">
        {items.map((item, index) => (
          <li className="final-result-review-item" key={`${kind}-${item.fieldKey ?? item.taskId ?? 'item'}-${index}`}>
            <ReviewItemBody item={item} />
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * One outstanding item.
 *
 * Every item reaching here is one of the contract's four classified shapes:
 * the parser refuses anything else outright, so there is no "unknown item"
 * branch to render and no way for an unclassifiable row to appear beside
 * verified fields.
 */
function ReviewItemBody({ item }: { item: ReviewItem }) {
  const codeLabel = describeReviewCode(item.code);
  return (
    <>
      <span className="final-result-review-subject">
        {item.fieldLabel ? safeText(item.fieldLabel) : safeText(item.taskLabel ?? 'Unnamed item')}
      </span>
      {/* `reason` is backend-owned but free-form by type, so it is bounded by
          the parser and rendered through safeText rather than trusted. */}
      {item.reason && <span className="final-result-review-reason">{safeText(item.reason)}</span>}
      {codeLabel && <span className="final-result-review-reason">{codeLabel}</span>}
      {item.code && <span className="identifier">{safeText(item.code)}</span>}
      {item.value && item.value.display !== 'empty' && (
        <span className="final-result-review-value">
          Reported value: <ValueText value={item.value} />
        </span>
      )}
      <Provenance provenance={item.provenance} />
    </>
  );
}

/**
 * A non-result state.
 *
 * Loading, a delayed poll, a non-product terminal status, an absent payload
 * and an invalid payload are five different facts and each gets its own
 * wording. The symbol and the label carry the meaning without colour.
 */
function StatusNote({
  tone = 'neutral',
  symbol,
  label,
  detail,
  children,
}: {
  tone?: 'neutral' | 'negative';
  symbol: string;
  label: string;
  detail: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="final-result-banner" data-tone={tone} role="status">
      <p className="final-result-outcome">
        <span className="final-result-symbol" aria-hidden="true">{symbol}</span>
        <span className="final-result-label">{label}</span>
      </p>
      <p className="final-result-summary">{detail}</p>
      {children}
    </div>
  );
}
