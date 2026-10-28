import Link from "next/link"
import { Empty, PageHeader } from "@/components/page-header"
import { NewProjectDialog } from "@/components/new-project-dialog"
import { Status } from "@/components/status"
import { BriefStatus } from "@/components/project-brief"
import { TimeAgo } from "@/components/time-ago"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { loadProjects, type Project, type ProjectView } from "@/lib/data"
import { ACTIVITY, PROJECT_STATUS } from "@/lib/status"
import { cn } from "@/lib/utils"

export const dynamic = "force-dynamic"
export const metadata = { title: "Projects" }

const FILTERS: [string, string, ProjectView, (p: Project) => boolean][] = [
  ["active", "Active", "active", (p) => p.activity_state !== "quiet"],
  ["quiet", "Quiet", "active", (p) => p.activity_state === "quiet"],
  ["hidden", "Hidden", "hidden", () => true],
  ["archived", "Archived", "archived", () => true],
]

export default async function Projects(props: { searchParams: Promise<{ show?: string }> }) {
  const searchParams = await props.searchParams
  const show = FILTERS.find(([k]) => k === searchParams.show) ?? FILTERS[0]
  const [projects, hidden, archived] = await Promise.all([loadProjects("active"), loadProjects("hidden"), loadProjects("archived")])
  if (!projects || !hidden || !archived) return <Empty>Lighthouse could not reach its API.</Empty>
  const source = { active: projects, hidden, archived, all: projects }[show[2]]
  const rows = source.filter(show[3])
  const count = (f: (typeof FILTERS)[number]) => ({ active: projects, hidden, archived, all: projects }[f[2]]).filter(f[3]).length

  return (
    <>
      <PageHeader
        title="Projects"
        description="Found automatically from your T3 projects and ~/Projects folders. Hide or merge any that aren't real projects."
        actions={<NewProjectDialog />}
      />
      <div className="mb-4 flex gap-1" role="tablist" aria-label="Filter projects">
        {FILTERS.map((f) => (
          <Link key={f[0]} href={f[0] === "active" ? "/projects" : `/projects?show=${f[0]}`} role="tab" aria-selected={show[0] === f[0]}
            className={cn("rounded-md px-3 py-1.5 text-sm transition-colors", show[0] === f[0] ? "bg-secondary text-foreground" : "text-muted-foreground hover:text-foreground")}>
            {f[1]} <span className="tabular text-xs text-muted-foreground">{count(f)}</span>
          </Link>
        ))}
      </div>
      {rows.length === 0 ? (
        <Empty>{show[0] === "hidden" ? "No hidden projects." : show[0] === "archived" ? "No archived projects." : "No projects here."}</Empty>
      ) : (
        <div className="overflow-hidden rounded-lg border border-border bg-card">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Project</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Needs you</TableHead>
                <TableHead className="hidden lg:table-cell">Agent work</TableHead>
                <TableHead className="hidden md:table-cell">Last activity</TableHead>
                <TableHead className="hidden text-right sm:table-cell">Alerts</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((p) => {
                const a = p.status === "active" ? ACTIVITY[p.activity_state] ?? ACTIVITY.quiet : { tone: "idle" as const, label: PROJECT_STATUS[p.status] ?? p.status }
                const latest = p.brief?.now || p.recent_threads?.find((t) => t.title)?.title
                return (
                  <TableRow key={p.id} className="group relative">
                    <TableCell className="max-w-[36rem] whitespace-normal py-3">
                      <Link href={`/projects/${p.id}`} className="block truncate font-medium after:absolute after:inset-0 group-hover:underline">{p.name}</Link>
                      <p className="text-sm leading-snug text-muted-foreground">
                        {latest ?? ""}
                      </p>
                    </TableCell>
                    <TableCell>{p.brief ? <><span className="text-xs text-muted-foreground"><BriefStatus brief={p.brief} className="whitespace-nowrap" /></span>{p.brief.checked_at && <span className="mt-1 block text-xs text-muted-foreground">checked <TimeAgo value={p.brief.checked_at} className="text-inherit" /></span>}</> : <Status tone={a.tone} label={a.label} pulse={"pulse" in a && a.pulse} />}</TableCell>
                    <TableCell className="max-w-[22rem]">{p.brief?.needs_you && <span className="block max-w-[22rem] truncate text-sm text-foreground" title={p.brief.needs_you.action}>{p.brief.needs_you.action}{p.brief.needs_you.count > 1 && <span className="text-muted-foreground"> · +{p.brief.needs_you.count - 1} more</span>}</span>}</TableCell>
                    <TableCell className="hidden lg:table-cell"><div className="text-xs text-muted-foreground">{p.brief?.agent_hours != null && p.brief.agent_hours > 0 ? `~${Number(p.brief.agent_hours.toFixed(1))} h` : ""}</div></TableCell>
                    <TableCell className="hidden text-sm md:table-cell"><TimeAgo value={p.last_activity_at} empty="none" /></TableCell>
                    <TableCell className={cn("tabular hidden text-right sm:table-cell", p.open_incidents ? "text-state-alert" : "text-muted-foreground")}>{p.open_incidents || "—"}</TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </div>
      )}
    </>
  )
}
