'use client';
import { SwarmStagePresentation } from './swarmPresentation';

export type SwarmStageTrackProps = {
  stages: SwarmStagePresentation[];
};

/**
 * Structural lifecycle track: four named stages, each carrying its own state as
 * text and as a shape. There is no bar, no ratio and no percentage, because the
 * backend never reports a completion fraction and inventing one would be a lie
 * the rest of the card would then have to keep.
 *
 * Every icon and every word comes from the presentation layer, so the outcome
 * rules live in one testable place: this component only positions them.
 */
export function SwarmStageTrack({ stages }: SwarmStageTrackProps) {
  return (
    <ol className="swarm-stages">
      {stages.map((stage) => (
        <li
          key={stage.stage}
          className="swarm-stage"
          data-state={stage.state}
          data-tone={stage.tone}
          aria-current={stage.current ? 'step' : undefined}
        >
          <span className="swarm-stage-icon" aria-hidden="true">{stage.icon}</span>
          <span className="swarm-stage-name">{stage.stage}</span>
          <span className="swarm-stage-state">{stage.stateLabel}</span>
        </li>
      ))}
    </ol>
  );
}
