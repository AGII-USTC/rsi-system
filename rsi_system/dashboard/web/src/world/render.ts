import { roleLabel } from "../types";
import type { Event } from "../types";
import { eventCaption, knowledgeAction, type Actor, type World } from "./engine";
import { drawMeadow, drawMeadowForeground, drawMeadowProp, drawMeadowShelfTrim, drawMeadowSmallProp, meadowSmallProps, type MeadowSmallProp } from "./meadow";
import { PET_CELL, petForTask, petFrame, validPetSheet, type PetSheets } from "./pets";

export interface HitBox { x: number; y: number; width: number; height: number; id: string }
const colors = ["#88af94", "#8eaabb", "#cdb88a", "#b79bb7", "#d89e8d", "#8bb5b0"];

function box(ctx: CanvasRenderingContext2D, x: number, y: number, width: number, height: number, color: string, radius = 4): void {
  ctx.fillStyle = color; ctx.beginPath(); ctx.roundRect(x, y, width, height, radius); ctx.fill();
}

function text(ctx: CanvasRenderingContext2D, value: string, x: number, y: number, color = "#6f8977", size = 11, align: CanvasTextAlign = "left"): void {
  ctx.fillStyle = color; ctx.font = `${size}px -apple-system, PingFang SC, sans-serif`; ctx.textAlign = align; ctx.fillText(value, x, y);
}

function truncate(value: string, length: number): string { return value.length > length ? `${value.slice(0, length)}…` : value; }
function fitText(ctx: CanvasRenderingContext2D, value: string, maxWidth: number): string {
  if (ctx.measureText(value).width <= maxWidth) return value;
  let length = value.length;
  while (length > 0 && ctx.measureText(`${value.slice(0, length)}…`).width > maxWidth) length--;
  return `${value.slice(0, length)}…`;
}

function plant(ctx: CanvasRenderingContext2D, x: number, y: number, meadow: boolean): void {
  if (!meadow) box(ctx, x - 8, y + 4, 16, 14, "#c9b69f", 3);
  ctx.strokeStyle = "#7c9e73"; ctx.lineWidth = 3; ctx.beginPath(); ctx.moveTo(x, y + 7); ctx.lineTo(x, y - 12); ctx.stroke();
  ctx.fillStyle = "#a5c68f";
  for (const [dx, dy] of [[-6, -9], [5, -16], [-4, -20]]) { ctx.beginPath(); ctx.ellipse(x + dx, y + dy, 7, 4, dx < 0 ? 0.5 : -0.5, 0, Math.PI * 2); ctx.fill(); }
}

