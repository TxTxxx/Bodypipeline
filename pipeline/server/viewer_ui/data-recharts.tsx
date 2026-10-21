"use client";
// bodypipeline: signal explorer v1

import React, { useEffect, useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useTime } from "../context/time-context";
import {
  buildSignalChannels,
  closestRow,
  formatValue,
  SIGNAL_FAMILIES,
  type ChartRow,
  type SignalChannel,
  type SignalSeries,
} from "../utils/chart-model";

type DataGraphProps = { data: ChartRow[][]; onChartsReady?: () => void };

export const DataRecharts = React.memo(function DataRecharts({
  data,
  onChartsReady,
}: DataGraphProps) {
  const [family, setFamily] = useState("position");
  const [search, setSearch] = useState("");
  const [wide, setWide] = useState(false);
  const [hoveredTime, setHoveredTime] = useState<number | null>(null);
  const channels = useMemo(() => buildSignalChannels(data), [data]);
  const families = useMemo(() => {
    const ids = new Set(channels.flatMap((c) => c.series.map((s) => s.family)));
    return [
      ...SIGNAL_FAMILIES.filter((f) => ids.has(f.id)),
      ...[...ids]
        .filter((id) => id.startsWith("feature:"))
        .map((id) => ({
          id,
          title: id.slice(8),
          description: "独立查看此字段，避免与其他量纲混在一起",
        })),
    ];
  }, [channels]);
  const activeFamily = families.find((f) => f.id === family) ?? families[0];
  const visible = useMemo(
    () =>
      channels
        .map((c) => ({
          ...c,
          series: c.series.filter((s) => s.family === activeFamily?.id),
        }))
        .filter(
          (c) =>
            c.series.length &&
            c.channel.toLowerCase().includes(search.trim().toLowerCase()),
        ),
    [channels, activeFamily?.id, search],
  );

  useEffect(() => {
    onChartsReady?.();
  }, [onChartsReady]);
  if (!activeFamily) return null;

  return (
    <section className="signal-explorer" aria-label="关节与运动信号">
      <header className="signal-heading">
        <div>
          <p className="signal-eyebrow">MOTION SIGNALS</p>
          <h2>
            关节与运动信号 <span>{visible.length} 个通道</span>
          </h2>
        </div>
        <span className="signal-help">悬停读数 · 点击曲线定位视频</span>
      </header>
      <div className="signal-toolbar">
        <div className="signal-tabs" role="tablist" aria-label="信号类别">
          {families.map((f) => (
            <button
              key={f.id}
              type="button"
              role="tab"
              id={`signal-tab-${f.id}`}
              aria-selected={activeFamily.id === f.id}
              aria-controls="signal-panel"
              onClick={() => {
                setFamily(f.id);
                setHoveredTime(null);
              }}
              onKeyDown={(event) => {
                if (
                  !["ArrowLeft", "ArrowRight", "Home", "End"].includes(
                    event.key,
                  )
                )
                  return;
                event.preventDefault();
                const index = families.indexOf(f);
                const next =
                  event.key === "Home"
                    ? 0
                    : event.key === "End"
                      ? families.length - 1
                      : (index +
                          (event.key === "ArrowRight" ? 1 : -1) +
                          families.length) %
                        families.length;
                setFamily(families[next].id);
                setHoveredTime(null);
                document
                  .getElementById(`signal-tab-${families[next].id}`)
                  ?.focus();
              }}
              tabIndex={activeFamily.id === f.id ? 0 : -1}
            >
              {f.title}
            </button>
          ))}
        </div>
        <div className="signal-tools">
          <input
            type="search"
            aria-label="筛选通道"
            placeholder="筛选通道…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <button
            type="button"
            className="signal-layout"
            aria-pressed={wide}
            onClick={() => setWide((v) => !v)}
          >
            {wide ? "切换双列" : "放大图表"}
          </button>
        </div>
      </div>
      <p className="signal-description">
        {activeFamily.description}。
        <span>每图一个通道，纵轴独立缩放；数值保留原始单位。</span>
      </p>
      <div
        id="signal-panel"
        role="tabpanel"
        aria-labelledby={`signal-tab-${activeFamily.id}`}
        className={`signal-grid${wide ? " signal-grid-wide" : ""}`}
      >
        {visible.map((channel) => (
          <SingleDataGraph
            key={`${channel.id}:${activeFamily.id}`}
            channel={channel}
            familyTitle={activeFamily.title}
            hoveredTime={hoveredTime}
            setHoveredTime={setHoveredTime}
          />
        ))}
      </div>
      {!visible.length && (
        <div className="signal-empty">
          没有匹配的通道。
          <button type="button" onClick={() => setSearch("")}>
            清除筛选
          </button>
        </div>
      )}
    </section>
  );
});

function seriesColor(series: SignalSeries) {
  return `var(--signal-${series.role})`;
}

