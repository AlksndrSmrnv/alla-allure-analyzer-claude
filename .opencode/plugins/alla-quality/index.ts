import { Plugin } from "@opencode/plugin"
import { execFile, spawn } from "node:child_process"
import { promises as fs } from "node:fs"
import path from "node:path"

const RULES_DIR = ".claude/rules"
const SKILL_ROOT = "qwen-skill/alla-launch"
const DOCS_TESTS = [
  `${SKILL_ROOT}/tests/test_skill_docs.py`,
  `${SKILL_ROOT}/tests/test_skill_prompts.py`,
]
const PRE_COMMIT = ".claude/hooks/pre_commit_check.sh"
const HOLDOUT_FILE = "corpus_holdout.py"

type Rule = {
  file: string
  name: string
  body: string
  matchers: RegExp[]
}

type SessionState = {
  injected: Set<string>
}

type CommandResult = {
  code: number
  output: string
}

type ContentBlock =
  | { type: "text"; text: string }
  | { type: "file"; uri: string; mime: string; name?: string }

type ResultLike = {
  content?: string | ReadonlyArray<ContentBlock>
  metadata?: Record<string, unknown>
}

type RegistrationLike = {
  dispose: () => Promise<void>
}

const sessions = new Map<string, SessionState>()
let rulesSignature = ""
let rulesCache: Rule[] = []

function toPosix(value: string): string {
  return value.replaceAll("\\", "/")
}

function relativeTo(root: string, file: string): string | null {
  const absolute = path.isAbsolute(file) ? file : path.resolve(root, file)
  const relative = path.relative(root, absolute)
  if (relative.startsWith("..") || path.isAbsolute(relative)) return null
  return toPosix(relative)
}

function absoluteTo(root: string, file: string): string | null {
  const absolute = path.isAbsolute(file) ? file : path.resolve(root, file)
  return isWithin(root, absolute) ? absolute : null
}

function isWithin(root: string, file: string): boolean {
  const relative = path.relative(root, path.resolve(root, file))
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative))
}

function globToRegExp(pattern: string): RegExp {
  const normalized = toPosix(pattern)
  let expression = ""
  for (let index = 0; index < normalized.length; index += 1) {
    const char = normalized[index]
    if (char === "*") {
      if (normalized[index + 1] === "*") {
        if (normalized[index + 2] === "/") {
          expression += "(?:.*/)?"
          index += 2
        } else {
          expression += ".*"
          index += 1
        }
      } else {
        expression += "[^/]*"
      }
      continue
    }
    if (char === "?") {
      expression += "[^/]"
      continue
    }
    if ("\\^$+?.()|{}[]".includes(char)) {
      expression += `\\${char}`
      continue
    }
    expression += char
  }
  return new RegExp(`^${expression}$`)
}

