'use client';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, api, executionUiEnabled, newIdempotencyKey, type WorkScopeInput } from '@/lib/api';
import { safeErrorText } from '@/lib/errorText';
import {
  INITIAL_WORKSPACE_SCOPE,
  PendingRequest,
  WorkspaceScope,
  beginPending,
  nextSessionScope,
  ownsConversation,
  ownsProject,
  ownsRun,
  ownsSession,
  settlePending,
  withConversation,
  withProject,
  withRun,
} from '@/lib/ownership';
import { getCurrentSession, onAuthStateChange, signInWithSupabase, signOutFromSupabase, SupabaseSession } from '@/lib/supabaseClient';
import { isTerminalRunStatus, isPartialSuccessRunStatus } from '@/lib/runStatus';
import { buildLiveRunViewModel } from '@/lib/liveRunViewModel';
import { parseProductOutcome } from '@/lib/productOutcome';
import { useRunRealtime } from '@/lib/useRunRealtime';
import {
  CanonicalCatalogPage,
  CatalogReviewPage as CatalogReviewPageData,
  Conversation,
  Project,
  Proposal,
  RunSummary,
} from '@/lib/types';
import {
  CATALOG_PAGE_SIZE,
  parseCanonicalPage,
  parseReviewPage,
} from '@/lib/catalogReview';
import {
  WorkScopeCapabilities,
  WorkScopeDirectory,
  WorkScopeDraft,
  WorkScopeEdit,
  WorkScopeNote,
  WorkScopeProgress,
  WorkScopeState,
  draftEdit,
  draftFromPlan,
  emptyDraft,
  parseBatchStart,
  parseCapabilities,
  parseDirectory,
  parseOpenWorkScope,
  parsePauseResult,
  parseProgress,
  parseWorkScopeMutation,
} from '@/lib/workScope';
import { AuthScreen, SessionRestoreScreen } from '@/components/auth/AuthScreen';
import {
  CatalogReviewPanel,
  CatalogReviewView,
} from '@/components/catalog/CatalogReviewPanel';
import { ConversationView } from '@/components/conversation/ConversationView';
import { ComposerRoute, TaskComposer } from '@/components/conversation/TaskComposer';
import { InspectorTab, RunInspector } from '@/components/inspector/RunInspector';
import { WorkflowProposalPanel } from '@/components/proposals/WorkflowProposalPanel';
import { MappingPlanPanel } from '@/components/scope/MappingPlanPanel';
import { FinalResultPanel } from '@/components/result/FinalResultPanel';
import { RunExportControl } from '@/components/result/RunExportControl';
import { VehicleCatalogResultPanel } from '@/components/result/VehicleCatalogResultPanel';
import { LiveRunPanel } from '@/components/run/LiveRunPanel';
import { RunHistoryList } from '@/components/run/RunHistoryList';
import { CurrentRunPanel } from '@/components/run/CurrentRunPanel';
import { SwarmRunCard } from '@/components/swarm/SwarmRunCard';
import { WorkspaceShell } from '@/components/workspace/WorkspaceShell';
import { WorkspaceSidebar } from '@/components/workspace/WorkspaceSidebar';

const HARDENING_NOTE = 'Execution controls are hidden: the execution UI flag is off. Backend execution flags and authorization stay authoritative either way.';

/**
 * Shown when a stored run id does not survive verification against the server.
 * Static and authored here: it says what happened without repeating anything
 * the server said about a run this conversation may not own.
 */
const STORED_RUN_REJECTED = 'The run stored for this conversation could not be verified as belonging to it and was cleared.';

const ACTIVE_RUN_KEY_PREFIX = 'milo.activeRun.';

/**
 * One catalog read in flight, and which page it is.
 *
 * The smallest wrapper that `lib/ownership.ts`'s `PendingRequest` needs to
 * become a catalog request: the monotonic identity and the workspace scope come
 * from `beginPending`, and `view`/`offset` say which page the token stands for
 * so a superseded answer can be reasoned about rather than merely dropped.
 */
type CatalogRequest = {
  readonly pending: PendingRequest;
  readonly view: CatalogReviewView;
  readonly offset: number;
};

/** Sentinel for "this page has not observed an authenticated identity yet". */
const NO_SESSION_YET = Symbol('no session observed yet');

function activeRunStorageKey(conversationId: string): string {
  return `${ACTIVE_RUN_KEY_PREFIX}${conversationId}`;
}

function readStoredRunId(conversationId?: string): string | undefined {
  if (!conversationId || typeof window === 'undefined') return undefined;
  try {
    return window.sessionStorage.getItem(activeRunStorageKey(conversationId)) ?? undefined;
  } catch {
    return undefined;
  }
}

function storeRunId(conversationId: string, runId?: string) {
  if (typeof window === 'undefined') return;
  try {
    if (runId) window.sessionStorage.setItem(activeRunStorageKey(conversationId), runId);
    else window.sessionStorage.removeItem(activeRunStorageKey(conversationId));
  } catch {
    // Storage may be unavailable (private mode); polling still works in-page.
  }
}

/**
 * Drop every stored active-run id.
 *
 * Session storage survives a sign-out, and a run id sitting under a
 * conversation key is browser-held state about work the signed-out user
 * started. Clearing it is what makes "sign-out removes rendered workspace
 * data" true of the browser and not only of the React tree.
 */
function clearStoredRunIds() {
  if (typeof window === 'undefined') return;
  try {
    const keys: string[] = [];
    for (let index = 0; index < window.sessionStorage.length; index += 1) {
      const key = window.sessionStorage.key(index);
      if (key?.startsWith(ACTIVE_RUN_KEY_PREFIX)) keys.push(key);
    }
    for (const key of keys) window.sessionStorage.removeItem(key);
  } catch {
    // Storage may be unavailable (private mode); nothing was stored either.
  }
}

/**
 * Orchestration boundary for the workspace.
 *
 * Every API call, every piece of session, project, conversation, proposal and
 * run state, the idempotency-key lifetime, the active-run session storage and
 * the polling hook live here. The components below are presentational: they
 * receive typed props and report intent back through callbacks, so no state
 * has a second owner.
 *
 * Because this component stays mounted across every switch, unmounting is not
 * the boundary that keeps one selection's data out of another's. `scope`
 * (lib/ownership.ts) is: it holds the session, project, conversation and run
 * this page is showing RIGHT NOW, it is updated synchronously in the handler
 * that causes the switch rather than after a render, and every asynchronous
 * result is applied only if the scope it was issued under still owns the state
 * it would write. A dropped answer is dropped silently — nobody is looking at
 * the selection it belonged to.
 */