function background(ctx: CanvasRenderingContext2D, world: World, events: Event[], scenicBackground?: HTMLImageElement, paintBackdrop = true): void {
  if (world.settings.style === "meadow") {
    drawMeadow(ctx, world, scenicBackground, paintBackdrop);
    return;
  }
  const { width, height } = world;
  ctx.fillStyle = "#f1f4e9"; ctx.fillRect(0, 0, width, height);
  box(ctx, 18, 18, width - 36, height - 36, "#fafaf5", 9);
  box(ctx, 18, 18, width - 36, 127, "#e3eee0", 9);
  ctx.fillStyle = "#cddbc5"; ctx.fillRect(18, 140, width - 36, 5);
  ctx.strokeStyle = "#eceee4"; ctx.lineWidth = 1;
  for (let x = 48; x < width - 30; x += 48) { ctx.beginPath(); ctx.moveTo(x, 145); ctx.lineTo(x, height - 18); ctx.stroke(); }
  for (let y = 180; y < height - 18; y += 48) { ctx.beginPath(); ctx.moveTo(18, y); ctx.lineTo(width - 18, y); ctx.stroke(); }
  const windowX = world.mobile ? 169 : width / 2 - 67;
  const windowWidth = world.mobile ? 91 : 134;
  box(ctx, windowX, 39, windowWidth, 62, "#afc2b5", 3); box(ctx, windowX + 5, 44, windowWidth - 10, 52, "#ebf7f3", 1);
  ctx.fillStyle = "#d3e6db"; ctx.fillRect(windowX + windowWidth / 2, 44, 3, 52); ctx.fillRect(windowX + 5, 69, windowWidth - 10, 3);
  text(ctx, "研究工作室", width / 2, 127, "#91a78d", 10, "center");
  plant(ctx, width - 48, height - 65, false); plant(ctx, 42, height - 65, false);
  if (!world.mobile) {
    box(ctx, 163, 184, 237, 284, "#e9efe9", 9); box(ctx, 552, 184, 237, 284, "#e8eff0", 9);
    text(ctx, "工作区 A", 283, 173, "#a2b099", 12, "center"); text(ctx, "工作区 B", 670, 173, "#98aca8", 12, "center");
    box(ctx, 424, 278, 115, 148, "#f0e3d7", 16);
    box(ctx, 444, 315, 75, 54, "#d8cfad", 15); box(ctx, 446, 313, 71, 49, "#e6ddbf", 15);
    box(ctx, 455, 304, 18, 9, "#a6bbb1", 4); box(ctx, 493, 304, 18, 9, "#bcc7a5", 4); box(ctx, 455, 371, 18, 9, "#bec7a6", 4); box(ctx, 493, 371, 18, 9, "#a6bbb1", 4);
    text(ctx, "讨论桌", 481, 411, "#ad9e81", 11, "center");
    box(ctx, 37, 207, 61, 67, "#c8d8bd", 12); box(ctx, 46, 221, 43, 41, "#dbe5cc", 8); box(ctx, 54, 265, 6, 9, "#a7b49a", 1); box(ctx, 78, 265, 6, 9, "#a7b49a", 1);
    text(ctx, "阅读角", 72, 294, "#a2b295", 11, "center");
    box(ctx, 30, 362, 106, 44, "#acc2b2", 10); box(ctx, 39, 346, 88, 40, "#cbdccd", 9); box(ctx, 40, 386, 85, 18, "#bad0bd", 5); box(ctx, 48, 405, 6, 9, "#a3b89e", 1); box(ctx, 110, 405, 6, 9, "#a3b89e", 1);
    text(ctx, "休息角", 80, 438, "#98ae96", 11, "center");
    box(ctx, 803, 169, 34, 32, "#d8d2bb", 5); box(ctx, 813, 161, 10, 12, "#fffdf4", 3); plant(ctx, 820, 140, false);
  }
  const shelf = world.shelf;
  box(ctx, shelf.x - 48, shelf.y - 47, 96, 72, "#b2c0a8", 3);
  box(ctx, shelf.x - 42, shelf.y - 42, 84, 25, "#f7f9ef", 1); box(ctx, shelf.x - 42, shelf.y - 11, 84, 25, "#f7f9ef", 1);
  text(ctx, "知识书架", shelf.x, shelf.y + 42, "#879a79", 12, "center");
  box(ctx, shelf.x - 24, shelf.y + 82, 52, 13, "#c9ccb7", 3); text(ctx, "证据托盘", shelf.x + 2, shelf.y + 115, "#a4aa91", 10, "center");
  const board = world.board;
  box(ctx, board.x - 90, board.y - 54, 180, 78, "#c4d1bb", 4); box(ctx, board.x - 84, board.y - 48, 168, 64, "#fdfef7", 2);
  text(ctx, "讨论黑板", board.x, board.y - 33, "#7a9276", 12, "center");
  const currentEvent = world.highlighted || events.find((event) => event.kind === "direct_message" || event.kind === "communication_published");
  const line = currentEvent?.text;
  ctx.font = "10px -apple-system, PingFang SC, sans-serif";
  text(ctx, fitText(ctx, line || "等待新的协作记录", 145), board.x, board.y - 13, "#869881", 10, "center");
  text(ctx, currentEvent ? `事件 #${currentEvent.event_id}` : "", board.x, board.y + 4, "#afba9f", 8, "center");
}

