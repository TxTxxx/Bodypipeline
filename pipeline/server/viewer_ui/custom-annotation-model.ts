export interface FieldDef {
  id: string;
  label?: string;
  description?: string;
  dtype: "text" | "int" | "float" | "bool" | "single" | "multi";
  widget?: string;
  options?: { value: string; label: string }[];
  required?: boolean;
  default?: unknown;
  min?: number;
  max?: number;
  step?: number;
  max_length?: number;
  unit?: string;
  key?: string;
}
export interface TypeDef {
  id: string;
  label?: string;
  scope: "episode" | "interval" | "frame";
  allow_overlap?: boolean;
  camera_bound?: boolean;
  fields: FieldDef[];
}
export interface Scheme {
  id?: string;
  name?: string;
  version?: number;
  types: TypeDef[];
  archived?: boolean;
}
export interface CustomRecord {
  id: string;
  type_id: string;
  start_frame?: number;
  end_frame?: number;
  frame_index?: number;
  camera?: string | null;
  values: Record<string, unknown>;
}
export interface EpisodeSummary {
  episode_uid: string;
  episode_index: number;
  status: string;
  trashed?: boolean;
}
export interface FrameSource {
  frames: number;
  fps: number;
  frame_timestamps?: number[];
}
export interface CustomState {
  values: Record<string, Record<string, unknown>>;
  records: CustomRecord[];
  status: string;
}
export function frameTime(source: FrameSource, index: number): number {
  const ts = source.frame_timestamps;
  if (!ts?.length) return index / source.fps;
  if (index < ts.length) return ts[Math.max(0, index)];
  return (
    ts[ts.length - 1] +
    (ts.length > 1 ? ts[ts.length - 1] - ts[ts.length - 2] : 1 / source.fps)
  );
}
export function nearestFrame(
  source: FrameSource,
  time: number,
  boundary = false,
): number {
  if (!Number.isFinite(time)) return NaN;
  const count = source.frames + (boundary ? 1 : 0);
  let best = 0,
    distance = Infinity;
  for (let i = 0; i < count; i++) {
    const d = Math.abs(frameTime(source, i) - time);
    if (d < distance) {
      best = i;
      distance = d;
    }
  }
  return best;
}
export function snapshotKey(value: unknown): string {
  return JSON.stringify(value);
}
export function fieldDefault(
  field: Pick<FieldDef, "id" | "dtype" | "default">,
): unknown {
  return field.default;
}
export function schemeDefaults(scheme: Scheme | null): CustomState["values"] {
  const values: CustomState["values"] = {};
  for (const type of scheme?.types || [])
    if (type.scope === "episode") {
      values[type.id] = {};
      for (const field of type.fields)
        if (field.default !== undefined)
          values[type.id][field.id] = field.default;
    }
  return values;
}
export function filterEpisodes(
  episodes: EpisodeSummary[],
  status: string,
): EpisodeSummary[] {
  return episodes.filter((e) => !e.trashed && (!status || e.status === status));
}
export function shortcutValue(field: FieldDef, current: unknown): unknown {
  if (field.dtype === "bool") return current !== true;
  if (field.dtype === "single" && field.options?.length) {
    const index = field.options.findIndex((o) => o.value === current);
    return field.options[(index + 1) % field.options.length].value;
  }
  return undefined;
}
