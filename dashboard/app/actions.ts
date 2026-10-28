"use server"

import { randomUUID } from "node:crypto"
import { revalidatePath } from "next/cache"
import { apiGet, apiSend } from "@/lib/api"
import { isTemporary, type SessionGroup } from "@/lib/sessions"
import { requireSession } from "@/lib/session"

export type ActionResult = { ok: boolean; message?: string }

function done(r: { ok: boolean; detail?: string }, ok: string, paths: string[]): ActionResult {
  if (r.ok) for (const p of paths) revalidatePath(p)
  return r.ok ? { ok: true, message: ok } : { ok: false, message: r.detail || "Lighthouse rejected the change" }
}

type GroupRef = Pick<SessionGroup, "source" | "working_directory" | "external_thread_id" | "observation_id">

export async function trackFolder(groups: GroupRef[], name: string): Promise<ActionResult> {
  await requireSession()
  const clean = name.trim()
  if (!clean || !groups.length) return { ok: false, message: "A project name is required" }
  const [first, ...rest] = groups
  const created = await apiSend(`/api/v1/unassigned/${first.observation_id}/create-project`, { name: clean, kind: "other" })
  if (!created.ok) return { ok: false, message: created.detail || "Could not create the project" }
  const projectId: string | undefined = created.data?.project_id
  const paths = ["/", "/sessions", "/projects"]
  if (!projectId && rest.length) {
    paths.forEach((path) => revalidatePath(path))
    return { ok: false, message: `Created “${clean}”, but Lighthouse did not return its ID. Link the remaining sources from Other activity.` }
  }
  let linked = 1
  for (const g of rest) {
    const r = await apiSend(`/api/v1/unassigned/${g.observation_id}/bind`, { project_id: projectId })
    if (!r.ok) {
      ;[...paths, `/projects/${projectId}`].forEach((path) => revalidatePath(path))
      return { ok: false, message: `Created “${clean}” and linked ${linked} of ${groups.length} sources. ${r.detail || "One source could not be linked"}. Retry the rest from Other activity with “Add to existing”.` }
    }
    linked++
  }
  return done({ ok: true }, `Tracking “${clean}”`, projectId ? [...paths, `/projects/${projectId}`] : paths)
}

export async function addFolderToProject(groups: GroupRef[], projectId: string): Promise<ActionResult> {
  await requireSession()
  if (!projectId) return { ok: false, message: "Choose a project" }
  for (const g of groups) {
    const r = await apiSend(`/api/v1/unassigned/${g.observation_id}/bind`, { project_id: projectId })
    if (!r.ok) return { ok: false, message: r.detail || "Could not link this folder" }
  }
  return done({ ok: true }, "Folder linked", ["/", "/sessions", "/projects", `/projects/${projectId}`])
}

export async function ignoreGroups(groups: GroupRef[], restore = false): Promise<ActionResult> {
  await requireSession()
  for (const g of groups) {
    const r = await apiSend(`/api/v1/unassigned/${restore ? "restore" : "ignore"}`, {
      source: g.source, working_directory: g.working_directory, external_thread_id: g.external_thread_id,
    })
    if (!r.ok) return { ok: false, message: r.detail || "Could not update this folder" }
  }
  return done({ ok: true }, restore ? "Restored" : "Hidden from Other activity", ["/", "/sessions"])
}

export async function ignoreTemporaryFolders(): Promise<ActionResult> {
  await requireSession()
  const data = await apiGet<{ items: SessionGroup[] }>("/api/v1/unassigned?show_ignored=false")
  const temp = (data?.items ?? []).filter((g) => isTemporary(g.working_directory))
  const r = await ignoreGroups(temp)
  return r.ok ? { ok: true, message: `Hid ${temp.length} temporary folder${temp.length === 1 ? "" : "s"}` } : r
}

export async function acknowledgeIncident(id: string): Promise<ActionResult> {
  await requireSession()
  const r = await apiSend(`/api/v1/incidents/${id}/ack`, { note: "Acknowledged from dashboard" }, "POST", { "Idempotency-Key": `dashboard-ack-${id}` })
  return done(r, "Acknowledged. Lighthouse now waits for real progress.", ["/", "/incidents", "/projects"])
}

export async function closeIncident(id: string): Promise<ActionResult> {
  await requireSession()
  const r = await apiSend(`/api/v1/incidents/${id}/close`, { note: "Closed from dashboard" })
  return done(r, "Incident closed", ["/", "/incidents", "/projects"])
}

export async function closeAllIncidents(ids: string[]): Promise<ActionResult> {
  await requireSession()
  for (const id of ids) {
    const r = await apiSend(`/api/v1/incidents/${id}/close`, { note: "Closed in bulk from dashboard" })
    if (!r.ok && r.status !== 409) return { ok: false, message: r.detail || "Could not close every incident" }
  }
  return done({ ok: true }, `Closed ${ids.length} incident${ids.length === 1 ? "" : "s"}`, ["/", "/incidents", "/projects"])
}

export async function updateProject(id: string, changes: { next_action?: string | null; status?: string; name?: string; description?: string; hidden?: boolean }): Promise<ActionResult> {
  await requireSession()
  const r = await apiSend(`/api/v1/projects/${id}`, changes, "PATCH")
  return done(r, "Project updated", ["/", "/projects", `/projects/${id}`])
}

export async function closeTask(id: string, projectId: string, status: "complete" | "cancelled"): Promise<ActionResult> {
  await requireSession()
  const r = await apiSend(`/api/v1/tasks/${id}`, { status }, "PATCH")
  return done(r, status === "complete" ? "Task marked complete" : "Task cancelled", ["/", "/projects", `/projects/${projectId}`])
}

export async function setChannel(controllerId: string, projectId: string, channel: string, url?: string): Promise<ActionResult> {
  await requireSession()
  const body: Record<string, string> = { channel }
  if (channel === "t3_webhook") body.t3_webhook_url = (url || "").trim()
  const r = await apiSend(`/api/v1/controllers/${controllerId}/channel`, body, "PUT")
  return done(r, "Notification channel saved", [`/projects/${projectId}`])
}

export async function createProject(name: string, nextAction: string): Promise<ActionResult & { id?: string }> {
  await requireSession()
  const clean = name.trim()
  if (!clean) return { ok: false, message: "A project name is required" }
  const r = await apiSend("/api/v1/projects", { name: clean, kind: "other", next_action: nextAction.trim() || null }, "POST", { "Idempotency-Key": `dashboard-project-${randomUUID()}` })
  return done(r, `Created “${clean}”`, ["/", "/projects"])
}

export async function mergeProject(id: string, into: string): Promise<ActionResult> {
  await requireSession()
  if (!into || into === id) return { ok: false, message: "Choose another project" }
  const r = await apiSend(`/api/v1/projects/${id}/merge`, { into }, "POST")
  return done(r, "Projects merged", ["/", "/projects", `/projects/${id}`, `/projects/${into}`])
}