function desk(ctx: CanvasRenderingContext2D, actor: Actor, index: number): void {
  const { x, y } = actor.home;
  box(ctx, x - 43, y - 24, 86, 17, "#c6cdb7", 3);
  ctx.fillStyle = "#acb99f"; ctx.fillRect(x - 37, y - 7, 5, 15); ctx.fillRect(x + 32, y - 7, 5, 15);
  box(ctx, x - 28, y - 64, 56, 37, "#7c948a", 3); box(ctx, x - 23, y - 59, 46, 27, "#e7f5ef", 1);
  ctx.fillStyle = actor.task.status === "running" ? "#a3c9b3" : "#c7ddd0";
  ctx.fillRect(x - 16, y - 53, 26, 2); ctx.fillRect(x - 16, y - 46, 17, 2); ctx.fillRect(x - 16, y - 39, 23, 2);
  box(ctx, x - 8, y - 27, 16, 4, "#94a895", 1); box(ctx, x - 21, y - 18, 42, 7, "#eef0e7", 1);
  box(ctx, x - 17, y + 12, 34, 20, colors[index % colors.length], 4);
}

function pixelActorPosition(actor: Actor, world: World): { x: number; y: number } {
  const bob = actor.action === "wave" || actor.action === "talk" ? Math.sin(world.time * 5) * 1.8 : 0;
  const shake = actor.action === "failed" ? Math.sin(world.time * 9) : 0;
  return { x: actor.x + shake, y: actor.y + bob };
}

function drawPixelActor(ctx: CanvasRenderingContext2D, actor: Actor, sheet: HTMLImageElement, world: World): void {
  const walking = actor.path.length > 0;
  let frame = walking ? [0, 1, 2, 1][Math.floor(world.time * 7) % 4] : actor.action === "work" ? 3 + Math.floor(world.time * 4) % 2 : actor.action === "read" || actor.action === "write" || actor.action === "archive" ? 5 + Math.floor(world.time * 2) % 2 : 1;
  if (!world.settings.motion) frame = actor.action === "work" ? 3 : 1;
  const direction = walking ? actor.direction : 0;
  const row = direction === 3 ? 1 : direction === 1 || direction === 2 ? 2 : 0;
  const { x: spriteX, y } = pixelActorPosition(actor, world);
  const x = actor.x;
  ctx.fillStyle = "#9db48a30"; ctx.beginPath(); ctx.ellipse(x, y + 4, 18, 5, 0, 0, Math.PI * 2); ctx.fill();
  ctx.save();
  ctx.imageSmoothingEnabled = false;
  if (direction === 1) { ctx.translate(spriteX, 0); ctx.scale(-1, 1); ctx.drawImage(sheet, frame * 16, row * 32, 16, 32, -20, y - 66, 40, 80); }
  else ctx.drawImage(sheet, frame * 16, row * 32, 16, 32, spriteX - 20, y - 66, 40, 80);
  ctx.restore();
  if (world.settings.style === "meadow") return;
  if (actor.action === "wave") { ctx.fillStyle = "#ecc8a0"; ctx.beginPath(); ctx.arc(x + 20, y - 43 + Math.sin(world.time * 7) * 5, 4, 0, Math.PI * 2); ctx.fill(); }
  if (actor.action === "write" || actor.action === "read") { box(ctx, x + 5, y - 28, 16, 13, actor.action === "write" ? "#d4b889" : "#aac9b2", 1); ctx.fillStyle = "#fafaf3"; ctx.fillRect(x + 12, y - 26, 1, 9); }
  if (actor.action === "archive") { box(ctx, x + 5, y - 28, 14, 16, "#fcfcf3", 1); ctx.fillStyle = "#b5c2b2"; ctx.fillRect(x + 8, y - 24, 8, 2); }
  const bubble = actor.source ? actor.action === "wave" ? "派发" : actor.action === "talk" ? "讨论" : actor.action === "read" ? "读取" : actor.action === "write" ? "写入" : actor.action === "archive" ? "归档" : "工作" : actor.action === "nap" ? "Z z" : actor.action === "failed" ? "!" : null;
  if (bubble) { box(ctx, x + 10, y - 84, 36, 18, actor.action === "failed" ? "#fbe8df" : "#fffdf5", 5); text(ctx, bubble, x + 28, y - 71, actor.action === "failed" ? "#ba8070" : "#99a07d", 9, "center"); }
  if (actor.action === "talk" && actor.source?.text) { const snippet = truncate(actor.source.text, 12); box(ctx, x - 79, y - 112, 158, 25, "#fffef7", 5); text(ctx, snippet, x, y - 95, "#8c9877", 11, "center"); }
  const label = `${roleLabel[actor.task.role] || actor.task.role} · ${actor.task.task_id.slice(-4)}`;
  ctx.font = "12px -apple-system, PingFang SC, sans-serif";
  const labelWidth = Math.min(148, ctx.measureText(label).width + 12);
  box(ctx, x - labelWidth / 2, y + 19, labelWidth, 20, "#ffffffd9", 4); text(ctx, label, x, y + 33, "#697e6a", 12, "center");
  if (actor.source) { text(ctx, `#${actor.source.event_id}`, x, y + 45, "#b59668", 8, "center"); }
}

