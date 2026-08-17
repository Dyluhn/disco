import { describe, expect, it } from "vitest";

import { latestStoppedStatus, type ConversationStatusEvent } from "./terminalConversationStatus";

const status = (seq: number, value: string, detail?: string): ConversationStatusEvent => ({
  seq,
  kind: "status",
  status: value,
  detail,
});

describe("latestStoppedStatus", () => {
  it.each(["ERROR", "STUCK", "AWAITING_USER_DECISION"])(
    "reports latest %s without waiting for a completion timeout",
    (value) => {
      const terminal = latestStoppedStatus([status(1, "RUNNING"), status(2, value, "why")]);
      expect(terminal).toMatchObject({ seq: 2, status: value, detail: "why" });
    },
  );

  it("does not treat RUNNING or FINISHED as stopped without finish", () => {
    expect(latestStoppedStatus([status(1, "RUNNING")])).toBeUndefined();
    expect(latestStoppedStatus([status(1, "RUNNING"), status(2, "FINISHED")])).toBeUndefined();
  });

  it("does not resurrect a historical user-decision stop after the run resumes", () => {
    expect(
      latestStoppedStatus([
        status(1, "AWAITING_USER_DECISION"),
        status(2, "RUNNING", "user resumed"),
      ]),
    ).toBeUndefined();
  });

  it("ignores stopped states from an earlier revision", () => {
    expect(
      latestStoppedStatus([status(2, "STUCK"), status(5, "RUNNING")], 3),
    ).toBeUndefined();
  });
});
