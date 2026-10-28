export type Tone = "alert" | "warn" | "ok" | "idle"

export const PROJECT_STATE: Record<string, { tone: Tone; label: string }> = {
  incident: { tone: "alert", label: "Needs you" },
  stalled: { tone: "alert", label: "Stalled" },
  milestone_overdue: { tone: "warn", label: "Milestone overdue" },
  inactive: { tone: "idle", label: "Quiet" },
  healthy: { tone: "ok", label: "On track" },
}

export const INCIDENT_STATE: Record<string, { tone: Tone; label: string }> = {
  open: { tone: "alert", label: "Open" },
  notifying: { tone: "alert", label: "Notifying" },
  awaiting_ack: { tone: "alert", label: "Waiting for reply" },
  recovering: { tone: "warn", label: "Recovering" },
  escalated: { tone: "alert", label: "Escalated to you" },
  resolved: { tone: "ok", label: "Resolved" },
  closed: { tone: "idle", label: "Closed" },
}

export const OPEN_INCIDENT = new Set(["open", "notifying", "awaiting_ack", "recovering", "escalated"])

export const CHANNELS: Record<string, { label: string; hint: string }> = {
  mac_alert: { label: "Mac notification", hint: "A notification on this Mac. Works for every tool." },
  t3_webhook: { label: "T3 thread", hint: "Wakes the project's T3 controller thread through a signed webhook." },
  test_sink: { label: "Test channel", hint: "Records the notice without telling anyone. For drills only." },
}

export const PROJECT_STATUS: Record<string, string> = {
  active: "Active", paused: "Paused", blocked: "Blocked", complete: "Complete", archived: "Archived",
}

/** How recently a project saw agent activity, from the API's activity_state. */
export const ACTIVITY: Record<string, { tone: Tone; label: string; pulse?: boolean }> = {
  working_now: { tone: "ok", label: "Working now", pulse: true },
  today: { tone: "ok", label: "Active today" },
  this_week: { tone: "idle", label: "This week" },
  quiet: { tone: "idle", label: "Quiet" },
}
