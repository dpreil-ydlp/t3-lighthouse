const assert = require("node:assert/strict")
const fs = require("node:fs")
const Module = require("node:module")
const path = require("node:path")
const React = require("react")
const { renderToStaticMarkup } = require("react-dom/server")
const ts = require("typescript")

require.extensions[".tsx"] = (module, filename) => {
  const source = fs.readFileSync(filename, "utf8")
  const output = ts.transpileModule(source, {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText
  module._compile(output, filename)
}

const originalResolve = Module._resolveFilename
Module._resolveFilename = function (request, parent, isMain, options) {
  if (request.startsWith("@/components/project-brief")) return path.join(__dirname, "../components/project-brief.tsx")
  if (request.startsWith("@/")) return `test-mock:${request}`
  return originalResolve.call(this, request, parent, isMain, options)
}

const icon = (name) => (props) => React.createElement("span", { ...props, "data-icon": name })
const originalLoad = Module._load
Module._load = function (request, parent, isMain) {
  if (request.startsWith("@/")) {
    const name = request
    if (name === "@/components/project-brief") return originalLoad.call(this, request, parent, isMain)
    if (name === "@/components/page-header") return { Empty: ({ children }) => React.createElement("div", null, children), PageHeader: ({ title, description, actions }) => React.createElement("header", null, title, description, actions) }
    if (name === "@/components/new-project-dialog") return { NewProjectDialog: () => React.createElement("button", null, "New project") }
    if (name === "@/components/time-ago") return { TimeAgo: ({ value, empty }) => React.createElement("span", null, value ? "10 minutes" : empty || "unknown") }
    if (name === "@/components/status") return {
      Status: ({ label, children, pulse, ...props }) => React.createElement("span", props, label || children),
      StatusDot: () => React.createElement("span", null),
    }
    if (name === "@/components/ui/table") return Object.fromEntries(Object.entries({ Table: "table", TableBody: "tbody", TableCell: "td", TableHead: "th", TableHeader: "thead", TableRow: "tr" }).map(([name, tag]) => [name, ({ children, ...props }) => React.createElement(tag, props, children)]))
    if (name === "@/lib/data") return { loadProjects: async () => [project(2.5)], PROJECT_STATUS: {} }
    if (name === "@/lib/format") return { plural: (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`, sourceName: (value) => value }
    if (name === "@/lib/status") return { ACTIVITY: { working_now: { tone: "ok", label: "Working now", pulse: true }, quiet: { tone: "idle", label: "Quiet" } }, PROJECT_STATUS: {} }
    if (name === "@/lib/utils") return { cn: (...values) => values.filter(Boolean).join(" ") }
    throw new Error(`Unexpected dashboard import: ${name}`)
  }
  if (request === "next/link") return {
    __esModule: true,
    default: ({ href, children, ...props }) => React.createElement("a", { href, ...props }, children),
  }
  if (request === "lucide-react") return new Proxy({}, { get: (_target, key) => icon(String(key)) })
  return originalLoad.call(this, request, parent, isMain)
}

const { ProjectCard, ProjectRow } = require("../components/project-board.tsx")
const { BriefPanel } = require("../components/project-brief.tsx")
const { default: Projects } = require("../app/projects/page.tsx")

const summary = (agent_hours) => ({
  now: "The agents check the release.",
  needs_you: { action: "Review the release.", count: 1 },
  agent_hours,
  state: "waiting_on_you",
  checked_at: "2026-10-26T10:00:00Z",
})
const project = (agent_hours) => ({
  id: "project-1",
  name: "Test project",
  activity_state: "working_now",
  last_activity_at: "2026-10-26T09:50:00Z",
  tools: [],
  open_incidents: 0,
  recent_threads: [],
  brief: summary(agent_hours),
})
const render = (element) => renderToStaticMarkup(element)

for (const Component of [ProjectCard, ProjectRow]) {
  for (const hours of [null, 0]) {
    const html = render(React.createElement(Component, { p: project(hours) }))
    assert.doesNotMatch(html, /estimate unknown|0 h agent work/)
    assert.match(html, /Needs you: Review the release\./)
  }
  const known = render(React.createElement(Component, { p: project(2.5) }))
  assert.match(known, /~2\.5 h agent work/)
  assert.match(known, /Waiting on you/)
  assert.match(known, /checked /)
  assert.match(known, /10 minutes/)
}

const detail = render(React.createElement(BriefPanel, {
  brief: {
    ...summary(null),
    about: "A test project.",
    done: ["The agents finished the settings screen."],
    left: [{ text: "Review the release.", who: "you" }],
    eta_basis: "The brief has no agent estimate.",
    activity_through: "2026-10-26T09:00:00Z",
    last_activity_at: "2026-10-26T09:50:00Z",
    paused_since: null,
  },
}))
assert.match(detail, /estimate unknown/)
assert.match(detail, /Waiting on you/)
assert.match(detail, /Checked /)
assert.match(detail, /10 minutes/)

async function checkProjectsTable() {
  const table = render(await Projects({ searchParams: {} }))
  assert.match(table, /<th[^>]*>Needs you<\/th>/)
  assert.match(table, /<td[^>]*><span[^>]*title="Review the release\."[^>]*>Review the release\./)
  assert.doesNotMatch(table, /Needs you: Review the release/)
  assert.match(table, /<td class="hidden lg:table-cell"><div class="text-xs text-muted-foreground">~2\.5 h<\/div><\/td>/)
  assert.match(table, /checked <span>10 minutes<\/span>/)
  assert.match(table, /whitespace-nowrap[^>]*>Waiting on you<\/span>/)
  console.log("dashboard brief render checks passed")
}

checkProjectsTable().catch((error) => { console.error(error); process.exitCode = 1 })
