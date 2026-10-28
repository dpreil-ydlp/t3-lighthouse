import { Check } from "lucide-react"
import { Empty } from "@/components/page-header"
import { TimeAgo } from "@/components/time-ago"
import type { Brief, BriefSummary, LeftItem } from "@/lib/data"
import { cn } from "@/lib/utils"

function Heading({ children }: { children: React.ReactNode }) {
  return <h2 className="mb-3 text-xs font-medium uppercase tracking-[0.08em] text-muted-foreground">{children}</h2>
}

function stateLabel(brief: BriefSummary) {
  if (brief.state === "nothing_open") return "Nothing open"
  if (brief.state === "agents_working") return "Agents working"
  if (brief.state === "waiting_on_you") return "Waiting on you"
  if (brief.state === "idle") {
    if (!brief.last_activity_at) return "Idle"
    const days = Math.max(1, Math.floor((Date.now() - new Date(brief.last_activity_at).getTime()) / 86400000))
    return `Idle ${days}d`
  }
  const date = brief.paused_since ? new Intl.DateTimeFormat("en", { month: "short", day: "numeric" }).format(new Date(brief.paused_since)) : null
  return date ? `Paused since ${date}` : "Paused"
}

export function NeedsYou({ brief, className }: { brief: BriefSummary | null; className?: string }) {
  if (!brief?.needs_you) return null
  const { action, count } = brief.needs_you
  return <span className={cn("block truncate text-foreground", className)} title={action}>
    Needs you: {action}{count > 1 && <span className="text-muted-foreground"> · +{count - 1} more</span>}
  </span>
}

export function AgentWork({ brief, className }: { brief: BriefSummary | null; className?: string }) {
  if (!brief || brief.agent_hours == null || brief.agent_hours <= 0) return null
  const value = `~${Number(brief.agent_hours.toFixed(1))} h agent work`
  return <span className={cn("text-muted-foreground", className)}>{value}</span>
}

export function BriefStatus({ brief, className }: { brief: BriefSummary | null; className?: string }) {
  if (!brief) return null
  const tone = brief.state === "agents_working" ? "text-state-ok" : "text-muted-foreground"
  return <span className={cn(tone, className)}>{stateLabel(brief)}</span>
}

export function BriefFooter({ brief }: { brief: BriefSummary | null }) {
  if (!brief) return null
  const state = brief.state === "agents_working" ? null : <BriefStatus brief={brief} />
  const parts = [<AgentWork key="agent" brief={brief} />, state, <span key="checked">checked <TimeAgo value={brief.checked_at} className="text-inherit" /></span>].filter(Boolean)
  return <div className="mt-auto space-y-1 border-t border-border/60 pt-3 text-xs">
    <NeedsYou brief={brief} />
    <div className="flex min-w-0 flex-wrap items-center gap-x-1 text-muted-foreground">
      {parts.map((part, i) => <span key={i} className="inline-flex min-w-0 items-center gap-x-1">{i > 0 && <span>·</span>}{part}</span>)}
    </div>
  </div>
}

function itemLabel(item: LeftItem) {
  if (item.who === "agents") return item.hours == null ? "Agents · estimate unknown" : `Agents · ${item.hours} h`
  if (item.who === "you") return "You"
  if (item.who === "outside") return "Outside"
  return "Unknown"
}

export function BriefPanel({ brief, lastActivity, aside }: { brief: Brief | null; lastActivity?: string | null; aside?: React.ReactNode }) {
  if (!brief) {
    return (
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_18rem]">
        <Empty>No brief yet. Lighthouse writes one from this project's recent work within 30 minutes of the next activity.</Empty>
        {aside}
      </div>
    )
  }
  const owners = brief.left.filter((item) => item.who === "you")
  return (
    <div className="grid gap-x-10 gap-y-8 lg:grid-cols-[minmax(0,1fr)_18rem]">
      <div className="min-w-0 space-y-8">
        <div className="max-w-prose space-y-3">
          {brief.about && <p className="text-[15px] leading-relaxed text-muted-foreground">{brief.about}</p>}
          {brief.now && <p className="text-lg font-medium leading-snug tracking-tight">{brief.now}</p>}
        </div>
        <div className="grid gap-8 md:grid-cols-2">
          <section>
            <Heading>Done</Heading>
            {brief.done.length === 0 ? <p className="text-sm text-muted-foreground">Nothing recorded yet.</p> : (
              <ul className="space-y-2.5">
                {brief.done.map((d, i) => <li key={i} className="flex gap-2.5 text-sm leading-snug"><Check className="mt-0.5 size-3.5 shrink-0 text-state-ok" aria-hidden /><span>{d}</span></li>)}
              </ul>
            )}
          </section>
          <section>
            <Heading>What's left</Heading>
            {brief.left.length === 0 ? <p className="text-sm text-muted-foreground">Nothing open.</p> : (
              <ol className="space-y-2.5">
                {brief.left.map((item, i) => (
                  <li key={i} className="flex items-start gap-2.5 text-sm leading-snug">
                    <span className="tabular mt-px w-3.5 shrink-0 text-right text-xs text-muted-foreground">{i + 1}</span>
                    <span className="min-w-0">{item.text}<span className="ml-2 whitespace-nowrap rounded-full bg-secondary px-1.5 py-0.5 text-[10px] text-muted-foreground">{itemLabel(item)}</span></span>
                  </li>
                ))}
              </ol>
            )}
          </section>
        </div>
      </div>
      <aside className="space-y-4">
        <div className="rounded-lg border border-border bg-card p-4">
          <Heading>Status</Heading>
          <BriefStatus brief={brief} className="inline-block" />
          <section className="mt-4">
            <h3 className="text-sm font-medium">Needs you</h3>
            {owners.length ? <ul className="mt-2 list-disc space-y-1 pl-4 text-sm">{owners.map((item, i) => <li key={i}>{item.text}</li>)}</ul> : <p className="mt-1 text-sm text-muted-foreground">No owner actions.</p>}
          </section>
          <section className="mt-4">
            <h3 className="text-sm font-medium">Agent work</h3>
            <p className="mt-1 text-sm text-muted-foreground">{brief.agent_hours == null ? "estimate unknown" : brief.agent_hours === 0 ? "0 h" : `~${Number(brief.agent_hours.toFixed(1))} h`}</p>
            {brief.eta_basis && <p className="mt-2 text-xs leading-relaxed text-muted-foreground">{brief.eta_basis}</p>}
          </section>
          <p className="mt-4 border-t border-border/60 pt-3 text-xs text-muted-foreground">
            Checked <TimeAgo value={brief.checked_at} className="text-inherit" /> · covers activity to <TimeAgo value={brief.activity_through} empty="unknown" className="text-inherit" />
          </p>
        </div>
        {aside}
      </aside>
    </div>
  )
}