export default function WorkspacePage() {
  const executionUi = executionUiEnabled();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [authError, setAuthError] = useState('');
  const [session, setSession] = useState<SupabaseSession | null>();

  const [projects, setProjects] = useState<Project[]>();
  const [projectsError, setProjectsError] = useState('');
  const [selectedProject, setSelectedProject] = useState<Project>();

  const [conversationTitle, setConversationTitle] = useState('');
  const [conversations, setConversations] = useState<Conversation[]>();
  const [activeConversation, setActiveConversation] = useState<Conversation>();
  const [conversationError, setConversationError] = useState('');
  const [creatingConversation, setCreatingConversation] = useState<PendingRequest>();

  const [proposalOpen, setProposalOpen] = useState(false);
  const [proposalRequest, setProposalRequest] = useState('');
  const [proposal, setProposal] = useState<Proposal>();
  const [proposalError, setProposalError] = useState('');
  const [proposalBusy, setProposalBusy] = useState<PendingRequest>();

  const [taskContent, setTaskContent] = useState('');
  const [runError, setRunError] = useState('');
  const [submittingRun, setSubmittingRun] = useState<PendingRequest>();
  const [activeRunId, setActiveRunId] = useState<string>();
  // The conversation's DURABLE run history (GET /conversations/{id}/runs).
  // Session storage only remembers the run this browser started; the history
  // is what survives a browser restart and lets any earlier result be
  // reopened. Scoped to the conversation, like the run itself.
  const [runHistory, setRunHistory] = useState<RunSummary[]>();
  const [runHistoryLoading, setRunHistoryLoading] = useState(false);
  const [runHistoryError, setRunHistoryError] = useState('');
  // One key per LOGICAL submission: this session, this conversation, this
  // content. A key held for a different one is not a retry of this one.
  const idempotencyKey = useRef<{ key: string; owner: WorkspaceScope; content: string }>();

  const [cancelReason, setCancelReason] = useState('');
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  const [cancelError, setCancelError] = useState('');

  const [tab, setTab] = useState<InspectorTab>('Agents');

  // The Mapping Plan (backend/catalog/scope/). Whether it applies and which
  // marques exist are PROJECT facts; the plan itself belongs to the
  // CONVERSATION. Every piece is server state read back, except the draft,
  // which is only the form a person is editing.
  const [planCapabilities, setPlanCapabilities] = useState<WorkScopeCapabilities>();
  const [planDirectory, setPlanDirectory] = useState<WorkScopeDirectory>();
  const [planState, setPlanState] = useState<WorkScopeState | null>();
  const [planOpen, setPlanOpen] = useState(false);
  const [planLoading, setPlanLoading] = useState(false);
  const [planBusy, setPlanBusy] = useState<PendingRequest>();
  const [planError, setPlanError] = useState('');
  const [planNotes, setPlanNotes] = useState<WorkScopeNote[]>([]);
  const [planInstruction, setPlanInstruction] = useState('');
  const [planDraft, setPlanDraft] = useState<WorkScopeDraft>();
  // The plan's batches (scoped catalog PR3): the server's progress read and
  // the one start being confirmed. The browser counts nothing and decides
  // nothing -- which batch is next, and whether it may start, is the
  // server's answer, checked again by the database at the start itself.
  const [planProgress, setPlanProgress] = useState<WorkScopeProgress>();
  const [planProgressLoading, setPlanProgressLoading] = useState(false);
  const [planProgressError, setPlanProgressError] = useState('');
  const [batchBusy, setBatchBusy] = useState<PendingRequest>();
  const [confirmingBatch, setConfirmingBatch] = useState(false);
  // One key per confirmed start of ONE batch. A retry of the same start (a
  // lost answer, a failed launch) reuses it, so the server answers with the
  // run it already created instead of refusing or duplicating it.
  const batchKey = useRef<{ key: string; owner: WorkspaceScope; batchId: string }>();

  // CODE-3 — durable catalog review state. Deliberately separate from every
  // run-scoped piece of state above: this answers "what does the catalog hold
  // now?", which outlives any run and belongs to the PROJECT selection.
  const [catalogOpen, setCatalogOpen] = useState(false);
  const [catalogView, setCatalogView] = useState<CatalogReviewView>('canonical');
  const [catalogOffset, setCatalogOffset] = useState(0);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState('');
  const [canonicalPage, setCanonicalPage] = useState<CanonicalCatalogPage>();
  const [reviewPage, setReviewPage] = useState<CatalogReviewPageData>();
  /**
   * The catalog request whose answer is still allowed to become visible state.
   *
   * A ref, not state: it is compared by every settle path and must describe
   * what is CURRENT at that moment, not what React last rendered — the same
   * reason `scope` below is a ref. `view` and `offset` travel with it so a
   * reader can see which page a token belongs to; the monotonic
   * `pending.id` is what decides.
   */
  const catalogRequest = useRef<CatalogRequest>();

  // The live ownership scope. Updated synchronously by the handlers below, so
  // it always describes what is selected now rather than what React last
  // rendered; a response that resolves after a switch compares against this.
  const scope = useRef<WorkspaceScope>(INITIAL_WORKSPACE_SCOPE);
  // The authenticated identity the page last held. `NO_SESSION_YET` is the
  // "never observed" state and is deliberately distinct from `null`
  // (signed out), so the first session a page load sees is not mistaken for a
  // user swap.
  const lastSessionIdentity = useRef<string | null | typeof NO_SESSION_YET>(NO_SESSION_YET);

  /** Change the active run and keep the scope's run level in step. */
  const changeActiveRun = useCallback((runId?: string) => {
    scope.current = withRun(scope.current, runId);
    setActiveRunId(runId);
  }, []);

  /**
   * The polling hook refused the stored run: it is not that run, or not this
   * conversation's run. Clear the stored id so a refresh cannot re-open it,
   * drop it from the page, and say so in one authored sentence.
   */
  const onRunRejected = useCallback((rejectedRunId: string) => {
    const conversationId = scope.current.conversationId;
    if (scope.current.runId !== rejectedRunId) return; // already moved on
    if (conversationId) storeRunId(conversationId, undefined);
    changeActiveRun(undefined);
    setRunError(STORED_RUN_REJECTED);
  }, [changeActiveRun]);

  // The project may choose the loading/new-run surface only. Once the run row
  // exists, its immutable identity alone selects V1 vs V2 presentation. The
  // conversation id
  // is handed down as the run's expected owner, so a run row that belongs to
  // another conversation is refused before anything is rendered.
  const { state, mode, swarm, identityUnavailable } = useRunRealtime(
    executionUi ? activeRunId : undefined,
    selectedProject?.workflow_key,
    activeConversation?.id,
    onRunRejected,
  );
  const agents = Object.values(state.agents);
  const runStatus = state.run?.status;
  // The unified live view and the canonical verdict, both derived (never
  // stored) from the run row and the reduced event projections, so they reset
  // with the workspace state on every run switch.
  const live = useMemo(
    () => buildLiveRunViewModel({ runId: activeRunId, state, swarm }),
    [activeRunId, state, swarm],
  );
  const productOutcome = useMemo(
    () => parseProductOutcome(state.run?.product_outcome),
    [state.run?.product_outcome],
  );
  const launchState = state.run?.launch_state;
  const launchReconciliationRequired = state.run?.launch_reconciliation_required;
  const runIsTerminal = isTerminalRunStatus(runStatus);
  // Exactly one run surface renders at a time. A Swarm V2 project with a live
  // run gets the dedicated card; every other case — Swarm V1, no run yet, the
  // execution UI switched off — keeps the existing CurrentRunPanel path.
  const swarmCardRunId =
    swarm.isSwarmV2 && executionUi && activeConversation !== undefined ? activeRunId : undefined;
  // The Final Result surface is the product answer and is shown for Swarm V2
  // only. It is gated exactly like the run card above — same trusted
  // workflow_key, same execution flag, same conversation requirement — and
  // stays mounted for the whole run, so its loading, not-finished, absent and
  // invalid states are visible rather than appearing from nowhere.
  const showFinalResult =
    !identityUnavailable && swarm.isSwarmV2 && executionUi && activeConversation !== undefined && activeRunId !== undefined;
  // The V1 typed result surface, selected by the SAME rule with the other
  // engine: identity trustworthy, identity says vehicle_catalog_v1. Nothing
  // in the payload can route a run here, and an untrustworthy identity gets
  // the bounded alert below instead of either surface.
  const showVehicleResult =
    !identityUnavailable && live.engine === 'vehicle_catalog_v1' && executionUi && activeConversation !== undefined && activeRunId !== undefined;
  const showLiveRun = executionUi && activeConversation !== undefined && activeRunId !== undefined;


  useEffect(() => {
    let mounted = true;
    getCurrentSession().then(current => { if (mounted) setSession(current); }).catch(() => { if (mounted) setSession(null); });
    const unsubscribe = onAuthStateChange(next => { if (mounted) setSession(next); });
    return () => { mounted = false; unsubscribe(); };
  }, []);

  const loadProjects = useCallback((owner: WorkspaceScope) => {
    setProjects(undefined);
    setProjectsError('');
    api.projects()
      .then(list => {
        if (!ownsSession(owner, scope.current)) return; // another session owns the page now
        setProjects(list);
      })
      .catch(error => {
        if (!ownsSession(owner, scope.current)) return;
        setProjects([]);
        setProjectsError(safeErrorText(error, 'Failed to load projects.'));
      });
  }, []);

  useEffect(() => {
    // `undefined` is "still restoring", not "signed out": nothing is rendered
    // and nothing is in flight, so there is nothing to invalidate or clear —
    // and clearing here would destroy the stored run a refresh exists to
    // reopen.
    if (session === undefined) return;

    // Identity, not object identity. Supabase hands back a NEW session object
    // on every token refresh, and treating that as a session replacement would
    // discard requests in flight for a user who never changed.
    const identity = session ? session.user?.id ?? '' : null;
    const identityKnown = lastSessionIdentity.current !== NO_SESSION_YET;
    const identityChanged = identityKnown && lastSessionIdentity.current !== identity;
    lastSessionIdentity.current = identity;

    if (!session || identityChanged) {
      // Sign-out, expiry or a different user: every request in flight under
      // the previous session is invalidated before any state is touched, the
      // rendered workspace is emptied, and the browser's own record of which
      // run each conversation was on goes with it.
      scope.current = nextSessionScope(scope.current, session?.user?.id);
      clearStoredRunIds();
      setProjects(undefined);
      setProjectsError('');
      setSelectedProject(undefined);
      setConversations(undefined);
      setActiveConversation(undefined);
      setActiveRunId(undefined);
      setRunHistory(undefined);
      setRunHistoryError('');
      setRunHistoryLoading(false);
      setProposal(undefined);
      setProposalError('');
      setConversationError('');
      setRunError('');
      setCancelError('');
      setTaskContent('');
      setConfirmingCancel(false);
      setCancelReason('');
      // Durable catalog rows the previous identity was authorized to see. They
      // are membership-authorized server-side, so a new or absent identity must
      // not inherit a rendered page from the old one.
      // Same rule as `endCatalogIntent`, written inline: this handler is
      // declared above that callback, so calling it here would read a
      // `const` before its initializer.
      catalogRequest.current = undefined;
      // The Mapping Plan, likewise: every plan, directory and draft the
      // previous identity read or typed goes, inline for the same reason.
      setPlanCapabilities(undefined);
      setPlanDirectory(undefined);
      setPlanState(undefined);
      setPlanDraft(undefined);
      setPlanOpen(false);
      setPlanNotes([]);
      setPlanError('');
      setPlanInstruction('');
      setPlanLoading(false);
      setPlanBusy(undefined);
      setCatalogOpen(false);
      setCatalogView('canonical');
      setCatalogOffset(0);
      setCatalogError('');
      setCatalogLoading(false);
      setCanonicalPage(undefined);
      setReviewPage(undefined);
      // The replacement owner inherits no busy state. An old request settling
      // afterwards cannot clear the new owner's, because `settlePending`
      // compares request identity, not truthiness.
      setCreatingConversation(undefined);
      setProposalBusy(undefined);
      setSubmittingRun(undefined);
      idempotencyKey.current = undefined;
      if (!session) return;
    }
    loadProjects(scope.current);
  }, [session, loadProjects]);

  /**
   * Is this the catalog request whose answer may still become visible state?
   *
   * TWO facts, and neither implies the other:
   *
   *  1. it is the NEWEST catalog request. `PendingRequest.id` is monotonic for
   *     the life of the page, so a superseded request can always be recognised
   *     as superseded — which a project-scope check cannot do, because two
   *     requests inside one project share a scope. Without this an older page
   *     could overwrite a newer one, an older `.finally()` could clear a
   *     loading state its replacement had just set, and an older failure could
   *     replace a newer success with an error;
   *  2. the workspace scope it was issued under still owns the surface. Every
   *     intent boundary now drops the token too (see `endCatalogIntent`), so
   *     this is DEFENSE IN DEPTH rather than the sole catcher of any currently
   *     reachable case — stated plainly because an earlier version of this
   *     comment claimed otherwise. It is kept because it is a second,
   *     independent rule: a future path that changed the selected project
   *     without going through `selectProject` would still be caught here.
   */
  const ownsCatalogRequest = useCallback((issued: CatalogRequest) => (
    catalogRequest.current?.pending.id === issued.pending.id
    && ownsProject(issued.pending.owner, scope.current)
  ), []);

  /**
   * End the current catalog intent, synchronously.
   *
   * The correctness boundary begins at the USER'S INTENT, not at the effect
   * that follows it. `setCatalogOffset` only schedules a render, so between the
   * click and the effect that issues the replacement there is a window in which
   * `catalogRequest.current` still holds the OLD token — and an answer settling
   * in that window passes `ownsCatalogRequest` and writes a page, an error or a
   * loading clear that belongs to a page the user has already left.
   *
   * So every handler that changes what the surface is asking for calls this
   * FIRST. Dropping the token makes the old request a no-op immediately, with
   * no replacement needed: `undefined?.pending.id` matches nothing.
   *
   * This does not issue a request. The effect below remains the only thing that
   * creates one, so there is exactly one request owner.
   */
  const endCatalogIntent = useCallback(() => {
    catalogRequest.current = undefined;
  }, []);

  /** A new page is a new intent: the previous page's answer no longer applies. */
  const changeCatalogOffset = useCallback((next: number) => {
    endCatalogIntent();
    setCatalogOffset(next);
  }, [endCatalogIntent]);

  /**
   * Opening or closing the panel is a new intent too.
   *
   * CLOSING drops the rendered pages as well as the token. That is deliberate,
   * and it is what makes the panel's own contract true: it documents that
   * opening re-reads, and a page retained from the previous visit would render
   * instantly on reopen and look like current state. Keeping it would only be
   * honest as stale-while-revalidate, visibly marked — a larger behaviour than
   * this surface needs.
   *
   * `view` and `offset` are KEPT, so reopening re-reads the same page the
   * operator was on rather than silently jumping back to the first.
   */
  const changeCatalogOpen = useCallback((open: boolean) => {
    endCatalogIntent();
    if (!open) {
      setCatalogError('');
      setCatalogLoading(false);
      setCanonicalPage(undefined);
      setReviewPage(undefined);
    }
    setCatalogOpen(open);
  }, [endCatalogIntent]);

  /**
   * Read one bounded catalog page for the selected project.
   *
   * The request identity is recorded SYNCHRONOUSLY, before the call is issued
   * and before React re-renders — exactly as `scope.current` is — so the newer
   * request has already superseded the older one by the time either can
   * settle. A superseded request is a complete no-op on every path: no page,
   * no error, no loading clear.
   *
   * There is deliberately no `AbortController` here. Aborting would be an
   * optimization at best: cancellation races too, and a request that is
   * already past the wire still settles. Identity is the authority.
   *
   * The response is parsed, never trusted: `parseCanonicalPage` /
   * `parseReviewPage` build UI state field by field, so nothing the server sent
   * outside the contract can reach the screen.
   */
  const loadCatalog = useCallback((
    project: Project,
    view: CatalogReviewView,
    offset: number,
    owner: WorkspaceScope,
  ) => {
    const requested = { limit: CATALOG_PAGE_SIZE, offset };
    const issued: CatalogRequest = { pending: beginPending(owner), view, offset };
    catalogRequest.current = issued;
    setCatalogLoading(true);
    setCatalogError('');
    const inFlight = view === 'canonical'
      ? api.catalogCanonical(project.id, requested)
      : api.catalogReviewCandidates(project.id, requested);
    inFlight
      .then(body => {
        if (!ownsCatalogRequest(issued)) return; // superseded, or another project
        if (view === 'canonical') setCanonicalPage(parseCanonicalPage(body, requested));
        else setReviewPage(parseReviewPage(body, requested));
      })
      .catch(error => {
        if (!ownsCatalogRequest(issued)) return;
        setCatalogError(safeErrorText(error, 'Failed to load the catalog page.'));
      })
      .finally(() => {
        if (!ownsCatalogRequest(issued)) return;
        setCatalogLoading(false);
      });
  }, [ownsCatalogRequest]);

  // The catalog is read when the panel is open and a project is selected, and
  // again whenever the view or the page changes. Closing the panel issues no
  // request; opening it re-reads, so what is shown is never a stale page from
  // an earlier visit.
  useEffect(() => {
    if (!catalogOpen || !selectedProject) return;
    loadCatalog(selectedProject, catalogView, catalogOffset, scope.current);
  }, [catalogOpen, selectedProject, catalogView, catalogOffset, loadCatalog]);

  /**
   * Switching views starts at the first page and drops the other view's rows.
   *
   * The request token is invalidated HERE rather than left to the effect that
   * follows: between this handler and that effect there is a window in which an
   * answer for the view the user just left could still settle, and the honest
   * reading of a view switch is that the previous view's read no longer matters.
   */
  const changeCatalogView = useCallback((next: CatalogReviewView) => {
    endCatalogIntent();
    setCatalogView(next);
    setCatalogOffset(0);
    setCatalogError('');
    setCanonicalPage(undefined);
    setReviewPage(undefined);
  }, [endCatalogIntent]);

  /** Every piece of rendered catalog state, and the request that would fill it. */
  const clearCatalogState = useCallback(() => {
    endCatalogIntent();
    setCatalogOffset(0);
    setCatalogError('');
    setCatalogLoading(false);
    setCanonicalPage(undefined);
    setReviewPage(undefined);
  }, [endCatalogIntent]);

  /**
   * Read the conversation's durable run history.
   *
   * Applied only while the conversation it was issued under is still selected
   * (`ownsConversation`), exactly like every other conversation-scoped read.
   * When `openLatest` is set and the workspace holds no run for the
   * conversation, the newest run is opened — that is how a completed result
   * is reachable again after a browser restart emptied session storage. The
   * polling hook then verifies the run against this conversation before
   * anything is rendered, as it does for a stored id.
   */
  const loadRunHistory = useCallback((conversationId: string, owner: WorkspaceScope, openLatest: boolean) => {
    if (!executionUi) return;
    setRunHistoryLoading(true);
    setRunHistoryError('');
    // Issued inside a resolved promise so a synchronous failure in the client
    // is a rejection handled below, never an exception thrown from a handler
    // that would take the rest of the workspace down with it.
    Promise.resolve()
      .then(() => api.runs(conversationId))
      .then(list => {
        if (!ownsConversation(owner, scope.current)) return;
        const rows = Array.isArray(list) ? list.filter(row => row && typeof row.id === 'string' && row.conversation_id === conversationId) : [];
        setRunHistory(rows);
        if (openLatest && scope.current.runId === undefined && rows.length > 0) {
          storeRunId(conversationId, rows[0].id);
          changeActiveRun(rows[0].id);
        }
      })
      .catch(error => {
        if (!ownsConversation(owner, scope.current)) return;
        setRunHistoryError(safeErrorText(error, 'Failed to load the run history.'));
      })
      .finally(() => {
        if (!ownsConversation(owner, scope.current)) return;
        setRunHistoryLoading(false);
      });
  }, [executionUi, changeActiveRun]);

  // Once the ACTIVE run is terminal its canonical outcome is durable; the
  // history row for it is re-read once so the list shows the verdict the
  // finalizer recorded rather than the status it had when the list loaded.
  // Keyed on the run whose row is loaded: a run switch renders once with the
  // previous terminal row still in state, and that must not count.
  const terminalRunId = runIsTerminal && state.run?.id === activeRunId ? activeRunId : undefined;
  const historyRefreshedFor = useRef<string>();
  useEffect(() => {
    if (!terminalRunId || !activeConversation) return;
    if (historyRefreshedFor.current === terminalRunId) return;
    historyRefreshedFor.current = terminalRunId;
    loadRunHistory(activeConversation.id, scope.current, false);
  }, [terminalRunId, activeConversation, loadRunHistory]);

  /**
   * Drop every piece of Mapping Plan state. A plan read under one conversation
   * (or one project, or one identity) may never stay on screen under another,
   * so each of those switches clears it synchronously, exactly like the
   * catalog. `project` also drops the project-scoped capability and directory.
   */
  const clearPlanState = useCallback((project: boolean) => {
    if (project) {
      setPlanCapabilities(undefined);
      setPlanDirectory(undefined);
      setPlanOpen(false);
    }
    setPlanState(undefined);
    setPlanDraft(undefined);
    setPlanNotes([]);
    setPlanError('');
    setPlanInstruction('');
    setPlanLoading(false);
    setPlanBusy(undefined);
    setPlanProgress(undefined);
    setPlanProgressError('');
    setPlanProgressLoading(false);
    setBatchBusy(undefined);
    setConfirmingBatch(false);
  }, []);

  /** Show a plan the server returned: its state, and a draft reset to it. */
  const showPlan = useCallback((state: WorkScopeState | null) => {
    setPlanState(state);
    setPlanDraft(state ? draftFromPlan(state.plan) : undefined);
  }, []);

  /**
   * Whether the Mapping Plan applies to this project, from the SERVER's
   * capability read. Only a Swarm V2 project is asked -- no other engine reads
   * a plan -- and any failure, including a client that has no such method,
   * leaves the surface hidden rather than half-shown.
   */
  const loadPlanCapabilities = useCallback((project: Project, owner: WorkspaceScope) => {
    if (!executionUi || project.workflow_key !== 'swarm_v2') return;
    Promise.resolve()
      .then(() => api.workScopeCapabilities(project.id))
      .then(body => {
        if (!ownsProject(owner, scope.current)) return;
        setPlanCapabilities(parseCapabilities(body));
      })
      .catch(() => {
        if (!ownsProject(owner, scope.current)) return;
        setPlanCapabilities(undefined);
      });
  }, [executionUi]);

  /**
   * The conversation's open plan, applied only while it is still selected.
   * `keepError` is for the read-back after a refused write: the refusal's
   * explanation stays on screen while the real plan is fetched.
   */
  const loadPlan = useCallback((conversationId: string, owner: WorkspaceScope, keepError = false) => {
    setPlanLoading(true);
    if (!keepError) setPlanError('');
    Promise.resolve()
      .then(() => api.openWorkScope(conversationId))
      .then(body => {
        if (!ownsConversation(owner, scope.current)) return;
        const parsed = parseOpenWorkScope(body);
        if (parsed === undefined) {
          setPlanError('The mapping plan could not be read.');
          return;
        }
        showPlan(parsed);
      })
      .catch(error => {
        if (!ownsConversation(owner, scope.current)) return;
        setPlanError(safeErrorText(error, 'The mapping plan could not be read.'));
      })
      .finally(() => {
        if (!ownsConversation(owner, scope.current)) return;
        setPlanLoading(false);
      });
  }, [showPlan]);

  /** The project's manufacturer directory, read when the panel is opened. */
  const loadPlanDirectory = useCallback((projectId: string, owner: WorkspaceScope) => {
    Promise.resolve()
      .then(() => api.workScopeDirectory(projectId))
      .then(body => {
        if (!ownsProject(owner, scope.current)) return;
        setPlanDirectory(parseDirectory(body));
      })
      .catch(() => {
        if (!ownsProject(owner, scope.current)) return;
        setPlanDirectory(undefined);
      });
  }, []);

  const planAvailable = executionUi && planCapabilities?.available === true;
  const batchesAvailable = planAvailable && planCapabilities?.canStartBatches === true;

  /**
   * Where a typed task may go, from the SERVER's capability read. Only a Swarm
   * V2 project can be a catalog project; every other project keeps its
   * ordinary composer. A Swarm V2 project whose capabilities have not answered
   * is `unconfirmed`, never `direct`: with the Government read on, an ordinary
   * run there would be refused, and the composer must not pretend otherwise.
   */
  const composerRoute: ComposerRoute = selectedProject?.workflow_key !== 'swarm_v2'
    ? { kind: 'direct' }
    : planCapabilities === undefined
      ? { kind: 'unconfirmed' }
      : planCapabilities.directRuns.allowed
        ? { kind: 'direct' }
        : { kind: 'blocked', blockedBy: planCapabilities.directRuns.blockedBy ?? 'unknown', planAvailable };

  /**
   * The plan's progress, applied only while its conversation is still the
   * selected one. `keepError` keeps a refusal's explanation on screen while
   * the real progress is read back after it.
   */
  const loadPlanProgress = useCallback((workScopeId: string, owner: WorkspaceScope, keepError = false) => {
    setPlanProgressLoading(true);
    if (!keepError) setPlanProgressError('');
    Promise.resolve()
      .then(() => api.workScopeProgress(workScopeId))
      .then(body => {
        if (!ownsConversation(owner, scope.current)) return;
        const parsed = parseProgress(body);
        if (parsed === undefined) {
          setPlanProgressError('The plan’s progress could not be read.');
          return;
        }
        setPlanProgress(parsed);
      })
      .catch(error => {
        if (!ownsConversation(owner, scope.current)) return;
        setPlanProgressError(safeErrorText(error, 'The plan’s progress could not be read.'));
      })
      .finally(() => {
        if (!ownsConversation(owner, scope.current)) return;
        setPlanProgressLoading(false);
      });
  }, []);

  // The progress is read when the panel is open on a plan this server lets a
  // member start batches of, and again whenever the plan's head changes.
  const planId = planState?.id;
  const planRevision = planState?.revision;
  useEffect(() => {
    if (!batchesAvailable || !planOpen || !planId) return;
    loadPlanProgress(planId, scope.current);
  }, [batchesAvailable, planOpen, planId, planRevision, loadPlanProgress]);

  // While a batch is running, its progress is polled -- the same bounded,
  // membership-scoped read, every few seconds, and only while the panel is
  // open. Polling reads; it never starts, retries or continues anything.
  const liveBatchRunId = planProgress?.live?.runId;
  useEffect(() => {
    if (!batchesAvailable || !planOpen || !planId || !liveBatchRunId) return;
    const timer = setInterval(() => loadPlanProgress(planId, scope.current, true), 4000);
    return () => clearInterval(timer);
  }, [batchesAvailable, planOpen, planId, liveBatchRunId, loadPlanProgress]);

  // The plan is read once the capability read says it applies and a
  // conversation is selected -- in either order, since both arrive async.
  useEffect(() => {
    if (!planAvailable || !activeConversation) return;
    loadPlan(activeConversation.id, scope.current);
  }, [planAvailable, activeConversation, loadPlan]);

  // The directory is read when the panel is first opened for this project.
  useEffect(() => {
    if (!planAvailable || !planOpen || !selectedProject || planDirectory) return;
    loadPlanDirectory(selectedProject.id, scope.current);
  }, [planAvailable, planOpen, selectedProject, planDirectory, loadPlanDirectory]);

  const loadConversations = useCallback((project: Project, owner: WorkspaceScope) => {
    setConversations(undefined);
    setConversationError('');
    api.conversations(project.id)
      .then(list => {
        if (!ownsProject(owner, scope.current)) return; // another project is selected now
        setConversations(list);
      })
      .catch(error => {
        if (!ownsProject(owner, scope.current)) return;
        setConversations([]);
        setConversationError(safeErrorText(error, 'Failed to load conversations.'));
      });
  }, []);

  async function login() {
    setAuthError('');
    try {
      setSession(await signInWithSupabase(email, password));
    } catch (error) {
      setSession(null);
      setAuthError(safeErrorText(error, 'Authentication failed.'));
    }
  }

  async function logout() {
    setAuthError('');
    // Invalidate BEFORE awaiting the provider: from this line on, every
    // response already on the wire belongs to a session that no longer owns
    // this page, and the stored run ids go with it.
    scope.current = nextSessionScope(scope.current, undefined);
    clearStoredRunIds();
    await signOutFromSupabase();
    setSession(null);
  }

  function selectProject(project: Project) {
    const owner = withProject(scope.current, project.id);
    scope.current = owner;
    setSelectedProject(project);
    setActiveConversation(undefined);
    setActiveRunId(undefined);
    setRunHistory(undefined);
    setRunHistoryError('');
    setProposal(undefined);
    setProposalError('');
    setConversationError('');
    setRunError('');
    setCancelError('');
    setSidebarOpen(false);
    // The catalog read is authorized against the project, so one project's page
    // may never stay on screen under another. It is dropped here rather than
    // left to be overwritten, so there is no moment where the previous
    // project's rows are shown beside the new project's name.
    clearCatalogState();
    clearPlanState(true);
    loadConversations(project, owner);
    loadPlanCapabilities(project, owner);
  }

  function selectConversation(conversation: Conversation) {
    scope.current = withConversation(scope.current, conversation.id);
    // The previous conversation's plan is dropped before this one is read.
    clearPlanState(false);
    setActiveConversation(conversation);
    setRunError('');
    setCancelError('');
    setSidebarOpen(false);
    // Reopen an existing run after refresh or navigation. The stored id is a
    // request, not a fact: the polling hook verifies the run it names against
    // this conversation before anything is rendered.
    const stored = readStoredRunId(conversation.id);
    changeActiveRun(stored);
    setRunHistory(undefined);
    setRunHistoryError('');
    // The durable history is read regardless; when nothing is stored (a new
    // browser, a restart) the newest run is reopened from it.
    loadRunHistory(conversation.id, scope.current, stored === undefined);
  }

  /** Open a run chosen from the durable history of the active conversation. */
  function selectHistoricalRun(runId: string) {
    const conversationId = scope.current.conversationId;
    if (!conversationId) return;
    storeRunId(conversationId, runId);
    setRunError('');
    setCancelError('');
    setConfirmingCancel(false);
    changeActiveRun(runId);
  }

  async function createConversation() {
    if (!selectedProject || creatingConversation) return;
    const owner = scope.current;
    const pending = beginPending(owner);
    setConversationError('');
    setCreatingConversation(pending);
    try {
      const conversation = await api.createConversation(selectedProject.id, conversationTitle.trim() || undefined);
      // The conversation was created and is safe on the server. It simply does
      // not belong in a project the user has since moved away from.
      if (!ownsProject(owner, scope.current)) return;
      setConversations(previous => [conversation, ...(previous ?? [])]);
      selectConversation(conversation);
      setConversationTitle('');
    } catch (error) {
      if (!ownsProject(owner, scope.current)) return;
      setConversationError(safeErrorText(error, 'Failed to create the conversation.'));
    } finally {
      // Only THIS request may clear the busy state it set.
      setCreatingConversation(current => settlePending(current, pending));
    }
  }

  /**
   * A proposal belongs to the project it was raised for. Every proposal
   * response — creation, revision and decision alike — is therefore applied
   * only while that project is still selected.
   */
  async function runProposalRequest(request: Promise<Proposal>, fallback: string) {
    const owner = scope.current;
    const pending = beginPending(owner);
    setProposalBusy(pending);
    setProposalError('');
    try {
      const next = await request;
      if (!ownsProject(owner, scope.current)) return;
      setProposal(next);
    } catch (error) {
      if (!ownsProject(owner, scope.current)) return;
      setProposalError(safeErrorText(error, fallback));
    } finally {
      setProposalBusy(current => settlePending(current, pending));
    }
  }

  async function generateProposal() {
    if (!selectedProject || proposalBusy || !proposalRequest.trim()) return;
    await runProposalRequest(
      api.createProposal(selectedProject.id, proposalRequest.trim()),
      'Proposal creation failed.',
    );
  }

  async function decideProposal(decision: 'approve' | 'reject') {
    if (!proposal || proposalBusy) return;
    await runProposalRequest(api.decideProposal(proposal.id, decision), `Proposal ${decision} failed.`);
  }

  async function reviseProposal() {
    if (!proposal || proposalBusy || !proposalRequest.trim()) return;
    await runProposalRequest(
      api.reviseProposal(proposal.id, proposalRequest.trim()),
      'Proposal revision failed.',
    );
  }

  async function startRun() {
    if (!activeConversation || submittingRun || !taskContent.trim()) return;
    // The composer offers no submission unless the route is direct; this
    // holds the same line for any other caller.
    if (composerRoute.kind !== 'direct') return;
    const owner = scope.current;
    const conversationId = activeConversation.id;
    const content = taskContent.trim();
    const pending = beginPending(owner);
    setSubmittingRun(pending);
    setRunError('');
    // One key per logical submission, and a submission is (this session, this
    // conversation, this content). A retry after a failure reuses the key, so
    // the backend returns the original run instead of creating a duplicate; a
    // key held for a DIFFERENT submission is dropped rather than reused,
    // because replaying it would either cross a session boundary or collide
    // with the backend's own idempotency conflict.
    const held = idempotencyKey.current;
    if (held && (!ownsConversation(held.owner, owner) || held.content !== content)) {
      idempotencyKey.current = undefined;
    }
    idempotencyKey.current ??= { key: newIdempotencyKey(), owner, content };
    const submission = idempotencyKey.current;
    try {
      const created = await api.startRun(conversationId, content, submission.key);
      // OWNERSHIP BEFORE ANY DURABLE BROWSER-SIDE WRITE. Session storage
      // survives this component, so a response belonging to a session that no
      // longer owns the page must store nothing and activate nothing — sign-out
      // already cleared these keys and this answer may not put one back.
      if (!ownsSession(owner, scope.current)) return;
      // Only the session that issued the key may retire it.
      if (idempotencyKey.current === submission) idempotencyKey.current = undefined;
      // Same session: the run exists and belongs to the conversation it was
      // created for, so it is stored under THAT key whichever conversation is
      // selected now.
      storeRunId(conversationId, created.run_id);
      // …but it only becomes the ACTIVE run if that conversation is still the
      // one on screen.
      if (!ownsConversation(owner, scope.current)) return;
      changeActiveRun(created.run_id);
      setTaskContent('');
      loadRunHistory(conversationId, owner, false);
    } catch (error) {
      if (!ownsConversation(owner, scope.current)) return;
      setRunError(safeErrorText(error, 'Run creation failed.'));
    } finally {
      setSubmittingRun(current => settlePending(current, pending));
    }
  }

  /**
   * Send ONE Mapping Plan write: the person's words, or the edited draft.
   *
   * Both go to the same server contract. A revision names the exact head it
   * was made against; if the plan changed meanwhile the server refuses it
   * (`WORK_SCOPE_STALE`) and the current plan is read back, so the person
   * redoes the change against what is really there instead of overwriting it.
   */
  async function writePlan(input: WorkScopeInput) {
    if (!activeConversation || planBusy || !planAvailable) return;
    const owner = scope.current;
    const conversationId = activeConversation.id;
    const pending = beginPending(owner);
    setPlanBusy(pending);
    setPlanError('');
    try {
      const body = planState
        ? await api.reviseWorkScope(planState.id, { revision: planState.revision, digest: planState.digest }, input)
        : await api.createWorkScope(conversationId, input);
      if (!ownsConversation(owner, scope.current)) return;
      const result = parseWorkScopeMutation(body);
      if (!result) {
        setPlanError('The mapping plan answer could not be read.');
        loadPlan(conversationId, owner, true);
        return;
      }
      showPlan(result.state);
      setPlanNotes(result.notes);
      if ('instruction' in input) setPlanInstruction('');
    } catch (error) {
      if (!ownsConversation(owner, scope.current)) return;
      setPlanError(safeErrorText(error, 'The mapping plan could not be updated.'));
      if (error instanceof ApiError && (error.code === 'WORK_SCOPE_STALE' || error.code === 'WORK_SCOPE_OPEN_EXISTS')) {
        loadPlan(conversationId, owner, true);
      }
    } finally {
      setPlanBusy(current => settlePending(current, pending));
    }
  }

  function draftEditOrNothing(): WorkScopeEdit | undefined {
    if (!planCapabilities || !planDraft) return undefined;
    const checked = draftEdit(planDraft, planCapabilities.limits);
    return 'edit' in checked ? checked.edit : undefined;
  }

  async function submitPlanInstruction() {
    const instruction = planInstruction.trim();
    if (instruction === '') return;
    await writePlan({ instruction });
  }

  async function savePlanDraft() {
    const edit = draftEditOrNothing();
    if (edit === undefined) return;
    await writePlan({ edit });
  }

  /**
   * Start the ONE batch the server's progress says may start, after the
   * person confirmed it. The request names the head the progress was read
   * against and the batch it means; the server decides whether that is still
   * the next batch, and the database checks it again under the plan's row lock.
   * Whatever the answer, the progress is read back so the screen shows what is
   * really there now.
   */
  async function confirmBatchStart() {
    const progress = planProgress;
    const batch = progress?.controls.start.batch;
    if (!activeConversation || !progress || !batch || batchBusy || !progress.controls.start.available) return;
    const owner = scope.current;
    const conversationId = activeConversation.id;
    const pending = beginPending(owner);
    setBatchBusy(pending);
    setPlanProgressError('');
    const held = batchKey.current;
    if (held && (!ownsConversation(held.owner, owner) || held.batchId !== batch.batchId)) {
      batchKey.current = undefined;
    }
    batchKey.current ??= { key: newIdempotencyKey(), owner, batchId: batch.batchId };
    const submission = batchKey.current;
    try {
      const body = await api.startWorkScopeBatch(
        progress.workScopeId, { revision: progress.revision, digest: progress.digest },
        batch.batchId, submission.key);
      if (!ownsSession(owner, scope.current)) return;
      if (batchKey.current === submission) batchKey.current = undefined;
      const started = parseBatchStart(body);
      if (started === undefined) {
        if (!ownsConversation(owner, scope.current)) return;
        setPlanProgressError('The batch answer could not be read. The progress has been reloaded.');
        loadPlanProgress(progress.workScopeId, owner, true);
        return;
      }
      // The run exists and belongs to the plan's conversation: stored under
      // that conversation whichever one is selected now...
      storeRunId(conversationId, started.runId);
      // ...and shown only if it is still the one on screen.
      if (!ownsConversation(owner, scope.current)) return;
      setConfirmingBatch(false);
      changeActiveRun(started.runId);
      loadRunHistory(conversationId, owner, false);
      loadPlanProgress(progress.workScopeId, owner);
    } catch (error) {
      if (!ownsConversation(owner, scope.current)) return;
      setPlanProgressError(safeErrorText(error, 'The batch could not be started.'));
      setConfirmingBatch(false);
      loadPlanProgress(progress.workScopeId, owner, true);
    } finally {
      setBatchBusy(current => settlePending(current, pending));
    }
  }

  /** Pause or resume the plan: the next batch waits while it is paused. */
  async function setPlanPaused(paused: boolean) {
    const progress = planProgress;
    if (!progress || batchBusy) return;
    const owner = scope.current;
    const pending = beginPending(owner);
    setBatchBusy(pending);
    setPlanProgressError('');
    try {
      const body = paused
        ? await api.pauseWorkScope(progress.workScopeId)
        : await api.resumeWorkScope(progress.workScopeId);
      if (!ownsConversation(owner, scope.current)) return;
      const result = parsePauseResult(body);
      if (result === undefined) {
        setPlanProgressError('The plan’s answer could not be read. The progress has been reloaded.');
        loadPlanProgress(progress.workScopeId, owner, true);
        return;
      }
      setPlanProgress(result.progress);
    } catch (error) {
      if (!ownsConversation(owner, scope.current)) return;
      setPlanProgressError(safeErrorText(error, paused ? 'The plan could not be paused.' : 'The plan could not be resumed.'));
      loadPlanProgress(progress.workScopeId, owner, true);
    } finally {
      setBatchBusy(current => settlePending(current, pending));
    }
  }

  /** Cancel the running batch through the existing run cancellation. */
  async function cancelLiveBatch() {
    const progress = planProgress;
    const runId = progress?.controls.cancel.runId;
    if (!progress || !runId || batchBusy) return;
    const owner = scope.current;
    const pending = beginPending(owner);
    setBatchBusy(pending);
    setPlanProgressError('');
    try {
      await api.cancel(runId, 'Cancelled from the mapping plan');
      if (!ownsConversation(owner, scope.current)) return;
      loadPlanProgress(progress.workScopeId, owner);
    } catch (error) {
      if (!ownsConversation(owner, scope.current)) return;
      setPlanProgressError(safeErrorText(error, 'The batch could not be cancelled.'));
      loadPlanProgress(progress.workScopeId, owner, true);
    } finally {
      setBatchBusy(current => settlePending(current, pending));
    }
  }

  async function confirmCancelRun() {
    if (!activeRunId) return;
    const owner = scope.current;
    setCancelError('');
    try {
      await api.cancel(activeRunId, cancelReason.trim() || undefined);
      if (!ownsRun(owner, scope.current)) return;
      setConfirmingCancel(false);
      setCancelReason('');
    } catch (error) {
      if (!ownsRun(owner, scope.current)) return;
      setCancelError(safeErrorText(error, 'Cancellation failed.'));
    }
  }

  if (session === undefined) return <SessionRestoreScreen/>;
  if (!session) return (
    <AuthScreen
      email={email}
      password={password}
      error={authError}
      onEmailChange={setEmail}
      onPasswordChange={setPassword}
      onSubmit={login}
    />
  );

  return (
    <WorkspaceShell
      sidebarOpen={sidebarOpen}
      inspectorOpen={inspectorOpen}
      onSidebarOpenChange={setSidebarOpen}
      onInspectorOpenChange={setInspectorOpen}
      sidebar={
        <WorkspaceSidebar
          userEmail={session.user?.email}
          executionUi={executionUi}
          hardeningNote={HARDENING_NOTE}
          projects={projects}
          projectsError={projectsError}
          selectedProjectId={selectedProject?.id}
          onSelectProject={selectProject}
          onRetryProjects={() => loadProjects(scope.current)}
          conversations={conversations}
          conversationsLoading={selectedProject !== undefined && conversations === undefined}
          conversationError={conversationError}
          activeConversationId={activeConversation?.id}
          onSelectConversation={selectConversation}
          conversationTitle={conversationTitle}
          onConversationTitleChange={setConversationTitle}
          onCreateConversation={createConversation}
          creatingConversation={creatingConversation !== undefined}
          onLogout={logout}
        />
      }
      inspector={
        <RunInspector
          executionUi={executionUi}
          tab={tab}
          onTabChange={setTab}
          agents={agents}
          state={state}
          swarm={swarm}
        />
      }
    >
      <ConversationView
        executionUi={executionUi}
        project={selectedProject}
        conversation={activeConversation}
        composer={
          <TaskComposer
            executionUi={executionUi}
            hasConversation={activeConversation !== undefined}
            content={taskContent}
            onContentChange={setTaskContent}
            onSubmit={startRun}
            submitting={submittingRun !== undefined}
            error={runError}
            route={composerRoute}
            onOpenMappingPlan={() => setPlanOpen(true)}
            onRecheck={() => { if (selectedProject) loadPlanCapabilities(selectedProject, scope.current); }}
          />
        }
      >
        <WorkflowProposalPanel
          executionUi={executionUi}
          hasProject={selectedProject !== undefined}
          hardeningNote={HARDENING_NOTE}
          open={proposalOpen}
          onOpenChange={setProposalOpen}
          request={proposalRequest}
          onRequestChange={setProposalRequest}
          proposal={proposal}
          error={proposalError}
          busy={proposalBusy !== undefined}
          onGenerate={generateProposal}
          onRevise={reviseProposal}
          onDecide={decideProposal}
        />
        <MappingPlanPanel
          visible={planAvailable && activeConversation !== undefined}
          open={planOpen}
          onOpenChange={setPlanOpen}
          capabilities={planCapabilities}
          directory={planDirectory}
          state={planState}
          loading={planLoading}
          busy={planBusy !== undefined}
          error={planError}
          notes={planNotes}
          instruction={planInstruction}
          onInstructionChange={setPlanInstruction}
          onSubmitInstruction={submitPlanInstruction}
          draft={planDraft ?? (planCapabilities ? emptyDraft(planCapabilities.limits) : { units: [], modelYearFrom: '', modelYearTo: '', maxItems: '', batchSize: 1 })}
          onDraftChange={setPlanDraft}
          onSaveDraft={savePlanDraft}
          onDiscardDraft={() => setPlanDraft(planState ? draftFromPlan(planState.plan) : undefined)}
          onRetry={() => { if (activeConversation) loadPlan(activeConversation.id, scope.current); }}
          batches={batchesAvailable ? {
            progress: planProgress,
            loading: planProgressLoading,
            busy: batchBusy !== undefined,
            error: planProgressError,
            confirming: confirmingBatch,
            onRequestStart: () => { setPlanProgressError(''); setConfirmingBatch(true); },
            onConfirmStart: confirmBatchStart,
            onCancelStart: () => setConfirmingBatch(false),
            onPause: () => setPlanPaused(true),
            onResume: () => setPlanPaused(false),
            onCancelBatch: cancelLiveBatch,
            onOpenRun: selectHistoricalRun,
            onRefresh: () => { if (planState) loadPlanProgress(planState.id, scope.current); },
          } : undefined}
        />
        {swarmCardRunId ? (
          <SwarmRunCard
            swarm={swarm}
            runId={swarmCardRunId}
            connection={mode}
            launchState={launchState}
            launchReconciliationRequired={launchReconciliationRequired}
            confirmingCancel={confirmingCancel}
            cancelReason={cancelReason}
            cancelError={cancelError}
            onCancelReasonChange={setCancelReason}
            onRequestCancel={() => setConfirmingCancel(true)}
            onConfirmCancel={confirmCancelRun}
            onKeepRunning={() => setConfirmingCancel(false)}
          />
        ) : (
          <CurrentRunPanel
            executionUi={executionUi}
            hasConversation={activeConversation !== undefined}
            runId={activeRunId}
            runStatus={runStatus}
            phase={state.currentPhase}
            connection={mode}
            isTerminal={runIsTerminal}
            isPartialSuccess={isPartialSuccessRunStatus(runStatus)}
            launchState={launchState}
            launchReconciliationRequired={launchReconciliationRequired}
            confirmingCancel={confirmingCancel}
            cancelReason={cancelReason}
            cancelError={cancelError}
            onCancelReasonChange={setCancelReason}
            onRequestCancel={() => setConfirmingCancel(true)}
            onConfirmCancel={confirmCancelRun}
            onKeepRunning={() => setConfirmingCancel(false)}
          />
        )}
        {/* Engine-specific result surfaces are selected only from the run's
            immutable identity. Missing/invalid identity gets a bounded alert,
            never an implicit V1 fallback. */}
        <LiveRunPanel visible={showLiveRun} live={live} connection={mode} />
        <FinalResultPanel
          visible={showFinalResult}
          runId={activeRunId}
          runStatus={runStatus}
          connection={mode}
          output={state.run?.output}
          outcome={productOutcome}
        />
        <VehicleCatalogResultPanel
          visible={showVehicleResult}
          runId={activeRunId}
          runStatus={runStatus}
          connection={mode}
          output={state.run?.output}
          outcome={productOutcome}
        />
        {identityUnavailable && executionUi && activeRunId !== undefined && (
          <p className="alert" role="alert">
            This run has no trustworthy immutable engine identity. Engine-specific result rendering is disabled.
          </p>
        )}
        {/* The canonical export: server-built, retrieved for a terminal run
            whose identity is trustworthy. The server refuses everything else. */}
        <RunExportControl
          visible={executionUi && activeConversation !== undefined && activeRunId !== undefined}
          runId={activeRunId}
          eligible={runIsTerminal && live.engine !== undefined}
        />
        <RunHistoryList
          visible={executionUi && activeConversation !== undefined}
          runs={runHistory}
          loading={runHistoryLoading}
          error={runHistoryError}
          activeRunId={activeRunId}
          onSelect={selectHistoricalRun}
          onRetry={() => { if (activeConversation) loadRunHistory(activeConversation.id, scope.current, false); }}
        />
        {/* CODE-3 — durable catalog state, not run state. It is shown for any
            selected project regardless of `executionUi`: the execution UI flag
            hides EXECUTION controls, and there are none here. Hiding a
            read-only inspection surface behind it would make the catalog
            invisible in exactly the posture an operator inspects it from. */}
        <CatalogReviewPanel
          visible={selectedProject !== undefined}
          open={catalogOpen}
          onOpenChange={changeCatalogOpen}
          view={catalogView}
          onViewChange={changeCatalogView}
          loading={catalogLoading}
          error={catalogError}
          canonical={canonicalPage}
          review={reviewPage}
          offset={catalogOffset}
          onOffsetChange={changeCatalogOffset}
          onRetry={() => {
            if (selectedProject) {
              loadCatalog(selectedProject, catalogView, catalogOffset, scope.current);
            }
          }}
        />
      </ConversationView>
    </WorkspaceShell>
  );
}
