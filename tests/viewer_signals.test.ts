import { expect, test } from "bun:test";
import {
  buildSignalChannels,
  closestRow,
  describeSeries,
  formatValue,
} from "../pipeline/server/viewer_ui/chart-model";

test("ten ARX fields separate into six families without mixing physical quantities", () => {
  const fields = {
    action: 1,
    action_base: 2,
    action_eef: 3,
    action_velocity: 4,
    "observation.state": 5,
    "observation.velocity": 6,
    "observation.base_velocity": 7,
    "observation.eef": 8,
    "observation.effort": 9,
    "observation.robot_base": 10,
  };
  const channels = buildSignalChannels([
    [{ timestamp: 0, "0": fields, "1": fields }],
  ]);
  expect(channels.map((c) => c.channel)).toEqual(["0", "1"]);
  for (const channel of channels) {
    expect(new Set(channel.series.map((s) => s.family)).size).toBe(6);
    expect(
      channel.series
        .filter((s) => s.family === "position")
        .map((s) => s.feature),
    ).toEqual(["action", "observation.state"]);
    expect(
      channel.series
        .filter((s) => s.family === "base-velocity")
        .map((s) => s.feature),
    ).toEqual(["action_velocity", "observation.base_velocity"]);
    for (const family of new Set(channel.series.map((s) => s.family))) {
      expect(
        channel.series.filter((s) => s.family === family).length,
      ).toBeLessThanOrEqual(2);
    }
  }
});

test("paired, unpaired and named channel schemas give the same feature and channel", () => {
  expect(describeSeries("0 | action")).toMatchObject({
    channel: "0",
    feature: "action",
    role: "command",
  });
  expect(describeSeries("action | 0")).toMatchObject({
    channel: "0",
    feature: "action",
    role: "command",
  });
  expect(describeSeries("left_shoulder | observation.state")).toMatchObject({
    channel: "left_shoulder",
    family: "position",
    role: "measured",
  });
  expect(describeSeries("observation.state | left_shoulder")).toMatchObject({
    channel: "left_shoulder",
    family: "position",
  });
  expect(describeSeries("0 | observation.temperature").family).toBe(
    "feature:observation.temperature",
  );
});

test("missing first-frame values still appear and unknown fields stay separate", () => {
  const channels = buildSignalChannels([
    [
      { timestamp: 0 },
      {
        timestamp: 1,
        "3": { "observation.effort": 0, "observation.temperature": 30 },
      },
    ],
  ]);
  expect(channels).toHaveLength(1);
  expect(channels[0].series.map((s) => s.family)).toEqual([
    "effort",
    "feature:observation.temperature",
  ]);
  expect(buildSignalChannels([])).toEqual([]);
});

test("hover reads the nearest sample and preserves invalid values as unavailable", () => {
  const rows = [
    { timestamp: 0, value: 1 },
    { timestamp: 2, value: 2 },
    { timestamp: 5, value: 3 },
  ];
  expect(closestRow(rows, 0.1).value).toBe(1);
  expect(closestRow(rows, 1.9).value).toBe(2);
  expect(closestRow(rows, 100).value).toBe(3);
  expect(closestRow([], 0)).toEqual({});
  expect(formatValue(0)).toBe("0.000");
  expect(formatValue(NaN)).toBe("—");
  expect(formatValue(undefined)).toBe("—");
});
