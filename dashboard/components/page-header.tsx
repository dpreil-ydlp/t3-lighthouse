import type { ReactNode } from "react"

export function PageHeader({ title, description, actions }: { title: ReactNode; description?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="flex flex-wrap items-end justify-between gap-4 pb-6">
      <div className="min-w-0 space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="text-sm text-muted-foreground">{description}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </header>
  )
}

export function Section({ title, meta, children, actions }: { title: string; meta?: ReactNode; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="space-y-3">
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-[13px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
          {title}{meta != null && <span className="ml-2 font-normal normal-case tracking-normal">{meta}</span>}
        </h2>
        {actions}
      </div>
      {children}
    </section>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-lg border border-dashed border-border px-6 py-10 text-center text-sm text-muted-foreground">{children}</div>
}
