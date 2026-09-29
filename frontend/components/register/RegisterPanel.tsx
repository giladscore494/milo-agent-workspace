'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api';
import {
  REGISTER_CAPTURE_FALLBACK,
  REGISTER_DIRECTORY_FALLBACK,
  REGISTER_READ_FALLBACK,
  safeErrorText,
} from '@/lib/errorText';
import {
  CapacityNumbers,
  REGISTER_STATE_COPY,
  RegisterUnit,
  RegisterView,
  capacityRefusal,
  capturable,
  formatBytes,
  formatCount,
  groupFits,
  parseRegister,
} from '@/lib/register';
import { safeText } from '@/lib/sanitize';

/** The three calls the page makes; injectable so tests need no network. */
export type RegisterClient = {
  register: (projectId: string) => Promise<unknown>;
  requestCapture: (projectId: string, registerVersion: string, tozars: string[], conversationId: string) => Promise<unknown>;
  requestDirectory: (projectId: string, conversationId: string) => Promise<unknown>;
};

/** The workspace's own API client, read at call time. */
const defaultClient: RegisterClient = {
  register: (projectId) => api.register(projectId),
  requestCapture: (projectId, version, tozars, conversationId) =>
    api.requestRegisterCapture(projectId, version, tozars, conversationId),
  requestDirectory: (projectId, conversationId) => api.requestRegisterDirectory(projectId, conversationId),
};

export type RegisterPanelProps = {
  /** The selected project; nothing is read without one. */
  projectId?: string;
  /** The open conversation a capture is recorded under; capture needs one. */
  conversationId?: string;
  client?: RegisterClient;
};

type Loaded =
  | { kind: 'idle' }
  | { kind: 'hidden' }
  | { kind: 'ready'; view: RegisterView; message?: string };

/**
 * PR-D1 — the Register page: the Government register's directory (every
 * exact tozar and its expected rows), each tozar's capture state, the totals
 * against the directory, and the capacity bar.
 *
 * It exists only while the server has register capture on: the read answers
 * 404 otherwise and this renders nothing. Capture is $0 and is not a run --
 * it reads the Government register into the catalog's snapshots, nothing
 * more. A group covers at most the server's cap of expected rows; a tozar
 * larger than the cap is captured alone. Every state shown comes from the
 * server's durable rows, read again after every action.
 */
