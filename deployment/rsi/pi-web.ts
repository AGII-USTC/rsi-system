import { createRequire } from "node:module";
import { homedir } from "node:os";
import { join } from "node:path";
// Install pi-web-access in the local Pi extension directory before enabling
// this optional adapter; the standalone bundle does not assume a host path.
export default async function (pi: any) {
  const prefix = join(homedir(), ".local/share/rsi/pi");
  const require = createRequire(join(prefix, "node_modules/@earendil-works/pi-coding-agent/package.json"));
  const { Type } = require("typebox");
  const { createJiti } = require("jiti");
  const jiti = createJiti(import.meta.url);
  const { default: webAccess } = await jiti.import(join(prefix, "node_modules/pi-web-access/index.ts"));
  const disabledTools = new Set<string>(JSON.parse(process.env.RSI_AGENT_DISABLED_TOOLS || "[]"));
  const scoped = new Proxy(pi, {
    get(target, key) {
      if (key !== "registerTool") {
        const value = Reflect.get(target, key);
        return typeof value === "function" ? value.bind(target) : value;
      }
      return (tool: any) => {
        if (disabledTools.has(tool.name)) return;
        if (tool.name === "web_search") {
          pi.registerTool({
            ...tool,
            description: "Search public web pages using the configured Exa provider. Return source URLs and excerpts; verify important claims against primary pages.",
            promptSnippet: "Search public web sources. Use varied queries and cite the returned URLs. Search failure is not evidence.",
            parameters: Type.Object({
              query: Type.Optional(Type.String()),
              queries: Type.Optional(Type.Array(Type.String(), { minItems: 1, maxItems: 4 })),
              numResults: Type.Optional(Type.Integer({ minimum: 1, maximum: 10 })),
              recencyFilter: Type.Optional(Type.Union([Type.Literal("day"), Type.Literal("week"), Type.Literal("month"), Type.Literal("year")])),
              domainFilter: Type.Optional(Type.Array(Type.String(), { maxItems: 5 })),
            }),
            execute: (id: string, params: any, signal: any, onUpdate: any, ctx: any) =>
              tool.execute(id, { ...params, provider: "exa", workflow: "none" }, signal, onUpdate, ctx),
          });
        } else if (tool.name === "fetch_content") {
          pi.registerTool({
            ...tool,
            description: "Read up to three public HTTPS pages as text. Use source URLs returned by web_search.",
            promptSnippet: "Read public HTTPS source pages to verify search excerpts.",
            parameters: Type.Object({
              url: Type.Optional(Type.String()),
              urls: Type.Optional(Type.Array(Type.String(), { minItems: 1, maxItems: 3 })),
            }),
            execute: (id: string, params: any, signal: any, onUpdate: any, ctx: any) =>
              tool.execute(id, { ...params, mode: "readable" }, signal, onUpdate, ctx),
          });
        } else if (tool.name === "get_search_content") {
          pi.registerTool(tool);
        }
      };
    },
  });
  return webAccess(scoped);
}
