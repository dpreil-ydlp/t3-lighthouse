export function ago(value?: string | null, now = Date.now()): string {
  if (!value) return "never"
  const s = Math.max(0, Math.floor((now - new Date(value).getTime()) / 1000))
  if (s < 45) return "just now"
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  const d = Math.floor(s / 86400)
  return d < 30 ? `${d}d ago` : `${Math.floor(d / 30)}mo ago`
}

export function until(value?: string | null, now = Date.now()): string | null {
  if (!value) return null
  const s = Math.floor((new Date(value).getTime() - now) / 1000)
  if (s <= 0) return null
  if (s < 3600) return `${Math.max(1, Math.ceil(s / 60))}m`
  return `${Math.floor(s / 3600)}h ${Math.ceil((s % 3600) / 60)}m`
}

export function utc(value?: string | null): string {
  if (!value) return "Not observed"
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "UTC", year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).format(new Date(value)) + " UTC"
}

export function minutesSince(value?: string | null, now = Date.now()) {
  return value ? (now - new Date(value).getTime()) / 60000 : Infinity
}

export function plural(n: number, word: string, many = word + "s") {
  return `${n.toLocaleString("en-US")} ${n === 1 ? word : many}`
}

const SOURCES: Record<string, string> = {
  claude_code: "Claude Code",
  codex: "Codex",
  chatgpt_desktop: "ChatGPT app",
  codex_desktop: "Codex app",
  claude_desktop: "Claude app",
  t3_code: "T3 Code",
  mcp: "MCP report",
  dashboard: "You",
  worker: "Lighthouse",
  mac_collector: "Mac collector",
  test_sink: "Test channel",
}
export const sourceName = (s?: string | null) => (s ? SOURCES[s] ?? s.replaceAll("_", " ") : "Unknown")
export const isDesktopSource = (s: string) => s.endsWith("_desktop")

const EVENTS: Record<string, string> = {
  collector_observation: "Session activity",
  desktop_file_activity: "App activity",
  progress: "Progress reported",
  notification_delivered: "Notice delivered",
  incident_acknowledged: "Incident acknowledged",
  incident_closed: "Incident closed",
  incident_opened: "Incident opened",
  incident_escalated: "Incident escalated",
  project_updated: "Project updated",
  task_closed: "Task closed",
}
export const eventName = (t: string) => EVENTS[t] ?? t.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase())

/**
 * Discovered projects start with their folder name ("garden_planner", "recipe-box").
 * Show those as words; leave names the owner typed, and mixed tokens like "build-2-v5.1", alone.
 */
export function projectName(p: { name: string; roots?: string[] }) {
  const folderNamed = (p.roots ?? []).some((r) => r.split("/").filter(Boolean).pop()?.toLowerCase() === p.name.toLowerCase())
  if (!folderNamed) return p.name
  return p.name
    .replaceAll("_", " ")
    .split(" ")
    .flatMap((t) => (/^[a-z]+(-[a-z]+)+$/i.test(t) ? t.split("-") : [t]))
    .map((w) => (/^[a-z][a-z0-9]*$/.test(w) ? (w.length <= 2 ? w.toUpperCase() : w[0].toUpperCase() + w.slice(1)) : w))
    .join(" ")
}