export function RegisterPanel({ projectId, conversationId, client = defaultClient }: RegisterPanelProps) {
  const [loaded, setLoaded] = useState<Loaded>({ kind: 'idle' });
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const [refusedCapacity, setRefusedCapacity] = useState<CapacityNumbers>();
  const [notice, setNotice] = useState('');
  // The answer that may still become state: a newer read, or another
  // project, supersedes every older one.
  const generation = useRef(0);

  const load = useCallback(async (project: string) => {
    const mine = ++generation.current;
    let view: RegisterView | undefined;
    let failure: unknown;
    try {
      view = parseRegister(await client.register(project));
    } catch (error) {
      failure = error;
    }
    if (mine !== generation.current) return;
    setLoaded((previous) => {
      if (view !== undefined) return { kind: 'ready', view };
      // The page exists only once the server has answered it: a first read
      // that fails -- 404 while register capture is off, a server that
      // predates it, or any other failure -- shows nothing at all.
      if (previous.kind !== 'ready') return { kind: 'hidden' };
      // A later read that fails keeps the last answer and says so.
      return { ...previous, message: failure === undefined ? REGISTER_READ_FALLBACK
        : safeErrorText(failure, REGISTER_READ_FALLBACK) };
    });
  }, [client]);

  useEffect(() => {
    generation.current += 1;
    setLoaded({ kind: 'idle' });
    setSelected(new Set());
    setActionError('');
    setRefusedCapacity(undefined);
    setNotice('');
    if (projectId) void load(projectId);
  }, [projectId, load]);

  if (!projectId || loaded.kind === 'hidden' || loaded.kind === 'idle') return null;

  const view = loaded.view;
  const version = view?.directory?.registerVersion;
  const selectedUnits = view ? view.units.filter((item) => selected.has(item.tozar)) : [];
  const canAct = view !== undefined && view.canCapture && conversationId !== undefined && !busy;

  const toggle = (tozar: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(tozar)) next.delete(tozar); else next.add(tozar);
      return next;
    });
  };

  const capture = async (tozars: string[]) => {
    if (!view || !version || !conversationId || busy) return;
    setBusy(true);
    setActionError('');
    setRefusedCapacity(undefined);
    setNotice('');
    try {
      await client.requestCapture(projectId, version, tozars, conversationId);
      setSelected(new Set());
      setNotice(tozars.length === 1 ? 'Capture requested.' : `Capture requested for ${tozars.length} tozars.`);
    } catch (error) {
      setRefusedCapacity(capacityRefusal(error));
      setActionError(safeErrorText(error, REGISTER_CAPTURE_FALLBACK));
    } finally {
      setBusy(false);
      await load(projectId);
    }
  };

  const refresh = async () => {
    if (!conversationId || busy) return;
    setBusy(true);
    setActionError('');
    setRefusedCapacity(undefined);
    setNotice('');
    try {
      await client.requestDirectory(projectId, conversationId);
      setNotice('Directory refresh requested. Read the page again in a few minutes.');
    } catch (error) {
      setActionError(safeErrorText(error, REGISTER_DIRECTORY_FALLBACK));
    } finally {
      setBusy(false);
      await load(projectId);
    }
  };

  return (
    <section className="panel register-panel" aria-labelledby="register-title">
      <header className="catalog-review-head">
        <div>
          <h3 className="panel-title" id="register-title">Register</h3>
          <p className="eyebrow">Government register capture — per exact tozar, $0, not a run</p>
        </div>
        <button type="button" className="disclosure" aria-expanded={open} aria-controls="register-body"
          onClick={() => setOpen(!open)}>
          {open ? 'Hide' : 'Show'}
        </button>
      </header>
      <div id="register-body">
        {!open ? null : (
          <>
            {loaded.message && <p className="alert" role="alert">{safeText(loaded.message)}</p>}
            {actionError && <p className="alert" role="alert">{safeText(actionError)}</p>}
            {refusedCapacity && (
              <p className="note" aria-label="Capacity refusal">
                Current {formatBytes(refusedCapacity.currentBytes)} ({formatCount(refusedCapacity.currentBytes)} bytes),
                projected {formatBytes(refusedCapacity.projectedBytes)} ({formatCount(refusedCapacity.projectedBytes)} bytes),
                limit {formatBytes(refusedCapacity.limitBytes)} ({formatCount(refusedCapacity.limitBytes)} bytes).
              </p>
            )}
            {notice && <p className="muted" role="status">{notice}</p>}
            {(
              <RegisterBody
                view={view}
                selected={selected}
                selectedUnits={selectedUnits}
                canAct={canAct}
                hasConversation={conversationId !== undefined}
                busy={busy}
                onToggle={toggle}
                onCapture={(tozars) => void capture(tozars)}
                onRefresh={() => void refresh()}
                onReload={() => void load(projectId)}
              />
            )}
          </>
        )}
      </div>
    </section>
  );
}

type BodyProps = {
  view: RegisterView;
  selected: ReadonlySet<string>;
  selectedUnits: RegisterUnit[];
  canAct: boolean;
  hasConversation: boolean;
  busy: boolean;
  onToggle: (tozar: string) => void;
  onCapture: (tozars: string[]) => void;
  onRefresh: () => void;
  onReload: () => void;
};

