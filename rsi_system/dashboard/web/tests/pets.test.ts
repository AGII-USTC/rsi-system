import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";
import { petForTask, petFrame, validPetSheet, PET_PRESETS, type PetFrame } from "../src/world/pets";
import { actionForTask, type Action, type Actor } from "../src/world/engine";

const actor = (action: Action, direction = 0, walking = false): Pick<Actor, "action" | "direction" | "path"> => ({
  action, direction, path: walking ? [{ col: 1, row: 1 }] : [],
});

test("task identity is stable and role choices use imported presets", () => {
  const choices = new Set(Array.from({ length: 20 }, (_, index) => petForTask({ task_id: `research-${index}`, role: "researcher" }).id));
  assert.deepEqual(choices, new Set(["violet", "liuying-firefly-pixel", "feibi"]));
  const leaders = ["demo-research-a", "demo-research-b", "demo-research-c"].map((task_id) => petForTask({ task_id, role: "researcher" }).id);
  assert.equal(new Set(leaders).size, 3);
  const first = petForTask({ task_id: "research-8", role: "researcher" }).id;
  petForTask({ task_id: "research-new", role: "researcher" });
  assert.equal(petForTask({ task_id: "research-8", role: "researcher" }).id, first);
  assert.ok(PET_PRESETS.some((preset) => preset.id === first));
});

test("specialist casting stays tied to roles across task identities", () => {
  const cast = {
    coordinator: "kurisu",
    coder: "generated-nuan",
    runner: "generated-student",
    hypothesis: "bocchi",
    critic: "eren",
    analyzer: "generated-researcher",
    reviewer: "generated-product-manager",
    student: "generated-student",
  } as const;
  for (const [role, id] of Object.entries(cast)) {
    for (const task_id of ["first-task", "another-task", "unrelated-work-42"]) {
      assert.equal(petForTask({ task_id, role }).id, id, `${role} casting changed for ${task_id}`);
    }
  }
  const leftGroup = [
    petForTask({ task_id: "demo-research-a", role: "researcher" }).id,
    petForTask({ task_id: "demo-runner", role: "runner" }).id,
    petForTask({ task_id: "demo-analyzer", role: "analyzer" }).id,
  ];
  assert.equal(new Set(leftGroup).size, 3);
});

test("sheet dimensions match the declared sprite version", () => {
  assert.equal(validPetSheet({ naturalWidth: 1536, naturalHeight: 1872 }, 1), true);
  assert.equal(validPetSheet({ naturalWidth: 1536, naturalHeight: 2288 }, 2), true);
  assert.equal(validPetSheet({ naturalWidth: 1536, naturalHeight: 2288 }, 1), false);
  assert.equal(validPetSheet({ naturalWidth: 1535, naturalHeight: 1872 }, 1), false);
});

test("every catalog preset points at an imported runtime sheet", () => {
  for (const preset of PET_PRESETS) {
    assert.ok(preset.sheet.startsWith("/assets/pets/"), `${preset.id} escaped the pet asset root`);
    assert.ok(existsSync(resolve(process.cwd(), "public", preset.sheet.slice(1))), `${preset.id} sheet is missing`);
  }
});

test("settled office actions vary by role while travel always wins", () => {
  const settled = (role: string, waiting = false, status: string = "running") => actionForTask({ role, waiting, status });
  assert.equal(settled("coder"), "work");
  assert.equal(settled("researcher"), "read");
  assert.equal(settled("hypothesis"), "write");
  assert.equal(settled("reviewer"), "archive");
  assert.equal(settled("coordinator", true), "nap");
  assert.equal(settled("coder", false, "completed"), "idle");
  assert.equal(petFrame(actor("work", 2, true), 0, true).row, 1);
  assert.equal(petFrame(actor("read", 1, true), 0, true).row, 2);
});

test("action rows and walk directions stay inside populated frames", () => {
  const rows: [Action, number][] = [
    ["idle", 0], ["wave", 3], ["failed", 5], ["nap", 0], ["talk", 6],
    ["work", 7], ["read", 8], ["write", 8], ["archive", 8],
  ];
  for (const [action, row] of rows) {
    for (const time of [0, 0.19, 1.7, 37.42]) {
      const frame: PetFrame = petFrame(actor(action), time, true);
      assert.equal(frame.row, row);
      assert.ok(frame.column >= 0 && frame.column < frame.count);
    }
  }
  assert.equal(petFrame(actor("idle", 2, true), 1.4, true).row, 1);
  assert.equal(petFrame(actor("idle", 1, true), 1.4, true).row, 2);
  assert.equal(petFrame(actor("idle", 3, true), 1.4, true).row, 2);
});

test("paused motion holds a valid first frame for every action", () => {
  for (const action of ["idle", "wave", "failed", "nap", "work", "read", "talk"] as Action[]) {
    assert.equal(petFrame(actor(action), 99.5, false).column, 0);
  }
  assert.notEqual(petFrame(actor("work"), 4.5, true).column, petFrame(actor("work"), 4.7, true).column);
  assert.deepEqual(petFrame(actor("work"), 4.5, false), petFrame(actor("work"), 4.7, false));
});
