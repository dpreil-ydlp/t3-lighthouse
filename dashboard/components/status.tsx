import { cn } from "@/lib/utils"
import type { Tone } from "@/lib/status"

const DOT: Record<Tone, string> = {
  alert: "bg-state-alert shadow-[0_0_0_3px_hsl(var(--state-alert)/0.18)]",
  warn: "bg-state-warn shadow-[0_0_0_3px_hsl(var(--state-warn)/0.16)]",
  ok: "bg-state-ok",
  idle: "bg-state-idle",
}
const TEXT: Record<Tone, string> = {
  alert: "text-state-alert", warn: "text-state-warn", ok: "text-state-ok", idle: "text-muted-foreground",
}

export function StatusDot({ tone, pulse, className }: { tone: Tone; pulse?: boolean; className?: string }) {
  return <span aria-hidden className={cn("inline-block size-2 shrink-0 rounded-full", DOT[tone], pulse && "animate-pulse-dot", className)} />
}

export function Status({ tone, label, pulse, className }: { tone: Tone; label: string; pulse?: boolean; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-2 whitespace-nowrap text-[13px] font-medium", TEXT[tone], className)}>
      <StatusDot tone={tone} pulse={pulse} />
      {label}
    </span>
  )
}
