'use client';
import { safeText } from '@/lib/sanitize';
import { SwarmTaskState } from '@/lib/swarmTypes';
import { TASK_STATUS_PRESENTATION, formatCount } from './swarmPresentation';

export type SwarmTaskListProps = {
  tasks: SwarmTaskState[];
  runningCount: number;
};

/**
 * The logical task graph.
 *
 * Rows are keyed by `taskId`, so a task keeps its identity across every poll
 * and no row is ever recreated by a re-render. Repairs, tool calls and evidence
 * are compact per-task counters: they can never add a row, and the list makes
 * no claim about how many workers or model calls produced it.
 *
 * Several rows can carry the "Running" status at once — the list is a list, not
 * a single active slot — because Swarm V2 genuinely executes ready tasks
 * concurrently.
 */
export function SwarmTaskList({ tasks, runningCount }: SwarmTaskListProps) {
  if (tasks.length === 0) {
    return <p className="muted">No logical tasks have been reported yet.</p>;
  }

  return (
    <>
      {runningCount > 1 && (
        <p className="swarm-concurrency">{formatCount(runningCount)} tasks running concurrently</p>
      )}
      <ol className="swarm-tasks">
        {tasks.map((task) => {
          const status = TASK_STATUS_PRESENTATION[task.status];
          const counts: string[] = [];
          if (task.repairCount > 0) counts.push(`${formatCount(task.repairCount)} repair${task.repairCount === 1 ? '' : 's'}`);
          if (task.toolCallCount > 0) counts.push(`${formatCount(task.toolCallCount)} tool call${task.toolCallCount === 1 ? '' : 's'}`);
          if (task.evidenceClaimIds.length > 0) counts.push(`${formatCount(task.evidenceClaimIds.length)} evidence`);
          return (
            <li className="swarm-task" key={task.taskId} data-status={task.status}>
              <span className="swarm-task-status">
                <span className="swarm-task-icon" aria-hidden="true">{status.icon}</span>
                <span className="swarm-task-status-text">{status.label}</span>
              </span>
              <span className="swarm-task-body">
                <span className="swarm-task-label">{safeText(task.label)}</span>
                <span className="swarm-task-meta">
                  <span className="identifier">{safeText(task.taskId)}</span>
                  {task.failureCode && (
                    <span className="swarm-task-code">Failure code {safeText(task.failureCode)}</span>
                  )}
                  {counts.length > 0 && <span className="swarm-task-counts">{counts.join(' · ')}</span>}
                </span>
              </span>
            </li>
          );
        })}
      </ol>
    </>
  );
}
