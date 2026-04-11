import { formatDateTime, titleCase } from "../../lib/format";
import type { EventRecord } from "../../types";

interface TaskTimelineProps {
  events: EventRecord[];
}

export function TaskTimeline({ events }: TaskTimelineProps) {
  if (events.length === 0) {
    return (
      <div className="empty-panel">
        <h3>No events recorded</h3>
        <p>The event store will populate once the task moves through the orchestrator.</p>
      </div>
    );
  }

  return (
    <div className="timeline">
      {events.map((event) => (
        <article className="timeline-item" key={event.id}>
          <div className="timeline-marker" />
          <div className="timeline-content">
            <div className="timeline-head">
              <div>
                <strong>{titleCase(event.event_type)}</strong>
                <div className="muted-text">
                  {event.stage ? titleCase(event.stage) : "No stage"} •{" "}
                  {event.role ? titleCase(event.role) : "System"}
                </div>
                {event.session_id ? <div className="muted-text">session {event.session_id}</div> : null}
              </div>
              <span className="muted-text">{formatDateTime(event.created_at)}</span>
            </div>

            <p>{event.message}</p>

            <div className="event-meta-row">
              <span className="mini-pill">{titleCase(event.source)}</span>
              {event.tool_name ? <span className="mini-pill">{event.tool_name}</span> : null}
            </div>

            {event.payload_json ? (
              <pre className="json-panel">{JSON.stringify(event.payload_json, null, 2)}</pre>
            ) : null}
          </div>
        </article>
      ))}
    </div>
  );
}
