"use client";
import { useEffect, useState } from "react";
import { useAnnotations } from "../context/annotations-context";
import { useTime } from "../context/time-context";
import { reviewRequest } from "../utils/annotation-review-client";
import {
  frameTime,
  shortcutValue,
  nearestFrame,
  type FieldDef,
  type TypeDef,
  type Scheme,
  type CustomRecord,
} from "../utils/custom-annotation-model";
const schemesUrl = "/api/annotation-schemes";
function failure(e: unknown) {
  return e instanceof Error ? e.message : String(e);
}
const buttonStyle = {
  padding: "6px 10px",
  border: "1px solid var(--border,#40516b)",
  borderRadius: 6,
  margin: 3,
};
function ValueEditor({
  field,
  value,
  onChange,
}: {
  field: FieldDef;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const common = {
    style: {
      width: "100%",
      padding: 7,
      border: "1px solid #40516b",
      borderRadius: 5,
      background: "var(--surface-1)",
      color: "inherit",
    },
    "aria-label": field.label || field.id,
    "data-annotation-key": field.key?.toLowerCase(),
  };
  if (field.dtype === "bool" && field.widget === "toggle")
    return (
      <div>
        <button
          {...common}
          type="button"
          role="switch"
          aria-checked={value === true}
          onClick={() => onChange(value === true ? false : true)}
        >
          {value == null ? "未标注" : value ? "是" : "否"}
        </button>
        <button type="button" onClick={() => onChange(undefined)}>
          清空
        </button>
      </div>
    );
  if (field.dtype === "single" && field.widget === "buttons")
    return (
      <div role="radiogroup" aria-label={field.label || field.id}>
        {field.options?.map((o) => (
          <button
            type="button"
            role="radio"
            aria-checked={value === o.value}
            key={o.value}
            style={{
              ...buttonStyle,
              background: value === o.value ? "#405f9e" : undefined,
            }}
            onClick={() => onChange(o.value)}
          >
            {o.label}
          </button>
        ))}
        <button type="button" onClick={() => onChange(undefined)}>
          清空
        </button>
      </div>
    );
  if (field.dtype === "bool")
    return (
      <select
        {...common}
        value={value === undefined ? "" : String(value)}
        onChange={(e) =>
          onChange(
            e.target.value === "" ? undefined : e.target.value === "true",
          )
        }
      >
        <option value="">未标注</option>
        <option value="true">是</option>
        <option value="false">否</option>
      </select>
    );
  if (field.dtype === "single")
    return (
      <select
        {...common}
        value={String(value ?? "")}
        onChange={(e) => onChange(e.target.value || undefined)}
      >
        <option value="">未标注</option>
        {field.options?.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    );
  if (field.dtype === "multi")
    return (
      <div>
        {field.options?.map((o) => (
          <label key={o.value} style={{ display: "inline-block", padding: 5 }}>
            <input
              type="checkbox"
              checked={Array.isArray(value) && value.includes(o.value)}
              onChange={(e) =>
                onChange(
                  e.target.checked
                    ? [...(Array.isArray(value) ? value : []), o.value]
                    : (Array.isArray(value) ? value : []).filter(
                        (v) => v !== o.value,
                      ),
                )
              }
            />
            {o.label}
          </label>
        ))}
      </div>
    );
  if (field.dtype === "text" && field.widget === "textarea")
    return (
      <textarea
        {...common}
        rows={3}
        maxLength={field.max_length}
        value={String(value ?? "")}
        onChange={(e) => onChange(e.target.value || undefined)}
      />
    );
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
      <input
        {...common}
        type={
          field.dtype === "text"
            ? "text"
            : field.widget === "slider"
              ? "range"
              : "number"
        }
        min={field.min}
        max={field.max}
        step={field.dtype === "int" ? 1 : (field.step ?? "any")}
        maxLength={field.max_length}
        value={value == null ? "" : String(value)}
        onChange={(e) =>
          onChange(
            e.target.value === ""
              ? undefined
              : field.dtype === "text"
                ? e.target.value
                : Number(e.target.value),
          )
        }
      />
      {field.widget === "slider" && (
        <output>{String(value ?? "未标注")}</output>
      )}
      {field.unit && <span>{field.unit}</span>}
    </div>
  );
}
function Fields({
  type,
  values,
  onChange,
}: {
  type: TypeDef;
  values: Record<string, unknown>;
  onChange: (values: Record<string, unknown>) => void;
}) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit,minmax(190px,1fr))",
        gap: 12,
      }}
    >
      {type.fields.map((f) => (
        <label key={f.id} style={{ display: "block" }}>
          {f.label || f.id}
          {f.required ? " *" : ""}
          {f.description && (
            <small style={{ display: "block" }}>{f.description}</small>
          )}
          <ValueEditor
            field={f}
            value={values[f.id]}
            onChange={(v) => {
              const next = { ...values };
              next[f.id] = v === undefined ? null : v;
              onChange(next);
            }}
          />
        </label>
      ))}
    </div>
  );
}
const blankField = (): FieldDef => ({
  id: "",
  label: "",
  dtype: "text",
  widget: "text",
});
const blankType = (): TypeDef => ({
  id: "",
  label: "",
  scope: "episode",
  allow_overlap: true,
  fields: [blankField()],
});
function SchemeEditor({
  onApplied,
  selected,
}: {
  onApplied: (scheme: Scheme) => Promise<void>;
  selected: Scheme | null;
}) {
  const [schemes, setSchemes] = useState<Scheme[]>([]),
    [draft, setDraft] = useState<Scheme | null>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const refresh = () =>
    reviewRequest<Scheme[]>(schemesUrl)
      .then(setSchemes)
      .catch((e) => setError(failure(e)));
  useEffect(() => {
    void refresh();
  }, []);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(failure(e));
    } finally {
      setBusy(false);
    }
  };
  const updateType = (i: number, patch: Partial<TypeDef>) =>
    setDraft((d) =>
      d
        ? {
            ...d,
            types: d.types.map((t, j) => (i === j ? { ...t, ...patch } : t)),
          }
        : d,
    );
  const updateField = (i: number, k: number, patch: Partial<FieldDef>) => {
    if (!draft) return;
    updateType(i, {
      fields: draft.types[i].fields.map((f, j) =>
        k === j ? { ...f, ...patch } : f,
      ),
    });
  };
  return (
    <section
      aria-label="标注方案管理"
      style={{ border: "1px solid #40516b", padding: 15, borderRadius: 8 }}
    >
      <h3>标注方案</h3>
      <p>
        修改会发布新版本；已使用版本保持不变。字段标识用于存储，请保持稳定。
      </p>
      {error && <p role="alert">{error}</p>}
      <button
        style={buttonStyle}
        onClick={() => setDraft({ name: "新标注方案", types: [blankType()] })}
      >
        新建方案
      </button>
      <button
        style={buttonStyle}
        onClick={() =>
          setDraft({
            name: "桌面操作示例",
            types: [
              {
                id: "outcome",
                label: "任务结果",
                scope: "episode",
                fields: [
                  {
                    id: "success",
                    label: "任务结果",
                    dtype: "single",
                    widget: "dropdown",
                    required: true,
                    options: [
                      { value: "success", label: "成功" },
                      { value: "failure", label: "失败" },
                    ],
                  },
                ],
              },
              {
                id: "contact",
                label: "接触状态",
                scope: "interval",
                allow_overlap: false,
                fields: [
                  {
                    id: "state",
                    label: "状态",
                    dtype: "single",
                    widget: "dropdown",
                    options: [
                      { value: "free", label: "未接触" },
                      { value: "holding", label: "稳定抓持" },
                    ],
                  },
                ],
              },
              {
                id: "incident",
                label: "异常事件",
                scope: "frame",
                fields: [
                  {
                    id: "note",
                    label: "备注",
                    dtype: "text",
                    widget: "textarea",
                  },
                ],
              },
            ],
          })
        }
      >
        复制示例
      </button>
      {schemes.map((s) => (
        <div
          key={`${s.id}:${s.version}`}
          style={{ padding: 8, borderBottom: "1px solid #40516b" }}
        >
          <strong>
            {s.name} v{s.version}
          </strong>
          {s.archived ? "（已停用）" : ""}
          {selected && selected.id === s.id && selected.version === s.version
            ? " · 当前方案"
            : ""}
          <button
            style={buttonStyle}
            disabled={busy || s.archived}
            onClick={() => void run(() => onApplied(s))}
          >
            应用到当前数据集
          </button>
          <button
            style={buttonStyle}
            disabled={busy || s.archived}
            onClick={() => setDraft(structuredClone(s))}
          >
            编辑并发布新版本
          </button>
          <button
            style={buttonStyle}
            disabled={busy}
            onClick={() =>
              void run(() =>
                reviewRequest(`${schemesUrl}/${s.id}/copy`, {
                  name: `${s.name} 副本`,
                }),
              )
            }
          >
            复制
          </button>
          <button
            style={buttonStyle}
            disabled={busy || s.archived}
            onClick={() => {
              if (window.confirm("停用此方案？历史标注仍然可读。"))
                void run(() =>
                  reviewRequest(`${schemesUrl}/${s.id}/archive`, {}),
                );
            }}
          >
            停用
          </button>
        </div>
      ))}
      {draft && (
        <div
          style={{ padding: 12, border: "1px solid #40516b", marginTop: 10 }}
        >
          <label>
            方案名称{" "}
            <input
              value={draft.name || ""}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            />
          </label>
          {draft.types.map((t, i) => (
            <fieldset key={i} style={{ padding: 10, marginTop: 12 }}>
              <legend>标注类型 {i + 1}</legend>
              <label>
                标识{" "}
                <input
                  value={t.id}
                  onChange={(e) => updateType(i, { id: e.target.value })}
                />
              </label>
              <label>
                名称{" "}
                <input
                  value={t.label || ""}
                  onChange={(e) => updateType(i, { label: e.target.value })}
                />
              </label>
              <label>
                范围{" "}
                <select
                  value={t.scope}
                  onChange={(e) =>
                    updateType(i, { scope: e.target.value as TypeDef["scope"] })
                  }
                >
                  <option value="episode">整个 Episode</option>
                  <option value="interval">时间区间</option>
                  <option value="frame">单帧事件</option>
                </select>
              </label>
              {t.scope === "interval" && (
                <label>
                  <input
                    type="checkbox"
                    checked={t.allow_overlap ?? true}
                    onChange={(e) =>
                      updateType(i, { allow_overlap: e.target.checked })
                    }
                  />
                  允许同类区间重叠
                </label>
              )}
              {t.scope !== "episode" && t.id !== "subtask" && (
                <label>
                  <input
                    type="checkbox"
                    checked={t.camera_bound || false}
                    onChange={(e) =>
                      updateType(i, { camera_bound: e.target.checked })
                    }
                  />
                  绑定相机
                </label>
              )}
              {t.id === "subtask" && (
                <p>
                  此类型为原有 Subtask
                  附加字段，请使用区间范围；不会新增重复轨道。
                </p>
              )}
              <button
                style={buttonStyle}
                onClick={() =>
                  setDraft({
                    ...draft,
                    types: draft.types.filter((_, j) => i !== j),
                  })
                }
              >
                删除类型
              </button>
              {t.fields.map((f, k) => (
                <fieldset key={k} style={{ padding: 9, marginTop: 8 }}>
                  <legend>字段 {k + 1}</legend>
                  <label>
                    标识{" "}
                    <input
                      value={f.id}
                      onChange={(e) =>
                        updateField(i, k, { id: e.target.value })
                      }
                    />
                  </label>
                  <label>
                    显示名称{" "}
                    <input
                      value={f.label || ""}
                      onChange={(e) =>
                        updateField(i, k, { label: e.target.value })
                      }
                    />
                  </label>
                  <label>
                    类型{" "}
                    <select
                      value={f.dtype}
                      onChange={(e) => {
                        const dtype = e.target.value as FieldDef["dtype"];
                        updateField(i, k, {
                          dtype,
                          widget:
                            dtype === "bool"
                              ? "toggle"
                              : dtype === "single"
                                ? "dropdown"
                                : dtype === "multi"
                                  ? "checkboxes"
                                  : dtype === "text"
                                    ? "text"
                                    : "number",
                          default: undefined,
                          options: ["single", "multi"].includes(dtype)
                            ? []
                            : undefined,
                        });
                      }}
                    >
                      {[
                        ["text", "文本"],
                        ["int", "整数"],
                        ["float", "小数"],
                        ["bool", "布尔"],
                        ["single", "单选"],
                        ["multi", "多选"],
                      ].map(([v, l]) => (
                        <option key={v} value={v}>
                          {l}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    控件{" "}
                    <select
                      value={f.widget}
                      onChange={(e) =>
                        updateField(i, k, { widget: e.target.value })
                      }
                    >
                      {(f.dtype === "text"
                        ? ["text", "textarea"]
                        : f.dtype === "int" || f.dtype === "float"
                          ? ["number", "slider"]
                          : f.dtype === "bool"
                            ? ["toggle"]
                            : f.dtype === "single"
                              ? ["dropdown", "buttons"]
                              : ["checkboxes"]
                      ).map((w) => (
                        <option key={w}>{w}</option>
                      ))}
                    </select>
                  </label>
                  <label>
                    <input
                      type="checkbox"
                      checked={f.required || false}
                      onChange={(e) =>
                        updateField(i, k, { required: e.target.checked })
                      }
                    />
                    必填
                  </label>
                  <label>
                    说明{" "}
                    <input
                      value={f.description || ""}
                      onChange={(e) =>
                        updateField(i, k, { description: e.target.value })
                      }
                    />
                  </label>
                  {["single", "multi"].includes(f.dtype) && (
                    <label style={{ display: "block" }}>
                      选项（每行 存储值=显示名称）
                      <textarea
                        rows={4}
                        value={(f.options || [])
                          .map((o) => `${o.value}=${o.label}`)
                          .join("\n")}
                        onChange={(e) =>
                          updateField(i, k, {
                            options: e.target.value.split("\n").map((line) => {
                              const [value, ...parts] = line.split("=");
                              return { value, label: parts.join("=") || value };
                            }),
                          })
                        }
                      />
                    </label>
                  )}
                  {(f.dtype === "int" || f.dtype === "float") &&
                    (["min", "max", "step"] as const).map((attr) => (
                      <label key={attr}>
                        {attr}{" "}
                        <input
                          type="number"
                          step="any"
                          value={f[attr] ?? ""}
                          onChange={(e) =>
                            updateField(i, k, {
                              [attr]:
                                e.target.value === ""
                                  ? undefined
                                  : Number(e.target.value),
                            })
                          }
                        />
                      </label>
                    ))}
                  {f.dtype === "text" && (
                    <label>
                      最大长度{" "}
                      <input
                        type="number"
                        min={1}
                        value={f.max_length ?? ""}
                        onChange={(e) =>
                          updateField(i, k, {
                            max_length:
                              e.target.value === ""
                                ? undefined
                                : Number(e.target.value),
                          })
                        }
                      />
                    </label>
                  )}
                  <label>
                    单位{" "}
                    <input
                      value={f.unit || ""}
                      onChange={(e) =>
                        updateField(i, k, { unit: e.target.value })
                      }
                    />
                  </label>
                  <label>
                    快捷键{" "}
                    <input
                      maxLength={1}
                      value={f.key || ""}
                      onChange={(e) =>
                        updateField(i, k, { key: e.target.value || undefined })
                      }
                    />
                  </label>
                  <label style={{ display: "block" }}>
                    默认值（可留空）
                    <ValueEditor
                      field={f}
                      value={f.default}
                      onChange={(v) => updateField(i, k, { default: v })}
                    />
                  </label>
                  <button
                    style={buttonStyle}
                    onClick={() =>
                      updateType(i, {
                        fields: t.fields.filter((_, j) => k !== j),
                      })
                    }
                  >
                    删除字段
                  </button>
                </fieldset>
              ))}
              <button
                style={buttonStyle}
                onClick={() =>
                  updateType(i, { fields: [...t.fields, blankField()] })
                }
              >
                添加字段
              </button>
            </fieldset>
          ))}
          <button
            style={buttonStyle}
            onClick={() =>
              setDraft({ ...draft, types: [...draft.types, blankType()] })
            }
          >
            添加标注类型
          </button>
          <button
            style={buttonStyle}
            disabled={busy}
            onClick={() =>
              void run(async () => {
                const { id, version } = draft;
                await reviewRequest(
                  id ? `${schemesUrl}/${id}/versions` : schemesUrl,
                  {
                    name: draft.name,
                    types: draft.types,
                    ...(id ? { revision: version } : {}),
                  },
                );
                setDraft(null);
              })
            }
          >
            保存并发布
          </button>
          <button style={buttonStyle} onClick={() => setDraft(null)}>
            取消
          </button>
        </div>
      )}
    </section>
  );
}
export function CustomAnnotations({
  cameraKeys = [],
}: {
  cameraKeys?: string[];
}) {
  const { customReview: bridge, atoms, updateAtom } = useAnnotations();
  const { currentTime, seek } = useTime();
  const [manager, setManager] = useState(false),
    [selected, setSelected] = useState<string | null>(null),
    [hidden, setHidden] = useState<Record<string, boolean>>({}),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false),
    [drafts, setDrafts] = useState(false);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (
        e.ctrlKey ||
        e.metaKey ||
        e.altKey ||
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLTextAreaElement ||
        e.target instanceof HTMLSelectElement ||
        (e.target instanceof HTMLElement && e.target.isContentEditable)
      )
        return;
      const scheme = bridge.review?.scheme;
      if (!bridge.ready || manager) return;
      const record = bridge.custom.records.find((r) => r.id === selected);
      for (const type of scheme?.types || []) {
        if (type.scope !== "episode" && type.id !== record?.type_id) continue;
        const field = type.fields.find(
          (f) => f.key?.toLowerCase() === e.key.toLowerCase(),
        );
        if (!field) continue;
        const previous =
          type.scope === "episode"
            ? bridge.custom.values[type.id]?.[field.id]
            : record?.values[field.id];
        const value = shortcutValue(field, previous);
        if (value !== undefined) {
          e.preventDefault();
          bridge.setCustom((old) =>
            type.scope === "episode"
              ? {
                  ...old,
                  values: {
                    ...old.values,
                    [type.id]: { ...old.values[type.id], [field.id]: value },
                  },
                }
              : {
                  ...old,
                  records: old.records.map((r) =>
                    r.id === record?.id
                      ? { ...r, values: { ...r.values, [field.id]: value } }
                      : r,
                  ),
                },
          );
          return;
        }
      }
      const target = document.querySelector<HTMLElement>(
        `[data-annotation-key="${CSS.escape(e.key.toLowerCase())}"]`,
      );
      if (target) {
        e.preventDefault();
        target.focus();
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [bridge, selected, manager]);
  if (!bridge.review) return null;
  const review = bridge.review,
    custom = bridge.custom,
    scheme = review.scheme;
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setNotice("");
    try {
      await fn();
    } catch (e) {
      setNotice(failure(e));
    } finally {
      setBusy(false);
    }
  };
  const changeRecord = (id: string, patch: Partial<CustomRecord>) =>
    bridge.setCustom((old) => ({
      ...old,
      records: old.records.map((r) => (r.id === id ? { ...r, ...patch } : r)),
    }));
  const add = (type: TypeDef) => {
    const frame = nearestFrame(review, currentTime),
      id = crypto.randomUUID(),
      values: Record<string, unknown> = {};
    for (const f of type.fields)
      if (f.default !== undefined) values[f.id] = f.default;
    bridge.setCustom((old) => ({
      ...old,
      records: [
        ...old.records,
        {
          id,
          type_id: type.id,
          values,
          ...(type.camera_bound ? { camera: cameraKeys[0] || null } : {}),
          ...(type.scope === "frame"
            ? { frame_index: frame }
            : {
                start_frame: frame,
                end_frame: Math.min(review.frames, frame + 1),
              }),
        },
      ],
    }));
    setSelected(id);
  };
  const selectedRecord = custom.records.find((r) => r.id === selected),
    selectedType = scheme?.types.find((t) => t.id === selectedRecord?.type_id);
  return (
    <section
      style={{
        border: "1px solid #40516b",
        borderRadius: 10,
        padding: 16,
        margin: "14px 0",
      }}
      aria-label="自定义标注"
    >
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: 8,
        }}
      >
        <h3 style={{ margin: 0 }}>自定义标注</h3>
        <span>
          {scheme ? `${scheme.name} v${scheme.version}` : "尚未应用方案"}
        </span>
        <span>
          {custom.status === "completed"
            ? "已完成"
            : custom.status === "not_started"
              ? "未开始"
              : "草稿"}
          {bridge.dirty ? " · 有未保存修改" : ""}
        </span>
        <button style={buttonStyle} onClick={() => setManager(!manager)}>
          标注方案
        </button>
        <button
          style={buttonStyle}
          disabled={busy || bridge.saving}
          onClick={() =>
            void run(async () => {
              const r = await bridge.save("completed");
              if (!r.ok) throw new Error(r.error);
              setNotice("已完成并保存");
            })
          }
        >
          标记完成
        </button>
        <button
          style={buttonStyle}
          disabled={!bridge.dirty || bridge.saving}
          onClick={bridge.undo}
        >
          撤销未保存修改
        </button>
      </div>
      {notice && <p role="status">{notice}</p>}
      {manager && (
        <SchemeEditor
          selected={scheme}
          onApplied={async (s) => {
            if (s.id && s.version) await bridge.bind(s.id, s.version);
          }}
        />
      )}
      {scheme?.types
        .filter((t) => t.scope === "episode")
        .map((t) => (
          <fieldset key={t.id} style={{ marginTop: 12, padding: 12 }}>
            <legend>{t.label || t.id}</legend>
            <Fields
              type={t}
              values={custom.values[t.id] || {}}
              onChange={(values) =>
                bridge.setCustom((old) => ({
                  ...old,
                  values: { ...old.values, [t.id]: values },
                }))
              }
            />
          </fieldset>
        ))}
      {scheme?.types
        .filter((t) => t.scope !== "episode" && t.id !== "subtask")
        .map((t) => (
          <div key={t.id} style={{ marginTop: 12 }}>
            <label>
              <input
                type="checkbox"
                checked={!hidden[t.id]}
                onChange={(e) =>
                  setHidden({ ...hidden, [t.id]: !e.target.checked })
                }
              />
              {t.label || t.id}
            </label>
            <button
              style={buttonStyle}
              disabled={busy || bridge.loading}
              onClick={() => add(t)}
            >
              在当前帧添加{t.scope === "interval" ? "区间" : "事件"}
            </button>
            {!hidden[t.id] && (
              <div
                style={{
                  height: 28,
                  position: "relative",
                  border: "1px solid #40516b",
                  borderRadius: 5,
                }}
              >
                {custom.records
                  .filter((r) => r.type_id === t.id)
                  .map((r) => {
                    const a = r.frame_index ?? r.start_frame ?? 0,
                      b = r.end_frame ?? a;
                    return (
                      <button
                        key={r.id}
                        title={`${t.label || t.id} · 帧 ${a}${r.end_frame !== undefined ? `–${b}（不含）` : ""}`}
                        aria-label={`选择 ${t.label || t.id} ${a}`}
                        onClick={() => {
                          setSelected(r.id);
                          seek(frameTime(review, a), "external");
                        }}
                        style={{
                          position: "absolute",
                          height: 24,
                          left: `${(100 * frameTime(review, a)) / frameTime(review, review.frames)}%`,
                          width:
                            r.end_frame !== undefined
                              ? `${Math.max(0.5, (100 * (frameTime(review, b) - frameTime(review, a))) / frameTime(review, review.frames))}%`
                              : 8,
                          border: "1px solid #81a7ff",
                          background: selected === r.id ? "#bd85db" : "#466aa6",
                          borderRadius: 3,
                        }}
                      />
                    );
                  })}
              </div>
            )}
          </div>
        ))}
      {selectedRecord && selectedType && (
        <fieldset style={{ padding: 12, marginTop: 12 }}>
          <legend>{selectedType.label || selectedType.id}</legend>
          {(selectedType.scope === "frame"
            ? ["frame_index"]
            : ["start_frame", "end_frame"]
          ).map((key) => (
            <label key={key}>
              {key === "start_frame"
                ? "开始帧"
                : key === "end_frame"
                  ? "结束帧（不含）"
                  : "帧"}{" "}
              <input
                type="number"
                min={0}
                max={key === "end_frame" ? review.frames : review.frames - 1}
                step={1}
                value={
                  ((selectedRecord as unknown as Record<string, unknown>)[
                    key
                  ] as number) ?? 0
                }
                onChange={(e) =>
                  changeRecord(selectedRecord.id, {
                    [key]: Number(e.target.value),
                  })
                }
              />
            </label>
          ))}
          {selectedType.camera_bound && (
            <label>
              相机
              <select
                value={selectedRecord.camera || ""}
                onChange={(e) =>
                  changeRecord(selectedRecord.id, {
                    camera: e.target.value || null,
                  })
                }
              >
                <option value="">选择相机</option>
                {cameraKeys.map((k) => (
                  <option key={k}>{k}</option>
                ))}
              </select>
            </label>
          )}
          {selectedType.scope === "interval" && (
            <label style={{ display: "block" }}>
              移动整个区间
              <input
                type="range"
                min={0}
                max={
                  review.frames -
                  ((selectedRecord.end_frame ?? 1) -
                    (selectedRecord.start_frame ?? 0))
                }
                step={1}
                value={selectedRecord.start_frame ?? 0}
                onChange={(e) => {
                  const start = Number(e.target.value);
                  changeRecord(selectedRecord.id, {
                    start_frame: start,
                    end_frame:
                      start +
                      (selectedRecord.end_frame ?? 1) -
                      (selectedRecord.start_frame ?? 0),
                  });
                }}
              />
            </label>
          )}
          <Fields
            type={selectedType}
            values={selectedRecord.values}
            onChange={(values) => changeRecord(selectedRecord.id, { values })}
          />
          <button
            style={buttonStyle}
            onClick={() => {
              bridge.setCustom((old) => ({
                ...old,
                records: old.records.filter((r) => r.id !== selectedRecord.id),
              }));
              setSelected(null);
            }}
          >
            删除此标注
          </button>
        </fieldset>
      )}
      {scheme?.types
        .filter((t) => t.id === "subtask")
        .map((t) => (
          <fieldset key={t.id} style={{ padding: 12, marginTop: 12 }}>
            <legend>{t.label || "Subtask 附加字段"}</legend>
            {atoms.map((a, i) =>
              a.style === "subtask" ? (
                <div key={a.annotation_id || i} style={{ padding: 8 }}>
                  <strong>
                    {a.content} · {a.timestamp.toFixed(3)}s
                  </strong>
                  <Fields
                    type={t}
                    values={a.annotation_values || {}}
                    onChange={(values) =>
                      updateAtom(i, { annotation_values: values })
                    }
                  />
                </div>
              ) : null,
            )}
          </fieldset>
        ))}
      <div style={{ marginTop: 16 }}>
        <label>
          <input
            type="checkbox"
            checked={drafts}
            onChange={(e) => setDrafts(e.target.checked)}
          />
          导出包含草稿（默认仅已完成）
        </label>
        <button
          style={buttonStyle}
          disabled={busy || bridge.dirty || bridge.saving}
          onClick={() => void run(() => bridge.exportPackage(drafts))}
        >
          导出标注包
        </button>
        <small>标注包不包含源视频，需配套指定源数据版本。</small>
      </div>
    </section>
  );
}
