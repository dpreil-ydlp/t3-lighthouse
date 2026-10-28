import Link from "next/link"
import { Button } from "@/components/ui/button"
import { Empty, PageHeader } from "@/components/page-header"
import { ActionButton } from "@/components/action-button"
import { SessionsTable } from "@/components/sessions-table"
import { ignoreTemporaryFolders } from "@/app/actions"
import { loadProjects, loadSessions } from "@/lib/data"
import { foldSessions } from "@/lib/sessions"

export const dynamic = "force-dynamic"
export const metadata = { title: "Other activity" }

export default async function Sessions(props: { searchParams: Promise<{ ignored?: string; temp?: string }> }) {
  const searchParams = await props.searchParams
  const showIgnored = searchParams.ignored === "1"
  const showTemp = searchParams.temp === "1"
  const [sessions, projects] = await Promise.all([loadSessions(showIgnored), loadProjects()])
  if (!sessions || !projects) return <Empty>Lighthouse could not reach its API.</Empty>
  const all = foldSessions(sessions).filter((r) => !r.desktop && (showIgnored ? r.ignored : !r.ignored))
  const temp = all.filter((r) => r.temporary)
  const rows = showTemp || showIgnored ? all : all.filter((r) => !r.temporary)
  const query = (next: Record<string, string>) => {
    const p = new URLSearchParams({ ...(showIgnored ? { ignored: "1" } : {}), ...(showTemp ? { temp: "1" } : {}), ...next })
    for (const [k, v] of [...p.entries()]) if (v === "0") p.delete(k)
    const s = p.toString()
    return s ? `/sessions?${s}` : "/sessions"
  }

  return (
    <>
      <PageHeader
        title={showIgnored ? "Ignored folders" : "Other activity"}
        description={showIgnored
          ? "Folders hidden from Other activity. Restore one to show it again."
          : "Agent sessions outside any project folder, such as scratch chats and one-off tasks. Track one to turn it into a project."}
        actions={
          <>
            {!showIgnored && temp.length > 0 && (
              <ActionButton variant="outline" size="sm" action={ignoreTemporaryFolders}
                confirm={`Hide ${temp.length} temporary folders (T3 scratch, /tmp, system temp)? You can restore them later.`}>
                Hide {temp.length} temporary
              </ActionButton>
            )}
            <Button asChild variant="ghost" size="sm">
              <Link href={showIgnored ? "/sessions" : query({ ignored: "1" })}>{showIgnored ? "Back" : "Ignored"}</Link>
            </Button>
          </>
        }
      />
      {!showIgnored && temp.length > 0 && (
        <p className="-mt-3 mb-4 text-xs text-muted-foreground">
          {showTemp ? "Showing" : "Not showing"} {temp.length} temporary folder{temp.length === 1 ? "" : "s"}.{" "}
          <Link href={query({ temp: showTemp ? "0" : "1" })} className="text-foreground underline-offset-4 hover:underline">{showTemp ? "Hide them" : "Show them"}</Link>
        </p>
      )}
      <SessionsTable rows={rows} projects={projects.map((p) => ({ id: p.id, name: p.name }))} showingIgnored={showIgnored} />
    </>
  )
}
