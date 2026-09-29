import assert from "node:assert/strict";
import test from "node:test";
import { chosenTasks, clearTransient, eventCaption, knowledgeAction, makeWorld, replayEvent, setWorldFocus, syncWorld, updateWorld } from "../src/world/engine";
import { demoSnapshot, demoStageAt } from "../src/demo";
import { drawMeadow } from "../src/world/meadow";
import type { Event, Task } from "../src/types";

const settings = { style: "meadow" as const, seed: "test", layout: "open" as const, motion: true };
const NOW = new Date().toISOString();
function task(id: string, parent: string | null, role = "coder", order = 0): Task {
  return {
    task_id: id, parent_task_id: parent, role, status: "running", objective: id, summary: id,
    waiting: role === "coordinator", reason: null, candidate_id: null, error: null,
    reply_to_task_id: null, idea_id: null, created_at: new Date(Date.UTC(2020, 0, 1, 0, order)).toISOString(),
    updated_at: new Date(Date.UTC(2020, 0, 1, 0, order)).toISOString(),
  };
}
function created(parent: string, child: string, id = 100): Event {
  return { event_id: id, kind: "task_created", title: "委派", text: "开始研究", from_task_id: parent, to_task_id: child, task_id: child, from_role: "coordinator", to_role: "researcher", created_at: NOW };
}
function spaced(world: ReturnType<typeof makeWorld>): void {
  const actors = [...world.actors.values()];
  for (const actor of actors) {
    assert.ok(actor.home.x >= 33 && actor.home.x <= world.width - 33);
    assert.ok(actor.home.y >= (world.mobile ? 300 : 250) && actor.home.y <= world.height - 25);
    for (const other of actors) {
      if (actor === other) continue;
      assert.ok(Math.abs(actor.home.x - other.home.x) >= 65 || Math.abs(actor.home.y - other.home.y) >= 90, `${actor.task.task_id} overlaps ${other.task.task_id}`);
    }
    const props = [
      { x: world.shelf.x, y: world.shelf.y, rx: 45, top: -49, bottom: 96 },
      { x: world.board.x, y: world.board.y, rx: 83, top: -54, bottom: 72 },
    ];
    for (const prop of props) assert.ok(actor.home.x + 33 <= prop.x - prop.rx || actor.home.x - 33 >= prop.x + prop.rx || actor.home.y + 15 <= prop.y + prop.top || actor.home.y - 70 >= prop.y + prop.bottom, `${actor.task.task_id} intersects a prop`);
  }
}

test("overview has a clear coordinator and three stable branch zones", () => {
  for (const mobile of [false, true]) {
    const world = makeWorld(settings, mobile);
    const initial = [
      task("main", null, "coordinator"),
      task("a", "main", "researcher", 1), task("b", "main", "researcher", 2), task("c", "main", "researcher", 3),
      task("a1", "a", "runner", 4), task("b1", "b", "hypothesis", 5), task("c1", "c", "critic", 6),
      task("a2", "a", "analyzer", 7), task("b2", "b1", "coder", 8), task("c2", "c", "reviewer", 9),
    ];
    syncWorld(world, initial, [], true);
    assert.deepEqual([...world.actors.keys()], mobile ? ["main", "a", "b", "c", "a1", "b1", "c1"] : ["main", "a", "b", "c", "a1", "a2", "b1", "b2", "c1", "c2"]);
    const center = world.actors.get("main")!;
    for (const group of world.groups) {
      assert.ok(group.zone);
      const leader = world.actors.get(group.id)!;
      assert.ok(leader.home.y - center.home.y >= 120);
      if (!mobile) {
        assert.equal(group.visibleCount, 3);
        const helpers = group.taskIds.slice(1).map((id) => world.actors.get(id)!);
        assert.ok(helpers.every((actor) => actor.home.y - leader.home.y >= 100));
        assert.ok(Math.abs(helpers[0].home.x - helpers[1].home.x) >= 100);
      }
      for (const id of group.taskIds) assert.equal(world.layoutState.reservations.get(id)?.groupId, group.id);
    }
    assert.equal(new Set(world.groups.map((group) => group.zone!.x)).size, 3);
    spaced(world);
    const saved = new Map([...world.actors].map(([id, actor]) => [id, { actor, home: { ...actor.home } }]));
    syncWorld(world, [task("a3", "a", "reviewer", -1), ...initial].reverse(), [], false);
    for (const [id, before] of saved) {
      assert.strictEqual(world.actors.get(id), before.actor);
      assert.deepEqual(world.actors.get(id)!.home, before.home);
    }
    spaced(world);
  }
});

