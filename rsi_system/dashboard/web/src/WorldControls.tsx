import { useState } from "react";
import { Check, Settings2, X } from "lucide-react";
import type { WorldSettings } from "./worldTypes";

export const defaultWorldSettings: WorldSettings = { style: "meadow", seed: "rsi-garden", layout: "open", motion: true };

export function loadWorldSettings(): WorldSettings {
  try {
    const saved = JSON.parse(window.localStorage.getItem("rsi-world-settings") || "null") as Partial<WorldSettings> | null;
    if (!saved) return defaultWorldSettings;
    return { style: saved.style === "office" ? "office" : "meadow", seed: typeof saved.seed === "string" ? saved.seed.slice(0, 100) : defaultWorldSettings.seed, layout: saved.layout === "clustered" ? "clustered" : "open", motion: saved.motion !== false, description: typeof saved.description === "string" ? saved.description.slice(0, 300) : "" };
  } catch { return defaultWorldSettings; }
}

export function WorldControls({ value, onChange }: { value: WorldSettings; onChange: (settings: WorldSettings) => void }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(value);
  const [feedback, setFeedback] = useState(false);
  const save = () => {
    onChange(draft); setFeedback(true);
  };
  return <div className="world-options"><button className="icon-button" type="button" aria-label="场景设置" title="场景设置" aria-expanded={open} onClick={() => { setOpen((current) => !current); setDraft(value); setFeedback(false); }}><Settings2 size={16} /></button>
    {open && <section className="world-options-popover" aria-label="场景设置"><div className="world-options-heading"><strong>场景设置</strong><button className="icon-button" type="button" aria-label="关闭场景设置" onClick={() => setOpen(false)}><X size={15} /></button></div><label>场景预设</label><div className="segmented"><button type="button" className={draft.style === "office" ? "active" : ""} onClick={() => { setDraft({ ...draft, style: "office" }); setFeedback(false); }}>工作室</button><button type="button" className={draft.style === "meadow" ? "active" : ""} onClick={() => { setDraft({ ...draft, style: "meadow" }); setFeedback(false); }}>草地</button></div>
      <label className="world-motion"><input type="checkbox" checked={draft.motion} onChange={(event) => { setDraft({ ...draft, motion: event.target.checked }); setFeedback(false); }} /> 角色动作</label>
      <div className="form-footer"><button className="primary-button" type="button" onClick={save}>应用设置</button>{feedback && <span className="feedback success" role="status"><Check size={13} /> 场景已更新</span>}</div>
    </section>}
  </div>;
}
