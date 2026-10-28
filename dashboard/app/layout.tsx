import "./globals.css"
import type { Metadata } from "next"
import Link from "next/link"
import { GeistSans } from "geist/font/sans"
import { GeistMono } from "geist/font/mono"
import { Nav } from "@/components/nav"
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import { apiGet } from "@/lib/api"
import { OPEN_INCIDENT } from "@/lib/status"
import { currentSession } from "@/lib/session"
import { logout } from "./auth-actions"

export const metadata: Metadata = {
  title: { default: "T3 Lighthouse", template: "%s · T3 Lighthouse" },
  description: "Private project supervisor",
  icons: { icon: "/icon.svg" },
}
export const dynamic = "force-dynamic"

async function counts() {
  const incidents = await apiGet<{ items: { state: string }[] }>("/api/v1/incidents")
  return { incidents: (incidents?.items ?? []).filter((i) => OPEN_INCIDENT.has(i.state)).length }
}

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Never call the API (with owner authority) before the visitor has a valid session.
  const session = await currentSession()
  const c = session ? await counts() : null
  return (
    <html lang="en" className={`dark ${GeistSans.variable} ${GeistMono.variable}`}>
      <body className="min-h-screen font-sans">
        <TooltipProvider delayDuration={200}>
          <div className="sticky top-0 z-40 border-b border-border/70 bg-background/85 backdrop-blur">
            {/* On a phone the nav takes its own row under the name, so the page never scrolls sideways. */}
            <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-2 px-4 sm:h-14 sm:flex-nowrap sm:px-6">
              <Link href="/" className="flex h-14 shrink-0 items-center gap-2.5 text-sm font-semibold tracking-tight">
                <svg viewBox="0 0 24 24" className="size-5 text-beam" aria-hidden>
                  <path fill="currentColor" d="M10 9h4l1.2 12H8.8L10 9Z" opacity=".9" />
                  <path fill="currentColor" d="M9.2 6.5h5.6V9H9.2z" />
                  <path fill="currentColor" d="M12 2.5 10 5h4l-2-2.5Z" />
                  <path fill="currentColor" d="M15 7.2 22 5v4.4L15 8.3zM9 7.2 2 5v4.4l7-1.1z" opacity=".35" />
                </svg>
                T3 Lighthouse
              </Link>
              {c && (
                <>
                  <div className="order-last -mx-2 w-[calc(100%+1rem)] overflow-x-auto pb-2 sm:order-none sm:mx-0 sm:ml-auto sm:w-auto sm:overflow-visible sm:pb-0">
                    <Nav items={[
                      { href: "/", label: "Overview" },
                      { href: "/projects", label: "Projects" },
                      { href: "/sessions", label: "Other activity" },
                      { href: "/incidents", label: "Incidents", count: c.incidents, tone: "alert" },
                    ]} />
                  </div>
                  <form action={logout} className="ml-auto sm:order-last sm:ml-0">
                    <button type="submit" className="h-8 whitespace-nowrap rounded-md px-3 text-sm text-muted-foreground transition-colors hover:bg-secondary/60 hover:text-foreground">Log out</button>
                  </form>
                </>
              )}
            </div>
          </div>
          <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6">{children}</main>
          <Toaster position="bottom-right" />
        </TooltipProvider>
      </body>
    </html>
  )
}
