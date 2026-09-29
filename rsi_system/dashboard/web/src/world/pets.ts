import type { Task } from "../types";
import type { Actor } from "./engine";

export interface PetPreset {
  id: string;
  name: string;
  version: 1 | 2;
  sheet: string;
}

// Names and sheet paths match the imported pet.json manifests.
export const PET_PRESETS = [
  { id: "kurisu", name: "Kurisu", version: 1, sheet: "/assets/pets/kurisu/spritesheet.webp" },
  { id: "nezu", name: "Nezuko", version: 1, sheet: "/assets/pets/nezu/spritesheet.webp" },
  { id: "violet", name: "Violet", version: 1, sheet: "/assets/pets/violet/spritesheet.webp" },
  { id: "liuying-firefly-pixel", name: "流萤", version: 1, sheet: "/assets/pets/liuying-firefly-pixel/spritesheet.webp" },
  { id: "bocchi", name: "bocchi", version: 1, sheet: "/assets/pets/bocchi/spritesheet.webp" },
  { id: "eren", name: "Eren", version: 1, sheet: "/assets/pets/eren/spritesheet.webp" },
  { id: "guga", name: "咕嘎", version: 1, sheet: "/assets/pets/guga/spritesheet.webp" },
  { id: "feibi", name: "菲比", version: 1, sheet: "/assets/pets/feibi/spritesheet.webp" },
  { id: "generated-nuan", name: "暖暖", version: 1, sheet: "/assets/pets/generated-nuan/spritesheet.png" },
  { id: "generated-product-manager", name: "小暖·产品经理", version: 1, sheet: "/assets/pets/generated-product-manager/spritesheet.png" },
  { id: "generated-student", name: "书书·学生", version: 1, sheet: "/assets/pets/generated-student/spritesheet.png" },
  { id: "generated-researcher", name: "探探·研究员", version: 1, sheet: "/assets/pets/generated-researcher/spritesheet.png" },
] as const satisfies readonly PetPreset[];

export type PetId = (typeof PET_PRESETS)[number]["id"];
export type ImportedPetPreset = (typeof PET_PRESETS)[number];
export type PetSheets = Partial<Record<PetId, HTMLImageElement>>;

const byId = Object.fromEntries(PET_PRESETS.map((preset) => [preset.id, preset])) as Record<PetId, ImportedPetPreset>;
const rolePresets: Record<string, PetId> = {
  coordinator: "kurisu",
  // The demo includes demo-coder. Keep that task identity stable while showing
  // the laptop-capable generated character where users can actually see it.
  coder: "generated-nuan",
  // The runner is visible in the compact mobile overview; use the generated
  // student atlas there so the profession sheets are not desktop-only.
  runner: "generated-student",
  hypothesis: "bocchi",
  critic: "eren",
  analyzer: "generated-researcher",
  // Keep the reviewer visibly distinct from the coder. This profession atlas
  // contains a clipboard/review pose and is one of the imported local sheets.
  reviewer: "generated-product-manager",
  student: "generated-student",
};
const roleChoices: Record<string, readonly PetId[]> = {
  researcher: ["violet", "liuying-firefly-pixel", "feibi"],
};
// Unknown roles are deliberately spread over every non-coordinator preset.
// This keeps task identity stable while ensuring imported profession atlases
// are actually visible when an API introduces a new role.
const otherChoices: readonly PetId[] = [
  "nezu", "guga", "bocchi", "eren", "feibi", "violet", "liuying-firefly-pixel",
  "generated-nuan", "generated-product-manager", "generated-student", "generated-researcher",
];

function hash(value: string): number {
  let number = 2166136261;
  for (let i = 0; i < value.length; i++) number = Math.imul(number ^ value.charCodeAt(i), 16777619);
  return number >>> 0;
}

export function petForTask(task: Pick<Task, "task_id" | "role">): ImportedPetPreset {
  const presetId = rolePresets[task.role];
  if (presetId) return byId[presetId];
  const choices = roleChoices[task.role] || otherChoices;
  return byId[choices[hash(task.task_id) % choices.length]];
}

export const PET_CELL = { width: 192, height: 208, columns: 8 } as const;
export const PET_ROW_FRAMES = [6, 8, 8, 4, 5, 8, 6, 6, 6] as const;

export function validPetSheet(image: Pick<HTMLImageElement, "naturalWidth" | "naturalHeight">, version: 1 | 2): boolean {
  return image.naturalWidth === PET_CELL.width * PET_CELL.columns && image.naturalHeight === PET_CELL.height * (version === 2 ? 11 : 9);
}

export async function loadPetSheets(): Promise<PetSheets> {
  const entries = await Promise.all(PET_PRESETS.map((preset) => new Promise<readonly [PetId, HTMLImageElement] | null>((resolve) => {
    const image = new Image();
    image.onload = () => resolve(validPetSheet(image, preset.version) ? [preset.id, image] : null);
    image.onerror = () => resolve(null);
    image.src = preset.sheet;
  })));
  return Object.fromEntries(entries.filter((entry): entry is readonly [PetId, HTMLImageElement] => entry !== null)) as PetSheets;
}

export interface PetFrame { row: number; column: number; count: number }

export function petFrame(actor: Pick<Actor, "path" | "direction" | "action">, time: number, motion: boolean): PetFrame {
  let row: number;
  if (actor.path.length) row = actor.direction === 1 || actor.direction === 3 ? 2 : 1;
  else if (actor.action === "wave") row = 3;
  else if (actor.action === "failed") row = 5;
  else if (actor.action === "talk") row = 6;
  else if (actor.action === "nap") row = 0;
  else if (actor.action === "work") row = 7;
  else if (actor.action === "read" || actor.action === "write" || actor.action === "archive") row = 8;
  else row = 0;
  const count = PET_ROW_FRAMES[row];
  const fps = row === 1 || row === 2 ? 8 : row === 6 || row === 0 ? 3 : 5;
  const column = motion && Number.isFinite(time) ? Math.floor(Math.max(0, time) * fps) % count : 0;
  return { row, column, count };
}
