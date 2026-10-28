import { Check } from "lucide-react"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { ago, utc } from "@/lib/format"
import { cn } from "@/lib/utils"

const STEPS = [
  ["delivered_at", "Delivered", "The notice reached its channel"],
  ["acknowledged_at", "Acknowledged", "The controller thread or you replied"],
  ["resumed_progress_at", "Resumed", "Real progress was observed afterwards"],
] as const

/** Three separate facts, never inferred from each other. */
export function IncidentTrail({ incident }: { incident: Record<string, any> }) {
  return (
    <ol className="flex items-center gap-1.5" aria-label="Notice trail">
      {STEPS.map(([key, label, hint], i) => {
        const at = incident[key] as string | null
        return (
          <li key={key} className="flex items-center gap-1.5">
            {i > 0 && <span aria-hidden className={cn("h-px w-3", at ? "bg-state-ok/60" : "bg-border")} />}
            <Tooltip>
              <TooltipTrigger asChild>
                <span className={cn(
                  "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium",
                  at ? "border-state-ok/40 text-state-ok" : "border-border text-muted-foreground",
                )}>
                  {at && <Check className="size-3" aria-hidden />}
                  {label}
                </span>
              </TooltipTrigger>
              <TooltipContent>{at ? `${ago(at)} · ${utc(at)}` : `Not yet. ${hint}.`}</TooltipContent>
            </Tooltip>
          </li>
        )
      })}
    </ol>
  )
}
