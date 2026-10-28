import Link from "next/link"
import { ArrowLeft, ArrowUpRight } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { Empty, Section } from "@/components/page-header"
import { IncidentActions } from "@/components/incident-actions"
import { IncidentTrail } from "@/components/incident-trail"
import { BriefPanel } from "@/components/project-brief"
import { ChannelForm, ProjectMenu, TaskActions } from "@/components/project-controls"
import { SourceChips } from "@/components/source-chips"
import { Status, StatusDot } from "@/components/status"
import { TimeAgo } from "@/components/time-ago"
import { apiGet } from "@/lib/api"
import { collapseIncidents, loadIncidents, loadProjects } from "@/lib/data"
import { eventName, plural, projectName, sourceName } from "@/lib/format"
import { shortPath } from "@/lib/sessions"
import { ACTIVITY, CHANNELS, INCIDENT_STATE, OPEN_INCIDENT, PROJECT_STATUS } from "@/lib/status"

export const dynamic = "force-dynamic"

const SIGNALS = [
  ["last_process_heartbeat_at", "Process", "The tool's process was last seen"],
  ["last_agent_event_at", "Agent event", "The session last wrote a record"],
  ["last_meaningful_progress_at", "Progress", "Real progress was last reported"],
  ["last_milestone_at", "Milestone", "A milestone was last completed"],
  ["last_user_interaction_at", "You", "You last interacted"],
] as const

export async function generateMetadata(props: { params: Promise<{ id: string }> }) {
  const params = await props.params
  const data = await apiGet<any>(`/api/v1/projects/${params.id}`)
  return { title: data?.project ? projectName({ ...data, ...data.project }) : "Project" }
}

