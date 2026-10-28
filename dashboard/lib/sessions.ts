import { isDesktopSource } from "./format"

export type SessionGroup = {
  source: string
  working_directory: string | null
  external_thread_id: string | null
  observation_id: string
  last_seen: string
  observations: number
  ignored: boolean
}

const TEMP_PATTERNS = [/\/\.t3\/scratch\//, /^\/private\/var\/folders\//, /^\/var\/folders\//, /^\/tmp\//, /^\/private\/tmp\//, /\/\.codex\/worktrees\//, /\/\.cache\//]

export function isTemporary(dir?: string | null) {
  return !!dir && TEMP_PATTERNS.some((p) => p.test(dir + "/"))
}

export function shortPath(dir: string) {
  return dir.replace(/^\/Users\/[^/]+/, "~")
}

export function suggestedName(dir?: string | null) {
  if (!dir) return "New project"
  const last = dir.split("/").filter(Boolean).pop() || "project"
  return last.replace(/[-_]+/g, " ").replace(/\s+/g, " ").trim().replace(/^./, (c) => c.toUpperCase())
}

export type FolderRow = {
  key: string
  directory: string | null
  label: string
  sources: string[]
  last_seen: string
  observations: number
  groups: SessionGroup[]
  temporary: boolean
  desktop: boolean
  ignored: boolean
}

/** One row per working directory (all tools merged); desktop apps get one row per app. */
export function foldSessions(groups: SessionGroup[]): FolderRow[] {
  const rows = new Map<string, FolderRow>()
  for (const g of groups) {
    const desktop = isDesktopSource(g.source)
    const key = desktop ? `app:${g.source}` : g.working_directory ? `dir:${g.working_directory}` : `thread:${g.source}:${g.external_thread_id}`
    const row = rows.get(key) ?? {
      key, directory: desktop ? null : g.working_directory, label: g.working_directory ? shortPath(g.working_directory) : g.source,
      sources: [], last_seen: g.last_seen, observations: 0, groups: [], temporary: isTemporary(g.working_directory),
      desktop, ignored: true,
    }
    if (!row.sources.includes(g.source)) row.sources.push(g.source)
    if (g.last_seen > row.last_seen) row.last_seen = g.last_seen
    row.observations += Number(g.observations)
    row.groups.push(g)
    row.ignored = row.ignored && g.ignored
    rows.set(key, row)
  }
  return [...rows.values()].sort((a, b) => (a.last_seen < b.last_seen ? 1 : -1))
}
