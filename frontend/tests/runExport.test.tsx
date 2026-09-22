import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiMocks = vi.hoisted(() => ({ exportRun: vi.fn() }));

vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api');
  return { ...actual, api: { exportRun: apiMocks.exportRun } };
});

import { ApiError } from '../lib/api';
import { EXPORT_SCHEMA_VERSION, exportFilename, parseExportEnvelope, serializeExport } from '../lib/runExport';
import { RunExportControl } from '../components/result/RunExportControl';

const RUN_ID = 'c1c1c1c1-1111-4111-8111-000000000001';

const ENVELOPE = {
  schema_version: EXPORT_SCHEMA_VERSION,
  run_id: RUN_ID,
  engine: 'vehicle_catalog_v1',
  run_identity: { run_id: RUN_ID, workflow_key: 'vehicle_catalog_v1', release_sha: 'a'.repeat(40) },
  terminal_status: 'completed',
  result_kind: 'usable_result',
  generated_at: '2026-09-22T00:00:00+00:00',
  government_provenance: { source_ids: [], datasets: [], present: false },
  result: { result: { models: [{ canonical_model_name: '<img src=x onerror=alert(1)>' }] } },
  usage: { model_calls: 3 },
  error: null,
};

describe('parseExportEnvelope', () => {
  it('reads exactly the contract fields of a valid envelope', () => {
    expect(parseExportEnvelope(ENVELOPE)).toEqual({
      schemaVersion: EXPORT_SCHEMA_VERSION,
      runId: RUN_ID,
      engine: 'vehicle_catalog_v1',
      terminalStatus: 'completed',
      resultKind: 'usable_result',
      generatedAt: '2026-09-22T00:00:00+00:00',
      governmentProvenance: false,
    });
  });

  it('refuses an unknown schema, a disagreeing identity and a non-terminal status', () => {
    expect(parseExportEnvelope({ ...ENVELOPE, schema_version: 'milo-run-export/2' })).toBeUndefined();
    expect(parseExportEnvelope({ ...ENVELOPE, run_identity: { ...ENVELOPE.run_identity, run_id: 'other' } })).toBeUndefined();
    expect(parseExportEnvelope({ ...ENVELOPE, run_identity: { ...ENVELOPE.run_identity, workflow_key: 'swarm_v2' } })).toBeUndefined();
    expect(parseExportEnvelope({ ...ENVELOPE, terminal_status: 'running' })).toBeUndefined();
    expect(parseExportEnvelope(null)).toBeUndefined();
    expect(parseExportEnvelope('{}')).toBeUndefined();
  });

  it('names the download after the run id only and serializes the document verbatim', () => {
    expect(exportFilename(RUN_ID)).toBe(`milo-run-${RUN_ID}.json`);
    expect(JSON.parse(serializeExport(ENVELOPE))).toEqual(ENVELOPE);
  });
});

describe('RunExportControl', () => {
  beforeEach(() => {
    apiMocks.exportRun.mockReset();
    Object.defineProperty(URL, 'createObjectURL', { value: vi.fn(() => 'blob:milo-export'), configurable: true });
    Object.defineProperty(URL, 'revokeObjectURL', { value: vi.fn(), configurable: true });
  });
  afterEach(() => {
    delete (URL as unknown as Record<string, unknown>).createObjectURL;
    delete (URL as unknown as Record<string, unknown>).revokeObjectURL;
  });

  it('renders nothing when not visible and a waiting note when not eligible', () => {
    const { container, rerender } = render(<RunExportControl visible={false} runId={RUN_ID} eligible />);
    expect(container).toBeEmptyDOMElement();
    rerender(<RunExportControl visible runId={RUN_ID} eligible={false} />);
    expect(screen.getByText(/Export is available once the run reaches a terminal state/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Export run (JSON)' })).toBeNull();
    expect(apiMocks.exportRun).not.toHaveBeenCalled();
  });

  it('retrieves the server envelope, shows the closed summary and offers the download', async () => {
    apiMocks.exportRun.mockResolvedValue(ENVELOPE);
    render(<RunExportControl visible runId={RUN_ID} eligible />);
    fireEvent.click(screen.getByRole('button', { name: 'Export run (JSON)' }));
    await waitFor(() => expect(screen.getByText('Export ready.')).toBeInTheDocument());
    expect(apiMocks.exportRun).toHaveBeenCalledWith(RUN_ID);
    const ready = screen.getByRole('status');
    expect(ready).toHaveTextContent('vehicle_catalog_v1');
    expect(ready).toHaveTextContent('completed');
    expect(ready).toHaveTextContent('usable_result');
    expect(ready).toHaveTextContent('none recorded');
    const link = screen.getByRole('link', { name: 'Download JSON' });
    expect(link).toHaveAttribute('download', `milo-run-${RUN_ID}.json`);
    expect(link).toHaveAttribute('href', 'blob:milo-export');
    // The result payload is downloaded, never rendered.
    expect(document.body.innerHTML).not.toContain('onerror');
    expect(document.querySelector('img')).toBeNull();
  });

  it('shows the server refusal code and nothing else from the response', async () => {
    apiMocks.exportRun.mockRejectedValue(new ApiError(409, 'RUN_NOT_EXPORTABLE', 'run cannot be exported: only a run in a terminal state can be exported'));
    render(<RunExportControl visible runId={RUN_ID} eligible />);
    fireEvent.click(screen.getByRole('button', { name: 'Export run (JSON)' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Export refused: RUN_NOT_EXPORTABLE'));
    expect(screen.queryByRole('link', { name: 'Download JSON' })).toBeNull();
  });

  it('refuses an envelope that is not the contract or names another run', async () => {
    apiMocks.exportRun.mockResolvedValue({ ...ENVELOPE, run_id: 'someone-elses', run_identity: { run_id: 'someone-elses', workflow_key: 'vehicle_catalog_v1' } });
    render(<RunExportControl visible runId={RUN_ID} eligible />);
    fireEvent.click(screen.getByRole('button', { name: 'Export run (JSON)' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('EXPORT_ENVELOPE_INVALID'));
  });
});
