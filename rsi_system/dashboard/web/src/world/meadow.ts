import type { Actor, World } from "./engine";

function ellipse(ctx: CanvasRenderingContext2D, x: number, y: number, rx: number, ry: number, color: string): void {
  ctx.fillStyle = color;
  ctx.beginPath(); ctx.ellipse(x, y, rx, ry, 0, 0, Math.PI * 2); ctx.fill();
}

function rounded(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, color: string, radius = 4): void {
  ctx.fillStyle = color;
  ctx.beginPath(); ctx.roundRect(x, y, w, h, radius); ctx.fill();
}

function line(ctx: CanvasRenderingContext2D, x1: number, y1: number, x2: number, y2: number, color: string, width = 2): void {
  ctx.strokeStyle = color; ctx.lineWidth = width; ctx.lineCap = "round";
  ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
}

function fallbackLandscape(ctx: CanvasRenderingContext2D, world: World): void {
  const { width, height } = world;
  const horizon = world.mobile ? 197 : 158;
  const sky = ctx.createLinearGradient(0, 0, 0, horizon + 80);
  sky.addColorStop(0, "#a8d6e8"); sky.addColorStop(1, "#f3eeca");
  ctx.fillStyle = sky; ctx.fillRect(0, 0, width, height);
  ellipse(ctx, width * .8, horizon * .38, 30, 30, "#fff7c6");
  for (const [x, y, w] of [[.23, .25, 92], [.52, .15, 68], [.83, .37, 77]]) {
    ellipse(ctx, width * x, horizon * y, w / 2, 12, "#ffffffa8");
    ellipse(ctx, width * x - w / 5, horizon * y - 8, w / 3, 13, "#ffffffa8");
  }
  ctx.fillStyle = "#9cba79"; ctx.beginPath(); ctx.moveTo(0, horizon + 26);
  ctx.bezierCurveTo(width * .2, horizon - 45, width * .38, horizon + 20, width * .55, horizon - 14);
  ctx.bezierCurveTo(width * .76, horizon - 52, width * .9, horizon - 12, width, horizon - 38);
  ctx.lineTo(width, height); ctx.lineTo(0, height); ctx.fill();
  ctx.fillStyle = "#bdd57f"; ctx.beginPath(); ctx.moveTo(0, horizon + 101);
  ctx.bezierCurveTo(width * .32, horizon - 8, width * .66, horizon + 85, width, horizon + 15);
  ctx.lineTo(width, height); ctx.lineTo(0, height); ctx.fill();
  ctx.fillStyle = "#91be60"; ctx.beginPath(); ctx.moveTo(0, horizon + 175);
  ctx.bezierCurveTo(width * .4, horizon + 80, width * .73, horizon + 153, width, horizon + 100);
  ctx.lineTo(width, height); ctx.lineTo(0, height); ctx.fill();
  const treeX = world.mobile ? 12 : 57;
  line(ctx, treeX, horizon + 85, treeX + 18, horizon - 102, "#816a44", 19);
  for (const [dx, dy, rx, ry, color] of [[-42, -91, 70, 55, "#668e54"], [33, -112, 72, 59, "#79a65b"], [-3, -148, 70, 56, "#85af66"]] as const)
    ellipse(ctx, treeX + dx, horizon + dy, rx, ry, color);
}

function flower(ctx: CanvasRenderingContext2D, x: number, y: number, color: string, scale = 1): void {
  line(ctx, x, y + 8 * scale, x, y, "#688e46", 1.5 * scale);
  for (let i = 0; i < 5; i++) {
    const angle = i * Math.PI * 2 / 5;
    ellipse(ctx, x + Math.cos(angle) * 3 * scale, y + Math.sin(angle) * 3 * scale, 2.5 * scale, 2.5 * scale, color);
  }
  ellipse(ctx, x, y, 2 * scale, 2 * scale, "#eed36e");
}

type MeadowZone = { x: number; y: number; rx: number; ry: number; tone?: string | number };
type Orchestration = { phase: "summon" | "approach" | "brief" | "depart" | "work"; fromId: string; toId: string; progress: number; eventId: number };
export interface MeadowSmallProp {
  kind: "notebook" | "laptop" | "paper";
  x: number;
  y: number;
  depth: number;
  running?: boolean;
  alpha?: number;
  angle?: number;
}

