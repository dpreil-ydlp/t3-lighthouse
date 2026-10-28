"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"
import { cn } from "@/lib/utils"

type Item = { href: string; label: string; count?: number; tone?: "alert" | "neutral" }

export function Nav({ items }: { items: Item[] }) {
  const path = usePathname()
  return (
    <nav className="flex items-center gap-1" aria-label="Main">
      {items.map((item) => {
        const active = item.href === "/" ? path === "/" : path.startsWith(item.href)
        return (
          <Link
            key={item.href}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "inline-flex h-8 items-center gap-2 rounded-md px-3 text-sm transition-colors",
              active ? "bg-secondary text-foreground" : "text-muted-foreground hover:bg-secondary/60 hover:text-foreground",
            )}
          >
            {item.label}
            {!!item.count && (
              <span className={cn(
                "tabular rounded-full px-1.5 text-[11px] font-semibold leading-5",
                item.tone === "alert" ? "bg-state-alert/15 text-state-alert" : "bg-muted text-muted-foreground",
              )}>{item.count > 99 ? "99+" : item.count}</span>
            )}
          </Link>
        )
      })}
    </nav>
  )
}
