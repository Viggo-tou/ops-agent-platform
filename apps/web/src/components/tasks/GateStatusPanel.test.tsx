import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { GateStatusPanel, resolveGateStatuses } from "./GateStatusPanel";
import type { EventRecord } from "../../types";

function makeEvent(overrides: Partial<EventRecord> = {}): EventRecord {
  return {
    id: overrides.id ?? crypto.randomUUID(),
    task_id: overrides.task_id ?? "task-1",
    session_id: overrides.session_id ?? null,
    event_type: overrides.event_type ?? "tool_succeeded",
    source: overrides.source ?? "tool",
    stage: overrides.stage ?? null,
    role: overrides.role ?? null,
    tool_name: overrides.tool_name ?? null,
    message: overrides.message ?? "Gate event",
    payload_json: overrides.payload_json ?? { tool_name: "compile_gate.check" },
    created_at: overrides.created_at ?? "2026-01-01T10:00:00",
  };
}

describe("resolveGateStatuses", () => {
  it("returns six skipped statuses for an empty event stream", () => {
    const statuses = resolveGateStatuses([]);

    expect(statuses).toHaveLength(6);
    expect(statuses.every((status) => status.verdict === "skipped")).toBe(true);
  });

  it("uses the last compile terminal event and counts attempts", () => {
    const statuses = resolveGateStatuses([
      makeEvent({
        id: "compile-1",
        event_type: "tool_succeeded",
        payload_json: { tool_name: "compile_gate.check" },
        created_at: "2026-01-01T10:00:00",
      }),
      makeEvent({
        id: "compile-2",
        event_type: "tool_failed",
        payload_json: { tool_name: "compile_gate.check" },
        created_at: "2026-01-01T10:01:00",
      }),
      makeEvent({
        id: "compile-3",
        event_type: "tool_succeeded",
        payload_json: { tool_name: "compile_gate.check" },
        created_at: "2026-01-01T10:02:00",
      }),
    ]);

    const compileGate = statuses.find((status) => status.id === "compile_gate");
    expect(compileGate?.verdict).toBe("pass");
    expect(compileGate?.attempts).toBe(3);
  });

  it("marks a requested gate without a terminal event as running", () => {
    const statuses = resolveGateStatuses([
      makeEvent({
        id: "runtime-1",
        event_type: "tool_call_requested",
        payload_json: { tool_name: "runtime_validation.check" },
      }),
    ]);

    const runtimeGate = statuses.find((status) => status.id === "runtime_validation");
    expect(runtimeGate?.verdict).toBe("running");
    expect(runtimeGate?.attempts).toBe(0);
  });

  it("uses the last spec conformance terminal event across sub-calls", () => {
    const statuses = resolveGateStatuses([
      makeEvent({
        id: "spec-1",
        event_type: "tool_failed",
        payload_json: { tool_name: "spec_conformance.check" },
        created_at: "2026-01-01T10:00:00",
      }),
      makeEvent({
        id: "spec-2",
        event_type: "tool_succeeded",
        payload_json: { tool_name: "spec_conformance.attest" },
        created_at: "2026-01-01T10:01:00",
      }),
    ]);

    const specGate = statuses.find((status) => status.id === "spec_conformance");
    expect(specGate?.verdict).toBe("pass");
    expect(specGate?.attempts).toBe(2);
  });
});

describe("GateStatusPanel", () => {
  it("renders mixed gate status output", () => {
    const { container } = render(
      <GateStatusPanel
        events={[
          makeEvent({
            id: "compile-pass",
            event_type: "tool_succeeded",
            message: "Compile passed",
            payload_json: { tool_name: "compile_gate.check" },
            created_at: "2026-01-01T10:00:00",
          }),
          makeEvent({
            id: "runtime-requested",
            event_type: "tool_call_requested",
            message: "Runtime validation started",
            payload_json: { tool_name: "runtime_validation.check" },
            created_at: "2026-01-01T10:01:00",
          }),
          makeEvent({
            id: "diff-fail",
            event_type: "tool_failed",
            message: "Diff reviewer blocked the patch because required evidence was missing.",
            payload_json: { tool_name: "diff_reviewer.check" },
            created_at: "2026-01-01T10:02:00",
          }),
          makeEvent({
            id: "spec-pass",
            event_type: "tool_succeeded",
            message: "Spec conformance passed",
            payload_json: { tool_name: "spec_conformance.attest" },
            created_at: "2026-01-01T10:03:00",
          }),
        ]}
      />,
    );

    expect(screen.getByText("Pipeline Gates")).toBeTruthy();
    expect(screen.getAllByText("Diff Reviewer")).toHaveLength(2);
    expect(screen.getByText("Diff reviewer blocked the patch because required evidence was missing.")).toBeTruthy();
    expect(container.firstChild).toMatchSnapshot();
  });
});
