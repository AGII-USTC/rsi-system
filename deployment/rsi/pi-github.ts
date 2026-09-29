import { createRequire } from "node:module";
import { homedir } from "node:os";
import { join } from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const prefix = join(homedir(), ".local/share/rsi/pi");
const require = createRequire(join(prefix, "node_modules/@earendil-works/pi-coding-agent/package.json"));
const { Type } = require("typebox");
const execFileAsync = promisify(execFile);

async function githubGet(path: string): Promise<any> {
  const { stdout } = await execFileAsync("curl", [
    "-q", "--silent", "--show-error", "--fail", "--max-time", "12", "--max-filesize", "500000",
    "--proto", "=https", "--max-redirs", "0", "--request", "GET",
    "--header", "Accept: application/vnd.github+json", "--user-agent", "embodied-rsi-readonly",
    `https://api.github.com${path}`,
  ], { encoding: "utf8", maxBuffer: 500_000, timeout: 15_000 });
  return JSON.parse(stdout);
}

const result = (value: object) => ({
  content: [{ type: "text", text: JSON.stringify(value) }],
  details: value,
});

export default function (pi: any) {
  pi.registerTool({
    name: "github_search",
    label: "Search public GitHub repositories",
    description: "Read-only search for public GitHub repositories. Returns repository metadata and license hints; inspect files before choosing an implementation.",
    parameters: Type.Object({ query: Type.String({ description: "GitHub repository search terms" }) }),
    async execute(_id: string, params: { query: string }) {
      const query = params.query.trim();
      if (query.length < 2 || query.length > 200) throw new Error("GitHub query must be 2-200 characters");
      const data = await githubGet(`/search/repositories?q=${encodeURIComponent(query)}&per_page=5`);
      return result({ query, total_count: data.total_count, repositories: data.items.map((item: any) => ({
        full_name: item.full_name, html_url: item.html_url, description: item.description,
        default_branch: item.default_branch, updated_at: item.updated_at,
        stars: item.stargazers_count, license_hint: item.license?.spdx_id ?? null,
      })) });
    },
  });

  pi.registerTool({
    name: "github_read_file",
    label: "Read a public GitHub file",
    description: "Read one file from a public GitHub repository, such as README, LICENSE, package metadata, or source. Returns its blob SHA and a bounded text excerpt.",
    parameters: Type.Object({
      repository: Type.String({ description: "Public owner/repository name" }),
      path: Type.String({ description: "File path within the repository" }),
      ref: Type.Optional(Type.String({ description: "Branch, tag, or commit; defaults to the repository default branch" })),
    }),
    async execute(_id: string, params: { repository: string; path: string; ref?: string }) {
      if (!/^[A-Za-z0-9][A-Za-z0-9_.-]*\/[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(params.repository))
        throw new Error("Invalid GitHub repository name");
      const parts = params.path.split("/");
      if (params.path.length > 240 || parts.some(part => !part || part === "." || part === ".." || /[\x00-\x1f]/.test(part)))
        throw new Error("Invalid GitHub file path");
      if (params.ref && (params.ref.length > 120 || /[\x00-\x1f]/.test(params.ref)))
        throw new Error("Invalid GitHub ref");
      const [owner, repo] = params.repository.split("/");
      const file = parts.map(encodeURIComponent).join("/");
      const ref = params.ref ? `?ref=${encodeURIComponent(params.ref)}` : "";
      const data = await githubGet(`/repos/${owner}/${repo}/contents/${file}${ref}`);
      if (data.type !== "file" || data.encoding !== "base64" || typeof data.content !== "string")
        throw new Error("GitHub path is not a readable file");
      const content = Buffer.from(data.content, "base64").toString("utf8");
      return result({ repository: params.repository, path: params.path, ref: params.ref ?? null,
        html_url: data.html_url, sha: data.sha, size: data.size,
        content: content.slice(0, 40_000), truncated: content.length > 40_000 });
    },
  });
}
