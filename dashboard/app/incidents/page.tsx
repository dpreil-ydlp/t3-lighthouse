import Link from "next/link"
import { Empty, PageHeader } from "@/components/page-header"
import { CloseAll } from "@/components/close-all"
import { IncidentActions } from "@/components/incident-actions"
import { IncidentTrail } from "@/components/incident-trail"
import { Status } from "@/components/status"
import { TimeAgo } from "@/components/time-ago"
import { collapseIncidents, loadIncidents, loadT3McpStatus } from "@/lib/data"
import { plural, until } from "@/lib/format"
import { INCIDENT_STATE, OPEN_INCIDENT } from "@/lib/status"
import { cn } from "@/lib/utils"

export const dynamic = "force-dynamic"
export const metadata = { title: "Incidents" }

export default async function Incidents(props: { searchParams: Promise<{ show?: string }> }) {
  const searchParams = await props.searchParams
  const [incidents, t3Status] = await Promise.all([loadIncidents(), loadT3McpStatus()])
  if (!incidents) return <Empty>Lighthouse could not reach its API.</Empty>
  const closedView = searchParams.show === "closed"
  const open = incidents.filter((i) => OPEN_INCIDENT.has(i.state))
  const closed = incidents.filter((i) => !OPEN_INCIDENT.has(i.state))
  const groups = collapseIncidents(closedView ? closed : open)

  return (
    <>
      <PageHeader
        title="Incidents"
        description="A stall opens an incident. Lighthouse notifies the project's channel, then waits for a reply and for real progress. Each step is recorded separately."
        actions={!closedView && open.length > 1 ? <CloseAll ids={open.map((i) => i.id)} /> : undefined}
      />
      {t3Status?.expires_at ? (() => {
        const expiry = new Date(t3Status.expires_at)
        const warning = expiry.getTime() - Date.now() <= 5 * 24 * 60 * 60 * 1000
        const date = new Intl.DateTimeFormat("en-GB", { dateStyle: "medium", timeStyle: "short", timeZone: "UTC" }).format(expiry)
        return <p className={cn("-mt-4 mb-4 text-xs", warning ? "text-state-warn" : "text-muted-foreground")}>T3 connection: expires {date} UTC</p>
      })() : <p className="-mt-4 mb-4 text-xs text-state-warn">T3 connection: expiry unavailable</p>}
      <div className="mb-4 flex gap-1" role="tablist">
        {[["open", "Open", open.length], ["closed", "Closed", closed.length]].map(([key, label, n]) => (
          <Link key={key as string} href={key === "open" ? "/incidents" : "/incidents?show=closed"} role="tab" aria-selected={closedView === (key === "closed")}
            className={cn("rounded-md px-3 py-1.5 text-sm", closedView === (key === "closed") ? "bg-secondary text-foreground" : "text-muted-foreground hover:text-foreground")}>
            {label} <span className="tabular text-xs text-muted-foreground">{n}</span>
          </Link>
        ))}
      </div>
      {groups.length === 0 ? (
        <Empty>{closedView ? "No closed incidents yet." : "No open incidents. Lighthouse is watching."}</Empty>
      ) : (
        <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
          {groups.map(({ latest, repeats }) => {
            const s = INCIDENT_STATE[latest.state] ?? { tone: "idle" as const, label: latest.state }
            const cooldown = until(latest.cooldown_until)
            return (
              <li key={latest.id} className="grid gap-3 px-4 py-3.5 lg:grid-cols-[minmax(0,1fr)_auto_auto] lg:items-center lg:gap-6">
                <div className="min-w-0 space-y-1">
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                    <Link href={`/projects/${latest.project_id}`} className="font-medium hover:underline">{latest.project_name}</Link>
                    <Status tone={s.tone} label={s.label} pulse={!closedView && s.tone === "alert"} className="text-xs" />
                  </div>
                  <p className="text-sm text-muted-foreground">
                    {latest.task_title ? <><span className="text-foreground">{latest.task_title}</span>: {latest.reason.charAt(0).toLowerCase() + latest.reason.slice(1)}</> : latest.reason}. Opened <TimeAgo value={latest.opened_at} />
                    {repeats.length > 0 && <> · repeated {plural(repeats.length, "time")}</>}
                    {latest.retry_count > 0 && <> · {latest.retry_count} of {latest.retry_budget} retries used</>}
                    {cooldown && !closedView && <> · quiet for {cooldown}</>}
                    {latest.kick_delivery_status === "kick not delivered" && <> · kick not delivered</>}
                  </p>
                </div>
                <IncidentTrail incident={latest} />
                {closedView ? <span /> : <IncidentActions id={latest.id} state={latest.state} ids={[latest.id, ...repeats.map((r) => r.id)]} />}
              </li>
            )
          })}
        </ul>
      )}
    </>
  )
}