test("late parents, cycles, and duplicate rows keep deterministic accessible branches", () => {
  for (const mobile of [false, true]) {
    const world = makeWorld(settings, mobile);
    const base = [task("main", null, "coordinator"), task("orphan", "parent", "coder", 2)];
    syncWorld(world, base, [], true);
    const orphan = world.actors.get("orphan")!;
    const home = { ...orphan.home };
    const parent = task("parent", "main", "researcher", 1);
    syncWorld(world, [parent, ...base], [], false);
    assert.strictEqual(world.actors.get("orphan"), orphan);
    assert.deepEqual(orphan.home, home);
    assert.deepEqual(world.groups.map((group) => group.taskIds), [["parent", "orphan"]]);
    spaced(world);
    const cycle = [task("x", "y", "coder", 3), task("y", "x", "reviewer", 4)];
    const latest = { ...parent, summary: "latest", updated_at: "2030-01-01T00:00:00Z" };
    const another = makeWorld(settings, mobile);
    syncWorld(another, [...base, parent, latest, ...cycle].reverse(), [], true);
    assert.equal(another.tasks.get("parent")?.summary, "latest");
    assert.deepEqual(another.groups.map((group) => group.taskIds), [["parent", "orphan"], ["x", "y"]]);
  }
});

test("simultaneous task rows without creation events occupy only their own reserved stations", () => {
  for (const mobile of [false, true]) {
    const world = makeWorld(settings, mobile);
    const main = task("main", null, "coordinator");
    syncWorld(world, [main], [], true);
    const arrivals = [
      task("a", "main", "researcher", 1), task("b", "main", "researcher", 2), task("c", "main", "researcher", 3),
      task("a1", "a", "runner", 4), task("b1", "b", "coder", 5), task("c1", "c", "critic", 6),
    ];
    syncWorld(world, [main, ...arrivals], [], false);
    assert.equal(world.queue.length, 0);
    for (const actor of world.actors.values()) {
      assert.deepEqual({ x: actor.x, y: actor.y }, actor.home);
      assert.equal(actor.path.length, 0);
    }
    spaced(world);
  }
});

test("simultaneous births play once in order and settle without stacking", () => {
  for (const mobile of [false, true]) {
    const world = makeWorld(settings, mobile);
    const main = task("main", null, "coordinator");
    syncWorld(world, [main], [], true);
    const tasks = [main, task("a", "main", "researcher", 1), task("b", "main", "researcher", 2), task("c", "main", "researcher", 3), task("a1", "a", "coder", 4)];
    const events = [created("main", "a", 1), created("main", "b", 2), created("main", "c", 3), created("a", "a1", 4)].reverse();
    syncWorld(world, tasks, events, false);
    const played: number[] = [];
    for (let frame = 0; frame < 900 && (world.queue.length || world.orchestration); frame++) {
      if (frame % 10 === 0) syncWorld(world, tasks, events, false);
      updateWorld(world, 0.1, true);
      if (world.orchestration && played.at(-1) !== world.orchestration.eventId) played.push(world.orchestration.eventId);
      const visible = [...world.actors.values()].filter((actor) => actor.x <= world.width);
      for (let a = 0; a < visible.length; a++) for (let b = a + 1; b < visible.length; b++)
        assert.ok(Math.abs(visible[a].x - visible[b].x) >= 65 || Math.abs(visible[a].y - visible[b].y) >= 90, `stack at frame ${frame}`);
    }
    assert.deepEqual(played, [1, 2, 3, 4]);
    assert.equal(world.queue.length, 0);
    assert.equal(world.orchestration, undefined);
    for (const actor of world.actors.values()) assert.deepEqual({ x: actor.x, y: actor.y }, actor.home);
  }
});

test("overflow branches and group pages remain reachable without sharing zones", () => {
  const branches = Array.from({ length: 12 }, (_, index) => task(`branch-${index}`, "main", "researcher", index + 1));
  const descendants = Array.from({ length: 18 }, (_, index) => task(`helper-${index}`, "branch-0", "coder", index + 20));
  for (const mobile of [false, true]) {
    const world = makeWorld(settings, mobile);
    const tasks = [task("main", null, "coordinator"), ...branches, ...descendants];
    syncWorld(world, tasks, [], true);
    assert.equal(world.groups.length, 12);
    assert.ok(world.groups.slice(3).every((group) => group.visibleCount === 0 && !group.zone));
    assert.deepEqual(chosenTasks(tasks, mobile).map((item) => item.task_id), [...world.actors.keys()]);
    spaced(world);
    for (const group of world.groups) {
      assert.ok(setWorldFocus(world, group.id));
      assert.ok(world.actors.has(group.id));
      assert.ok(group.taskIds.every((id) => world.layoutState.reservations.get(id)?.groupId === group.id));
      spaced(world);
    }
    assert.ok(setWorldFocus(world, "branch-0"));
    const pages = world.groups.find((group) => group.id === "branch-0")!.pageCount;
    assert.ok(pages > 1);
    const seen = new Set<string>();
    for (let page = 0; page < pages; page++) {
      setWorldFocus(world, "branch-0", page);
      for (const id of world.actors.keys()) seen.add(id);
      spaced(world);
    }
    assert.deepEqual(seen, new Set(["main", "branch-0", ...descendants.map((item) => item.task_id)]));
    assert.ok(setWorldFocus(world, null));
    assert.equal(setWorldFocus(world, "missing"), false);
  }
});

