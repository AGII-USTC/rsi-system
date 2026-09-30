import { createRequire } from "node:module";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// Isolated read-only research server; its dedicated key stays outside the repository.
export default async function (pi: any) {
  const prefix = join(homedir(), ".local/share/rsi/pi");
  const require = createRequire(join(prefix, "node_modules/@earendil-works/pi-coding-agent/package.json"));
  const { createJiti } = require("jiti");
  const jiti = createJiti(import.meta.url);
  const { createMcpAdapter } = await jiti.import(join(prefix, "node_modules/pi-mcp-adapter/index.ts"));
  const credential = join(homedir(), ".config/rsi/alphaxiv.key");
  return createMcpAdapter({ config: { mcpServers: {
    alphaxiv: {
      url: "https://api.alphaxiv.org/mcp/v1", auth: "bearer",
      bearerToken: existsSync(credential) ? `!cat "${credential}"` : "",
      lifecycle: "lazy", requestTimeoutMs: 90000
    }
  }}})(pi);
}
