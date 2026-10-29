// The manual install steps (docs/install-by-hand.md) create the session key with `openssl rand -hex 32 > file`, which ends in a newline.
// dashboard-entrypoint.sh exports the key through $(cat), which drops it. The server and the
// proxy must still agree on the signature, or every login redirects back to /login.
const assert = require("node:assert/strict")
const fs = require("node:fs")
const os = require("node:os")
const path = require("node:path")
const ts = require("typescript")

function load(file) {
  const source = fs.readFileSync(path.join(__dirname, "..", file), "utf8")
  const output = ts.transpileModule(source, {
    compilerOptions: { esModuleInterop: true, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const module = { exports: {} }
  new Function("module", "exports", "require", output)(module, module.exports, require)
  return module.exports
}

async function main() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "lighthouse-session-key-"))
  const keyFile = path.join(dir, "dashboard-session-key")
  const key = "test-only-session-signing-key-32".repeat(2)
  fs.writeFileSync(keyFile, key + "\n")
  process.env.DASHBOARD_SESSION_KEY_FILE = keyFile
  // What dashboard-entrypoint.sh exports: export DASHBOARD_SESSION_KEY="$(cat "$key_file")"
  process.env.DASHBOARD_SESSION_KEY = key
  try {
    const core = load("lib/session-core.ts")
    const edge = load("lib/session-edge.ts")
    const value = core.issueSession("owner")
    assert.ok(core.verifySession(value), "server rejects its own session")
    assert.equal(await edge.validEdgeSession(value), true, "proxy rejects a session the server issued")
    fs.writeFileSync(keyFile, "short\n")
    assert.throws(() => core.sessionKey(), /at least 32 bytes/)
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
  console.log("session key regression passed")
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
