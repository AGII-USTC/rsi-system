import type { Event, Task } from "./types";

export interface WorldSettings {
  style: "office" | "meadow";
  seed: string;
  layout: "open" | "clustered";
  motion: boolean;
  description?: string;
}

export interface WorldSceneProps {
  tasks: Task[];
  events: Event[];
  missionId: string;
  objective: string;
  live: boolean;
  demo: boolean;
  settings: WorldSettings;
  onTask: (taskId: string) => void;
  onEvent: (eventId: number) => void;
}
