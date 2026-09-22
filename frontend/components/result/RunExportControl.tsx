'use client';
import { useEffect, useState } from 'react';
import { ApiError, api } from '@/lib/api';
import { ExportSummary, exportFilename, parseExportEnvelope, serializeExport } from '@/lib/runExport';
import { safeText } from '@/lib/sanitize';

export type RunExportControlProps = {
  /** Whether the surface applies at all (execution UI, a conversation, a run). */
  visible: boolean;
  runId?: string;
  /** Eligible = the run is terminal AND states a trustworthy identity. The server is authoritative. */
  eligible: boolean;
};

type ExportState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'ready'; summary: ExportSummary; href?: string; filename: string }
  | { kind: 'refused'; code: string };

/**
 * Retrieve, view and download the canonical export of ONE finished run.
 *
 * The browser does not build the export. It asks `GET /runs/{id}/export`
 * (the server's `build_export_envelope`), shows a CLOSED summary of the
 * envelope, and offers the server's document verbatim as a download. A
 * refusal is the server's static code; nothing else from the response is
 * rendered.
 */
export function RunExportControl({ visible, runId, eligible }: RunExportControlProps) {
  const [state, setState] = useState<ExportState>({ kind: 'idle' });

  // A new run, or a run that is not terminal any more, resets the surface.
  useEffect(() => {
    setState({ kind: 'idle' });
  }, [runId, eligible]);

  // Release the object URL of a download that is no longer offered.
  useEffect(() => {
    if (state.kind !== 'ready' || !state.href) return;
    const href = state.href;
    return () => {
      try { URL.revokeObjectURL(href); } catch { /* not every environment offers object URLs */ }
    };
  }, [state]);

  if (!visible || !runId) return null;

  const requestExport = async () => {
    setState({ kind: 'loading' });
    try {
      const raw = await api.exportRun(runId);
      const summary = parseExportEnvelope(raw);
      if (!summary || summary.runId !== runId) {
        setState({ kind: 'refused', code: 'EXPORT_ENVELOPE_INVALID' });
        return;
      }
      let href: string | undefined;
      try {
        if (typeof URL.createObjectURL === 'function') {
          href = URL.createObjectURL(new Blob([serializeExport(raw)], { type: 'application/json' }));
        }
      } catch {
        href = undefined;
      }
      setState({ kind: 'ready', summary, href, filename: exportFilename(runId) });
    } catch (error) {
      setState({ kind: 'refused', code: error instanceof ApiError ? error.code : 'EXPORT_FAILED' });
    }
  };

  return (
    <section className="panel run-export" aria-labelledby="run-export-title">
      <h3 className="panel-title" id="run-export-title">Export</h3>
      {!eligible ? (
        <p className="muted">Export is available once the run reaches a terminal state with a trustworthy identity.</p>
      ) : (
        <>
          <p className="muted">The canonical export is built by the server from the run's durable state, identity and outcome.</p>
          <button
            type="button"
            className="button"
            onClick={requestExport}
            disabled={state.kind === 'loading'}
          >
            {state.kind === 'loading' ? 'Preparing export…' : 'Export run (JSON)'}
          </button>
          {state.kind === 'refused' && (
            <p className="alert" role="alert">Export refused: {safeText(state.code)}</p>
          )}
          {state.kind === 'ready' && (
            <div className="run-export-ready" role="status">
              <p>Export ready.</p>
              <dl className="run-facts">
                <div className="run-fact"><dt>Schema</dt><dd>{safeText(state.summary.schemaVersion)}</dd></div>
                <div className="run-fact"><dt>Engine</dt><dd>{safeText(state.summary.engine)}</dd></div>
                <div className="run-fact"><dt>Terminal status</dt><dd>{safeText(state.summary.terminalStatus)}</dd></div>
                <div className="run-fact"><dt>Result kind</dt><dd>{state.summary.resultKind ? safeText(state.summary.resultKind) : 'none (non-product terminal)'}</dd></div>
                <div className="run-fact"><dt>Government provenance</dt><dd>{state.summary.governmentProvenance ? 'present' : 'none recorded'}</dd></div>
                <div className="run-fact"><dt>Generated</dt><dd>{safeText(state.summary.generatedAt)}</dd></div>
              </dl>
              {state.href ? (
                <a className="button" href={state.href} download={state.filename}>Download JSON</a>
              ) : (
                <p className="muted">Download is not available in this environment.</p>
              )}
            </div>
          )}
        </>
      )}
    </section>
  );
}
