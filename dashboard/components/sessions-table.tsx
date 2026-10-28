"use client"

import { useMemo, useState } from "react"
import { Search } from "lucide-react"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ActionButton } from "@/components/action-button"
import { SourceChips } from "@/components/source-chips"
import { TrackDialog } from "@/components/track-dialog"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { ignoreGroups } from "@/app/actions"
import { ago, plural, utc } from "@/lib/format"
import { suggestedName, type FolderRow } from "@/lib/sessions"

export function SessionsTable({ rows, projects, showingIgnored }: { rows: FolderRow[]; projects: { id: string; name: string }[]; showingIgnored: boolean }) {
  const [q, setQ] = useState("")
  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return needle ? rows.filter((r) => r.label.toLowerCase().includes(needle) || r.sources.some((s) => s.includes(needle))) : rows
  }, [q, rows])

  return (
    <div className="space-y-3">
      <div className="relative max-w-sm">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
        <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter folders" className="pl-8" aria-label="Filter folders" />
      </div>
      <div className="overflow-hidden rounded-lg border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>Folder</TableHead>
              <TableHead className="hidden md:table-cell">Tools</TableHead>
              <TableHead>Last active</TableHead>
              <TableHead className="hidden text-right lg:table-cell">Records</TableHead>
              <TableHead className="w-[1%]"><span className="sr-only">Actions</span></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filtered.map((r) => (
              <TableRow key={r.key} className={r.ignored ? "opacity-60" : undefined}>
                <TableCell className="max-w-[28rem]">
                  <Tooltip>
                    <TooltipTrigger asChild><span className="block truncate font-mono text-[13px]">{r.label}</span></TooltipTrigger>
                    <TooltipContent className="max-w-lg break-all font-mono text-xs">{r.directory ?? r.label}</TooltipContent>
                  </Tooltip>
                  {r.temporary && <span className="text-xs text-muted-foreground">temporary folder</span>}
                </TableCell>
                <TableCell className="hidden md:table-cell"><SourceChips sources={r.sources} /></TableCell>
                <TableCell>
                  <Tooltip>
                    <TooltipTrigger asChild><time dateTime={r.last_seen} className="tabular whitespace-nowrap text-sm">{ago(r.last_seen)}</time></TooltipTrigger>
                    <TooltipContent>{utc(r.last_seen)}</TooltipContent>
                  </Tooltip>
                </TableCell>
                <TableCell className="tabular hidden text-right text-sm text-muted-foreground lg:table-cell">{r.observations.toLocaleString("en-US")}</TableCell>
                <TableCell>
                  <div className="flex justify-end gap-2">
                    {r.ignored ? (
                      <ActionButton size="sm" variant="ghost" action={() => ignoreGroups(r.groups, true)}>Restore</ActionButton>
                    ) : (
                      <>
                        <TrackDialog groups={r.groups} label={r.directory ?? r.label} suggested={suggestedName(r.directory)} projects={projects} variant="outline" />
                        <ActionButton size="sm" variant="ghost" className="text-muted-foreground" action={() => ignoreGroups(r.groups)}>Ignore</ActionButton>
                      </>
                    )}
                  </div>
                </TableCell>
              </TableRow>
            ))}
            {filtered.length === 0 && (
              <TableRow className="hover:bg-transparent">
                <TableCell colSpan={5} className="py-10 text-center text-sm text-muted-foreground">
                  {q ? "No folder matches this filter." : showingIgnored ? "Nothing is ignored." : "Every folder with agent activity is linked to a project."}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>
      <p className="text-xs text-muted-foreground">{plural(filtered.length, "folder")} shown</p>
    </div>
  )
}