test("task creation uses ordered phases and never pulls the coordinator away", () => {
  for (const mobile of [false, true]) {
    const world = makeWorld(settings, mobile);
    const main = task("main", null, "coordinator");
    const child = task("child", "main", "researcher", 1);
    syncWorld(world, [main], [], true);
    syncWorld(world, [main, child], [created("main", "child")], false);
    const coordinator = world.actors.get("main")!;
    const newcomer = world.actors.get("child")!;
    const center = { x: coordinator.x, y: coordinator.y };
    const waiting = { x: newcomer.x, y: newcomer.y };
    assert.equal(newcomer.path.length, 0);
    assert.notDeepEqual(waiting, newcomer.home);
    syncWorld(world, [main, child], [created("main", "child")], false);
    assert.deepEqual({ x: newcomer.x, y: newcomer.y }, waiting);
    assert.equal(world.queue.length, 1);
    const phases: string[] = [];
    for (let step = 0; step < 500 && (world.queue.length || world.orchestration); step++) {
      updateWorld(world, 0.1, true);
      if (world.orchestration && phases.at(-1) !== world.orchestration.phase) phases.push(world.orchestration.phase);
      assert.deepEqual({ x: coordinator.x, y: coordinator.y }, center);
    }
    assert.deepEqual(phases, ["summon", "approach", "brief", "depart", "work"]);
    assert.equal(world.orchestration, undefined);
    assert.deepEqual({ x: newcomer.x, y: newcomer.y }, newcomer.home);
    assert.equal(newcomer.source, null);
    spaced(world);
  }
});

test("a branch leader summons its own helper and paused motion freezes the scene", () => {
  const world = makeWorld(settings, false);
  const main = task("main", null, "coordinator");
  const leader = task("leader", "main", "researcher", 1);
  const helper = task("helper", "leader", "coder", 2);
  syncWorld(world, [main, leader], [], true);
  syncWorld(world, [main, leader, helper], [created("leader", "helper")], false);
  updateWorld(world, 0.1, true);
  assert.equal(world.orchestration?.fromId, "leader");
  assert.equal(world.orchestration?.toId, "helper");
  assert.equal(world.actors.get("main")?.source, null);
  const before = [world.time, world.actors.get("helper")!.x, world.actors.get("helper")!.y, world.orchestration!.progress];
  updateWorld(world, 10, false);
  assert.deepEqual([world.time, world.actors.get("helper")!.x, world.actors.get("helper")!.y, world.orchestration!.progress], before);
  clearTransient(world);
  assert.equal(world.orchestration, undefined);
  assert.equal(world.queue.length, 0);
  assert.ok([...world.actors.values()].every((actor) => actor.source === null && actor.path.length === 0));
  syncWorld(world, [main, leader, helper], [created("leader", "helper")], true);
  assert.deepEqual([world.time, world.actors.get("helper")!.x, world.actors.get("helper")!.y], before.slice(0, 3));
});

test("focus changes and late-parent pagination preserve valid reserved positions", () => {
  const world = makeWorld(settings, true);
  const main = task("main", null, "coordinator");
  const orphan = task("orphan", "parent", "coder", 2);
  syncWorld(world, [main, orphan], [], true);
  const saved = { ...world.actors.get("orphan")!.home };
  const tasks = [main, task("parent", "main", "researcher", 1), orphan, ...Array.from({ length: 10 }, (_, index) => task(`helper-${index}`, "orphan", "runner", index + 3))];
  syncWorld(world, tasks, [], false);
  assert.deepEqual(world.actors.get("orphan")!.home, saved);
  replayEvent(world, created("main", "parent"));
  updateWorld(world, 0.1, true);
  assert.ok(world.orchestration);
  setWorldFocus(world, "parent");
  assert.equal(world.orchestration, undefined);
  const count = world.groups[0].pageCount;
  const seen = new Set<string>();
  for (let page = 0; page < count; page++) {
    setWorldFocus(world, "parent", page);
    for (const actor of world.actors.values()) {
      seen.add(actor.task.task_id);
      assert.equal(actor.source, null);
      assert.deepEqual({ x: actor.x, y: actor.y }, actor.home);
    }
    spaced(world);
  }
  assert.deepEqual(seen, new Set(tasks.map((item) => item.task_id)));
});

