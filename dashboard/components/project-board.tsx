import Link from "next/link"
import { Status, StatusDot } from "@/components/status"
import { BriefFooter } from "@/components/project-brief"
import { TimeAgo } from "@/components/time-ago"
import type { Project } from "@/lib/data"
import { plural, sourceName } from "@/lib/format"
import { ACTIVITY } from "@/lib/status"

function Alerts({ count }: { count: number }) {
  if (!count) return null
  return <span className="rounded-full bg-state-alert/15 px-2 py-0.5 text-xs font-medium text-state-alert">{plural(count, "alert")}</span>
}

/** A project that is active now or today: what it is doing and in which tools. */
export function ProjectCard({ p }: { p: Project }) {
  const a = ACTIVITY[p.activity_state] ?? ACTIVITY.quiet
  const latest = p.brief?.now || p.recent_threads.find((t) => t.title)?.title
  return (
    <Link href={`/projects/${p.id}`} className="group flex flex-col gap-3 rounded-lg border border-border bg-card p-4 transition-colors hover:border-muted-foreground/40">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <p className="truncate font-medium group-hover:underline">{p.name}</p>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <Status tone={a.tone} label={a.label} pulse={a.pulse} className="text-xs" />
            <span><TimeAgo value={p.last_activity_at} /></span>
            {p.tools.length > 0 && <span>{p.tools.map(sourceName).join(" + ")}</span>}
          </div>
        </div>
        <Alerts count={p.open_incidents} />
      </div>
      {latest && <p className="text-sm leading-snug">{latest}</p>}
      <BriefFooter brief={p.brief} />
    </Link>
  )
}

/** A compact row for projects that were active this week or have gone quiet. */
export function ProjectRow({ p }: { p: Project }) {
  const a = ACTIVITY[p.activity_state] ?? ACTIVITY.quiet
  const latest = p.brief?.now || p.recent_threads.find((t) => t.title)?.title
  return (
    <li>
      <Link href={`/projects/${p.id}`} className="grid gap-1 px-4 py-3 transition-colors hover:bg-secondary/40 sm:grid-cols-[minmax(0,14rem)_minmax(0,1fr)_auto] sm:items-start sm:gap-6">
        <span className="flex min-w-0 items-center gap-2.5">
          <StatusDot tone={a.tone} />
          <span className="truncate font-medium">{p.name}</span>
        </span>
        <span className="min-w-0 space-y-2 text-sm leading-snug text-muted-foreground">
          {latest ?? ""}
          <span className="block"><BriefFooter brief={p.brief} /></span>
        </span>
        <span className="flex items-center gap-3 text-xs text-muted-foreground">
          <Alerts count={p.open_incidents} />
          <TimeAgo value={p.last_activity_at} empty="no activity" />
        </span>
      </Link>
    </li>
  )
}
