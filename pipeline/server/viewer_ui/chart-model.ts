export type ChartRow = Record<string, number | Record<string, number>>;
export type FlatRow = Record<string, number>;
export type SignalRole = "command" | "measured" | "other";
export type SignalSeries = {
  key: string;
  feature: string;
  channel: string;
  family: string;
  role: SignalRole;
  label: string;
};
export type SignalChannel = {
  id: string;
  channel: string;
  rows: FlatRow[];
  series: SignalSeries[];
};

export const SIGNAL_FAMILIES = [
  {
    id: "position",
    title: "关节位置",
    description: "比较同一通道的指令位置与实际位置",
  },
  {
    id: "velocity",
    title: "关节速度",
    description: "查看各关节的实际运动速度",
  },
  {
    id: "effort",
    title: "关节力矩",
    description: "查看各通道的 effort 信号，单位以数据集定义为准",
  },
  {
    id: "eef",
    title: "末端位姿",
    description: "比较同一末端分量的指令值与实际值",
  },
  {
    id: "base",
    title: "底盘位姿",
    description: "比较同一底盘分量的指令值与实际值",
  },
  {
    id: "base-velocity",
    title: "底盘速度",
    description: "比较同一底盘速度分量的指令值与实际值",
  },
];

const FEATURES: Record<string, [string, SignalRole, string]> = {
  action: ["position", "command", "指令位置"],
  "observation.state": ["position", "measured", "实际位置"],
  "observation.velocity": ["velocity", "measured", "实际速度"],
  "observation.effort": ["effort", "measured", "实际力矩"],
  action_eef: ["eef", "command", "指令位姿"],
  "observation.eef": ["eef", "measured", "实际位姿"],
  action_base: ["base", "command", "指令位姿"],
  "observation.robot_base": ["base", "measured", "实际位姿"],
  action_velocity: ["base-velocity", "command", "指令速度"],
  "observation.base_velocity": ["base-velocity", "measured", "实际速度"],
};

export function flattenRow(row: ChartRow, prefix = ""): FlatRow {
  const result: FlatRow = {};
  for (const [key, value] of Object.entries(row)) {
    const name = prefix ? `${prefix} | ${key}` : key;
    if (typeof value === "number") result[name] = value;
    else if (value && typeof value === "object") {
      Object.assign(result, flattenRow(value, name));
    }
  }
  return result;
}

export function describeSeries(key: string): SignalSeries {
  const parts = key.split(" | ");
  // groupRowBySuffix emits channel -> feature for paired fields, but keeps
  // feature | channel for a field that has no partner. Handle both forms.
  const featureFirst =
    parts.length > 1 &&
    (parts[0] in FEATURES || /^(action|observation)[._]/.test(parts[0]));
  const feature = featureFirst ? parts.slice(0, -1).join(" | ") : parts.at(-1)!;
  const channel =
    parts.length === 1
      ? key
      : featureFirst
        ? parts.at(-1)!
        : parts.slice(0, -1).join(" | ");
  const [family, role, label] = FEATURES[feature] ?? [
    `feature:${feature}`,
    "other",
    feature,
  ];
  return { key, feature, channel, family, role, label };
}

export function buildSignalChannels(data: ChartRow[][]): SignalChannel[] {
  return data.flatMap((group, groupIndex) => {
    const rows = group.map((row) => flattenRow(row));
    const keys = new Set(rows.flatMap((row) => Object.keys(row)));
    keys.delete("timestamp");
    const channels = new Map<string, SignalSeries[]>();
    for (const key of keys) {
      const series = describeSeries(key);
      const list = channels.get(series.channel) ?? [];
      list.push(series);
      channels.set(series.channel, list);
    }
    return [...channels].map(([channel, series]) => ({
      id: `${groupIndex}:${channel}`,
      channel,
      rows,
      series,
    }));
  });
}

export function closestRow(rows: FlatRow[], time: number): FlatRow {
  if (!rows.length) return {};
  let low = 0;
  let high = rows.length - 1;
  while (low < high) {
    const mid = Math.floor((low + high) / 2);
    if (rows[mid].timestamp < time) low = mid + 1;
    else high = mid;
  }
  if (
    low > 0 &&
    Math.abs(rows[low - 1].timestamp - time) <=
      Math.abs(rows[low].timestamp - time)
  )
    low--;
  return rows[low];
}

export function formatValue(value: number | undefined): string {
  if (value === undefined || !Number.isFinite(value)) return "—";
  if (value !== 0 && (Math.abs(value) < 0.001 || Math.abs(value) >= 100000))
    return value.toExponential(2);
  return value.toLocaleString("en-US", {
    maximumFractionDigits: 3,
    minimumFractionDigits: 3,
  });
}