function advance(world: ReturnType<typeof makeWorld>, seconds: number): void {
  for (let frame = 0; frame < seconds * 30; frame++) updateWorld(world, 1 / 30, true);
}

function settled(world: ReturnType<typeof makeWorld>): void {
  assert.equal(world.orchestration, undefined);
  assert.equal(world.queue.length, 0);
  for (const actor of world.actors.values()) {
    assert.equal(actor.source, null);
    assert.equal(actor.path.length, 0);
    assert.equal(actor.awaitingCreation, false);
    assert.equal(actor.snapHomeOnArrival, false);
    assert.deepEqual({ x: actor.x, y: actor.y }, actor.home);
  }
}

test("switching group or page during delegation cancels the pair and queue, then accepts another event", () => {
  for (const mobile of [false, true]) for (const switchPage of [false, true]) {
    const world = makeWorld(settings, mobile);
    const tasks = [task("main", null, "coordinator"), task("a", "main", "researcher", 1), task("b", "main", "researcher", 2),
      ...Array.from({ length: 8 }, (_, index) => task(`helper-${index}`, "a", "coder", index + 3))];
    syncWorld(world, tasks, [], true);
    setWorldFocus(world, "a");
    syncWorld(world, tasks, [created("a", "helper-1", 502), created("a", "helper-0", 501)]);
    advance(world, 0.8);
    assert.equal(world.orchestration?.phase, "approach");
    assert.equal(world.queue.length, 1);
    setWorldFocus(world, switchPage ? "a" : "b", switchPage ? 1 : 0);
    settled(world);
    assert.ok([...world.actorCache.values()].every((actor) => !actor.source && !actor.path.length && !actor.awaitingCreation));
    assert.ok(world.groups.filter((group) => !group.visibleCount).every((group) => !group.zone));
    const target = switchPage ? "helper-4" : "b";
    const parent = switchPage ? "a" : "main";
    syncWorld(world, tasks, [created(parent, target, 503), created("a", "helper-1", 502), created("a", "helper-0", 501)]);
    updateWorld(world, 1 / 30, true);
    assert.equal(world.orchestration?.eventId, 503);
    advance(world, 30);
    settled(world);
    assert.equal(world.focusGroup, switchPage ? "a" : "b");
    assert.equal(world.focusPage, switchPage ? 1 : 0);
  }
});

test("removing either delegation participant settles its survivor and permits later events", () => {
  for (const removed of ["a", "helper"]) {
    const world = makeWorld(settings, false);
    const tasks = [task("main", null, "coordinator"), task("a", "main", "researcher", 1), task("helper", "a", "coder", 2)];
    syncWorld(world, tasks, [], true);
    syncWorld(world, tasks, [created("a", "helper", 601)]);
    advance(world, 0.8);
    const remaining = tasks.filter((task) => task.task_id !== removed);
    syncWorld(world, remaining, []);
    settled(world);
    const next = { ...created("main", "main", 602), kind: "agent_tool_audit", to_task_id: null };
    syncWorld(world, remaining, [next]);
    updateWorld(world, 1 / 30, true);
    assert.equal(world.actors.get("main")!.source?.event_id, 602);
    advance(world, 10);
    settled(world);
  }
});

test("pending arrivals and an active pair survive idle polling without teleporting", () => {
  const world = makeWorld(settings, false);
  const main = task("main", null, "coordinator");
  const tasks = [main, task("a", "main", "researcher", 1), task("b", "main", "researcher", 2)];
  syncWorld(world, [main], [], true);
  syncWorld(world, tasks, [created("main", "b", 702), created("main", "a", 701)]);
  advance(world, 0.8);
  const active = world.actors.get("a")!;
  const pending = world.actors.get("b")!;
  const position = [active.x, active.y, pending.x, pending.y];
  const path = [...active.path];
  syncWorld(world, tasks, []);
  assert.deepEqual([active.x, active.y, pending.x, pending.y], position);
  assert.deepEqual(active.path, path);
  assert.equal(pending.awaitingCreation, true);
  // A queue cap or cancellation may drop its event; polling still cannot materialize it in a live path.
  world.queue = [];
  syncWorld(world, tasks, []);
  assert.deepEqual([active.x, active.y, pending.x, pending.y], position);
  advance(world, 30);
  settled(world);
});