function RegisterBody({ view, selected, selectedUnits, canAct, hasConversation, busy, onToggle, onCapture,
  onRefresh, onReload }: BodyProps) {
  const version = view.directory?.registerVersion;
  const selectedRows = selectedUnits.reduce((sum, item) => sum + item.expectedRows, 0);
  const fits = groupFits(selectedUnits, view.groupMaxRows);
  const { capacity } = view;
  const used = capacity.limitBytes > 0 ? Math.min(100, (capacity.currentBytes / capacity.limitBytes) * 100) : 100;
  return (
    <>
      <div className="register-capacity" aria-label="Database capacity">
        <p>
          Database {formatBytes(capacity.currentBytes)} of the {formatBytes(capacity.limitBytes)} threshold
          ({Math.round(capacity.threshold * 100)}% of {formatBytes(capacity.capacityBytes)});
          estimate {formatCount(capacity.bytesPerRowEstimate)} bytes per row.
        </p>
        <progress max={100} value={Math.round(used)} aria-label="Capacity used">{Math.round(used)}%</progress>
        {capacity.overThreshold && (
          <p className="alert" role="alert">The database is above its capacity threshold: capture is refused.</p>
        )}
      </div>

      {view.directory ? (
        <p className="muted" aria-label="Register totals">
          Captured {formatCount(view.totals.unitsCaptured)} of {formatCount(view.totals.unitsTotal)} tozars,
          {' '}{formatCount(view.totals.rowsCaptured)} of {formatCount(view.totals.rowsTotal)} rows.
          {' '}Directory <span className="identifier">{view.directory.registerVersion.slice(0, 12)}</span>,
          read {safeText(view.directory.fetchedAt)}.
        </p>
      ) : (
        <p className="muted">The register directory has not been read yet.</p>
      )}
      {!hasConversation && view.canCapture && (
        <p className="note">Open a conversation of this project to capture: each capture is recorded under it.</p>
      )}

      <div className="button-row">
        {view.canCapture && (
          <button type="button" className="button button--primary"
            disabled={!canAct || selectedUnits.length === 0 || !fits}
            onClick={() => onCapture(selectedUnits.map((item) => item.tozar))}>
            {busy ? 'Requesting…' : `Capture selected (${selectedUnits.length})`}
          </button>
        )}
        {view.canRefreshDirectory && (
          <button type="button" className="button button--quiet" disabled={busy || !hasConversation} onClick={onRefresh}>
            Refresh directory
          </button>
        )}
        <button type="button" className="button button--quiet" disabled={busy} onClick={onReload}>
          Reload
        </button>
      </div>
      {selectedUnits.length > 0 && (
        <p className={fits ? 'muted' : 'alert'} aria-label="Selection">
          Selected {selectedUnits.length} tozar{selectedUnits.length === 1 ? '' : 's'},
          {' '}{formatCount(selectedRows)} expected rows (group cap {formatCount(view.groupMaxRows)}).
          {!fits && ' A group over the cap is refused; choose fewer, or capture a large tozar alone.'}
        </p>
      )}

      {view.units.length > 0 && (
        <table className="register-table">
          <caption>Every tozar in the directory, exactly as the register spells it</caption>
          <thead>
            <tr>
              <th scope="col"><span className="sr-only">Select</span></th>
              <th scope="col">Tozar</th>
              <th scope="col">Expected rows</th>
              <th scope="col">State</th>
              <th scope="col">Snapshot</th>
              <th scope="col" title="Measured: the snapshot's raw records, candidates, variants and their ledger rows (table data; indexes excluded)">Bytes per row</th>
              <th scope="col"><span className="sr-only">Action</span></th>
            </tr>
          </thead>
          <tbody>
            {view.units.map((item) => {
              const can = capturable(item, version);
              return (
                <tr key={item.tozar} data-state={item.state}>
                  <td>
                    {view.canCapture && can && (
                      <input type="checkbox" aria-label={`Select ${item.tozar}`}
                        checked={selected.has(item.tozar)} disabled={!canAct}
                        onChange={() => onToggle(item.tozar)} />
                    )}
                  </td>
                  <td>{safeText(item.tozar)}</td>
                  <td>{formatCount(item.expectedRows)}</td>
                  <td>
                    {REGISTER_STATE_COPY[item.state]}
                    {item.state === 'failed' && item.failureCode && (
                      <> <span className="identifier">{safeText(item.failureCode)}</span></>
                    )}
                    {item.state === 'captured' && (
                      <span className="note">
                        {' '}— {formatCount(item.capturedRows ?? 0)} rows, {item.verified ? 'verified' : 'not verified'}
                      </span>
                    )}
                  </td>
                  <td>{item.snapshotKey ? <span className="identifier">{safeText(item.snapshotKey)}</span> : '—'}</td>
                  <td>{item.measuredBytesPerRow !== undefined ? formatCount(item.measuredBytesPerRow) : '—'}</td>
                  <td>
                    {view.canCapture && can && (
                      <button type="button" className="button button--quiet" disabled={!canAct}
                        onClick={() => onCapture([item.tozar])}>
                        {item.state === 'failed' ? 'Capture again' : 'Capture'}
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </>
  );
}
