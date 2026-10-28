"use client"

import { ActionButton } from "@/components/action-button"
import { closeAllIncidents } from "@/app/actions"

export function CloseAll({ ids }: { ids: string[] }) {
  return (
    <ActionButton size="sm" variant="outline" action={() => closeAllIncidents(ids)} confirm={`Close all ${ids.length} open incidents? Use this after a drill or once you have handled them.`}>
      Close all {ids.length}
    </ActionButton>
  )
}