test("a task without a creation event waits outside an existing travel route", () => {
  const world = makeWorld(settings, false);
  const tasks = [task("main", null, "coordinator"), task("a", "main", "researcher", 1)];
  syncWorld(world, tasks, [], true);
  const traveler = world.actors.get("a")!;
  traveler.x = 540; traveler.y = 372;
  traveler.path = Array.from({ length: 12 }, (_, index) => ({ col: 21 - index, row: 15 }));
  traveler.returning = true;
  traveler.snapHomeOnArrival = true;
  traveler.arrivalAction = "work";
  const withArrival = [...tasks, task("b", "main", "researcher", 2)];
  syncWorld(world, withArrival, []);
  const arrival = world.actors.get("b")!;
  assert.equal(arrival.awaitingCreation, true);
  assert.ok(arrival.x > world.width);
  assert.equal(world.queue.length, 0);
  for (let frame = 0; frame < 10 * 30; frame++) {
    if (frame % 30 === 0) syncWorld(world, withArrival, []);
    updateWorld(world, 1 / 30, true);
    assert.ok(Math.abs(traveler.x - arrival.x) >= 65 || Math.abs(traveler.y - arrival.y) >= 90);
  }
  settled(world);
});

test("a dropped parent creation record cannot leave its subsequent delegation offscreen", () => {
  const world = makeWorld(settings, false);
  const main = task("main", null, "coordinator");
  const tasks = [main, task("a", "main", "researcher", 1), task("helper", "a", "coder", 2)];
  syncWorld(world, [main], [], true);
  syncWorld(world, tasks, [created("a", "helper", 752), created("main", "a", 751)]);
  world.queue = world.queue.filter((event) => event.event_id !== 751);
  updateWorld(world, 1 / 30, true);
  assert.equal(world.orchestration?.eventId, 752);
  for (const id of ["a", "helper"]) assert.ok(world.actors.get(id)!.x < world.width);
  advance(world, 20);
  settled(world);
});

test("ground cloth covers visible feet, omits hidden groups and waits for pending arrivals", () => {
  for (const mobile of [false, true]) {
    const world = makeWorld(settings, mobile);
    const snapshot = demoSnapshot("ground", 50);
    syncWorld(world, [...snapshot.tasks, ...Array.from({ length: 4 }, (_, index) => task(`ground-helper-${index}`, "demo-research-a", "coder", index))], [], true);
    let path: number[][] = [];
    const cloths: number[][][] = [];
    const palette = new Set(["#adc3aa30", "#e6d1af32", "#b0c8cc30"]);
    const ctx = new Proxy({
      fillStyle: "",
      beginPath() { path = []; },
      moveTo(x: number, y: number) { path.push([x, y]); },
      lineTo(x: number, y: number) { path.push([x, y]); },
      fill() { if (palette.has(this.fillStyle)) cloths.push(path); },
    }, { get(target, key) { return key in target ? target[key as keyof typeof target] : () => {}; } }) as unknown as CanvasRenderingContext2D;
    const contains = (polygon: number[][], x: number, y: number): boolean => {
      let inside = false;
      for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
        const [xi, yi] = polygon[i]; const [xj, yj] = polygon[j];
        if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside;
      }
      return inside;
    };
    for (const focus of [null, "demo-research-a", "demo-research-b", "demo-research-c"]) {
      setWorldFocus(world, focus);
      cloths.length = 0;
      drawMeadow(ctx, world, undefined, false);
      assert.equal(cloths.length, focus ? 1 : 3);
      for (const actor of world.actors.values()) {
        if (actor.task.role === "coordinator") continue;
        assert.ok(cloths.some((polygon) => contains(polygon, actor.home.x, actor.home.y)), `${mobile}:${focus}:${actor.task.task_id} outside cloth`);
      }
    }
    for (const actor of world.actors.values()) if (actor.task.role !== "coordinator") actor.awaitingCreation = true;
    cloths.length = 0;
    drawMeadow(ctx, world, undefined, false);
    assert.equal(cloths.length, 0);
  }
});

test("a blocked departure stays pending until a route home opens", () => {
  for (const mobile of [false, true]) {
    const world = makeWorld(settings, mobile);
    const main = task("main", null, "coordinator");
    const tasks = [main, task("a", "main", "researcher", 1)];
    syncWorld(world, [main], [], true);
    syncWorld(world, tasks, [created("main", "a", 801)]);
    for (let frame = 0; frame < 600 && world.orchestration?.phase !== "brief"; frame++) updateWorld(world, 1 / 30, true);
    assert.equal(world.orchestration?.phase, "brief");
    const child = world.actors.get("a")!;
    const position = { x: child.x, y: child.y };
    assert.ok(Math.hypot(child.x - child.home.x, child.y - child.home.y) > 36);
    const originalBlocks = new Set(world.blocked);
    for (let row = 0; row < world.tiles.length; row++) for (let col = 0; col < world.tiles[0].length; col++) world.blocked.add(`${col},${row}`);
    advance(world, 3);
    assert.equal(world.orchestration?.phase, "depart");
    assert.equal(child.snapHomeOnArrival, true);
    assert.deepEqual({ x: child.x, y: child.y }, position);
    world.blocked = originalBlocks;
    advance(world, 20);
    settled(world);
  }
});

