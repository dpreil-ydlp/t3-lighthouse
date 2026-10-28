"use client"

import { useState, useTransition } from "react"
import { useRouter } from "next/navigation"
import { ChevronDown } from "lucide-react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { closeTask, mergeProject, setChannel, updateProject } from "@/app/actions"
import { CHANNELS, PROJECT_STATUS } from "@/lib/status"

function useAction() {
  const [pending, start] = useTransition()
  const run = (fn: () => Promise<{ ok: boolean; message?: string }>, after?: () => void) =>
    start(async () => {
      const r = await fn()
      if (r.ok) { toast.success(r.message ?? "Saved"); after?.() } else toast.error(r.message ?? "Something went wrong")
    })
  return [pending, run] as const
}

export function ProjectMenu({ id, name, status, hidden, projects }: { id: string; name: string; status: string; hidden: boolean; projects: { id: string; name: string }[] }) {
  const [pending, run] = useAction()
  const router = useRouter()
  const [dialog, setDialog] = useState<null | "rename" | "merge">(null)
  const [newName, setNewName] = useState(name)
  const [into, setInto] = useState("")
  const statuses = Object.entries(PROJECT_STATUS).filter(([k]) => k !== status && k !== "blocked")
  const others = projects.filter((p) => p.id !== id)
  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="outline" size="sm" disabled={pending}>Manage<ChevronDown className="size-3.5" /></Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-52">
          <DropdownMenuItem onSelect={() => { setNewName(name); setDialog("rename") }}>Rename</DropdownMenuItem>
          <DropdownMenuItem onSelect={() => setDialog("merge")} disabled={!others.length}>Merge into another project</DropdownMenuItem>
          <DropdownMenuItem onSelect={() => run(() => updateProject(id, { hidden: !hidden }))}>{hidden ? "Show in Lighthouse" : "Hide — not a real project"}</DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">Status: {PROJECT_STATUS[status] ?? status}</DropdownMenuLabel>
          {statuses.map(([key, label]) => (
            <DropdownMenuItem key={key} onSelect={() => run(() => updateProject(id, { status: key }))}>
              {key === "active" ? "Resume (active)" : `Mark ${label.toLowerCase()}`}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog open={dialog === "rename"} onOpenChange={(o) => setDialog(o ? "rename" : null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Rename project</DialogTitle>
            <DialogDescription>Lighthouse keeps this name even when it rediscovers the folder.</DialogDescription>
          </DialogHeader>
          <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); run(() => updateProject(id, { name: newName.trim() }), () => setDialog(null)) }}>
            <Input value={newName} onChange={(e) => setNewName(e.target.value)} maxLength={200} autoFocus aria-label="Project name" />
            <DialogFooter><Button type="submit" disabled={pending || !newName.trim() || newName.trim() === name}>Save name</Button></DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={dialog === "merge"} onOpenChange={(o) => setDialog(o ? "merge" : null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Merge “{name}”</DialogTitle>
            <DialogDescription>Its folders, sessions and history move to the project you choose. “{name}” is then archived.</DialogDescription>
          </DialogHeader>
          <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); run(() => mergeProject(id, into), () => { setDialog(null); router.push(`/projects/${into}`) }) }}>
            <Select value={into} onValueChange={setInto}>
              <SelectTrigger aria-label="Merge into"><SelectValue placeholder="Choose a project" /></SelectTrigger>
              <SelectContent>{others.map((p) => <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>)}</SelectContent>
            </Select>
            <DialogFooter><Button type="submit" disabled={pending || !into}>Merge</Button></DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  )
}

export function TaskActions({ id, projectId }: { id: string; projectId: string }) {
  const [pending, run] = useAction()
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild><Button size="sm" variant="ghost" disabled={pending} className="text-muted-foreground">Close<ChevronDown className="size-3.5" /></Button></DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onSelect={() => run(() => closeTask(id, projectId, "complete"))}>Mark complete</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => run(() => closeTask(id, projectId, "cancelled"))}>Cancel task</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export function ChannelForm({ controllerId, projectId, channel }: { controllerId: string; projectId: string; channel: string }) {
  const [value, setValue] = useState(channel)
  const [url, setUrl] = useState("")
  const [pending, run] = useAction()
  const dirty = value !== channel || (value === "t3_webhook" && url.trim())
  return (
    <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); run(() => setChannel(controllerId, projectId, value, url), () => setUrl("")) }}>
      <div className="space-y-2">
        <Label>When this project stalls, tell</Label>
        <Select value={value} onValueChange={setValue}>
          <SelectTrigger className="max-w-sm"><SelectValue /></SelectTrigger>
          <SelectContent>
            {Object.entries(CHANNELS).map(([k, c]) => <SelectItem key={k} value={k}>{c.label}</SelectItem>)}
          </SelectContent>
        </Select>
        <p className="text-sm text-muted-foreground">{CHANNELS[value]?.hint}</p>
      </div>
      {value === "t3_webhook" && (
        <div className="space-y-2">
          <Label htmlFor="t3-url">T3 webhook URL</Label>
          <Input id="t3-url" type="url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder={channel === "t3_webhook" ? "Saved. Paste a new URL to replace it." : "https://…"} className="max-w-xl font-mono text-xs" />
          <p className="text-xs text-muted-foreground">Stored on the server only. It is never shown again.</p>
        </div>
      )}
      <Button type="submit" size="sm" disabled={pending || !dirty || (value === "t3_webhook" && channel !== "t3_webhook" && !url.trim())}>Save channel</Button>
    </form>
  )
}
