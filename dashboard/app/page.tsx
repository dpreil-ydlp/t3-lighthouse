import Link from "next/link"
import { ArrowRight } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Empty, Section } from "@/components/page-header"
import { IncidentActions } from "@/components/incident-actions"
import { IncidentTrail } from "@/components/incident-trail"
import { ProjectCard, ProjectRow } from "@/components/project-board"
import { StatusDot } from "@/components/status"
import { TimeAgo } from "@/components/time-ago"
import { collapseIncidents, loadIncidents, loadProjects, openIncidents, type Project } from "@/lib/data"
import { plural } from "@/lib/format"
import { INCIDENT_STATE } from "@/lib/status"

export const dynamic = "force-dynamic"

export default async function Overview() {
  const [projects, incidents] = await Promise.all([loadProjects(), loadIncidents()])
  if (!projects || !incidents) {
    return <Empty>Lighthouse could not reach its API. Check that the Lighthouse services are running.</Empty>
  }
  const byId = new Map(projects.map((p) => [p.id, p]))
  const alerts = collapseIncidents(openIncidents(incidents)).filter((a) => byId.has(a.latest.project_id))
  const alertsByProject = [...new Set(alerts.map((a) => a.latest.project_id))].map((id) => ({ id, groups: alerts.filter((a) => a.latest.project_id === id) }))
  const by = (state: Project["activity_state"]) => projects.filter((p) => p.activity_state === state)
  const now = by("working_now")
  const today = by("today")
  const week = by("this_week")
  const quiet = by("quiet")

  return (
    <div className="space-y-10">
      <header className="space-y-1">
        <p className="text-2xl font-semibold tracking-tight">
          {now.length ? <span>{plural(now.length, "project")} working now</span> : <span>Nothing running right now</span>}
          <span className="text-muted-foreground"> · {today.length + now.length} active today · {week.length} more this week</span>
        </p>
        {alertsByProject.length > 0 && (
          <p className="text-sm text-state-alert">{plural(alertsByProject.length, "project")} need{alertsByProject.length === 1 ? "s" : ""} you</p>
        )}
      </header>

      {alertsByProject.length > 0 && (
        <Section title="Needs you" meta={alertsByProject.length}>
          <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
            {alertsByProject.map(({ id, groups }) => {
              const latest = groups[0].latest
              const st = INCIDENT_STATE[latest.state] ?? { tone: "alert" as const, label: latest.state }
              const ids = groups.flatMap((g) => [g.latest, ...g.repeats]).filter((i) => i.state !== "closed" && i.state !== "resolved").map((i) => i.id)
              return (
                <li key={id} className="grid gap-3 px-4 py-3.5 md:grid-cols-[minmax(0,1fr)_auto_auto] md:items-center md:gap-6">
                  <div className="min-w-0 space-y-1">
                    <div className="flex items-center gap-2.5">
                      <StatusDot tone={st.tone} pulse />
                      <Link href={`/projects/${id}`} className="truncate font-medium hover:underline">{byId.get(id)?.name}</Link>
                      <span className="text-sm text-muted-foreground">· {st.label}</span>
                    </div>
                    <p className="pl-[18px] text-sm text-muted-foreground">
                      {groups.length > 1 ? `${groups.length} tasks stalled` : latest.task_title ? `${latest.task_title} stalled` : latest.reason}. Opened <TimeAgo value={latest.opened_at} />
                    </p>
                  </div>
                  <IncidentTrail incident={latest} />
                  <IncidentActions id={latest.id} state={groups.length > 1 ? "escalated" : latest.state} ids={ids} />
                </li>
              )
            })}
          </ul>
        </Section>
      )}

      <Section title="Working now" meta={now.length ? "activity in the last 15 min" : undefined}>
        {now.length === 0 ? (
          <Empty>No agent is working on any project right now.</Empty>
        ) : (
          <div className="grid items-start gap-3 md:grid-cols-2">{now.map((p) => <ProjectCard key={p.id} p={p} />)}</div>
        )}
      </Section>

      {today.length > 0 && (
        <Section title="Earlier today" meta={today.length}>
          <div className="grid items-start gap-3 md:grid-cols-2">{today.map((p) => <ProjectCard key={p.id} p={p} />)}</div>
        </Section>
      )}

      {week.length > 0 && (
        <Section title="This week" meta={week.length}>
          <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">{week.map((p) => <ProjectRow key={p.id} p={p} />)}</ul>
        </Section>
      )}

      {quiet.length > 0 && (
        <p className="text-sm text-muted-foreground">
          {plural(quiet.length, "project")} quiet for more than a week.{" "}
          <Button asChild variant="link" size="sm" className="h-auto p-0"><Link href="/projects?show=quiet">See them <ArrowRight className="size-3.5" /></Link></Button>
        </p>
      )}
    </div>
  )
}