test("hidden descendants stay accessible through their group without animating an unrelated visible child", () => {
  for (const mobile of [false, true]) {
    const world = makeWorld(settings, mobile);
    const tasks = [task("main", null, "coordinator"), task("a", "main", "researcher", 1),
      ...Array.from({ length: 6 }, (_, index) => task(`helper-${index}`, "a", "coder", index + 2))];
    syncWorld(world, tasks, [], true);
    const before = [...world.actors.keys()];
    syncWorld(world, tasks, [created("a", "helper-5", 901)]);
    updateWorld(world, 1 / 30, true);
    assert.equal(world.focusGroup, null);
    assert.equal(world.orchestration, undefined);
    assert.deepEqual([...world.actors.keys()], before);
    assert.ok(before.filter((id) => id.startsWith("helper")).every((id) => !world.actors.get(id)!.source));
    const group = world.groups.find((group) => group.id === "a")!;
    assert.equal(group.total, 7);
    assert.ok(group.taskIds.includes("helper-5"));
    setWorldFocus(world, "a", world.layoutState.reservations.get("helper-5")!.page);
    assert.ok(world.actors.has("helper-5"));
  }
});

test("polling ignores historical events and replays a new event once", () => {
  const world = makeWorld(settings, false);
  const tasks = [task("main", null, "coordinator"), task("a", "main", "researcher", 1)];
  const old = { ...created("main", "a", 5), created_at: "2020-01-01T00:00:00Z" };
  syncWorld(world, tasks, [old], true);
  syncWorld(world, tasks, [old], false);
  assert.equal(world.queue.length, 0);
  const recent = created("main", "a", 6);
  syncWorld(world, tasks, [recent, old], false);
  syncWorld(world, tasks, [recent, old], false);
  assert.equal(world.queue.length, 1);
});

test("message and knowledge actions preserve runtime event mapping", () => {
  const world = makeWorld(settings, false);
  const tasks = [task("main", null, "coordinator"), task("a", "main", "researcher", 1)];
  syncWorld(world, tasks, [], true);
  const event = { ...created("a", "main", 7), kind: "direct_message", task_id: null };
  replayEvent(world, event); updateWorld(world, 0.1, true);
  assert.equal(world.actors.get("a")!.arrivalAction, "talk");
  assert.equal(world.actors.get("main")!.action, "talk");
  assert.equal(world.actors.get("main")!.source?.event_id, 7);
  clearTransient(world);
  replayEvent(world, { ...event, event_id: 8, to_task_id: null, to_role: "researcher" });
  updateWorld(world, 0.1, true);
  assert.equal(world.actors.get("main")!.source, null);
  assert.equal(world.actors.get("a")!.arrivalAction, "talk");
  const archived = { ...event, kind: "run_written_to_wiki", knowledge_action: "archive" as const };
  assert.equal(knowledgeAction(archived), "archive");
  assert.equal(eventCaption(archived), "证据归档");
  assert.equal(knowledgeAction({ ...event, kind: "run_written_to_wiki" }), null);
  assert.equal(knowledgeAction({ ...event, kind: "knowledge_read" }), "read");
  assert.equal(knowledgeAction({ ...event, kind: "idea_written_to_wiki" }), "write");
});

test("discussion targets the recipient's current position and both participants return home", () => {
  for (const mobile of [false, true]) {
    const world = makeWorld(settings, mobile);
    const tasks = [task("main", null, "coordinator"), task("a", "main", "researcher", 1)];
    syncWorld(world, tasks, [], true);
    const recipient = world.actors.get("main")!;
    recipient.x = mobile ? 252 : 516;
    recipient.y = mobile ? 408 : 348;
    const recipientPosition = { x: recipient.x, y: recipient.y };
    syncWorld(world, tasks, [{ ...created("a", "main", 1001), kind: "direct_message" }]);
    assert.deepEqual({ x: recipient.x, y: recipient.y }, recipientPosition);
    updateWorld(world, 1 / 30, true);
    const sender = world.actors.get("a")!;
    assert.ok(sender.path.length);
    const end = sender.path.at(-1)!;
    const x = end.col * 24 + 12; const y = end.row * 24 + 12;
    assert.ok(Math.hypot(x - recipientPosition.x, y - recipientPosition.y) < 150);
    assert.ok(Math.abs(x - recipientPosition.x) >= 65 || Math.abs(y - recipientPosition.y) >= 90);
    advance(world, 40);
    settled(world);
  }
});

