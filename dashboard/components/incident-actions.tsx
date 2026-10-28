"use client"

import { ActionButton } from "@/components/action-button"
import { acknowledgeIncident, closeAllIncidents } from "@/app/actions"

const ACKABLE = new Set(["notifying", "awaiting_ack", "recovering"])

export function IncidentActions({ id, state, ids }: { id: string; state: string; ids: string[] }) {
  return (
    <div className="flex items-center justify-end gap-2">
      {ACKABLE.has(state) && state !== "recovering" && (
        <ActionButton size="sm" variant="secondary" action={() => acknowledgeIncident(id)}>Acknowledge</ActionButton>
      )}
      <ActionButton size="sm" variant="ghost" action={() => closeAllIncidents(ids)}
        confirm={ids.length > 1 ? `Close ${ids.length} open incidents, including earlier repeats?` : undefined}>
        {ids.length > 1 ? `Close ${ids.length}` : "Close"}
      </ActionButton>
    </div>
  )
}
