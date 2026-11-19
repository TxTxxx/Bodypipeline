import { expect, test } from "bun:test";
import {
  addVariable,
  movePart,
  parseInstructionSource,
  removePart,
  renderInstruction,
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