function parseRule(text: string): { body: string; paths: string[] } | null {
  if (!text.startsWith("---")) return null
  const end = text.indexOf("\n---", 3)
  if (end === -1) return null
  const header = text.slice(3, end)
  const body = text.slice(end + 4).replace(/^\s*\n/, "").trim()
  const paths: string[] = []
  let inPaths = false

  for (const raw of header.split("\n")) {
    const line = raw.trimEnd()
    if (/^paths\s*:/.test(line)) {
      inPaths = true
      const inline = line.slice(line.indexOf(":") + 1).trim()
      if (inline && inline !== "[]") {
        for (const part of inline.replace(/^\[|\]$/g, "").split(",")) {
          const value = part.trim().replace(/^["']|["']$/g, "")
          if (value) paths.push(value)
        }
      }
      continue
    }
    if (!inPaths) continue
    const item = line.match(/^\s*-\s*(.+?)\s*$/)
    if (item) {
      const value = item[1].replace(/^["']|["']$/g, "")
      if (value) paths.push(value)
      continue
    }
    if (/^\S/.test(line)) inPaths = false
  }

  return { body, paths }
}

async function loadRules(canonical: string): Promise<Rule[]> {
  const directory = path.join(canonical, RULES_DIR)
  let names: string[]
  try {
    names = (await fs.readdir(directory)).filter((name) => name.endsWith(".md")).sort()
  } catch {
    return []
  }

  const signatureParts: string[] = []
  for (const name of names) {
    try {
      const stat = await fs.stat(path.join(directory, name))
      signatureParts.push(`${name}:${stat.mtimeMs}:${stat.size}`)
    } catch {
      // A rule removed between readdir and stat is simply skipped.
    }
  }

  const signature = `${canonical}|${signatureParts.join("|")}`
  if (signature === rulesSignature) return rulesCache

  const rules: Rule[] = []
  for (const name of names) {
    try {
      const text = await fs.readFile(path.join(directory, name), "utf8")
      const parsed = parseRule(text)
      if (!parsed || parsed.paths.length === 0) continue
      rules.push({
        file: `${RULES_DIR}/${name}`,
        name: name.replace(/\.md$/, ""),
        body: parsed.body,
        matchers: parsed.paths.map(globToRegExp),
      })
    } catch {
      // A broken or unreadable rule must not break tool execution.
    }
  }

  rulesCache = rules
  rulesSignature = signature
  return rules
}

function matchingRules(rules: Rule[], relativePath: string): Rule[] {
  return rules.filter((rule) => rule.matchers.some((matcher) => matcher.test(relativePath)))
}

function formatRule(rule: Rule): string {
  return [
    `## Правило проекта: ${rule.name}`,
    "",
    `Источник: \`${rule.file}\`. Следуй ему при работе с этим файлом.`,
    "",
    rule.body,
  ].join("\n")
}

async function injectRules(
  ctx: Plugin.Context,
  sessionID: string,
  state: SessionState,
  relativePaths: string[],
  canonical: string,
): Promise<void> {
  const rules = await loadRules(canonical)
  for (const relativePath of relativePaths) {
    for (const rule of matchingRules(rules, relativePath)) {
      const key = `rule.${rule.name.replace(/[^a-z0-9._-]/g, "-")}`
      if (state.injected.has(key)) continue
      try {
        await ctx.session.instructions.entry.put({
          sessionID,
          key,
          value: formatRule(rule),
        })
        state.injected.add(key)
        if (process.env.ALLA_QUALITY_DEBUG === "1") {
          console.error(`[alla-quality] injected ${rule.file}`)
        }
      } catch (error) {
        console.error(`[alla-quality] cannot inject ${rule.file}:`, error)
      }
    }
  }
}

function pathsFromTool(tool: string, input: unknown): string[] {
  if (!input || typeof input !== "object") return []
  const value = input as Record<string, unknown>

  if (tool === "patch" && typeof value.patchText === "string") {
    const paths: string[] = []
    for (const line of value.patchText.split("\n")) {
      const file = line.match(/^\*\*\* (?:Update|Add) File:\s*(.+?)\s*$/)
      if (file) paths.push(file[1])
      const move = line.match(/^\*\*\* Move to:\s*(.+?)\s*$/)
      if (move) paths.push(move[1])
    }
    return paths
  }

  const file = value.path ?? value.filePath ?? value.file_path
  return typeof file === "string" ? [file] : []
}

function shouldCheckDocs(relativePath: string): boolean {
  if (relativePath === `${SKILL_ROOT}/SKILL.md`) return true
  if (relativePath.startsWith(`${SKILL_ROOT}/references/`)) return true
  if (relativePath === `${SKILL_ROOT}/tests/test_skill_docs.py`) return true
  if (relativePath === `${SKILL_ROOT}/tests/test_skill_prompts.py`) return true
  return relativePath.startsWith(`${SKILL_ROOT}/scripts/alla_skill_lib/`) && relativePath.endsWith(".py")
}

function tail(value: string, lines = 60): string {
  return value.trim().split("\n").slice(-lines).join("\n")
}

function runCommand(file: string, args: string[], cwd: string, timeout: number): Promise<CommandResult> {
  return new Promise((resolve) => {
    execFile(file, args, { cwd, timeout, maxBuffer: 10 * 1024 * 1024 }, (error, stdout, stderr) => {
      const code = error ? (typeof error.code === "number" ? error.code : 1) : 0
      resolve({ code, output: `${stdout}${stderr}`.trim() })
    })
  })
}

async function checkRuff(root: string, file: string): Promise<string | null> {
  if (!file.endsWith(".py")) return null
  try {
    await fs.access(file)
  } catch {
    return null
  }
  const ruff = path.join(root, ".venv/bin/ruff")
  try {
    await fs.access(ruff)
  } catch {
    return null
  }
  const result = await runCommand(
    ruff,
    ["check", "--quiet", "--output-format", "concise", file],
    root,
    30_000,
  )
  if (result.code === 0) return null
  return `ruff: ${toPosix(path.relative(root, file))}\n${tail(result.output)}`
}

async function checkDocs(root: string): Promise<string | null> {
  const python = path.join(root, ".venv/bin/python")
  try {
    await fs.access(python)
  } catch {
    return null
  }
  if (process.env.ALLA_QUALITY_DEBUG === "1") {
    console.error("[alla-quality] docs check")
  }
  const result = await runCommand(
    python,
    ["-m", "pytest", "-q", "-x", ...DOCS_TESTS],
    root,
    120_000,
  )
  if (result.code === 0) return null
  return `Справочники и задания разошлись с кодом:\n${tail(result.output)}`
}

function appendResult(result: ResultLike, text: string): ResultLike {
  const current = result.content
  if (typeof current === "string") {
    return { ...result, content: `${current}\n\n${text}` }
  }
  if (Array.isArray(current)) {
    return { ...result, content: [...current, { type: "text", text }] }
  }
  return { ...result, content: text }
}

function stripQuoted(command: string): string {
  let result = ""
  let quote: string | null = null
  for (let index = 0; index < command.length; index += 1) {
    const char = command[index]
    if (quote) {
      if (char === "\\" && quote === '"') {
        index += 1
        continue
      }
      if (char === quote) {
        quote = null
        result += "X"
      }
      continue
    }
    if (char === '"' || char === "'" || char === "`") {
      quote = char
      continue
    }
    result += char
  }
  return result
}

function isCommitCommand(command: string): boolean {
  const plain = stripQuoted(command)
  if (/(^|\s)--no-verify(\s|$)/.test(plain)) return false
  if (/(^|\s)-n(\s|$)/.test(plain)) return false
  return /(^|[;&|(]|\s)git(\s+-C\s+\S+)?\s+commit(\s|$)/.test(plain)
}

function isHoldout(resource: string): boolean {
  const normalized = toPosix(resource)
  return normalized === HOLDOUT_FILE || normalized.endsWith(`/${HOLDOUT_FILE}`)
}

async function runPreCommit(root: string, command: string, cwd: string): Promise<void> {
  const script = path.join(root, PRE_COMMIT)
  try {
    await fs.access(script)
  } catch {
    return
  }

  const child = spawn("sh", [script], {
    cwd: root,
    env: { ...process.env, CLAUDE_PROJECT_DIR: root },
  })
  let stdout = ""
  let stderr = ""
  child.stdout?.on("data", (chunk) => {
    stdout += String(chunk)
  })
  child.stderr?.on("data", (chunk) => {
    stderr += String(chunk)
  })
  child.stdin?.end(JSON.stringify({ tool_input: { command }, cwd }))

  const timer = setTimeout(() => child.kill("SIGKILL"), 300_000)
  const code = await new Promise<number>((resolve) => {
    child.on("error", () => resolve(1))
    child.on("close", (value) => resolve(value ?? 1))
  })
  clearTimeout(timer)
  if (code !== 0) {
    throw new Error(`Проверки перед коммитом не прошли:\n${tail(stderr || stdout)}`)
  }
}

export default Plugin.define({
  id: "alla-quality",
  async setup(ctx) {
    const canonical = ctx.location.project.canonical
    const active = ctx.location.directory
    let rulesRoot = canonical
    try {
      await fs.access(path.join(active, RULES_DIR))
      rulesRoot = active
    } catch {
      // A worktree may not have .claude/rules; use the canonical checkout.
    }
    const registrations: RegistrationLike[] = []

    // Rules must reach the model before the next action, so inject on the
    // before-hook. The entry is kept in the session instructions and is only
    // added once per rule.
    registrations.push(
      await ctx.tool.hook("execute.before", async (event) => {
        const paths = pathsFromTool(event.tool, event.input)
          .map((file) => relativeTo(active, file) ?? relativeTo(canonical, file))
          .filter((value): value is string => value !== null)
        if (paths.length === 0) return

        let state = sessions.get(event.sessionID)
        if (!state) {
          if (sessions.size > 500) sessions.clear()
          state = { injected: new Set<string>() }
          sessions.set(event.sessionID, state)
        }
        await injectRules(ctx, event.sessionID, state, paths, rulesRoot)
      }),
    )

    // Checks run only after a mutation, not after every read.
    registrations.push(
      await ctx.tool.hook("execute.after", async (event) => {
        if (event.tool !== "edit" && event.tool !== "write" && event.tool !== "patch") return
        if (event.status !== "completed") return

        const files = pathsFromTool(event.tool, event.input)
          .map((file) => absoluteTo(active, file))
          .filter((value): value is string => value !== null)
        if (files.length === 0) return

        const relativePaths = files.map((file) => toPosix(path.relative(active, file)))
        const diagnostics: string[] = []
        for (const file of files) {
          const ruff = await checkRuff(active, file)
          if (ruff) diagnostics.push(ruff)
        }
        if (relativePaths.some(shouldCheckDocs)) {
          const docs = await checkDocs(active)
          if (docs) diagnostics.push(docs)
        }
        if (diagnostics.length > 0) {
          event.result = appendResult(event.result as ResultLike, [
            "",
            "[alla-quality]",
            diagnostics.join("\n\n"),
          ].join("\n")) as typeof event.result
        }
      }),
    )

    // The same ruff + pytest + mypy gate as the Claude Code pre-commit hook.
    registrations.push(
      await ctx.shell.hook("create.before", async (event) => {
        if (!isCommitCommand(event.command)) return
        if (!isWithin(active, event.cwd)) return
        if (process.env.ALLA_QUALITY_DEBUG === "1") {
          console.error(`[alla-quality] pre-commit: ${event.command}`)
        }
        event.timeout = Math.max(event.timeout, 300_000)
        await runPreCommit(active, event.command, event.cwd)
      }),
    )

    // Defence in depth for the check-only holdout. The project config also
    // asks before reading it and denies edits.
    registrations.push(
      await ctx.permission.hook("evaluate", (event) => {
        if (!event.resources.some(isHoldout)) return
        if (process.env.ALLA_QUALITY_DEBUG === "1") {
          console.error(`[alla-quality] holdout ${event.action} -> ask/deny`)
        }
        if (event.action === "read") {
          event.effect = "ask"
          event.message =
            "Holdout: просмотр требует записи в qwen-skill/alla-launch/tests/eval/holdout_journal.md."
        }
        if (event.action === "edit") {
          event.effect = "deny"
          event.message = "Holdout нельзя править: это проверочный корпус."
        }
      }),
    )

    return async () => {
      for (const registration of registrations.reverse()) {
        await registration.dispose()
      }
    }
  },
})