function orchestration(world: World): Orchestration | undefined {
  return (world as World & { orchestration?: Orchestration }).orchestration;
}

function clothPath(ctx: CanvasRenderingContext2D, zone: MeadowZone, inset = 0): void {
  const rx = Math.max(8, zone.rx - inset);
  const ry = Math.max(6, zone.ry - inset);
  const exponent = zone.rx > 130 ? .58 : .78;
  ctx.beginPath();
  for (let step = 0; step <= 96; step++) {
    const angle = step / 96 * Math.PI * 2;
    const cosine = Math.cos(angle);
    const sine = Math.sin(angle);
    const ripple = 1 + Math.sin(angle * 3) * .009 + Math.sin(angle * 5) * .007;
    const x = zone.x + Math.sign(cosine) * Math.abs(cosine) ** exponent * rx * ripple;
    const y = zone.y + Math.sign(sine) * Math.abs(sine) ** exponent * ry * ripple;
    if (step === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.closePath();
}

function patch(ctx: CanvasRenderingContext2D, zone: MeadowZone, tone: number): void {
  const palette = [
    ["#adc3aa30", "#e5edd337"],
    ["#e6d1af32", "#fff1d23a"],
    ["#b0c8cc30", "#dbece83b"],
  ];
  const [fabric, edge] = palette[((tone % palette.length) + palette.length) % palette.length];
  ctx.save();
  ctx.translate(1, 3); clothPath(ctx, zone); ctx.fillStyle = "#456d3612"; ctx.fill();
  ctx.restore();
  clothPath(ctx, zone); ctx.fillStyle = fabric; ctx.fill();
  ctx.save(); clothPath(ctx, zone, 3); ctx.clip();
  ctx.strokeStyle = "#fffcdf13"; ctx.lineWidth = .6;
  for (let y = zone.y - zone.ry; y <= zone.y + zone.ry; y += 13) {
    ctx.beginPath(); ctx.moveTo(zone.x - zone.rx, y); ctx.lineTo(zone.x + zone.rx, y + 2); ctx.stroke();
  }
  ctx.strokeStyle = "#698b7010";
  for (let x = zone.x - zone.rx; x <= zone.x + zone.rx; x += 19) {
    ctx.beginPath(); ctx.moveTo(x, zone.y - zone.ry); ctx.lineTo(x + 3, zone.y + zone.ry); ctx.stroke();
  }
  ctx.restore();
  ctx.save();
  ctx.strokeStyle = edge;
  ctx.lineWidth = .8; ctx.setLineDash([2, 5]);
  clothPath(ctx, zone, 4); ctx.stroke();
  ctx.restore();
}

function notebook(ctx: CanvasRenderingContext2D, x: number, y: number): void {
  ellipse(ctx, x, y + 3, 13, 4, "#516c4230");
  rounded(ctx, x - 10, y - 5, 20, 13, "#849d93", 2);
  rounded(ctx, x - 8, y - 5, 17, 11, "#f0edda", 1);
  line(ctx, x - 4, y - 3, x - 4, y + 5, "#b1b9a4", 1);
}

function laptop(ctx: CanvasRenderingContext2D, x: number, y: number, running: boolean): void {
  ellipse(ctx, x, y + 3, 16, 4, "#516c422c");
  rounded(ctx, x - 12, y - 11, 24, 14, "#788b82", 2);
  rounded(ctx, x - 10, y - 9, 20, 10, running ? "#c8dfd3" : "#d7dfd1", 1);
  rounded(ctx, x - 15, y + 3, 30, 3, "#a0aaa0", 1);
}

function overlapsActor(x: number, y: number, halfWidth: number, top: number, bottom: number, actor: { x: number; y: number }): boolean {
  return x + halfWidth > actor.x - 48 && x - halfWidth < actor.x + 48
    && y + bottom > actor.y - 101 && y - top < actor.y + 5;
}

function visibleGroupZones(world: World): { zone: MeadowZone; tone: number; members: Actor[] }[] {
  return world.groups.flatMap((group, index) => {
    const source = (group as typeof group & { zone?: MeadowZone }).zone;
    const members = group.taskIds.map((id) => world.actors.get(id)).filter((actor): actor is Actor => Boolean(actor && actor.task.role !== "coordinator" && !actor.awaitingCreation));
    if (!source || !members.length) return [];
    const tone = typeof source.tone === "number" ? source.tone : source.tone === "sand" ? 1 : source.tone === "linen" || source.tone === "blue" ? 2 : index;
    const zone = members.length === 1 ? { ...source, x: members[0].home.x, y: members[0].home.y + 3, rx: 49, ry: 22 } : source;
    return [{ zone, tone, members }];
  });
}

function drawGroupGround(ctx: CanvasRenderingContext2D, world: World): void {
  const coordinator = [...world.actors.values()].find((actor) => actor.task.role === "coordinator");
  if (coordinator) {
    ellipse(ctx, coordinator.home.x + 1, coordinator.home.y + 4, 42, 16, "#4d70361c");
    ellipse(ctx, coordinator.home.x, coordinator.home.y + 2, 39, 14, "#e8d5ad42");
  }

  for (const { zone, tone } of visibleGroupZones(world)) patch(ctx, zone, tone);
}

export function meadowSmallProps(world: World): MeadowSmallProp[] {
  const result: MeadowSmallProp[] = [];

  const state = orchestration(world);
  if (state?.phase === "brief") {
    const from = world.actors.get(state.fromId);
    const to = world.actors.get(state.toId);
    const progress = Math.max(0, Math.min(1, state.progress));
    if (from && to && Math.hypot(from.x - to.x, from.y - to.y) < 145 && progress > 0 && progress < 1) {
      const handoff = .36 + .28 * progress * progress * (3 - 2 * progress);
      const x = from.x + (to.x - from.x) * handoff;
      const y = from.y + (to.y - from.y) * handoff - 32;
      const clear = [...world.actors.values()].every((actor) => actor === from || actor === to || !overlapsActor(x, y, 7, 5, 6, actor));
      if (clear) result.push({ kind: "paper", x, y, depth: Math.max(from.y, to.y) + .1, alpha: Math.sin(progress * Math.PI) * .68, angle: -.12 + progress * .18 });
    }
  }
  return result;
}

export function drawMeadowSmallProp(ctx: CanvasRenderingContext2D, prop: MeadowSmallProp): void {
  if (prop.kind === "notebook") notebook(ctx, prop.x, prop.y);
  else if (prop.kind === "laptop") laptop(ctx, prop.x, prop.y, Boolean(prop.running));
  else {
    ctx.save(); ctx.translate(prop.x, prop.y); ctx.rotate(prop.angle ?? 0); ctx.globalAlpha = prop.alpha ?? 1;
    rounded(ctx, -6, -4, 12, 9, "#fff9e8", 1);
    line(ctx, -3, -1, 3, -1, "#c6bb9d", .7);
    ctx.restore();
  }
}

function drawPairLight(ctx: CanvasRenderingContext2D, world: World): void {
  const state = orchestration(world);
  if (!state || state.phase === "work") return;
  const strength = state.phase === "depart" ? Math.max(0, 1 - state.progress) : state.phase === "summon" ? Math.min(1, .45 + state.progress) : 1;
  for (const id of [state.fromId, state.toId]) {
    const actor = world.actors.get(id);
    if (!actor) continue;
    ctx.save(); ctx.globalAlpha = .26 * strength;
    ellipse(ctx, actor.x, actor.y + 2, 31, 9, "#fff3ce");
    ctx.globalAlpha = .28 * strength;
    ellipse(ctx, actor.x, actor.y + 2, 19, 5, "#fff6dc");
    ctx.restore();
  }
}

function bookSpine(ctx: CanvasRenderingContext2D, x: number, bottom: number, height: number, color: string, width = 6): void {
  rounded(ctx, x, bottom - height, width, height, color, 1);
  line(ctx, x + 1, bottom - 4, x + width - 1, bottom - 4, "#fff5d38a", 1);
}

function cart(ctx: CanvasRenderingContext2D, world: World): void {
  const { x, y } = world.shelf;
  ellipse(ctx, x + 2, y + 33, 47, 9, "#496d3d37");
  rounded(ctx, x - 42, y - 61, 84, 84, "#876b4d", 3);
  rounded(ctx, x - 37, y - 56, 74, 73, "#e3c69b", 2);
  for (const top of [-53, -29, -5]) rounded(ctx, x - 34, y + top, 68, 19, "#f3e0bd", 1);
  for (const [bottom, palette] of [
    [-34, ["#8da99e", "#c4aa80", "#a9b89b"]],
    [-10, ["#c09d85", "#9bafbd", "#b6a978"]],
    [14, ["#9aae91", "#d0ad8c", "#9db7ad"]],
  ] as const) {
    palette.forEach((color, index) => bookSpine(ctx, x - 31 + index * 7, y + bottom, 13 + (index % 2) * 3, color));
  }
  rounded(ctx, x - 43, y - 64, 86, 5, "#b58c62", 2);
  line(ctx, x - 42, y + 24, x + 42, y + 24, "#6f5743", 4);
  line(ctx, x + 43, y - 46, x + 49, y - 46, "#836b50", 3);
  line(ctx, x + 49, y - 46, x + 49, y - 25, "#836b50", 3);
  ellipse(ctx, x - 27, y + 31, 6, 6, "#58544a"); ellipse(ctx, x + 28, y + 31, 6, 6, "#58544a");
  ellipse(ctx, x - 27, y + 31, 2, 2, "#a5a79a"); ellipse(ctx, x + 28, y + 31, 2, 2, "#a5a79a");
}

function easel(ctx: CanvasRenderingContext2D, world: World): void {
  const { x, y } = world.board;
  ellipse(ctx, x, y + 68, 89, 13, "#4d713b50");
  line(ctx, x - 62, y + 16, x - 77, y + 72, "#806042", 5);
  line(ctx, x + 62, y + 16, x + 77, y + 72, "#806042", 5);
  line(ctx, x, y + 13, x, y + 70, "#9d7348", 5);
  rounded(ctx, x - 83, y - 54, 166, 76, "#9b7149", 4);
  rounded(ctx, x - 77, y - 48, 154, 64, "#fcf9e9", 2);
  line(ctx, x - 67, y + 23, x + 68, y + 23, "#b88753", 5);
  rounded(ctx, x + 36, y - 38, 19, 25, "#ede9d8", 1);
  ellipse(ctx, x + 45, y - 39, 2, 2, "#ba9a77");
}

export function drawMeadow(ctx: CanvasRenderingContext2D, world: World, scenicBackground?: HTMLImageElement, paintBackdrop = true): void {
  if (paintBackdrop) {
    if (scenicBackground?.complete && scenicBackground.naturalWidth) ctx.drawImage(scenicBackground, 0, 0, world.width, world.height);
    else fallbackLandscape(ctx, world);
  }

  const { width, height } = world;
  drawGroupGround(ctx, world);
  drawPairLight(ctx, world);
  if (!world.actors.size) return;
  for (let i = 0; i < 16; i++) {
    const x = (i * 73 + 29) % width;
    const y = (i * 97 + 37) % height;
    if (y < (world.mobile ? 302 : 255) || x < 18 || x > width - 18) continue;
    if ([...world.actors.values()].some((actor) => Math.abs(x - actor.x) < 35 && Math.abs(y - actor.y) < 22)) continue;
    flower(ctx, x, y, i % 4 === 0 ? "#fff8db" : i % 4 === 1 ? "#e9a4a0" : "#f0cd73", .7);
  }
}

export function drawMeadowProp(ctx: CanvasRenderingContext2D, world: World, prop: "shelf" | "board"): void {
  if (prop === "shelf") cart(ctx, world);
  else easel(ctx, world);
}

export function drawMeadowShelfTrim(ctx: CanvasRenderingContext2D, world: World): void {
  for (const bottom of [-34, -10, 14]) rounded(ctx, world.shelf.x - 36, world.shelf.y + bottom, 72, 3, "#a17b54", 1);
}

export function drawMeadowForeground(ctx: CanvasRenderingContext2D, world: World): void {
  if (!world.actors.size) return;
  const y = world.height - 19;
  for (const [x, dy, color] of [[18, 5, "#f9e6a5"], [38, -2, "#f4b2a3"], [world.width - 36, 0, "#ffefd3"], [world.width - 17, 8, "#df9ca0"]] as const)
    flower(ctx, x, y + dy, color, 1.25);
}
