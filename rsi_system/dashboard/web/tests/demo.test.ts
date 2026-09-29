import assert from "node:assert/strict";
import { test } from "node:test";
import { demoSnapshot, demoStageAt } from "../src/demo";
import { PET_PRESETS, petForTask } from "../src/world/pets";

const START = Date.parse("2026-09-27T00:00:00.000Z");

test("demo introduces research leaders before specialist roles", () => {
  assert.equal(demoStageAt(-1), 0);
  assert.equal(demoStageAt(3.9), 0);
  assert.equal(demoStageAt(4), 1);
  assert.equal(demoStageAt(16), 3);
  assert.equal(demoStageAt(39), 7);
  assert.equal(demoStageAt(45.9), 7);
  assert.equal(demoStageAt(46), 8);
  const first = demoSnapshot("测试目标", 0, START);
  assert.deepEqual(first.tasks.map((task) => task.role), ["coordinator"]);
  assert.equal(first.events.some((event) => event.to_task_id === "demo-coder"), false);
  assert.equal(demoSnapshot("测试目标", 16, START).tasks.filter((task) => task.role === "researcher").length, 3);
  assert.equal(demoSnapshot("测试目标", 38, START).tasks.some((task) => task.role === "coder"), false);
  assert.equal(demoSnapshot("测试目标", 39, START).tasks.some((task) => task.role === "coder"), true);
  assert.equal(demoSnapshot("测试目标", 50, START).tasks.length, 9);
  assert.deepEqual(demoSnapshot("测试目标", 60, START).tasks.slice(-3).map((task) => task.role), ["reviewer", "analyzer", "student"]);
});

test("stage updates retain task identities, creation times, and valid event references", () => {
  const early = demoSnapshot("资料研究", 5, START);
  const late = demoSnapshot("资料研究", 50, START);
  const lateById = new Map(late.tasks.map((task) => [task.task_id, task]));
  for (const task of early.tasks) {
    const same = lateById.get(task.task_id);
    assert.ok(same);
    assert.equal(same.created_at, task.created_at);
    assert.equal(same.parent_task_id, task.parent_task_id);
  }
  assert.equal(new Set(late.tasks.map((task) => task.task_id)).size, late.tasks.length);
  for (const event of late.events) {
    if (event.from_task_id) assert.ok(lateById.has(event.from_task_id));
    if (event.to_task_id) assert.ok(lateById.has(event.to_task_id));
    if (event.task_id) assert.ok(lateById.has(event.task_id));
  }
  assert.deepEqual(late.events.map((event) => event.event_id), [...late.events.map((event) => event.event_id)].sort((a, b) => b - a));
  assert.ok(late.tasks.every((task) => PET_PRESETS.some((preset) => preset.id === petForTask(task).id)));
  assert.equal(petForTask(lateById.get("demo-main")!).id, "kurisu");
  assert.equal(petForTask(lateById.get("demo-coder")!).id, "generated-nuan");
  const professionLate = demoSnapshot("资料研究", 61, START);
  const professionById = new Map(professionLate.tasks.map((task) => [task.task_id, task]));
  assert.equal(petForTask(professionById.get("demo-reviewer")!).id, "generated-product-manager");
  assert.equal(petForTask(professionById.get("demo-analyzer")!).id, "generated-researcher");
  assert.equal(petForTask(professionById.get("demo-student")!).id, "generated-student");
  assert.deepEqual(late.events.filter((event) => event.kind === "task_created").map((event) => event.created_at).reverse(), [4, 10, 16, 23, 29, 34, 39, 46].map((second) => new Date(START + second * 1000).toISOString()));
});
