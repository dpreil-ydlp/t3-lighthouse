import { redirect } from "next/navigation"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { currentSession } from "@/lib/session"
import LoginForm from "./login-form"

export const dynamic = "force-dynamic"
export const metadata = { title: "Log in" }

export default async function LoginPage() {
  if (await currentSession()) redirect("/")
  return (
    <div className="flex min-h-[70vh] items-center justify-center">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="text-lg">Log in to Lighthouse</CardTitle>
          <CardDescription>
            Paste your owner token. It is the owner-token file in the server&apos;s Lighthouse secrets directory:
          </CardDescription>
          <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-all rounded-md bg-muted p-2.5 font-mono text-[11px] leading-relaxed text-muted-foreground">cat &quot;$LIGHTHOUSE_SECRETS_DIR/owner-token&quot;</pre>
        </CardHeader>
        <CardContent><LoginForm /></CardContent>
      </Card>
    </div>
  )
}
