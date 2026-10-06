import { createConnection } from "node:net";
import { createRequire } from "node:module";
import { homedir } from "node:os";
import { join } from "node:path";

const prefix = join(homedir(), ".local/share/rsi/pi");
const require = createRequire(join(prefix, "node_modules/@earendil-works/pi-coding-agent/package.json"));
const { Type } = require("typebox");

function request(tool: string, callId: string, args: object, attempt = 0): Promise<any> {
  const path = process.env.RSI_TOOL_SOCKET;
  if (!path) return Promise.resolve({ ok: false, error: { code: "unavailable", message: "RSI runtime tool bridge is unavailable" } });
  return new Promise((resolve) => {
    const socket = createConnection(path);
    let buffer = "";
    let settled = false;
    const finish = (value: any) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      resolve(value);
    };
    const transportFailure = (message: string) => {
      if (settled) return;
      // A lost Unix socket can happen after the server has committed a tool
      // call. Reusing the same call ID makes the server replay its receipt.
      if (attempt < 2) {
        settled = true;
        socket.destroy();
        setTimeout(() => request(tool, callId, args, attempt + 1).then(resolve), 50 * (attempt + 1));
      } else {
        finish({ ok: false, error: { code: "bridge_error", message,
          retryable: true, repair: "The bridge connection failed; retry the same tool call ID." } });
      }
    };
    socket.setEncoding("utf8");
    socket.on("connect", () => socket.write(JSON.stringify({ tool, call_id: callId, args, bridge_protocol: 2 }) + "\n"));
    socket.setTimeout(30_000, () => transportFailure("RSI tool bridge timed out before returning a result"));
    socket.on("data", (chunk: string) => {
      buffer += chunk;
      let end: number;
      while ((end = buffer.indexOf("\n")) !== -1) {
        if (end > 256_000) {
          finish({ ok: false, error: { code: "response_limit", message: "RSI tool response exceeded its limit" } });
          return;
        }
        let frame: any;
        try { frame = JSON.parse(buffer.slice(0, end)); }
        catch {
          finish({ ok: false, error: { code: "invalid_response", message: "RSI tool bridge returned invalid JSON" } });
          return;
        }
        buffer = buffer.slice(end + 1);
        if (frame && frame.type === "heartbeat" && Object.keys(frame).length === 1) continue;
        if (!frame || typeof frame.ok !== "boolean") {
          finish({ ok: false, error: { code: "invalid_response", message: "RSI tool bridge returned an unknown frame" } });
          return;
        }
        finish(frame);
        return;
      }
      if (buffer.length > 256_000) {
        finish({ ok: false, error: { code: "response_limit", message: "RSI tool response exceeded its limit" } });
      }
    });
    socket.on("error", (error: Error) => transportFailure(error.message));
    socket.on("end", () => transportFailure("RSI tool bridge closed before returning a result"));
    socket.on("close", () => transportFailure("RSI tool bridge closed before returning a result"));
  });
}