test("a late branch preserves a full overview and remains reachable through group focus", () => {
  for (const mobile of [false, true]) {
    const world = makeWorld(settings, mobile);
    const tasks = [task("main", null, "coordinator"), ...["a", "b", "c"].flatMap((id, index) => [
      task(id, "main", "researcher", index + 1), task(`${id}1`, id, "hypothesis", index + 4), task(`${id}2`, `${id}1`, "coder", index + 7),
    ])];
    syncWorld(world, tasks, [], true);
    const before = new Map([...world.actors].map(([id, actor]) => [id, { ...actor.home }]));
    syncWorld(world, [task("late", "main", "researcher", -1), ...tasks].reverse(), []);
    assert.deepEqual([...world.actors.keys()], [...before.keys()]);
    for (const [id, home] of before) assert.deepEqual(world.actors.get(id)!.home, home);
    assert.equal(world.groups.find((group) => group.id === "late")?.total, 1);
    setWorldFocus(world, "late");
    assert.ok(world.actors.has("late"));
    spaced(world);
  }
});

test("all occupied stations can visit the knowledge cart or board and return in both scene presets", () => {
  const tasks = [task("main", null, "coordinator"), ...["a", "b", "c"].flatMap((id, index) => [
    task(id, "main", "researcher", index + 1), task(`${id}1`, id, "hypothesis", index + 4), task(`${id}2`, `${id}1`, "coder", index + 7),
  ])];
  for (const style of ["meadow", "office"] as const) for (const mobile of [false, true]) {
    const ids = chosenTasks(tasks, mobile).map((task) => task.task_id);
    for (const id of ids) for (const kind of ["knowledge_read", "communication_published"]) {
      const world = makeWorld({ ...settings, style }, mobile);
      syncWorld(world, tasks, [], true);
      replayEvent(world, { ...created(id, id, 1101), kind, to_task_id: null });
      updateWorld(world, 1 / 30, true);
      const actor = world.actors.get(id)!;
      const destination = kind === "knowledge_read" ? { x: world.shelf.x + 25, y: world.shelf.y + 90 } : { x: world.board.x, y: world.board.y + 56 };
      assert.ok(actor.path.length || Math.hypot(actor.x - destination.x, actor.y - destination.y) <= 240, `${style}:${mobile}:${id}:${kind} cannot reach the prop`);
      assert.equal(actor.arrivalAction, kind === "knowledge_read" ? "read" : "talk");
      advance(world, 40);
      settled(world);
    }
  }
});

test("knowledge and publication actions still reach clear prop positions", () => {
  const tasks = [
    task("main", null, "coordinator"),
    task("a", "main", "researcher", 1), task("b", "main", "researcher", 2), task("c", "main", "researcher", 3),
    task("a1", "a", "runner", 4), task("b1", "b", "hypothesis", 5), task("c1", "c", "critic", 6),
  ];
  for (const mobile of [false, true]) for (const kind of ["knowledge_read", "idea_written_to_wiki", "run_written_to_wiki", "communication_published"]) {
    const world = makeWorld(settings, mobile);
    syncWorld(world, tasks, [], true);
    const event: Event = { ...created("a", "a", 200), kind, to_task_id: null, knowledge_action: kind === "run_written_to_wiki" ? "archive" : undefined };
    replayEvent(world, event);
    updateWorld(world, 0.1, true);
    const actor = world.actors.get("a")!;
    const end = actor.path.at(-1);
    const x = end ? end.col * 24 + 12 : actor.x; const y = end ? end.row * 24 + 12 : actor.y;
    const destination = kind === "communication_published" ? { x: world.board.x, y: world.board.y + 56 }
      : { x: world.shelf.x + (kind === "run_written_to_wiki" ? 50 : 25), y: world.shelf.y + (kind === "run_written_to_wiki" ? 135 : 90) };
    assert.ok(Math.hypot(x - destination.x, y - destination.y) <= 240, `${mobile ? "mobile" : "desktop"} ${kind} has no reachable destination`);
    assert.equal(actor.arrivalAction, kind === "communication_published" ? "talk" : kind === "run_written_to_wiki" ? "archive" : kind === "knowledge_read" ? "read" : "write");
    for (const prop of [{ ...world.shelf, rx: 45, top: -49, bottom: 96 }, { ...world.board, rx: 83, top: -54, bottom: 72 }])
      assert.ok(x + 33 <= prop.x - prop.rx || x - 33 >= prop.x + prop.rx || y + 15 <= prop.y + prop.top || y - 70 >= prop.y + prop.bottom,
        `${mobile ? "mobile" : "desktop"} ${kind} ends inside prop`);
    advance(world, 40);
    settled(world);
  }
});

test("demo staged task events reference visible parents", () => {
  const final = demoSnapshot("研究主题", 50, Date.now() - 50_000);
  const ids = new Set(final.tasks.map((item) => item.task_id));
  for (const event of final.events.filter((item) => item.kind === "task_created")) {
    assert.ok(ids.has(event.from_task_id!));
    assert.ok(ids.has(event.to_task_id!));
  }
});

