import { describe, expect, it } from "vitest";
import { ErrorCode, ReminderState } from "../contracts/metis";
import { FLOW_STATUS, REMINDER_STATUS, flowStateForError, reminderStatus, type FlowState } from "./status";

/** Every state the product specification requires must be expressible. */
const REQUIRED: FlowState[] = [
  "idle",
  "mic_denied",
  "recording",
  "canceled",
  "upload_pending",
  "offline",
  "queued",
  "transcribing",
  "transcript_ready",
  "extraction_pending",
  "clarification_required",
  "saved",
  "failed",
  "retrying",
  "provider_unavailable",
  "budget_exceeded",
  "notifications_disabled",
  "no_relevant_memory",
  "plan_conflict",
];

describe("status vocabulary", () => {
  it("has an honest label and a non-colour cue for every required state", () => {
    for (const state of REQUIRED) {
      const descriptor = FLOW_STATUS[state];
      expect(descriptor, state).toBeDefined();
      expect(descriptor.label.length, state).toBeGreaterThan(0);
      expect(descriptor.cue.length, state).toBeGreaterThan(0);
      expect(descriptor.detail.length, state).toBeGreaterThan(0);
    }
  });

  it("never calls an unsynchronised reminder scheduled", () => {
    expect(REMINDER_STATUS[ReminderState.PENDING_SCHEDULE].label).toBe("Scheduling reminder");
    expect(REMINDER_STATUS[ReminderState.SCHEDULED].label).toBe("Reminder scheduled");
    expect(REMINDER_STATUS[ReminderState.SCHEDULE_FAILED].label).toBe("Schedule needs retry");
  });

  it("covers every reminder state the contract can return", () => {
    for (const state of Object.values(ReminderState)) {
      expect(REMINDER_STATUS[state], state).toBeDefined();
    }
  });

  it("maps the error codes the user must be told apart", () => {
    expect(flowStateForError(ErrorCode.BUDGET_EXCEEDED)).toBe("budget_exceeded");
    expect(flowStateForError(ErrorCode.PROVIDER_UNAVAILABLE)).toBe("provider_unavailable");
    expect(flowStateForError(ErrorCode.RATE_LIMITED)).toBe("retrying");
    expect(flowStateForError(ErrorCode.VALIDATION_FAILED)).toBe("failed");
  });
});

describe("reminder timing", () => {
  const due = Date.parse("2026-11-26T10:00:00Z");
  const reminder = {
    id: "rem_late", trigger_at: new Date(due).toISOString(), timezone: "UTC",
    state: ReminderState.SCHEDULED,
  } as import("../contracts/metis").Reminder;

  it("changes a confirmed schedule to due and overdue as time passes", () => {
    expect(reminderStatus(reminder, due - 1).label).toBe("Reminder scheduled");
    expect(reminderStatus(reminder, due).label).toBe("Reminder due");
    const late = reminderStatus(reminder, due + 7.5 * 3600_000);
    expect(late.label).toBe("Reminder overdue");
    expect(late.detail).toContain("450 min");
    expect(late.detail).toContain("Delivery is not confirmed");
  });

  it("uses the snoozed time and leaves terminal states alone", () => {
    expect(reminderStatus({ ...reminder, state: ReminderState.SNOOZED, snoozed_until: new Date(due + 3600_000).toISOString() }, due).label).toBe("Snoozed");
    for (const state of [ReminderState.CANCELED, ReminderState.DELIVERED, ReminderState.SUPERSEDED]) {
      expect(reminderStatus({ ...reminder, state }, due + 3600_000)).toBe(REMINDER_STATUS[state]);
    }
    expect(reminderStatus({ ...reminder, state: ReminderState.PENDING_SCHEDULE }, due + 60_000).label).toBe("Reminder overdue");
  });
});
