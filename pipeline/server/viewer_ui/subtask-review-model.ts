/** Explicit half-open frame intervals; gaps are never inferred or filled. */
export interface Segment { start_frame: number; end_frame: number; text: string }
export interface EpisodeDraft {
  episode_index: number; fps: number; frames: number; revision: number;
  source_signature: string; has_draft: boolean; segments: Segment[];
}

export function ordered(segments: Segment[]): Segment[] {
  return [...segments].sort((a, b) => a.start_frame - b.start_frame || a.end_frame - b.end_frame);
}

export function validate(segments: Segment[], frames: number): string | null {
  if (segments.length > 2000) return "每个 Episode 最多支持 2000 段";
  let end = 0;
  for (const s of ordered(segments)) {
    if (!Number.isInteger(s.start_frame) || !Number.isInteger(s.end_frame) ||
        s.start_frame < end || s.start_frame < 0 || s.start_frame >= s.end_frame || s.end_frame > frames)
      return "区间不能重叠，且必须满足 0 ≤ 开始帧 < 结束帧 ≤ 总帧数";
    if (!s.text.trim() || s.text.trim().length > 400) return "每段文本需要 1–400 个字符";
    end = s.end_frame;
  }
  return null;
}

export function split(segments: Segment[], index: number, frame: number): Segment[] {
  const s = segments[index];
  if (!s || !Number.isInteger(frame) || frame <= s.start_frame || frame >= s.end_frame) return segments;
  return [...segments.slice(0, index), { ...s, end_frame: frame },
    { ...s, start_frame: frame }, ...segments.slice(index + 1)];
}

export function merge(segments: Segment[], index: number): Segment[] {
  const a = segments[index], b = segments[index + 1];
  // Never silently label a gap, or discard a different instruction.
  if (!a || !b || a.end_frame !== b.start_frame || a.text !== b.text) return segments;
  return [...segments.slice(0, index), { ...a, end_frame: b.end_frame }, ...segments.slice(index + 2)];
}

export function gapAt(segments: Segment[], frame: number, frames: number): [number, number] | null {
  const rows = ordered(segments);
  let start = 0;
  for (const s of rows) {
    if (frame >= start && frame < s.start_frame) return [frame, s.start_frame];
    start = s.end_frame;
  }
  return frame >= start && frame < frames ? [frame, frames] : null;
}
