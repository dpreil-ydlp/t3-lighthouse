import "server-only"
import fs from "node:fs"

export const apiBase = process.env.LIGHTHOUSE_API_URL || "http://api:8000"
const base = apiBase

function authHeader(): Record<string, string> {
  const file = process.env.OWNER_TOKEN_FILE || "/run/secrets/owner-token"
  try {
    return { Authorization: `Bearer ${fs.readFileSync(/*turbopackIgnore: true*/ file, "utf8").trim()}` }
  } catch {
    return {}
  }
}

export async function apiGet<T = any>(path: string): Promise<T | null> {
  try {
    const res = await fetch(base + path, { headers: authHeader(), cache: "no-store" })
    return res.ok ? ((await res.json()) as T) : null
  } catch {
    return null
  }
}

export async function apiSend(path: string, body: unknown, method = "POST", extraHeaders: Record<string, string> = {}) {
  try {
    const res = await fetch(base + path, {
      method,
      headers: { ...authHeader(), "Content-Type": "application/json", ...extraHeaders },
      body: JSON.stringify(body),
      cache: "no-store",
    })
    let data: any = null
    try { data = await res.json() } catch { /* empty body */ }
    const detail = res.ok ? undefined : typeof data?.detail === "string" ? data.detail : undefined
    return { ok: res.ok, status: res.status, detail, data }
  } catch {
    return { ok: false, status: 0, detail: "Lighthouse API is unreachable", data: null }
  }
}
