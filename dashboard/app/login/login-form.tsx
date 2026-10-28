"use client"

import { useActionState } from "react"
import { useFormStatus } from "react-dom"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { login, type LoginState } from "../auth-actions"

const initialState: LoginState = {}

function Submit() {
  const { pending } = useFormStatus()
  return <Button type="submit" className="w-full" disabled={pending}>{pending ? "Checking…" : "Log in"}</Button>
}

export default function LoginForm() {
  const [state, action] = useActionState(login, initialState)
  return (
    <form action={action} className="space-y-4">
      <div className="space-y-2">
        <Label htmlFor="owner-token">Owner token</Label>
        <Input id="owner-token" name="token" type="password" autoComplete="off" required autoFocus className="font-mono" />
      </div>
      {state.error && <p role="alert" className="text-sm text-state-alert">{state.error}</p>}
      <Submit />
    </form>
  )
}
