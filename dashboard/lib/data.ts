import "server-only"
import { apiGet } from "./api"
import { projectName } from "./format"
import { OPEN_INCIDENT } from "./status"
import type { SessionGroup } from "./sessions"

export type RecentThread = { title: string | null; source: string; last_activity_at: string; t3_url?: string | null }
/** The short brief carried on project lists. */
export type LeftItem = { text: string; who: "you" | "agents" | "outside" | "unknown"; hours?: number | null }
export type BriefState = "agents_working" | "waiting_on_you" | "paused" | "nothing_open" | "idle"
export type BriefSummary = {
  now: string | null
  updated_at: string
  left: LeftItem[]
  needs_you: { action: string; count: number } | null
  agent_hours: number | null
  state: BriefState
  paused_since: string | null
  checked_at: string
  last_activity_at: string | null
  activity_through: string | null
}
/** The full brief written by the Mac job from recent work: plain-language state of a project. */
export type Brief = BriefSummary & {
  about: string | null
  done: string[]
  eta_basis: string | null
}
export type Project = Record<string, any> & {
  id: string
  name: string
  status: string
  attention_state: string
  activity_state: "working_now" | "today" | "this_week" | "quiet"
  last_activity_at: string | null
  tools: string[]
  active_threads: number
  open_incidents: number
  recent_threads: RecentThread[]
  roots: string[]
  hidden: boolean
  brief: BriefSummary | null
  t3_url: string | null
}
export type ProjectView = "active" | "hidden" | "archived" | "all"
export type Incident = Record<string, any> & { id: string; project_id: string; task_id?: string | null; state: string; opened_at: string }

const withDefaults = (p: Project): Project => ({
  ...p,
  name: projectName(p),
  activity_state: p.activity_state ?? "quiet",
  last_activity_at: p.last_activity_at ?? p.last_signal_at ?? null,
  tools: p.tools ?? [],
  active_threads: p.active_threads ?? 0,
  open_incidents: p.open_incidents ?? 0,
  recent_threads: p.recent_threads ?? [],
  roots: p.roots ?? [],
  hidden: !!p.hidden,
  brief: p.brief ?? null,
  t3_url: p.t3_url ?? null,
})

export async function loadProjects(view: ProjectView = "active") {
  return (await apiGet<{ items: Project[] }>(`/api/v1/projects?view=${view}`))?.items?.map(withDefaults) ?? null
}
export async function loadIncidents() {
  return (await apiGet<{ items: Incident[] }>("/api/v1/incidents"))?.items ?? null
}
export async function loadT3McpStatus() {
  return apiGet<{ configured: boolean; expires_at: string | null }>("/api/v1/t3-mcp/status")
}
export async function loadSessions(showIgnored = false) {
  return (await apiGet<{ items: SessionGroup[] }>(`/api/v1/unassigned?show_ignored=${showIgnored}`))?.items ?? null
}

/** Repeated stalls on the same task collapse into one row: the newest incident plus a repeat count. */
export function collapseIncidents(items: Incident[]) {
  const byKey = new Map<string, { latest: Incident; repeats: Incident[] }>()
  for (const i of items) {
    const key = `${i.project_id}:${i.task_id ?? i.parent_thread_id ?? i.id}`
    const g = byKey.get(key)
    if (!g) byKey.set(key, { latest: i, repeats: [] })
    else if (i.opened_at > g.latest.opened_at) { g.repeats.push(g.latest); g.latest = i } else g.repeats.push(i)
  }
  return [...byKey.values()].sort((a, b) => (a.latest.opened_at < b.latest.opened_at ? 1 : -1))
}

export const openIncidents = (items: Incident[]) => items.filter((i) => OPEN_INCIDENT.has(i.state))
