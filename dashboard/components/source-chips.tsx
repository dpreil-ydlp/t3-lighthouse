import { sourceName } from "@/lib/format"

export function SourceChips({ sources }: { sources: string[] }) {
  return (
    <span className="inline-flex flex-wrap gap-1">
      {sources.map((s) => (
        <span key={s} className="rounded border border-border bg-secondary/60 px-1.5 py-px text-[11px] font-medium text-secondary-foreground">
          {sourceName(s)}
        </span>
      ))}
    </span>
  )
}
