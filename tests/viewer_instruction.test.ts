import { expect, test } from "bun:test";
import {
  addVariable,
  appliedInstructionOutcome,
  mergeSavedEpisodeDraft,
  movePart,
  parseInstructionSource,
  removePart,
  renderInstruction,
  selectionsForTemplate,
  updateText,
  type InstructionTemplate,
} from "../pipeline/server/viewer_ui/instruction-model";

function template(): InstructionTemplate {
  return {
    parts: [
      { type: "text", text: "请将" },
      { type: "variable", key: "object" },
      { type: "text", text: "放入" },
      { type: "variable", key: "target" },
      { type: "text", text: "中" },
    ],
    variables: {
      object: { label: "物体", options: ["红色方块", "蓝色圆柱"], default: "红色方块" },
      target: { label: "目标位置", options: ["左侧盒子", "右侧盒子"], default: "左侧盒子" },
    },
  };
}

test("visual operations add, reorder, edit and remove variable chips", () => {
  const added = addVariable(template(), "speed", {
    label: "速度", options: ["慢速", "快速"], default: "慢速",
  });
  expect(added.parts.at(-1)).toEqual({ type: "variable", key: "speed" });
  const moved = movePart(added, added.parts.length - 1, 1);
  expect(moved.parts[1]).toEqual({ type: "variable", key: "speed" });
  const edited = updateText(moved, 0, "请快速将");
  expect(edited.parts[0]).toEqual({ type: "text", text: "请快速将" });
  const removed = removePart(edited, 1);
  expect(removed.variables.speed).toBeUndefined();
  expect(removed.parts.some(part => part.type === "variable" && part.key === "speed")).toBeFalse();
});

test("render is immediate and duplicate variable names are rejected", () => {
  expect(renderInstruction(template(), {
    object: "蓝色圆柱", target: "右侧盒子",
  })).toBe("请将蓝色圆柱放入右侧盒子中");
  expect(() => addVariable(template(), "object", {
    label: "重复", options: ["x"], default: "x",
  })).toThrow("已存在");
});

test("advanced source syntax maps to the visual parts without losing definitions", () => {
  const parsed = parseInstructionSource(
    "请将{{object}}放入{{target}}中",
    template().variables,
  );
  expect(parsed.parts).toEqual(template().parts);
  expect(parsed.variables).toEqual(template().variables);
  expect(() => parseInstructionSource("没有变量", template().variables)).toThrow("变量");
});

test("template changes drop stale selections and fill missing defaults", () => {
  expect(selectionsForTemplate(template(), {
    instruction: "旧模板变量",
    object: "蓝色圆柱",
  })).toEqual({
    object: "蓝色圆柱",
    target: "左侧盒子",
  });
});

test("a saved episode draft advances its version so a failed apply can retry", () => {
  const head = {
    revision: 4,
    source_signature: "a".repeat(64),
    template: template(),
    warnings: [],
  };
  const episode = {
    episode_index: 0,
    frames: 12,
    current_instruction: "旧指令",
    revision: 4,
    source_signature: "a".repeat(64),
    has_draft: false,
    selections: { object: "红色方块", target: "左侧盒子" },
    rendered: "请将红色方块放入左侧盒子中",
    valid: true,
    error: null,
  };
  const next = mergeSavedEpisodeDraft(head, episode, {
    revision: 5,
    source_signature: "b".repeat(64),
    selections: { object: "蓝色圆柱", target: "右侧盒子" },
    rendered: "请将蓝色圆柱放入右侧盒子中",
  });

  expect(next.head.revision).toBe(5);
  expect(next.head.source_signature).toBe("b".repeat(64));
  expect(next.episode.revision).toBe(5);
  expect(next.episode.source_signature).toBe("b".repeat(64));
  expect(next.episode.has_draft).toBeTrue();
  expect(next.episode.current_instruction).toBe("旧指令");
});

test("a failed post-apply refresh requires reload instead of claiming success", () => {
  expect(
    appliedInstructionOutcome(
      false,
      "Episode 0 的指令已写入原数据集",
    ),
  ).toEqual({
    notice: "",
    refreshRequired: true,
  });
  expect(
    appliedInstructionOutcome(
      true,
      "Episode 0 的指令已写入原数据集",
    ),
  ).toEqual({
    notice: "Episode 0 的指令已写入原数据集",
    refreshRequired: false,
  });
});