test("120-second staged demo at 30 fps keeps overview delegation visible and returns every actor", (t) => {
  const started = Date.parse("2026-09-27T00:00:00Z");
  let now = started;
  t.mock.method(Date, "now", () => now);
  for (const mobile of [false, true]) {
    now = started;
    const world = makeWorld(settings, mobile);
    const first = demoSnapshot("generic objective", 0, started);
    syncWorld(world, first.tasks, first.events, true);
    const phases = new Set<string>();
    const homes = new Map<string, { x: number; y: number }>();
    let stage = 0;
    let drainedAt = 0;
    for (let frame = 0; frame < 120 * 30; frame++) {
      const second = frame / 30;
      now = started + second * 1000;
      if (demoStageAt(second) !== stage) {
        const snapshot = demoSnapshot("generic objective", second, started);
        syncWorld(world, snapshot.tasks, snapshot.events, false);
        stage = demoStageAt(second);
        for (const [id, actor] of world.actors) {
          if (homes.has(id)) assert.deepEqual(actor.home, homes.get(id));
          homes.set(id, { ...actor.home });
        }
      }
      updateWorld(world, 1 / 30, true);
      assert.equal(world.focusGroup, null, "overview must not auto-focus a hidden child");
      const coordinator = world.actors.get("demo-main")!;
      assert.deepEqual({ x: coordinator.x, y: coordinator.y }, coordinator.home);
      if (second >= 39 && !world.queue.length && !world.orchestration && !drainedAt) drainedAt = second;
      if (world.orchestration) {
        phases.add(`${world.orchestration.eventId}:${world.orchestration.phase}`);
        const from = world.actors.get(world.orchestration.fromId)!;
        const to = world.actors.get(world.orchestration.toId)!;
        assert.equal(to.task.parent_task_id, from.task.task_id);
        if (world.orchestration.eventId === 8) assert.deepEqual([from.task.task_id, to.task.task_id], ["demo-hypothesis", "demo-coder"]);
        if (world.orchestration.phase === "summon") {
          assert.ok(Math.hypot(from.x - to.x, from.y - to.y) < 240, `event ${world.orchestration.eventId} did not summon nearby`);
          assert.ok(to.x <= world.width);
        }
        if (world.orchestration.phase === "work") assert.deepEqual({ x: to.x, y: to.y }, to.home, `event ${world.orchestration.eventId} did not reach its station`);
      }
      const actors = [...world.actors.values()];
      for (const actor of actors) for (const prop of [{ ...world.shelf, rx: 45, top: -49, bottom: 96 }, { ...world.board, rx: 83, top: -54, bottom: 72 }])
        assert.ok(actor.x + 33 <= prop.x - prop.rx || actor.x - 33 >= prop.x + prop.rx || actor.y + 15 <= prop.y + prop.top || actor.y - 70 >= prop.y + prop.bottom,
          `${mobile ? "mobile" : "desktop"} prop collision at ${second}: ${actor.task.task_id} (${actor.x},${actor.y})`);
      for (let a = 0; a < actors.length; a++) for (let b = a + 1; b < actors.length; b++) {
        const firstActor = actors[a]; const secondActor = actors[b];
        assert.ok(Math.abs(firstActor.x - secondActor.x) >= 65 || Math.abs(firstActor.y - secondActor.y) >= 90,
          `${mobile ? "mobile" : "desktop"} collision at ${second}: ${firstActor.task.task_id} (${firstActor.x},${firstActor.y}) / ${secondActor.task.task_id} (${secondActor.x},${secondActor.y}), phase ${world.orchestration?.eventId}:${world.orchestration?.phase}`);
      }
    }
    assert.equal(world.queue.length, 0);
    assert.equal(world.orchestration, undefined);
    // Desktop has room for the post-demo profession roles in their branch
    // overview; mobile keeps the original compact two-seat choreography.
    for (let eventId = 2; eventId <= (mobile ? 7 : 10); eventId++) for (const phase of ["summon", "approach", "brief", "depart", "work"]) assert.ok(phases.has(`${eventId}:${phase}`), `missing ${eventId}:${phase}; saw ${[...phases].join(", ")}`);
    assert.ok(drainedAt > 0 && drainedAt <= 50, `demo drained at ${drainedAt}`);
    if (!mobile) {
      assert.ok(world.actors.has("demo-coder"));
      assert.deepEqual(world.groups.find((group) => group.id === "demo-research-b")!.taskIds, ["demo-research-b", "demo-hypothesis", "demo-coder"]);
    }
    for (const actor of world.actors.values()) {
      assert.deepEqual({ x: actor.x, y: actor.y }, actor.home);
      assert.equal(actor.source, null);
      assert.equal(actor.awaitingCreation, false);
      assert.equal(actor.snapHomeOnArrival, false);
    }
    t.diagnostic(`${mobile ? "mobile" : "desktop"}: 3600 frames, zero body/prop overlaps, drained at ${drainedAt.toFixed(2)}s`);
  }
});
