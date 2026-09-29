import { realpathSync, readFileSync, readdirSync, lstatSync } from "node:fs";
import { createHash } from "node:crypto";
import { isIP } from "node:net";
import { isAbsolute, relative, resolve, sep } from "node:path";

// Pi supplies these hooks before built-in tool execution.
export default function (pi: any) {
  const root = realpathSync(process.env.RSI_AGENT_ROOT || process.cwd());
  const evidence = new Map<string, string>();
  const externalRoots: string[] = [];
  const workspace = process.env.RSI_AGENT_WORKSPACE ? realpathSync(process.env.RSI_AGENT_WORKSPACE) : undefined;
  const hash = (path: string) => createHash("sha256").update(readFileSync(path)).digest("hex");
  for (const manifestFile of [process.env.RSI_EVIDENCE_MANIFEST, process.env.RSI_COMMUNICATION_MANIFEST]) {
    if (!manifestFile) continue;
    const manifestPath = realpathSync(manifestFile);
    const bundle = resolve(manifestPath, "..");
    const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
    evidence.set(manifestPath, hash(manifestPath));
    for (const file of manifest.files) {
      const path = resolve(bundle, file.path);
      if (!path.startsWith(bundle + sep) || realpathSync(path) !== path || hash(path) !== file.sha256)
        throw new Error("Invalid RSI read-only manifest");
      evidence.set(path, file.sha256);
    }
  }
  const externalManifestFile = process.env.RSI_EXTERNAL_READONLY_MANIFEST;
  if (externalManifestFile) {
    const manifestPath = realpathSync(externalManifestFile);
    const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
    if (manifest.schema_version !== 1 || !Array.isArray(manifest.roots))
      throw new Error("Invalid RSI external read-only manifest");
    for (const item of manifest.roots) {
      if (!item || item.mode !== "read-only" || typeof item.path !== "string" || !item.path.startsWith("/"))
        throw new Error("Invalid RSI external read-only root");
      const path = realpathSync(item.path);
      if (lstatSync(item.path).isSymbolicLink() || !lstatSync(path).isDirectory() ||
          externalRoots.some(root => inside(root, path) || inside(path, root)))
        throw new Error("Invalid RSI external read-only root");
      externalRoots.push(path);
    }
  }
  const forbidden = new Set([".git", ".pi", ".rsi"]);
  function inside(base: string, path: string): boolean {
    const rel = relative(base, path);
    return rel !== ".." && !rel.startsWith(".." + sep) && !isAbsolute(rel);
  }
  // Resolve each existing ancestor; write may create the missing suffix.
  function safeWorkspacePath(path: string, create: boolean): boolean {
    let current = workspace!;
    for (const part of relative(workspace!, path).split(sep).filter(Boolean)) {
      current = resolve(current, part);
      try {
        const stat = lstatSync(current);
        if (stat.isSymbolicLink() || (stat.isFile() && stat.nlink !== 1) ||
            (!stat.isDirectory() && !stat.isFile()) || !inside(workspace!, realpathSync(current))) return false;
      } catch (error: any) {
        return create && error.code === "ENOENT";
      }
    }
    return true;
  }
  // Recursive tools can follow descendants beyond their explicit input path.
  function safeTree(path: string): boolean {
    for (const item of readdirSync(path, { withFileTypes: true })) {
      if (forbidden.has(item.name) || item.isSymbolicLink()) return false;
      if (item.isDirectory() && !safeTree(resolve(path, item.name))) return false;
    }
    return true;
  }
  const editable = new Set([
    "experiments/residual_memory/memory.py",
    "experiments/runtime_harness/scheduler.py",
  ]);
  pi.on("tool_call", async (event: any) => {
    if (["rsi_delegate", "rsi_run_probe", "rsi_research", "rsi_research_checkpoint", "rsi_register_hypothesis", "rsi_register_candidate", "rsi_branch_control",
      "rsi_message", "rsi_inbox", "rsi_status", "rsi_wait", "rsi_read_handoff", "rsi_wiki_check"].includes(event.toolName))
      return process.env.RSI_TOOL_SOCKET ? undefined :
        { block: true, reason: "RSI runtime tools require an active task bridge" };
    if (event.toolName === "bash")
      return (["researcher", "coder"].includes(process.env.RSI_AGENT_ROLE || "") ||
        (["analyzer", "critic"].includes(process.env.RSI_AGENT_ROLE || "") && !!process.env.RSI_TOOL_SOCKET))
        ? undefined : { block: true, reason: "This role has no authorized shell" };
    if (["web_search", "fetch_content", "get_search_content"].includes(event.toolName)) {
      if (!["coordinator", "researcher", "coder"].includes(process.env.RSI_AGENT_ROLE || ""))
        return { block: true, reason: "Web research tools belong to coordinator, research, and coding roles" };
      const input = event.input;
      if (!input || typeof input !== "object" || Array.isArray(input))
        return { block: true, reason: "Invalid web research input" };
      if (event.toolName === "web_search") {
        const allowed = new Set(["query", "queries", "numResults", "recencyFilter", "domainFilter", "workflow"]);
        if (Object.keys(input).some(key => !allowed.has(key)) ||
            (input.workflow !== undefined && input.workflow !== "none"))
          return { block: true, reason: "Use the configured read-only web search route" };
        return undefined;
      }
      if (event.toolName === "fetch_content") {
        const allowed = new Set(["url", "urls", "mode"]);
        const urls = input.urls ?? (input.url === undefined ? [] : [input.url]);
        if (Object.keys(input).some(key => !allowed.has(key)) ||
            (input.mode !== undefined && input.mode !== "readable") ||
            !Array.isArray(urls) || urls.length < 1 || urls.length > 3 ||
            urls.some((url: unknown) => {
              if (typeof url !== "string" || url.length > 2048) return true;
              try {
                const parsed = new URL(url);
                const host = parsed.hostname.toLowerCase();
                return parsed.protocol !== "https:" || !!parsed.username || !!parsed.password ||
                  isIP(host.replace(/^\[|\]$/g, "")) !== 0 || host === "localhost" ||
                  host.endsWith(".localhost") || host.endsWith(".local") || host.endsWith(".internal");
              }
              catch { return true; }
            }))
          return { block: true, reason: "Fetch up to three public HTTPS pages in readable mode" };
        return undefined;
      }
      return undefined;
    }
    if (event.toolName === "github_search" || event.toolName === "github_read_file") {
      return ["coordinator", "researcher", "coder"].includes(process.env.RSI_AGENT_ROLE || "") ? undefined :
        { block: true, reason: "GitHub tools belong to coordinator, researcher, and coder" };
    }
    if (event.toolName === "mcp") {
      if (!["coordinator", "researcher", "coder"].includes(process.env.RSI_AGENT_ROLE || ""))
        return { block: true, reason: "MCP belongs to coordinator, researcher, and coder" };
      const allowed = new Set(["alphaxiv_discover_papers", "alphaxiv_answer_pdf_queries", "alphaxiv_get_paper_content", "alphaxiv_read_files_from_github_repository"]);
      const input = event.input;
      if (input.connect === "alphaxiv" && Object.keys(input).length === 1) return undefined;
      if (typeof input.tool === "string" && allowed.has(input.tool) && Object.keys(input).every(k => ["tool", "args"].includes(k))) return undefined;
      if (!input.tool && (!input.server || input.server === "alphaxiv") &&
          Object.keys(input).every(k => ["search", "describe", "server", "limit", "offset"].includes(k))) return undefined;
      return { block: true, reason: "Only registered read-only alphaXiv tools are available" };
    }
    const tools = new Set(["read", "grep", "find", "ls", "edit", "write"]);
    if (!tools.has(event.toolName)) return { block: true, reason: "Tool is outside this RSI role contract" };
    const inputPath = event.input.path ?? ".";
    if (typeof inputPath !== "string") return { block: true, reason: "Invalid tool path" };
    const requested = resolve(root, inputPath);
    if (workspace && inside(workspace, requested)) {
      if (!safeWorkspacePath(requested, event.toolName === "write"))
        return { block: true, reason: "Use regular files inside your communication workspace" };
      if (["edit", "write"].includes(event.toolName) && requested === workspace)
        return { block: true, reason: "Choose a file within your communication workspace" };
      if (["grep", "find"].includes(event.toolName) && lstatSync(requested).isDirectory() && !safeTree(requested))
        return { block: true, reason: "Choose a communication directory without symlinks" };
      return undefined;
    }
    let actual: string;
    try { actual = realpathSync(requested); }
    catch { return { block: true, reason: "Create new notes or prototypes in your communication workspace" }; }
    if (evidence.has(actual)) {
      if (event.toolName !== "read" || hash(actual) !== evidence.get(actual))
        return { block: true, reason: "Assigned evidence and earlier handoffs support verified read only" };
      return undefined;
    }
    if (externalRoots.some(base => inside(base, actual))) {
      if (!["read", "grep", "find", "ls"].includes(event.toolName))
        return { block: true, reason: "Assigned external evidence is read only" };
      return undefined;
    }
    const rel = relative(root, actual);
    if (rel === ".." || rel.startsWith(".." + sep) || isAbsolute(rel) ||
        rel.split(sep).some((part: string) => forbidden.has(part))) {
      return { block: true, reason: "Path is outside the research source boundary" };
    }
    if (["grep", "find"].includes(event.toolName) && lstatSync(actual).isDirectory() && !safeTree(actual))
      return { block: true, reason: "Choose a narrower source directory without private paths or symlinks" };
    if (["edit", "write"].includes(event.toolName) &&
        (process.env.RSI_AGENT_ROLE !== "coder" || process.env.RSI_AGENT_EXPLORATORY === "1" || !editable.has(rel))) {
      return { block: true, reason: "Only the coding role may edit registered implementation files" };
    }
    return undefined;
  });
}