function drawPet(ctx: CanvasRenderingContext2D, actor: Actor, sheet: HTMLImageElement, world: World): void {
  const { row, column } = petFrame(actor, world.time, world.settings.motion);
  const width = 96;
  const height = 104;
  ctx.fillStyle = "#354e2d24";
  ctx.beginPath(); ctx.ellipse(actor.x, actor.y + 1, 19, 4, 0, 0, Math.PI * 2); ctx.fill();
  ctx.save();
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(sheet, column * PET_CELL.width, row * PET_CELL.height, PET_CELL.width, PET_CELL.height,
    actor.x - width / 2, actor.y - height + 3, width, height);
  ctx.restore();
}

function drawFallbackActor(ctx: CanvasRenderingContext2D, actor: Actor, meadow: boolean): void {
  const color = colors[actor.palette % colors.length];
  ctx.fillStyle = color;
  ctx.beginPath(); ctx.arc(actor.x, actor.y - 48, 12, 0, Math.PI * 2); ctx.fill();
  box(ctx, actor.x - 17, actor.y - 34, 34, 39, color, 7);
  if (meadow) return;
  const label = roleLabel[actor.task.role] || actor.task.role;
  box(ctx, actor.x - 42, actor.y + 9, 84, 19, "#ffffffdf", 4);
  text(ctx, truncate(label, 8), actor.x, actor.y + 22, "#697e6a", 11, "center");
}

function drawKnowledge(ctx: CanvasRenderingContext2D, world: World, events: Event[]): void {
  if (world.settings.style === "meadow") {
    const shelf = world.shelf;
    events.filter((event) => knowledgeAction(event) === "write").slice(0, 18).forEach((event, index) => {
      const row = Math.floor(index / 6);
      const bottom = shelf.y + [-34, -10, 14][row];
      const height = 12 + index % 3 * 2;
      box(ctx, shelf.x - 6 + index % 6 * 7, bottom - height, 5, height,
        colors[event.event_id % colors.length], 1);
      ctx.fillStyle = "#fff5d39c";
      ctx.fillRect(shelf.x - 5 + index % 6 * 7, bottom - 4, 3, 1);
    });
    events.filter((event) => knowledgeAction(event) === "archive").slice(0, 3).forEach((_, index) =>
      box(ctx, shelf.x + 7, shelf.y - 69 - index * 2, 27, 3, index % 2 ? "#e8eddb" : "#fff7e6", 1));
    return;
  }
  const knowledge = events.filter((event) => knowledgeAction(event) === "write").slice(0, 18);
  knowledge.forEach((event, index) => { box(ctx, world.shelf.x - 35 + index % 10 * 7, world.shelf.y - 35 + Math.floor(index / 10) * 31, 5, 17 + index % 3 * 2, colors[event.event_id % colors.length], 1); });
  events.filter((event) => knowledgeAction(event) === "archive").slice(0, 4).forEach((_, index) => box(ctx, world.shelf.x - 15, world.shelf.y + 81 - index * 3, 32, 4, index % 2 ? "#e9eedf" : "#fdfef5", 1));
}

