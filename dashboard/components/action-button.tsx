"use client"

import { useTransition, type ReactNode } from "react"
import { toast } from "sonner"
import { Button, type ButtonProps } from "@/components/ui/button"
import type { ActionResult } from "@/app/actions"

/** Runs a server action and reports the outcome as a toast. */
export function ActionButton({
  action, children, success, confirm, ...props
}: { action: () => Promise<ActionResult>; children: ReactNode; success?: string; confirm?: string } & Omit<ButtonProps, "onClick">) {
  const [pending, start] = useTransition()
  return (
    <Button
      {...props}
      disabled={pending || props.disabled}
      onClick={() => {
        if (confirm && !window.confirm(confirm)) return
        start(async () => {
          const r = await action()
          if (r.ok) toast.success(success ?? r.message ?? "Saved")
          else toast.error(r.message ?? "Something went wrong")
        })
      }}
    >
      {children}
    </Button>
  )
}