const SingleDataGraph = React.memo(function SingleDataGraph({
  channel,
  familyTitle,
  hoveredTime,
  setHoveredTime,
}: {
  channel: SignalChannel;
  familyTitle: string;
  hoveredTime: number | null;
  setHoveredTime: (time: number | null) => void;
}) {
  const { currentTime, seek } = useTime();
  const [hidden, setHidden] = useState<string[]>([]);
  const [focused, setFocused] = useState<string | null>(null);
  const { rows, series } = channel;
  const time = hoveredTime ?? currentTime;
  const current = closestRow(rows, time);
  const command = series.find((s) => s.role === "command");
  const measured = series.find((s) => s.role === "measured");
  const delta =
    command &&
    measured &&
    Number.isFinite(current[command.key]) &&
    Number.isFinite(current[measured.key])
      ? Math.abs(current[command.key] - current[measured.key])
      : undefined;
  const channelLabel = /^\d+$/.test(channel.channel)
    ? channel.channel.padStart(2, "0")
    : channel.channel;
  const selected = series.filter((s) => !hidden.includes(s.key));

  return (
    <article
      className="signal-card"
      aria-label={`${familyTitle} · 通道 ${channelLabel}`}
    >
      <header className="signal-card-heading">
        <div>
          <span className="signal-channel-number" aria-hidden="true">CH</span>
          <h3>
            通道 {channelLabel}
            <small>{familyTitle}</small>
          </h3>
        </div>
        <span className="signal-time">
          {time.toFixed(2)} <small>s</small>
        </span>
      </header>
      <div className="signal-readouts">
        {series.map((s) => (
          <button
            type="button"
            key={s.key}
            className={`signal-readout${hidden.includes(s.key) ? " is-muted" : ""}`}
            aria-pressed={!hidden.includes(s.key)}
            title={`${s.feature}[${channel.channel}] · 点击显示或隐藏`}
            onClick={() =>
              setHidden((prev) =>
                prev.includes(s.key)
                  ? prev.filter((k) => k !== s.key)
                  : [...prev, s.key],
              )
            }
            onMouseEnter={() => setFocused(s.key)}
            onMouseLeave={() => setFocused(null)}
            onFocus={() => setFocused(s.key)}
            onBlur={() => setFocused(null)}
          >
            <span
              className={`signal-swatch${s.role === "measured" ? " is-dashed" : ""}`}
              style={{ color: seriesColor(s) }}
            />
            <span className="signal-readout-label">
              {s.label}
              <small>{s.feature}</small>
            </span>
            <strong style={{ color: seriesColor(s) }}>
              {formatValue(current[s.key])}
            </strong>
          </button>
        ))}
      </div>
      <div className="signal-plot" onMouseLeave={() => setHoveredTime(null)}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart
            data={rows}
            syncId="episode-signals"
            syncMethod="value"
            margin={{ top: 14, right: 16, left: 0, bottom: 2 }}
            onClick={(state) => {
              const timestamp = state?.activePayload?.[0]?.payload?.timestamp;
              if (typeof timestamp === "number") seek(timestamp);
            }}
            onMouseMove={(state) => {
              const timestamp = state?.activePayload?.[0]?.payload?.timestamp;
              setHoveredTime(typeof timestamp === "number" ? timestamp : null);
            }}
            onMouseLeave={() => setHoveredTime(null)}
          >
            <CartesianGrid
              stroke="var(--chart-grid)"
              vertical={false}
              strokeDasharray="3 5"
            />
            <XAxis
              type="number"
              dataKey="timestamp"
              domain={[rows[0]?.timestamp ?? 0, rows.at(-1)?.timestamp ?? 1]}
              tickFormatter={(v: number) => `${Number(v.toFixed(1))}s`}
              tick={{ fontSize: 10, fill: "var(--text-muted)" }}
              axisLine={false}
              tickLine={false}
              minTickGap={35}
            />
            <YAxis
              domain={["auto", "auto"]}
              tick={{ fontSize: 10, fill: "var(--text-muted)" }}
              axisLine={false}
              tickLine={false}
              width={60}
              tickFormatter={(v: number) =>
                Math.abs(v) >= 10000 || (v !== 0 && Math.abs(v) < 0.001)
                  ? v.toExponential(1)
                  : Number(v.toFixed(3)).toString()
              }
            />
            <Tooltip
              content={() => null}
              cursor={{
                stroke: "var(--text-muted)",
                strokeDasharray: "3 4",
                strokeWidth: 1,
              }}
              isAnimationActive={false}
            />
            <ReferenceLine
              x={currentTime}
              stroke="var(--playhead)"
              strokeWidth={1}
              strokeOpacity={0.6}
            />
            {selected.map((s) => (
              <Line
                key={s.key}
                type="linear"
                dataKey={s.key}
                stroke={seriesColor(s)}
                strokeWidth={s.role === "measured" ? 1.8 : 2.3}
                strokeDasharray={s.role === "measured" ? "6 4" : undefined}
                strokeOpacity={focused && focused !== s.key ? 0.2 : 1}
                dot={false}
                activeDot={{ r: 3, strokeWidth: 2, stroke: "var(--surface-0)" }}
                isAnimationActive={false}
                connectNulls={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
        {!selected.length && (
          <div className="signal-plot-empty">点击上方图例显示曲线</div>
        )}
      </div>
      <footer className="signal-card-footer">
        <span>{selected.length} 条曲线 · 原始采样</span>
        {delta !== undefined && (
          <span>
            当前绝对差值 <strong>{formatValue(delta)}</strong>
          </span>
        )}
      </footer>
    </article>
  );
});

export default DataRecharts;
