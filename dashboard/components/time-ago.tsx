import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { ago, utc } from "@/lib/format"
import { cn } from "@/lib/utils"

export function TimeAgo({ value, className, empty = "—" }: { value?: string | null; className?: string; empty?: string }) {
  if (!value) return <span className={cn("text-muted-foreground", className)}>{empty}</span>
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <time dateTime={value} className={cn("tabular cursor-default whitespace-nowrap", className)}>{ago(value)}</time>
      </TooltipTrigger>
      <TooltipContent>{utc(value)}</TooltipContent>
    </Tooltip>
  )
}
