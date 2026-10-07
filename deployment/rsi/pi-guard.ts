import { realpathSync, readFileSync, readdirSync, lstatSync, mkdirSync, writeFileSync, appendFileSync, existsSync, openSync, closeSync, fstatSync, statSync, constants } from "node:fs";
import { createHash } from "node:crypto";
import { isIP } from "node:net";
import { isAbsolute, relative, resolve, sep } from "node:path";

const READ_CONTENT_BYTES = 16384;
const PAGE_RESPONSE_BYTES = 20000;
const PAGE_DEFAULT_BYTES = 8192;
const PAGE_SOURCE_BYTES = 16 * 1024 * 1024;
const PI_NATIVE_RESULT_TOOLS = new Set(["read", "bash", "grep", "find", "ls", "edit", "write", "rsi_read_source"]);

function utf8Prefix(text: string, maxBytes: number): string {
  let bytes = 0;
  let end = 0;
  for (const character of text) {
    const size = Buffer.byteLength(character, "utf8");
    if (bytes + size > maxBytes) break;
    bytes += size;
    end += character.length;
  }
  return text.slice(0, end);
}

function pageInput(input: any) {
  if (!input || typeof input !== "object" || Array.isArray(input) ||
      Object.keys(input).some(key => !["path", "sha256", "json_pointer", "offset", "max_bytes"].includes(key)) ||
      typeof input.path !== "string" || !input.path || input.path.length > 4096 || input.path.includes("\0"))
    throw new Error("Invalid source path or page parameters");
  const offset = input.offset ?? 0;
  const maxBytes = input.max_bytes ?? PAGE_DEFAULT_BYTES;
  if (!Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(maxBytes) || maxBytes < 4 || maxBytes > READ_CONTENT_BYTES)
    throw new Error("offset must be a nonnegative codepoint index; max_bytes must be 4..16384");
  if (input.sha256 !== undefined && (typeof input.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(input.sha256)))
    throw new Error("Invalid SHA-256 source hash");
  if (offset > 0 && !input.sha256) throw new Error("Continuation pages require the SHA-256 returned by the first page");
  if (input.json_pointer !== undefined && (typeof input.json_pointer !== "string" || input.json_pointer.length > 2048 ||
      (input.json_pointer !== "" && !input.json_pointer.startsWith("/")) || /~(?:[^01]|$)/.test(input.json_pointer)))
    throw new Error("Invalid RFC 6901 JSON pointer");
  return { ...input, offset, max_bytes: maxBytes };
}

function selectedSourceText(raw: Buffer, pointer?: string): string {
  let text: string;
  try { text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(raw); }
  catch { throw new Error("Source is not valid UTF-8 text"); }
  if (pointer === undefined) return text;
  let value: any;
  try { value = JSON.parse(text); }
  catch { throw new Error("JSON pointer requires a valid JSON source"); }
  for (const token of pointer.split("/").slice(1)) {
    const part = token.replace(/~1/g, "/").replace(/~0/g, "~");
    if (!value || typeof value !== "object" || !Object.prototype.hasOwnProperty.call(value, part) ||
        (Array.isArray(value) && !/^(0|[1-9][0-9]*)$/.test(part)))
      throw new Error("JSON pointer does not identify an existing source value");
    value = value[part];
  }
  return typeof value === "string" ? value : JSON.stringify(value);
}

function sourceReadHint(source: any, content: any[], prefix = "", startIndex = 0) {
  const index = (content || []).findIndex((part, index) => index >= startIndex && part.type === "text" && typeof part.text === "string");
  return { tool: "rsi_read_source", path: source.path, sha256: source.sha256,
    ...(index >= 0 ? { json_pointer: `${prefix}/content/${index}/text` } : {}),
    offset: 0, max_bytes: PAGE_DEFAULT_BYTES };
}

export function contextPayloadBytes(payload: any): number {
  return Buffer.byteLength(JSON.stringify(payload), "utf8");
}