export function renderWorld(ctx: CanvasRenderingContext2D, world: World, sheets: HTMLImageElement[], events: Event[], scenicBackground?: HTMLImageElement, petSheets?: PetSheets, paintBackdrop = true): HitBox[] {
  ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
  const scale = ctx.canvas.width / world.width;
  ctx.save(); ctx.scale(scale, scale);
  ctx.imageSmoothingEnabled = true;
  background(ctx, world, events, scenicBackground, paintBackdrop);
  const actors = [...world.actors.values()];
  const meadow = world.settings.style === "meadow";
  if (!meadow) {
    drawKnowledge(ctx, world, events);
    actors.forEach((actor, index) => desk(ctx, actor, index));
  }
  const hitboxes: HitBox[] = [];
  const items: { depth: number; actor?: Actor; prop?: "shelf" | "board"; smallProp?: MeadowSmallProp }[] = actors.map((actor) => ({ depth: actor.y, actor }));
  if (meadow) {
    items.push({ depth: world.shelf.y + 31, prop: "shelf" }, { depth: world.board.y + 72, prop: "board" });
    items.push(...meadowSmallProps(world).map((smallProp) => ({ depth: smallProp.depth, smallProp })));
  }
  items.sort((a, b) => a.depth - b.depth).forEach((item) => {
    if (item.smallProp) {
      drawMeadowSmallProp(ctx, item.smallProp);
      return;
    }
    if (item.prop) {
      drawMeadowProp(ctx, world, item.prop);
      if (item.prop === "shelf") {
        drawKnowledge(ctx, world, events);
        drawMeadowShelfTrim(ctx, world);
      }
      return;
    }
    const actor = item.actor!;
    const preset = petForTask(actor.task);
    const pet = petSheets?.[preset.id];
    const usablePet = pet?.complete && validPetSheet(pet, preset.version);
    const fallback = sheets[actor.palette]?.complete && sheets[actor.palette]?.naturalWidth
      ? sheets[actor.palette] : sheets.find((sheet) => sheet?.complete && sheet.naturalWidth);
    let bounds: Omit<HitBox, "id">;
    if (usablePet && (meadow || !fallback)) {
      drawPet(ctx, actor, pet, world);
      bounds = { x: actor.x - 48, y: actor.y - 101, width: 96, height: 104 };
    } else if (fallback) {
      drawPixelActor(ctx, actor, fallback, world);
      const position = pixelActorPosition(actor, world);
      bounds = { x: position.x - 20, y: position.y - 66, width: 40, height: 80 };
    } else {
      drawFallbackActor(ctx, actor, meadow);
      bounds = { x: actor.x - 17, y: actor.y - 60, width: 34, height: 65 };
    }
    hitboxes.push({ ...(meadow ? bounds : { x: actor.x - 30, y: actor.y - 70, width: 60, height: 110 }), id: actor.task.task_id });
  });
  if (meadow) drawMeadowForeground(ctx, world);
  const event = world.highlighted;
  if (!meadow && event) { box(ctx, 20, world.height - 29, world.width - 40, 21, "#ffffffdf", 5); text(ctx, `${eventCaption(event)} #${event.event_id} · ${truncate(event.text, world.mobile ? 23 : 55)}`, world.width / 2, world.height - 15, "#9b9873", 11, "center"); }
  if (!meadow && event && event.from_task_id && event.to_task_id) {
    const from = world.actors.get(event.from_task_id); const to = world.actors.get(event.to_task_id);
    if (from && to) { const t = (world.time % 2) / 2; const x = from.x + (to.x - from.x) * t; const y = from.y + (to.y - from.y) * t - 35; box(ctx, x - 5, y - 3, 10, 7, "#d8ad73", 2); }
  }
  ctx.restore();
  return hitboxes;
}
