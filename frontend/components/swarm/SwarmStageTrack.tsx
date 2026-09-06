'use client';
import { SwarmStagePresentation } from './swarmPresentation';

export type SwarmStageTrackProps = {
  stages: SwarmStagePresentation[];
};

const STATE_TEXT: Record<SwarmStagePresentation['state'], string> = {
  pending: 'Not started',
  active: 'In progress',
  complete: 'Done',
};

const STATE_ICON: Record<SwarmStagePresentation['state'], string> = {
  pending: '○',
  active: '◐',
  complete: '✓',
};

/**
 * Structural lifecycle track: four named stages, each carrying its own state as
 * text and as a shape. There is no bar, no ratio and no percentage, because the
 * backend never reports a completion fraction and inventing one would be a lie
 * the rest of the card would then have to keep.
 */
export function SwarmStageTrack({ stages }: SwarmStageTrackProps) {
  return (
    <ol className="swarm-stages">
      {stages.map((stage) => (
        <li
          key={stage.stage}
          className="swarm-stage"
          data-state={stage.state}
          aria-current={stage.current ? 'step' : undefined}
        >
          <span className="swarm-stage-icon" aria-hidden="true">{STATE_ICON[stage.state]}</span>
          <span className="swarm-stage-name">{stage.stage}</span>
          <span className="swarm-stage-state">{STATE_TEXT[stage.state]}</span>
        </li>
      ))}
    </ol>
  );
}