export default async function ProjectPage(props: { params: Promise<{ id: string }>; searchParams: Promise<{ all?: string; threads?: string }> }) {
  const [params, searchParams] = await Promise.all([props.params, props.searchParams])
  const [data, events, incidents, list] = await Promise.all([
    apiGet<any>(`/api/v1/projects/${params.id}`),
    apiGet<{ items: any[] }>(`/api/v1/projects/${params.id}/events?limit=200`),
    loadIncidents(),
    loadProjects(),
  ])
  if (!data) return <Empty>This project was not found, or Lighthouse could not reach its API.</Empty>
  // Activity fields may sit on the project object or beside it; prefer the project's own.
  const p = { ...data, ...data.project }
  p.name = projectName(p)
  const st = p.status === "active" ? ACTIVITY[p.activity_state] ?? ACTIVITY.quiet : { tone: "idle" as const, label: PROJECT_STATUS[p.status] ?? p.status }
  const threads: any[] = data.threads ?? []
  const shownThreads = searchParams.threads === "all" ? threads : threads.slice(0, 12)
  const own = (incidents ?? []).filter((i) => i.project_id === p.id)
  const openGroups = collapseIncidents(own.filter((i) => OPEN_INCIDENT.has(i.state)))
  const closedGroups = collapseIncidents(own.filter((i) => !OPEN_INCIDENT.has(i.state))).slice(0, 20)
  const showAll = searchParams.all === "1"
  const allEvents = events?.items ?? []
  const routine = allEvents.filter((e) => e.event_type === "collector_observation" || e.event_type === "desktop_file_activity")
  const shown = showAll ? allEvents : allEvents.filter((e) => !routine.includes(e))
  const channel = data.controller?.policy?.channel || "mac_alert"

  return (
    <div className="space-y-10">
      <div className="space-y-4">
        <Link href="/projects" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"><ArrowLeft className="size-3.5" />Projects</Link>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0 space-y-1.5">
            <h1 className="text-2xl font-semibold tracking-tight">{p.name}</h1>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted-foreground">
              <Status tone={st.tone} label={st.label} pulse={"pulse" in st && st.pulse} />
              {p.hidden && <Badge variant="outline" className="font-normal text-muted-foreground">Hidden</Badge>}
              {p.active_threads > 0 && <span>{plural(p.active_threads, "thread")} active</span>}
              <span>Last activity <TimeAgo value={p.last_activity_at} empty="none yet" /></span>
              {p.tools?.length > 0 && <SourceChips sources={p.tools} />}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {p.t3_url && (
              <Button asChild variant="outline" size="sm">
                <a href={p.t3_url}>Open in T3<ArrowUpRight className="size-3.5" /></a>
              </Button>
            )}
            <ProjectMenu id={p.id} name={p.name} status={p.status} hidden={!!p.hidden} projects={(list ?? []).map((x) => ({ id: x.id, name: x.name }))} />
          </div>
        </div>
      </div>

      <BriefPanel brief={p.brief ?? null} lastActivity={p.last_activity_at} />

      <Tabs defaultValue={openGroups.length ? "incidents" : "overview"}>
        <TabsList>
          <TabsTrigger value="overview">Sessions</TabsTrigger>
          <TabsTrigger value="activity">Activity</TabsTrigger>
          <TabsTrigger value="incidents">Incidents{openGroups.length > 0 && <span className="ml-1.5 rounded-full bg-state-alert/15 px-1.5 text-[11px] text-state-alert">{openGroups.length}</span>}</TabsTrigger>
          <TabsTrigger value="settings">Settings</TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="space-y-8 pt-4">
          <Section title="Recent sessions" meta={threads.length ? plural(threads.length, "session") : undefined}>
            {threads.length === 0 ? (
              <Empty>No sessions seen for this project yet. Lighthouse picks them up from Claude Code, Codex and T3 automatically.</Empty>
            ) : (
              <>
                <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
                  {shownThreads.map((t) => (
                    <li key={`${t.source}:${t.external_thread_id}`} className="grid gap-1 px-4 py-2.5 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:gap-6">
                      <div className="min-w-0">
                        <p className={t.title ? "truncate text-sm font-medium" : "truncate text-sm italic text-muted-foreground"}>{t.title || "Untitled session"}</p>
                        {t.working_directory && <p className="truncate font-mono text-xs text-muted-foreground">{shortPath(t.working_directory)}</p>}
                      </div>
                      <span className="flex items-center gap-3 text-xs text-muted-foreground">
                        {t.t3_url && <a href={t.t3_url} className="inline-flex items-center gap-0.5 text-foreground underline-offset-4 hover:underline">Open in T3<ArrowUpRight className="size-3" /></a>}
                        <SourceChips sources={[t.source]} />
                        <TimeAgo value={t.last_activity_at} />
                      </span>
                    </li>
                  ))}
                </ul>
                {threads.length > shownThreads.length && (
                  <Link href={`/projects/${p.id}?threads=all`} className="text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline">Show all {threads.length}</Link>
                )}
              </>
            )}
          </Section>

          {data.open_tasks.length > 0 && <Section title="Open tasks" meta={data.open_tasks.length}>
            {(
              <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
                {data.open_tasks.map((t: any) => (
                  <li key={t.id} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3">
                    <div className="min-w-0">
                      <p className="truncate font-medium">{t.title}</p>
                      <p className="text-sm text-muted-foreground">
                        {t.status.replaceAll("_", " ")} · progress <TimeAgo value={t.last_meaningful_progress_at} empty="never reported" />
                        {t.latest_incident_state && OPEN_INCIDENT.has(t.latest_incident_state) && <span className="text-state-alert"> · {INCIDENT_STATE[t.latest_incident_state]?.label.toLowerCase()}</span>}
                      </p>
                    </div>
                    <TaskActions id={t.id} projectId={p.id} />
                  </li>
                ))}
              </ul>
            )}
          </Section>}
        </TabsContent>

        <TabsContent value="activity" className="space-y-3 pt-4">
          <div className="flex flex-wrap items-center justify-between gap-2 text-sm text-muted-foreground">
            <span>Latest {plural(allEvents.length, "record")}{!showAll && routine.length > 0 && <> · {plural(routine.length, "routine session record")} hidden</>}</span>
            {routine.length > 0 && <Link href={showAll ? `/projects/${p.id}` : `/projects/${p.id}?all=1`} className="text-foreground underline-offset-4 hover:underline">{showAll ? "Hide routine records" : "Show everything"}</Link>}
          </div>
          {shown.length === 0 ? (
            <Empty>{routine.length ? "Only routine session records so far. Progress reports, incidents, and your changes appear here." : "No activity recorded yet."}</Empty>
          ) : (
            <ol className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
              {shown.map((e) => (
                <li key={e.id} className="grid grid-cols-[6.5rem_minmax(0,1fr)] gap-4 px-4 py-2.5 text-sm">
                  <TimeAgo value={e.observed_at} className="text-muted-foreground" />
                  <div className="min-w-0">
                    <span className="font-medium">{eventName(e.event_type)}</span>
                    <span className="text-muted-foreground"> · {sourceName(e.source)}{e.confidence !== "high" && ` · ${e.confidence} confidence`}</span>
                    {e.details?.summary && e.details.summary !== eventName(e.event_type) && <p className="truncate text-muted-foreground">{e.details.summary}</p>}
                  </div>
                </li>
              ))}
            </ol>
          )}
        </TabsContent>

        <TabsContent value="incidents" className="space-y-8 pt-4">
          {[["Open", openGroups], ["Closed", closedGroups]].map(([title, groups]: any) => (
            <Section key={title} title={title} meta={groups.length || undefined}>
              {groups.length === 0 ? <Empty>{title === "Open" ? "No open incidents." : "No closed incidents."}</Empty> : (
                <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
                  {groups.map(({ latest, repeats }: any) => {
                    const s = INCIDENT_STATE[latest.state] ?? { tone: "idle", label: latest.state }
                    return (
                      <li key={latest.id} className="grid gap-3 px-4 py-3 md:grid-cols-[minmax(0,1fr)_auto_auto] md:items-center md:gap-6">
                        <div className="min-w-0">
                          <Status tone={s.tone} label={s.label} />
                          <p className="text-sm text-muted-foreground">{latest.task_title ? <><span className="text-foreground">{latest.task_title}</span>: {latest.reason.charAt(0).toLowerCase() + latest.reason.slice(1)}</> : latest.reason}. Opened <TimeAgo value={latest.opened_at} />{repeats.length > 0 && <> · repeated {plural(repeats.length, "time")}</>}</p>
                        </div>
                        <IncidentTrail incident={latest} />
                        {title === "Open" ? <IncidentActions id={latest.id} state={latest.state} ids={[latest.id, ...repeats.filter((r: any) => OPEN_INCIDENT.has(r.state)).map((r: any) => r.id)]} /> : <span />}
                      </li>
                    )
                  })}
                </ul>
              )}
            </Section>
          ))}
        </TabsContent>

        <TabsContent value="settings" className="space-y-8 pt-4">
          <Section title="Stall notices">
            <div className="rounded-lg border border-border bg-card p-4">
              {data.controller ? (
                <ChannelForm controllerId={data.controller.id} projectId={p.id} channel={channel} />
              ) : (
                <p className="text-sm text-muted-foreground">No controller is registered, so stalls escalate straight to you as a {CHANNELS.mac_alert.label.toLowerCase()}.</p>
              )}
            </div>
          </Section>
          <Section title="Folders" meta={p.roots?.length || undefined}>
            {!p.roots?.length ? (
              <Empty>No folders are linked to this project.</Empty>
            ) : (
              <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
                {p.roots.map((r: string) => <li key={r} className="truncate px-4 py-2.5 font-mono text-[13px]">{shortPath(r)}</li>)}
              </ul>
            )}
          </Section>
          <Section title="Supervised threads" meta={data.parent_threads.length ? undefined : "none registered"}>
            {data.parent_threads.length === 0 ? (
              <Empty>No thread reports tasks to Lighthouse. Stall alerts need an agent that registers tasks through MCP.</Empty>
            ) : (
              <div className="overflow-hidden rounded-lg border border-border bg-card">
                <Table>
                  <TableHeader>
                    <TableRow className="hover:bg-transparent">
                      <TableHead>Thread</TableHead>
                      {SIGNALS.map(([k, label, hint]) => (
                        <TableHead key={k} className="hidden md:table-cell">
                          <Tooltip><TooltipTrigger className="cursor-help underline decoration-dotted underline-offset-4">{label}</TooltipTrigger><TooltipContent>{hint}</TooltipContent></Tooltip>
                        </TableHead>
                      ))}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.parent_threads.map((t: any) => (
                      <TableRow key={t.id}>
                        <TableCell>
                          <div className="flex items-center gap-2">
                            <StatusDot tone={t.state === "running" ? "ok" : t.state === "blocked" ? "warn" : "idle"} />
                            <span className="font-medium">{sourceName(t.source)}</span>
                            <Tooltip><TooltipTrigger className="font-mono text-xs text-muted-foreground">{t.external_thread_id.slice(0, 8)}</TooltipTrigger><TooltipContent className="font-mono text-xs">{t.external_thread_id}</TooltipContent></Tooltip>
                          </div>
                        </TableCell>
                        {SIGNALS.map(([k]) => <TableCell key={k} className="hidden text-sm md:table-cell"><TimeAgo value={t[k]} /></TableCell>)}
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </Section>

          <Section title="Linked sources" meta={data.sources?.length || undefined}>
            {!data.sources?.length ? (
              <Empty>No manual links. Folders above are matched automatically.</Empty>
            ) : (
              <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
                {data.sources.map((s: any) => (
                  <li key={s.id} className="flex flex-wrap items-center justify-between gap-3 px-4 py-2.5 text-sm">
                    <span className="truncate font-mono text-[13px]">{s.match_kind === "working_directory" ? shortPath(s.match_value) : s.match_value}</span>
                    <span className="flex items-center gap-3"><SourceChips sources={[s.source]} /><Badge variant="outline" className="font-normal text-muted-foreground">{s.match_kind === "working_directory" ? "folder" : "thread"}</Badge></span>
                  </li>
                ))}
              </ul>
            )}
          </Section>
        </TabsContent>
      </Tabs>
    </div>
  )
}