const optionalId = Type.Optional(Type.String({ description: "Omit unless this ID already exists or was assigned in the task" }));
const jsonObject = Type.Record(Type.String(), Type.Any());
const flexibleList = Type.Union([Type.Array(Type.String()), Type.String()]);
const flexibleInteger = (minimum: number) => Type.Union([Type.Integer({ minimum }), Type.String()]);
const researchRef = Type.Object({
  node_id: Type.String(),
  revision: Type.Integer({ minimum: 1 }),
});
const researchData = Type.Object({
  reason: Type.Optional(Type.String({ minLength: 1, description: "Required for create, revise, relate, and compose. Explain why this graph change is being made; put the reason inside data." })),
  node_id: Type.Optional(Type.String({ description: "New node ID for create/compose, or existing node ID for revise" })),
  question: Type.Optional(Type.String({ description: "Required for create/compose; may be updated by revise" })),
  scope: Type.Optional(Type.String({ description: "Required for create/compose; may be updated by revise" })),
  summary: Type.Optional(Type.String()),
  owner: Type.Optional(Type.String()),
  execution_status: Type.Optional(Type.String()),
  finding_status: Type.Optional(Type.String()),
  artifact_refs: Type.Optional(Type.Array(Type.String())),
  run_refs: Type.Optional(Type.Array(Type.String())),
  parent: Type.Optional(researchRef),
  separate_root: Type.Optional(Type.Boolean()),
  expected_revision: Type.Optional(Type.Integer({ minimum: 1 })),
  relation_id: Type.Optional(Type.String()),
  kind: Type.Optional(Type.String()),
  source: Type.Optional(researchRef),
  target: Type.Optional(researchRef),
  status: Type.Optional(Type.String()),
  evidence_refs: Type.Optional(Type.Array(Type.String())),
  inputs: Type.Optional(Type.Array(Type.Object({
    node_id: Type.String(), revision: Type.Integer({ minimum: 1 }),
    selection: Type.Optional(Type.String()),
  }), { minItems: 2 })),
  plan: Type.Optional(jsonObject),
});
const delegation = {
  role: Type.Union(["researcher", "hypothesis", "coder", "analyzer", "critic", "curator"].map(role => Type.Literal(role))),
  objective: Type.String({ description: "Specific question or work for the collaborator" }),
  idea_id: Type.Optional(Type.String({ description: "Use the assigned Idea ID, or choose a short stable ID for a new draft branch when none is assigned" })),
  candidate_id: optionalId,
  research_node_id: Type.Optional(Type.String({ description: "Exact research node to work on; defaults to the current task's node" })),
  research_revision: Type.Optional(Type.Integer({ minimum: 1, description: "Exact node revision; omitted only when inheriting the current task's pinned revision" })),
  task_kind: Type.Optional(Type.Union([Type.Literal("standard"), Type.Literal("problem_reframe")],
    { description: "Use problem_reframe only when the coordinator asks an independent researcher to revisit the Mission question mid-course" })),
  research_mandate: Type.Optional(Type.Object({
    mode: Type.Union([Type.Literal("direction"), Type.Literal("check")]),
    responsibility: Type.String({ minLength: 1, maxLength: 8000 }),
    constraints: Type.Optional(Type.Array(Type.Object({ statement: Type.String({ minLength: 1, maxLength: 8000 }), source_ref: Type.String({ minLength: 1, maxLength: 8000 }) }, { additionalProperties: false }))),
    observations: Type.Optional(Type.Array(Type.Object({ statement: Type.String({ minLength: 1, maxLength: 8000 }), source_refs: Type.Array(Type.String({ minLength: 1, maxLength: 8000 }), { minItems: 1 }) }, { additionalProperties: false }))),
    suggestions: Type.Optional(Type.Array(Type.String({ minLength: 1, maxLength: 8000 })))
  }, { additionalProperties: false, description: "Optional assignment brief for any collaborator role: responsibility, source-linked constraints, observations, and suggestions. Does not change the recipient's role or permissions." })),
};

const researchCheckpoint = Type.Object({
  problem_statement: Type.String({ minLength: 1, maxLength: 8000 }),
  summary: Type.String({ minLength: 1, maxLength: 8000 }),
  changes: Type.Array(Type.Object({
    source_refs: Type.Array(Type.String({ minLength: 1, maxLength: 8000 }), { minItems: 1 }),
    previous_view: Type.String({ minLength: 1, maxLength: 8000 }), current_view: Type.String({ minLength: 1, maxLength: 8000 }),
    reason: Type.String({ minLength: 1, maxLength: 8000 }), next_test: Type.Optional(Type.String({ minLength: 1, maxLength: 8000 }))
  }, { additionalProperties: false })),
  next_question: Type.Optional(Type.String({ minLength: 1, maxLength: 8000 })),
  unresolved: Type.Optional(Type.String({ minLength: 1, maxLength: 8000 })),
  continuation: Type.Optional(Type.Union([
    Type.Object({
      requested: Type.Optional(Type.Literal(true)),
      objective: Type.String({ minLength: 1, maxLength: 8000,
        description: "Concrete follow-up work. Omit the continuation object entirely when no follow-up is needed." }),
      after_task_ids: Type.Optional(Type.Array(Type.String({ minLength: 1 }),
        { description: "Task IDs whose terminal results are required before this follow-up starts; omit only for an independent follow-up." }))
    }, { additionalProperties: false }),
    // Structured opt-out. Never encode this choice in objective prose.
    Type.Object({ requested: Type.Literal(false,
      { description: "Record that this checkpoint requests no follow-up task." }) }, { additionalProperties: false })
  ]))
}, { additionalProperties: false });