// Pi supplies these hooks before built-in tool execution.
export default function (pi: any) {
  const root = realpathSync(process.env.RSI_AGENT_ROOT || process.cwd());
  const evidence = new Map<string, string>();
  const externalRoots: string[] = [];
  const workspace = process.env.RSI_AGENT_WORKSPACE ? realpathSync(process.env.RSI_AGENT_WORKSPACE) : undefined;
  const hash = (path: string) => createHash("sha256").update(readFileSync(path)).digest("hex");
  const contextLimit = Number(process.env.RSI_AGENT_CONTEXT_LIMIT_TOKENS || 0);
  let acceptedSubmissionCallId: string | undefined;
  let submissionResultEmitted = false;
  if (process.env.RSI_AGENT_ROLE === "curator") {
    pi.on("tool_result", (event: any) => {
      if (event.toolName === "rsi_wiki_submit" && event.details?.ok === true && event.details?.status === "accepted")
        acceptedSubmissionCallId = event.toolCallId;
    });
    pi.on("message_end", (event: any) => {
      if (acceptedSubmissionCallId !== undefined && event.message?.role === "toolResult" &&
          event.message.toolName === "rsi_wiki_submit" && event.message.toolCallId === acceptedSubmissionCallId)
        submissionResultEmitted = true;
    });
    pi.on("turn_end", () => {
      if (acceptedSubmissionCallId !== undefined && submissionResultEmitted) process.exit(0);
    });
    pi.on("before_provider_request", () => {
      if (acceptedSubmissionCallId !== undefined && submissionResultEmitted) {
        // The SDK can continue a mixed read/submit batch despite terminate; the receipt has been emitted.
        process.stderr.write("RSI Curator submission accepted; no further provider request is needed.\n");
        process.exit(0);
      }
    });
  }
  function archiveContextSource(value: any) {
    if (!workspace) throw new Error("Context source archives require a task workspace");
    const raw = JSON.stringify(value, null, 2) + "\n";
    const sha256 = createHash("sha256").update(raw).digest("hex");
    const directory = resolve(workspace, ".context-sources");
    if (!safeWorkspacePath(directory, true)) throw new Error("Invalid context source directory");
    mkdirSync(directory, { recursive: true });
    const path = resolve(directory, sha256 + ".json");
    if (!safeWorkspacePath(path, true)) throw new Error("Invalid context source file");
    if (existsSync(path)) {
      if (hash(path) !== sha256) throw new Error("Context source hash mismatch");
    } else writeFileSync(path, raw, { flag: "wx" });
    evidence.set(path, sha256);
    return { path, sha256, bytes: Buffer.byteLength(raw, "utf8") };
  }
  if (contextLimit) {
    // Pi owns token estimation, output fitting, compaction, and overflow recovery.
    // Only verify the operator's native model configuration; never rewrite history
    // or infer token overflow from serialized UTF-8 bytes.
    pi.on("before_provider_request", (event: any, ctx: any) => {
      const window = ctx.model?.contextWindow;
      if (!Number.isSafeInteger(window) || window <= 0 || window > contextLimit) {
        process.stderr.write(`RSI Pi model.contextWindow=${String(window)} must be a positive integer <= configured cap ${contextLimit}; configure the selected model in Pi models.json.\n`);
        process.exit(78);
      }
      if (process.env.RSI_CONTEXT_BUDGET_RECEIPT) {
        try {
          appendFileSync(process.env.RSI_CONTEXT_BUDGET_RECEIPT, JSON.stringify({
            stage: "provider_request", management: "pi-native",
            limit_tokens: contextLimit, model_context_window: window,
            model_max_tokens: ctx.model?.maxTokens,
            model_configuration_valid: true,
            checked_at: new Date().toISOString() }) + "\n");
        } catch {
          process.stderr.write("RSI could not record Pi-native context evidence; provider request blocked.\n");
          process.exit(78);
        }
      }
      // No payload return or mutation: the SDK retains its own output budget.
    });
    pi.on("tool_result", (event: any) => {
      // Pi's built-in file/shell tools already paginate or truncate their
      // results. rsi_read_source also returns a bounded, hash-pinned page.
      // Preserve those exact payloads so RSI does not add a second prefix or
      // hide runtime-appended page content from Pi's native compaction.
      if (PI_NATIVE_RESULT_TOOLS.has(event.toolName)) return;
      if (!workspace || contextPayloadBytes({ content: event.content || [], details: event.details }) <= 6000) return;
      const source = archiveContextSource({ tool_name: event.toolName, tool_call_id: event.toolCallId,
        content: event.content, details: event.details });
      const text = (event.content || []).filter((item: any) => item.type === "text")
        .map((item: any) => item.text).join("\n");
      const details = { source_ref: { path: source.path, sha256: source.sha256, json_pointer: "/details" },
        ...(event.details?.error ? { error: { message: "Tool error; see full attributed source", source_ref: source } } : {}),
        ...(event.details?.successful !== undefined ? { successful: event.details.successful } : {}) };
      if (contextPayloadBytes(event.content || []) <= READ_CONTENT_BYTES)
        return { content: event.content, details };
      return { details,
        content: [{ type: "text", text: JSON.stringify({ tool_name: event.toolName,
        source_ref: { path: source.path, sha256: source.sha256 }, result_bytes: source.bytes,
        read_source: sourceReadHint(source, event.content), excerpt: utf8Prefix(text, 2048),
        note: "Verbatim prefix only. Use rsi_read_source with read_source parameters; continue with next_offset and sha256." }) }] };
    });
  }
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
    evidence.set(manifestPath, hash(manifestPath));
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
  function authorizeFileTool(toolName: string, input: any, sourceLimit = 0) {
    try { return checkFileTool(toolName, input, sourceLimit); }
    catch { return { block: true, reason: "Source path or its integrity could not be checked" }; }
  }
  function checkFileTool(toolName: string, input: any, sourceLimit: number) {
    const tools = new Set(["read", "grep", "find", "ls", "edit", "write"]);
    if (!tools.has(toolName)) return { block: true, reason: "Tool is outside this RSI role contract" };
    if (!input || typeof input !== "object" || Array.isArray(input)) return { block: true, reason: "Invalid file tool input" };
    const inputPath = input.path ?? ".";
    if (typeof inputPath !== "string") return { block: true, reason: "Invalid tool path" };
    const requested = resolve(root, inputPath);
    if (sourceLimit && statSync(requested).size > sourceLimit)
      return { block: true, reason: "Paged source reads support files up to 16777216 bytes (16 MiB); select a smaller text source" };
    if (process.env.RSI_CONTEXT_BUDGET_RECEIPT && requested === resolve(process.env.RSI_CONTEXT_BUDGET_RECEIPT) &&
        toolName !== "read")
      return { block: true, reason: "Context budget receipts support read only" };
    if (evidence.has(requested)) {
      if (toolName !== "read" || hash(requested) !== evidence.get(requested))
        return { block: true, reason: "Context sources support hash-checked read only" };
      return undefined;
    }
    if (workspace && inside(resolve(workspace, ".context-sources"), requested) &&
        !["read", "ls"].includes(toolName))
      return { block: true, reason: "Context source archives support read only" };
    if (workspace && inside(workspace, requested)) {
      if (!safeWorkspacePath(requested, toolName === "write"))
        return { block: true, reason: "Use regular files inside your communication workspace" };
      if (["edit", "write"].includes(toolName) && requested === workspace)
        return { block: true, reason: "Choose a file within your communication workspace" };
      if (["grep", "find"].includes(toolName) && lstatSync(requested).isDirectory() && !safeTree(requested))
        return { block: true, reason: "Choose a communication directory without symlinks" };
      return undefined;
    }
    let actual: string;
    try { actual = realpathSync(requested); }
    catch { return { block: true, reason: "Create new notes or prototypes in your communication workspace" }; }
    if (evidence.has(actual)) {
      if (toolName !== "read" || hash(actual) !== evidence.get(actual))
        return { block: true, reason: "Assigned evidence and earlier handoffs support verified read only" };
      return undefined;
    }
    if (externalRoots.some(base => inside(base, actual))) {
      if (!["read", "grep", "find", "ls"].includes(toolName))
        return { block: true, reason: "Assigned external evidence is read only" };
      return undefined;
    }
    const rel = relative(root, actual);
    if (rel === ".." || rel.startsWith(".." + sep) || isAbsolute(rel) ||
        rel.split(sep).some((part: string) => forbidden.has(part))) {
      return { block: true, reason: "Path is outside the research source boundary" };
    }
    if (["grep", "find"].includes(toolName) && lstatSync(actual).isDirectory() && !safeTree(actual))
      return { block: true, reason: "Choose a narrower source directory without private paths or symlinks" };
    if (["edit", "write"].includes(toolName) &&
        (process.env.RSI_AGENT_ROLE !== "coder" || process.env.RSI_AGENT_EXPLORATORY === "1" || !editable.has(rel))) {
      return { block: true, reason: "Only the coding role may edit registered implementation files" };
    }
    return undefined;
  }
  function noSourceSymlinks(path: string) {
    let current = resolve(sep);
    for (const part of path.split(sep).filter(Boolean)) {
      current = resolve(current, part);
      if (lstatSync(current).isSymbolicLink()) throw new Error("Paged source reads require a path without symlinks");
    }
  }
  function readSourcePage(input: any) {
    const args = pageInput(input);
    const denied = authorizeFileTool("read", args, PAGE_SOURCE_BYTES);
    if (denied?.block) throw new Error(denied.reason);
    const requested = resolve(root, args.path);
    noSourceSymlinks(requested);
    const path = realpathSync(requested);
    const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    let raw: Buffer;
    try {
      const before = fstatSync(fd);
      if (!before.isFile() || before.nlink !== 1) throw new Error("Paged source reads require a regular file with one link");
      if (before.size > PAGE_SOURCE_BYTES) throw new Error("Paged source exceeds the 16 MiB source file limit");
      raw = readFileSync(fd);
      const after = fstatSync(fd);
      noSourceSymlinks(requested);
      const current = statSync(requested);
      if (realpathSync(requested) !== path || before.dev !== current.dev || before.ino !== current.ino ||
          before.size !== after.size || before.mtimeMs !== after.mtimeMs || before.ctimeMs !== after.ctimeMs)
        throw new Error("Source changed while it was being read");
      const recheck = authorizeFileTool("read", args, PAGE_SOURCE_BYTES);
      if (recheck?.block) throw new Error(recheck.reason);
    } finally { closeSync(fd); }
    const sha256 = createHash("sha256").update(raw).digest("hex");
    const expected = evidence.get(requested) ?? evidence.get(path) ?? args.sha256;
    if ((expected && sha256 !== expected) || (args.sha256 && sha256 !== args.sha256))
      throw new Error("Source SHA-256 changed; restart the read from offset 0 only after inspecting the new source");
    const text = selectedSourceText(raw, args.json_pointer);
    const characters: string[] = [];
    let totalCodepoints = 0;
    let bytes = 0;
    let pageFull = false;
    for (const character of text) {
      if (totalCodepoints >= args.offset && !pageFull) {
        const size = Buffer.byteLength(character, "utf8");
        if (bytes + size > args.max_bytes) pageFull = true;
        else { bytes += size; characters.push(character); }
      }
      totalCodepoints++;
    }
    if (args.offset > totalCodepoints) throw new Error("offset is beyond the selected source text");
    const resultAt = (length: number) => {
      const text = characters.slice(0, length).join("");
      const metadata = { path, sha256, ...(args.json_pointer !== undefined ? { json_pointer: args.json_pointer } : {}),
        offset: args.offset, offset_unit: "unicode_codepoints", next_offset: args.offset + length,
        eof: args.offset + length === totalCodepoints, total_codepoints: totalCodepoints,
        bytes: Buffer.byteLength(text, "utf8") };
      return { content: [{ type: "text", text: JSON.stringify({ ok: true, ...metadata, text }) }], details: metadata };
    };
    // Escaped JSON strings can be several times larger than the selected UTF-8 text.
    let low = 0;
    let high = characters.length;
    if (contextPayloadBytes(resultAt(0)) > PAGE_RESPONSE_BYTES) throw new Error("Source address is too large for a bounded page response");
    while (low < high) {
      const middle = Math.ceil((low + high) / 2);
      if (contextPayloadBytes(resultAt(middle)) <= PAGE_RESPONSE_BYTES) low = middle;
      else high = middle - 1;
    }
    if (!low && args.offset < totalCodepoints) throw new Error("Source address leaves no room for a page");
    return resultAt(low);
  }
  pi.registerTool({
    name: "rsi_read_source", label: "rsi_read_source",
    description: "Read a complete authorized UTF-8 source up to 16 MiB in deterministic pages without a runtime bridge. path and sha256 come from a source_ref. An RFC 6901 json_pointer selects an archived string (for example /content/0/text or /messages/3/content/0/text); objects are serialized as JSON. offset counts Unicode codepoints, not lines or bytes. Continue with next_offset and the returned sha256; each response is at most 20000 serialized UTF-8 bytes.",
    parameters: { type: "object", additionalProperties: false, required: ["path"], properties: {
      path: { type: "string", minLength: 1, maxLength: 4096 },
      sha256: { type: "string", pattern: "^[0-9a-f]{64}$", description: "Optional on the first page; required for offset greater than zero" },
      json_pointer: { type: "string", maxLength: 2048, description: "RFC 6901 pointer; omit to read the raw UTF-8 file" },
      offset: { type: "integer", minimum: 0, description: "Unicode codepoint index; default 0" },
      max_bytes: { type: "integer", minimum: 4, maximum: READ_CONTENT_BYTES, description: "Maximum selected text UTF-8 bytes per page; default 8192" },
    } },
    async execute(_id: string, args: any) {
      try { return readSourcePage(args); }
      catch (error: any) {
        const message = typeof error?.message === "string" ? utf8Prefix(error.message, 1024) : "Source read failed";
        return { content: [{ type: "text", text: JSON.stringify({ ok: false, error: { message } }) }],
          details: { error: { message } } };
      }
    },
  });
  pi.on("tool_call", async (event: any) => {
    if (event.toolName === "rsi_dev_exec") {
      if (!["researcher", "coder"].includes(process.env.RSI_AGENT_ROLE || ""))
        return { block: true, reason: "Development containers belong to Researcher and Coder" };
      if (!process.env.RSI_TOOL_SOCKET)
        return { block: true, reason: "Development containers require the active RSI bridge" };
      return;
    }
    if (acceptedSubmissionCallId !== undefined)
      return { block: true, terminate: true, reason: "This Curator task already has an accepted submission" };
    if (event.toolName === "rsi_read_source") {
      try { return authorizeFileTool("read", pageInput(event.input), PAGE_SOURCE_BYTES); }
      catch (error: any) { return { block: true, reason: error.message }; }
    }
    if (["rsi_delegate", "rsi_run_probe", "rsi_research", "rsi_research_checkpoint", "rsi_register_hypothesis", "rsi_register_candidate", "rsi_branch_control",
      "rsi_message", "rsi_inbox", "rsi_status", "rsi_wait", "rsi_read_handoff", "rsi_wiki_check", "rsi_wiki_submit"].includes(event.toolName)) {
      if (event.toolName === "rsi_wiki_submit" && process.env.RSI_AGENT_ROLE !== "curator")
        return { block: true, reason: "Wiki submission belongs to the Curator role" };
      return process.env.RSI_TOOL_SOCKET ? undefined :
        { block: true, reason: "RSI runtime tools require an active task bridge" };
    }
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
    return authorizeFileTool(event.toolName, event.input);
  });
}
