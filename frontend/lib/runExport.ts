/**
 * The canonical run export, as the browser is allowed to understand it.
 *
 * `GET /runs/{id}/export` returns the envelope `backend/export_envelope.py`
 * built. No export logic lives here: the download is the server's document
 * serialized verbatim, and the on-screen summary is a CLOSED read of the few
 * fields the envelope contract names. Nothing in `result` is walked or
 * rendered by this module.
 */

export const EXPORT_SCHEMA_VERSION = 'milo-run-export/2';

/**
 * Every envelope version this release reads. `/2` (PR-R) only widened the
 * `usage` block, which this module never reads, so a `/1` document from a
 * server still on the previous release parses the same way. Anything else is
 * refused.
 */
export const ACCEPTED_EXPORT_SCHEMA_VERSIONS: ReadonlySet<string> = new Set([
  'milo-run-export/1',
  EXPORT_SCHEMA_VERSION,
]);

const TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  'completed', 'partial_success', 'failed', 'cancelled', 'timed_out', 'budget_exhausted',
]);

export type ExportSummary = {
  schemaVersion: string;
  runId: string;
  engine: string;
  terminalStatus: string;
  /** `undefined` for a non-product terminal; the envelope states `null`. */
  resultKind?: string;
  generatedAt: string;
  /** Whether the envelope carries any government provenance at all. */
  governmentProvenance: boolean;
};

function text(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() !== '' ? value : undefined;
}

/**
 * The summary of an envelope, or `undefined` when the document is not one
 * this release understands. A refusal here is a bounded message, never a
 * rendering of whatever came back.
 */
export function parseExportEnvelope(raw: unknown): ExportSummary | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const record = raw as Record<string, unknown>;
  const schemaVersion = text(record.schema_version);
  const runId = text(record.run_id);
  const engine = text(record.engine);
  const terminalStatus = text(record.terminal_status);
  const generatedAt = text(record.generated_at);
  if (!schemaVersion || !ACCEPTED_EXPORT_SCHEMA_VERSIONS.has(schemaVersion)) return undefined;
  if (!runId || !engine || !terminalStatus || !generatedAt) return undefined;
  if (!TERMINAL_STATUSES.has(terminalStatus)) return undefined;
  const identity = record.run_identity;
  if (!identity || typeof identity !== 'object') return undefined;
  if ((identity as Record<string, unknown>).run_id !== runId) return undefined;
  if ((identity as Record<string, unknown>).workflow_key !== engine) return undefined;
  const provenance = record.government_provenance;
  const governmentProvenance =
    !!provenance && typeof provenance === 'object' && (provenance as Record<string, unknown>).present === true;
  return {
    schemaVersion,
    runId,
    engine,
    terminalStatus,
    resultKind: text(record.result_kind),
    generatedAt,
    governmentProvenance,
  };
}

/** The file the download is offered as. Run id only: no title, no user text. */
export function exportFilename(runId: string): string {
  return `milo-run-${runId}.json`;
}

/** The server's document, serialized verbatim for the download. */
export function serializeExport(raw: unknown): string {
  return JSON.stringify(raw, null, 2);
}
