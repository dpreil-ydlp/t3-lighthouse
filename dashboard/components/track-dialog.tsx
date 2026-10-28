"use client"

import { useState, useTransition } from "react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { addFolderToProject, trackFolder } from "@/app/actions"
import type { SessionGroup } from "@/lib/sessions"

type GroupRef = Pick<SessionGroup, "source" | "working_directory" | "external_thread_id" | "observation_id">

export function TrackDialog({
  groups, label, suggested, projects, size = "sm", variant = "default",
}: {
  groups: GroupRef[]; label: string; suggested: string; projects: { id: string; name: string }[]
  size?: "sm" | "default"; variant?: "default" | "outline" | "secondary"
}) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState(suggested)
  const [projectId, setProjectId] = useState("")
  const [pending, start] = useTransition()

  const run = (fn: () => Promise<{ ok: boolean; message?: string }>) =>
    start(async () => {
      const r = await fn()
      if (r.ok) { toast.success(r.message ?? "Saved"); setOpen(false) } else toast.error(r.message ?? "Something went wrong")
    })

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size={size} variant={variant}>Track</Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Track this work</DialogTitle>
          <DialogDescription className="break-all font-mono text-xs">{label}</DialogDescription>
        </DialogHeader>
        <Tabs defaultValue="new">
          <TabsList className="grid w-full grid-cols-2">
            <TabsTrigger value="new">New project</TabsTrigger>
            <TabsTrigger value="existing" disabled={!projects.length}>Add to existing</TabsTrigger>
          </TabsList>
          <TabsContent value="new" className="space-y-4 pt-2">
            <form
              className="space-y-4"
              onSubmit={(e) => { e.preventDefault(); run(() => trackFolder(groups, name)) }}
            >
              <div className="space-y-2">
                <Label htmlFor="project-name">Project name</Label>
                <Input id="project-name" value={name} onChange={(e) => setName(e.target.value)} autoFocus required maxLength={200} />
              </div>
              <DialogFooter>
                <Button type="submit" disabled={pending || !name.trim()}>{pending ? "Creating…" : "Create and track"}</Button>
              </DialogFooter>
            </form>
          </TabsContent>
          <TabsContent value="existing" className="space-y-4 pt-2">
            <div className="space-y-2">
              <Label>Project</Label>
              <Select value={projectId} onValueChange={setProjectId}>
                <SelectTrigger><SelectValue placeholder="Choose a project" /></SelectTrigger>
                <SelectContent>
                  {projects.map((p) => <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <DialogFooter>
              <Button disabled={pending || !projectId} onClick={() => run(() => addFolderToProject(groups, projectId))}>
                {pending ? "Linking…" : "Link folder"}
              </Button>
            </DialogFooter>
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  )
}
