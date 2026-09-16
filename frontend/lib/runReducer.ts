import { maxEventId, normalizeEventId, sortByEventId } from './eventId';
import { ownsAgentProjection, ownsSpendTelemetry, ownsV1Projection } from './eventVocabulary';
import { isTerminalRunStatus } from './runStatus';
import { reduceSwarmEvent } from './swarmReducer';
import { initialSwarmRunState } from './swarmTypes';
import { AgentState, Run, RunEvent, WorkspaceState, SourceRecord } from './types';
export const initialWorkspaceState: WorkspaceState = { events: [], agents: {}, sources: [], claims: [], conflicts: [], currentPhase: 'idle', progress: 0, tokens: 0, cost: 0, supervisor: [], validationErrors: [], checkpoints: [], rawErrors: [], swarm: initialSwarmRunState };
export function defaultAgent(name: string): AgentState { return { name, responsibility: 'Dynamic MILO workspace agent', status: 'pending', progress: 0, internet: 'conditional', searchesUsed: 0, sources: [], tokens: 0, cost: 0, retries: 0, fallbacks: [] }; }
/**
 * Fold one run event into the workspace.
 *
 * RECOGNITION COMES FIRST. An event is appended to the raw list and offered to
 * the swarm reducer whatever its type — observing an unknown type is safe and
 * forward-compatible — but it writes a trusted projection only if
 * `lib/eventVocabulary.ts` says its type owns that projection. Until F5 the
 * order was reversed: the generic fields (`agent`, `phase`, `progress`,
 * `payload.tokens`, `payload.cost_usd`) were applied before anything asked
 * whether the type was one the frontend recognises, so an invented type could
 * manufacture a V1 agent, a lifecycle phase and a spend total, and could pick
 * its own agent status out of a substring of its own name.
 *
 * Three consequences, each deliberate:
 *
 *  - an unrecognised type is INERT. It is visible in the event stream and in
 *    `swarm.unknownEventTypes`, and it touches nothing else;
 *  - a Swarm V2 type never reaches the V1 agent registry, even carrying an
 *    `agent` field. Swarm V2 has no agent concept, so such a field is a payload
 *    asserting something the engine that emitted it cannot assert;
 *  - the substring tests below (`includes('failed')`, `includes('retry')`,
 *    `includes('supervisor')`) run only for types already on the allowlist,
 *    where they describe a real backend naming convention rather than an
 *    attacker's choice of name.
 *
 * The V1 projection for every legitimate V1 event is otherwise unchanged.
 */
export function reduceRunEvent(state: WorkspaceState, incoming: RunEvent): WorkspaceState {
  // One canonical decimal-string identity per event, so `===` is exact even
  // for bigint ids beyond Number.MAX_SAFE_INTEGER.
  const event: RunEvent = { ...incoming, id: normalizeEventId(incoming.id) };
  if (state.events.some(e => e.id === event.id)) return state;
  const next: WorkspaceState = { ...state, events: [...state.events, event], lastEventId: maxEventId(state.lastEventId, event.id), swarm: reduceSwarmEvent(state.swarm, event) };
  // Everything below this line is the V1 projection and is gated on the type
  // owning it. An unrecognised type stops here, recorded and inert.
  if (!ownsV1Projection(event.event_type)) return next;
  if (event.phase) next.currentPhase = event.phase;
  if (event.progress?.percent != null) next.progress = Number(event.progress.percent);
  if (event.event_type.includes('supervisor')) next.supervisor = [...next.supervisor, event.message ?? event.event_type];
  if (event.event_type === 'checkpoint_saved') next.checkpoints = [...next.checkpoints, event.payload];
  if (event.event_type === 'validation_error') next.validationErrors = [...next.validationErrors, event.payload];
  if (event.event_type.includes('error') || event.event_type.includes('failed')) next.rawErrors = [...next.rawErrors, event.payload ?? event.message];
  const agentName = ownsAgentProjection(event.event_type) ? (event.agent ?? event.payload?.agent) : undefined;
  if (agentName) {
    const agent = { ...(next.agents[agentName] ?? defaultAgent(agentName)) };
    agent.status = statusFromEvent(event.event_type, agent.status); agent.currentTask = event.message ?? agent.currentTask;
    agent.progress = Number(event.progress?.percent ?? event.payload?.progress ?? agent.progress);
    agent.internet = internetFromEvent(event.event_type, agent.internet); agent.internetReason = event.payload?.reason ?? agent.internetReason;
    agent.domains = event.payload?.domains ?? agent.domains; agent.searchesUsed += event.event_type === 'tool_used' ? 1 : 0;
    agent.tokens += Number(event.payload?.tokens ?? event.payload?.token_usage?.total ?? 0); agent.cost += Number(event.payload?.cost_usd ?? 0);
    if (event.event_type.includes('retry')) agent.retries += 1; if (event.payload?.fallback) agent.fallbacks = [...agent.fallbacks, event.payload.fallback];
    next.agents = { ...next.agents, [agentName]: agent };
  }
  if (event.event_type === 'source_recorded') { const source = normalizeSource(event.payload); next.sources = upsert(next.sources, source); if (source.agent && next.agents[source.agent]) next.agents[source.agent].sources = upsert(next.agents[source.agent].sources, source); }
  if (event.event_type === 'claim_recorded') next.claims = upsert(next.claims, event.payload);
  if (event.event_type === 'conflict_detected') next.conflicts = upsert(next.conflicts, event.payload);
  // Event-derived spend is developer telemetry, labelled as such in the
  // Inspector. `run.usage` remains the sole authority and is untouched here.
  if (ownsSpendTelemetry(event.event_type)) { next.tokens += Number(event.payload?.tokens ?? 0); next.cost += Number(event.payload?.cost_usd ?? 0); }
  return next;
}
function upsert<T extends {id?: string}>(items: T[], item: T): T[] { return item?.id && items.some(i => i.id === item.id) ? items.map(i => i.id === item.id ? item : i) : [...items, item]; }
function normalizeSource(p: any): SourceRecord { return { id: String(p?.id ?? crypto.randomUUID()), title: p?.title ?? 'Untitled source', domain: p?.domain ?? 'unknown', url: p?.url, source_type: p?.source_type ?? 'unknown', source_strength: p?.source_strength ?? 'unknown', source_date: p?.source_date, retrieved_at: p?.retrieved_at ?? new Date().toISOString(), claims: p?.claims ?? [], agent: p?.agent, query: p?.query, tool_operation: p?.tool_operation }; }
function statusFromEvent(type: string, current: string) { if (type.includes('completed')) return 'completed'; if (type.includes('failed')) return 'failed'; if (type.includes('started') || type.includes('tool_')) return 'active'; return current; }
function internetFromEvent(type: string, current: AgentState['internet']) { if (type === 'tool_access_requested') return 'requested'; if (type === 'tool_access_granted') return 'approved'; if (type === 'tool_access_denied') return 'denied'; if (type === 'tool_used') return 'active'; return current; }
export function reconstructRun(run: Run, events: RunEvent[]): WorkspaceState {
  // Order defensively before folding. The swarm slice advances strictly by
  // event id, so an unsorted array handed in by a caller must not silently
  // drop events; the backend already returns them ordered by id.
  const ordered = sortByEventId(events.map(event => ({ ...event, id: normalizeEventId(event.id) })));
  return ordered.reduce(reduceRunEvent, { ...initialWorkspaceState, run, currentPhase: isTerminalRunStatus(run.status) ? run.status : 'reconnected' });
}
