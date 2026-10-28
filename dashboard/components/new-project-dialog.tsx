"use client"

import { useState, useTransition } from "react"
import { Plus } from "lucide-react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { createProject } from "@/app/actions"

export function NewProjectDialog() {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState("")
  const [next, setNext] = useState("")
  const [pending, start] = useTransition()
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button size="sm"><Plus className="size-4" />New project</Button></DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>New project</DialogTitle>
          <DialogDescription>For any work, code or not. Link folders to it later from Other activity.</DialogDescription>
        </DialogHeader>
        <form className="space-y-4" onSubmit={(e) => {
          e.preventDefault()
          start(async () => {
            const r = await createProject(name, next)
            if (r.ok) { toast.success(r.message); setOpen(false); setName(""); setNext("") } else toast.error(r.message)
          })
        }}>
          <div className="space-y-2">
            <Label htmlFor="np-name">Name</Label>
            <Input id="np-name" value={name} onChange={(e) => setName(e.target.value)} required autoFocus maxLength={200} />
          </div>
          <div className="space-y-2">
            <Label htmlFor="np-next">Next step</Label>
            <Input id="np-next" value={next} onChange={(e) => setNext(e.target.value)} placeholder="What has to happen next?" maxLength={500} />
          </div>
          <DialogFooter><Button type="submit" disabled={pending || !name.trim()}>{pending ? "Creating…" : "Create project"}</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
