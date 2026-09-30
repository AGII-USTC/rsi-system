import type { Config, ProviderDiscovery, ResearchGraph, ResearchNode, RuntimeJob, RuntimeState, Snapshot } from "./types";

async function jsonRequest<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 6000);
  try {
    response = await fetch(path, { cache: "no-store", ...options, signal: controller.signal });
    let payload: unknown;
    try { payload = await response.json(); }
    catch { throw new Error("服务返回了无法读取的数据"); }
    if (!response.ok) {
      const error = payload && typeof payload === "object" && "error" in payload ? String(payload.error) : `请求失败 (${response.status})`;
      throw new Error(error);
    }
    return payload as T;
  } catch (error) {
    if (controller.signal.aborted) throw new Error("本机服务响应超时");
    if (error instanceof TypeError) throw new Error("无法连接到本机 RSI 服务");
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

export function fetchState(mission?: string): Promise<Snapshot> {
  return jsonRequest<Snapshot>(`/api/state${mission ? `?mission=${encodeURIComponent(mission)}` : ""}`);
}

export function fetchResearch(mission: string): Promise<ResearchGraph> {
  return jsonRequest<ResearchGraph>(`/api/research?mission_id=${encodeURIComponent(mission)}`);
}

export function proposeResearchNode(body: {
  mission_id: string;
  parent: { node_id: string; revision: number };
  question: string;
  scope: string;
  summary: string;
}): Promise<{ node: ResearchNode; mission_version: number }> {
  return jsonRequest("/api/research/nodes", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
}

export function fetchConfig(): Promise<Config> {
  return jsonRequest<Config>("/api/config");
}

export function saveProvider(body: object): Promise<Config> {
  return jsonRequest<Config>("/api/provider", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function discoverProvider(body: object): Promise<ProviderDiscovery> {
  return jsonRequest<ProviderDiscovery>("/api/provider/discover", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function saveRoles(body: object): Promise<Config> {
  return jsonRequest<Config>("/api/roles", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function fetchRuntime(): Promise<RuntimeState> {
  return jsonRequest<RuntimeState>("/api/runtime");
}

export function createMission(body: object): Promise<{ mission_id: string; status: string }> {
  return jsonRequest("/api/missions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

export function missionAction(id: string, action: "run" | "pause" | "resume", body: object = {}): Promise<{ mission_id: string; status: string; job?: RuntimeJob }> {
  return jsonRequest(`/api/missions/${encodeURIComponent(id)}/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}