export default function (pi: any) {
  let inboxVersion: number | undefined;
  let deliveryQueue: Promise<any> = Promise.resolve();
  let acceptedSubmissionCallId: string | undefined;
  const register = (name: string, description: string, parameters: any) => pi.registerTool({
    name, label: name, description, parameters,
    async execute(id: string, args: object) {
      const value = await request(name, id, name === "rsi_wait"
        ? { ...args, after_message_version: inboxVersion } : args);
      if (name === "rsi_inbox" && value.ok) inboxVersion = Math.max(inboxVersion ?? 0, value.next_version);
      if (value.ok && value.inbox) inboxVersion = Math.max(inboxVersion ?? 0, value.inbox.next_version);
      return { content: [{ type: "text", text: JSON.stringify(value) }],
        details: value.ok ? value : { error: value.error } };
    },
  });
  if (["analyzer", "critic"].includes(process.env.RSI_AGENT_ROLE || "")) {
    pi.registerTool({
      name: "bash", label: "bash",
      description: "Run a bounded, network-isolated audit command. Read frozen Run files under /evidence and write scripts or derived results under /work. The returned receipt is diagnostic evidence, not a formal Run.",
      parameters: Type.Object({ command: Type.String({ minLength: 1, maxLength: 12000 }),
        timeout: Type.Optional(Type.Integer({ minimum: 1, maximum: 60 })) }, { additionalProperties: false }),
      async execute(id: string, args: object) {
        const value = await request("rsi_audit_bash", id, args);
        const passed = value.ok && value.exit_code === 0 && !value.timed_out && !value.integrity_error;
        return { content: [{ type: "text", text: JSON.stringify(value) }],
          details: passed ? value : { error: value.error || { code: "audit_command_failed",
            message: value.integrity_error || `Audit command exited with status ${value.exit_code}` }, ...value } };
      },
    });
  }
  register("rsi_delegate", "Persist a child Agent task now. Delegation count and recursion depth are unlimited; returns a task ID or correctable error.",
    Type.Object(delegation));
  register("rsi_research", "Create or revise a versioned research node, record a proposed relation, compose fixed historical inputs into a new node and queued researcher task, or read the graph. Every mutation requires a nonempty data.reason; snapshot needs no data. For create, data.parent is an exact {node_id, revision} reference; when omitted, the new question decomposes the actor's bound node or the Mission root. Only a coordinator may set data.separate_root=true to create an independent root. Composition preserves its source branches and does not imply experimental support.",
    Type.Object({ action: Type.Union(["create", "revise", "relate", "compose", "snapshot"].map(action => Type.Literal(action))),
      data: Type.Optional(researchData),
      reason: Type.Optional(Type.String({ description: "Legacy fallback only; use data.reason for new calls. Rejected if it conflicts with data.reason." })),
      at_version: Type.Optional(Type.Integer({ minimum: 1 })) }));
  register("rsi_research_checkpoint", "Save a source-addressed Researcher checkpoint. It summarizes changed views and unresolved questions; it is not a formal finding. A continuation is queued only when explicitly requested and remains gated on its source/dependencies. Runtime enforces UTF-8 byte limits (the TypeBox limits are character limits).",
    researchCheckpoint);
  register("rsi_run_probe", "Queue a controlled exploratory Python probe. Runner performs preflight and bounded execution, returns code-contract failures to the source Coder, and reports a typed receipt to the Researcher; review_roles is metadata only. An identical source/script/input fingerprint is rejected unless recheck_reason explicitly explains the intentional replay.",
    Type.Object({ objective: Type.String(), idea_id: delegation.idea_id,
      research_node_id: delegation.research_node_id, research_revision: delegation.research_revision,
      experiment: Type.Object({ source_task_id: Type.String(), script_file_id: Type.String({ description: "Published Python script file_id from a completed Agent handoff" }), question: Type.String(),
        baseline: Type.String(), expected_difference: Type.String(), falsifier: Type.String(),
        environment: Type.Optional(Type.String({ minLength: 1, description: "Authorized Runner environment name from exploration_contract.available_environments; omit for Mission default" })),
        source_reason: Type.String(), review_roles: Type.Optional(Type.Array(Type.String())),
        recheck_reason: Type.Optional(Type.String({ minLength: 1, description: "Required when this exact source/script/input fingerprint intentionally repeats an existing exploration" })) },
        { additionalProperties: false }) }));
  register("rsi_register_hypothesis", "Persist a complete Hypothesis revision during the run; use the returned version for Candidate admission.",
    Type.Object({ hypothesis_id: Type.String(), idea_id: optionalId, statement: Type.String(), mechanism: Type.String(),
      assumptions: flexibleList, predictions: flexibleList,
      experimental_design: Type.Union([Type.String(), jsonObject]),
      prior_art_status: Type.Union([Type.String(), jsonObject]),
      status: Type.Optional(Type.Union([Type.Literal("developing"), Type.Literal("testable"),
        Type.Literal("candidate_ready"), Type.Literal("candidate-ready"), Type.Literal("candidate ready"), Type.Literal("paused")])),
      expected_version: flexibleInteger(0), revision_reason: Type.Optional(Type.String()) },
      { additionalProperties: false }));
  register("rsi_register_candidate", "Admit a candidate-ready Hypothesis as a Candidate during the run; then delegate coder or Runner work directly.",
    Type.Object({ candidate_id: Type.String(), idea_id: optionalId,
      kind: Type.Optional(Type.Union([Type.Literal("hypothesis"), Type.Literal("baseline")])),
      hypothesis_id: Type.Optional(Type.String()), hypothesis_version: Type.Optional(flexibleInteger(1)),
      hypothesis: Type.Optional(Type.String()), expected_observation: Type.Optional(Type.String()),
      protocol_id: Type.String(), config: Type.Union([jsonObject, Type.String()]), purpose: Type.Optional(Type.String()),
      algorithm: Type.Optional(jsonObject),
      parent_id: Type.Optional(Type.String()), parent_selection_reason: Type.Optional(Type.String()),
      baseline_reason: Type.Optional(Type.String()) }, { additionalProperties: false }));
  register("rsi_branch_control", "Coordinator-only intervention: cancel queued descendants and send a durable stop request to running branch tasks.",
    Type.Object({ action: Type.Optional(Type.Literal("stop")), task_id: Type.String(), reason: Type.String() },
      { additionalProperties: false }));
  register("rsi_message", "Persist an attributed Idea-scoped message now; recording does not mean the recipient has read it.",
    Type.Object({ text: Type.String(), idea_id: optionalId, to_task_id: optionalId,
      to_role: optionalId, in_reply_to: optionalId }));
  register("rsi_inbox", "Read messages received since task input was frozen and see active branch tasks before duplicating work. Results are attributed communications, not verified evidence.",
    Type.Object({ after_version: Type.Optional(Type.Integer({ minimum: 0 })),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 20 })) }));
  register("rsi_status", "Read a descendant task's durable status and available result.",
    Type.Object({ task_id: Type.String(), checkpoint_id: Type.Optional(Type.String({ description: "Optional checkpoint ID for one complete checkpoint payload; ordinary status returns bounded excerpts" })) }));
  register("rsi_wait", "Wait for a descendant result or a new addressed message. Check status on return; a message can wake you while the child continues. An explicit continuation requested by this same task cannot be awaited until this task finishes.",
    Type.Object({ task_id: Type.String() }));
  register("rsi_read_handoff", "Read a completed child Agent's published UTF-8 file by its file_id; supports paged reads.",
    Type.Object({ task_id: Type.String(), file_id: Type.String(), offset: Type.Optional(Type.Integer({ minimum: 0 })),
      limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 12000 })) }));
  register("rsi_wiki_check", "Validate your editable Wiki proposals before finishing; returns correctable source, schema, and link errors.",
    Type.Object({ reason: Type.Optional(Type.String({ maxLength: 4000,
      description: "Optional explanation for the check; does not change validation." })) }, { additionalProperties: false }));
  if (process.env.RSI_AGENT_ROLE === "curator") {
    pi.registerTool({
      name: "rsi_wiki_submit", label: "rsi_wiki_submit", executionMode: "sequential",
      description: "Submit complete Wiki proposal files from your task workspace. Runtime validates and durably accepts the exact files before the task finishes. Use decision no_change with an empty paths list when no publication is needed. An accepted receipt completes this task; validation errors remain correctable.",
      parameters: Type.Object({
        paths: Type.Array(Type.String({ minLength: 1, maxLength: 4096 }), { maxItems: 20 }),
        summary: Type.String({ minLength: 1, maxLength: 4000 }),
        decision: Type.Optional(Type.Union([Type.Literal("publish"), Type.Literal("no_change")], { default: "publish" })),
      }, { additionalProperties: false }),
      async execute(id: string, args: any) {
        if (acceptedSubmissionCallId !== undefined) {
          const error = { code: "submission_already_accepted", message: "This Curator task already has an accepted submission" };
          return { content: [{ type: "text", text: JSON.stringify({ ok: false, error }) }], details: { error }, terminate: true };
        }
        const value = await request("rsi_wiki_submit", id, { ...args, decision: args.decision ?? "publish" });
        const accepted = value.ok === true && value.status === "accepted";
        if (accepted) acceptedSubmissionCallId = id;
        return { content: [{ type: "text", text: JSON.stringify(value) }],
          details: value.ok ? value : { error: value.error }, ...(accepted ? { terminate: true } : {}) };
      },
    });
    pi.on("tool_call", () => acceptedSubmissionCallId === undefined ? undefined : {
      block: true, terminate: true, reason: "This Curator task already has an accepted submission" });
    pi.on("message_end", (event: any, ctx: any) => {
      if (acceptedSubmissionCallId !== undefined && event.message?.role === "toolResult" &&
          event.message.toolName === "rsi_wiki_submit" && event.message.toolCallId === acceptedSubmissionCallId)
        ctx.abort();
    });
  }
  pi.on("tool_result", (event: any) => {
    if (acceptedSubmissionCallId !== undefined) return;
    if (!process.env.RSI_TOOL_SOCKET || event.toolName === "rsi_inbox") return;
    const delivery = deliveryQueue.then(async () => {
      const value = await request("rsi_inbox", `inbox:${event.toolCallId}:${inboxVersion}`,
        { after_version: inboxVersion });
      if (!value.ok) return; // Older running bridges retain their frozen contract.
      inboxVersion = Math.max(inboxVersion ?? 0, value.next_version);
      if (!value.messages.length) return;
      return { content: [...event.content, { type: "text", text:
        "RSI live inbox: attributed collaborator messages (not verified evidence). Consider whether they change your next action.\n" + JSON.stringify(value) }] };
    });
    deliveryQueue = delivery.catch(() => undefined);
    return delivery;
  });
}
