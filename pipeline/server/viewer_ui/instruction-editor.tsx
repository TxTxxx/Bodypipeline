"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  addVariable,
  defaultSelections,
  movePart,
  parseInstructionSource,
  removePart,
  renderInstruction,
  sourceText,
  updateText,
  type InstructionTemplate,
  type VariableDefinition,
} from "@/utils/instruction-model";

interface EpisodeSummary {
  episode_index: number;
  frames: number;
  current_instruction: string | null;
  assigned: boolean;
}

interface InstructionHead {
  dataset_id: string;
  dataset_path: string;
  fps: number;
  revision: number;
  source_signature: string;
  template: InstructionTemplate;
  episodes: EpisodeSummary[];
  assignment_count: number;
  warnings: Array<{
    episode_index: number;
    variable: string;
    value: string;
    message: string;
  }>;
  writable: boolean;
}

interface Props {
  repoId: string;
}

async function request<T>(
  url: string,
  method: "GET" | "PUT" | "POST" = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch(url, {
    method,
    credentials: "same-origin",
    cache: "no-store",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(
      typeof payload.detail === "string"
        ? payload.detail
        : `请求失败 (${response.status})`,
    );
  }
  return payload as T;
}

function CloneButton({
  children,
  onClick,
  disabled,
  title,
  primary,
  danger,
}: {
  children: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  title?: string;
  primary?: boolean;
  danger?: boolean;
}) {
  return (
    <button
      type="button"
      className={`instruction-button${primary ? " primary" : ""}${danger ? " danger" : ""}`}
      onClick={onClick}
      disabled={disabled}
      title={title}
    >
      {children}
    </button>
  );
}

export function InstructionEditor({ repoId }: Props) {
  const datasetId = repoId.startsWith("bp/") ? repoId.slice(3) : "";
  const base = `/api/instructions/${encodeURIComponent(datasetId)}`;
  const [head, setHead] = useState<InstructionHead | null>(null);
  const [template, setTemplate] = useState<InstructionTemplate | null>(null);
  const [selectedVariable, setSelectedVariable] = useState<string | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const [sourceDraft, setSourceDraft] = useState("");
  const [newVariable, setNewVariable] = useState({
    key: "",
    label: "",
    options: "",
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [savedTemplate, setSavedTemplate] = useState("");

  const load = useCallback(async () => {
    if (!datasetId) return;
    setLoading(true);
    setError("");
    try {
      const nextHead = await request<InstructionHead>(base);
      setHead(nextHead);
      setTemplate(nextHead.template);
      setSelectedVariable(
        nextHead.template.parts.find((part) => part.type === "variable")?.key ?? null,
      );
      setSourceDraft(sourceText(nextHead.template));
      setSavedTemplate(JSON.stringify(nextHead.template));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setLoading(false);
    }
  }, [base, datasetId]);

  useEffect(() => {
    void load();
  }, [load]);

  const preview = useMemo(
    () =>
      template
        ? renderInstruction(template, defaultSelections(template))
        : "",
    [template],
  );
  const templateDirty = !!template && JSON.stringify(template) !== savedTemplate;
  const selectedDefinition =
    template && selectedVariable ? template.variables[selectedVariable] : null;

  const updateVariable = (
    key: string,
    update: (definition: VariableDefinition) => VariableDefinition,
  ) => {
    if (!template) return;
    const current = template.variables[key];
    if (!current) return;
    setTemplate({
      ...template,
      variables: {
        ...template.variables,
        [key]: update(current),
      },
    });
  };

  const saveTemplate = async () => {
    if (!head || !template) return null;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const result = await request<{
        revision: number;
        source_signature: string;
        template: InstructionTemplate;
        warnings: InstructionHead["warnings"];
      }>(`${base}/template`, "PUT", {
        revision: head.revision,
        source_signature: head.source_signature,
        template,
      });
      setHead({
        ...head,
        revision: result.revision,
        source_signature: result.source_signature,
        template: result.template,
        warnings: result.warnings,
        writable: result.warnings.length === 0,
      });
      setTemplate(result.template);
      setSavedTemplate(JSON.stringify(result.template));
      setSourceDraft(sourceText(result.template));
      setNotice(`模板草稿已保存为 v${result.revision}`);
      return result;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      return null;
    } finally {
      setSaving(false);
    }
  };

  if (!repoId.startsWith("bp/")) {
    return (
      <div className="instruction-editor-empty">
        语言指令编辑仅适用于 Server 管理的 LeRobot 数据集。
      </div>
    );
  }
  if (loading) {
    return <div className="instruction-editor-empty">正在加载语言模板…</div>;
  }
  if (error && !head) {
    return (
      <div className="instruction-editor-empty error">
        {error}
        <CloneButton onClick={() => void load()}>重新加载</CloneButton>
      </div>
    );
  }
  if (!head || !template) return null;

  return (
    <section className="instruction-editor">
      <header className="instruction-editor-header">
        <div>
          <p className="instruction-eyebrow">DATASET LANGUAGE</p>
          <h1>Language Template Studio</h1>
          <p>
            共 {head.episodes.length} 个 Episode · 模板草稿 v{head.revision} ·
            Episode 指令请在 Episode 页面编辑
          </p>
        </div>
        <div className="instruction-actions">
          <CloneButton
            onClick={() => void saveTemplate()}
            disabled={saving || !templateDirty}
          >
            {templateDirty ? "保存模板草稿" : "模板已保存"}
          </CloneButton>
        </div>
      </header>

      {error && (
        <div className="instruction-message error" role="alert">
          {error}
        </div>
      )}
      {notice && <div className="instruction-message success">{notice}</div>}
      {head.warnings.length > 0 && (
        <div className="instruction-message warning">
          {head.warnings.map((warning) => (
            <div key={`${warning.episode_index}:${warning.variable}`}>
              {warning.message}
            </div>
          ))}
        </div>
      )}

      <div className="instruction-studio-grid">
        <main className="instruction-canvas">
          <div className="instruction-section-heading">
            <div>
              <span>模板画布</span>
              <small>文本与变量可自由排序</small>
            </div>
            <div className="instruction-actions">
              <CloneButton
                onClick={() =>
                  setTemplate({
                    ...template,
                    parts: [...template.parts, { type: "text", text: "新文本" }],
                  })
                }
              >
                + 文本
              </CloneButton>
              <CloneButton
                onClick={() => {
                  if (!newVariable.key || !newVariable.label || !newVariable.options.trim()) {
                    setError("请填写变量名、显示名称和候选值");
                    return;
                  }
                  try {
                    const options = newVariable.options
                      .split(/\n|,/)
                      .map((option) => option.trim())
                      .filter(Boolean);
                    const next = addVariable(template, newVariable.key.trim(), {
                      label: newVariable.label.trim(),
                      options,
                      default: options[0],
                    });
                    setTemplate(next);
                    setSelectedVariable(newVariable.key.trim());
                    setNewVariable({ key: "", label: "", options: "" });
                    setError("");
                  } catch (caught) {
                    setError(caught instanceof Error ? caught.message : String(caught));
                  }
                }}
              >
                + 变量
              </CloneButton>
              <CloneButton
                onClick={() => {
                  if (!advanced) setSourceDraft(sourceText(template));
                  setAdvanced((value) => !value);
                }}
              >
                {advanced ? "返回可视编辑" : "高级源文本"}
              </CloneButton>
            </div>
          </div>

          <div className="instruction-new-variable">
            <input
              value={newVariable.key}
              onChange={(event) =>
                setNewVariable({ ...newVariable, key: event.target.value })
              }
              placeholder="变量名，如 object"
              aria-label="新变量名"
            />
            <input
              value={newVariable.label}
              onChange={(event) =>
                setNewVariable({ ...newVariable, label: event.target.value })
              }
              placeholder="显示名称，如 物体"
              aria-label="新变量显示名称"
            />
            <input
              value={newVariable.options}
              onChange={(event) =>
                setNewVariable({ ...newVariable, options: event.target.value })
              }
              placeholder="候选值，用逗号分隔"
              aria-label="新变量候选值"
            />
          </div>

          {advanced ? (
            <div className="instruction-source-editor">
              <p>使用 {"{{变量名}}"} 标记变量；候选值仍由右侧检查器配置。</p>
              <textarea
                value={sourceDraft}
                onChange={(event) => setSourceDraft(event.target.value)}
              />
              <CloneButton
                primary
                onClick={() => {
                  try {
                    setTemplate(
                      parseInstructionSource(sourceDraft, template.variables),
                    );
                    setError("");
                    setAdvanced(false);
                  } catch (caught) {
                    setError(
                      caught instanceof Error ? caught.message : String(caught),
                    );
                  }
                }}
              >
                解析为可视模板
              </CloneButton>
            </div>
          ) : (
            <div className="instruction-parts">
              {template.parts.map((part, index) =>
                part.type === "text" ? (
                  <div className="instruction-text-part" key={`text:${index}`}>
                    <textarea
                      value={part.text}
                      onChange={(event) =>
                        setTemplate(updateText(template, index, event.target.value))
                      }
                      aria-label={`文本片段 ${index + 1}`}
                    />
                    <div className="instruction-part-tools">
                      <button
                        onClick={() => setTemplate(movePart(template, index, index - 1))}
                        disabled={index === 0}
                        title="前移"
                      >
                        ←
                      </button>
                      <button
                        onClick={() => setTemplate(movePart(template, index, index + 1))}
                        disabled={index === template.parts.length - 1}
                        title="后移"
                      >
                        →
                      </button>
                      <button
                        onClick={() => setTemplate(removePart(template, index))}
                        title="移除文本片段"
                      >
                        ×
                      </button>
                    </div>
                  </div>
                ) : (
                  <div
                    className={`instruction-variable-chip${selectedVariable === part.key ? " selected" : ""}`}
                    key={`variable:${part.key}:${index}`}
                  >
                    <button
                      className="instruction-variable-main"
                      onClick={() => setSelectedVariable(part.key)}
                    >
                      <small>{template.variables[part.key]?.label || "未定义"}</small>
                      <strong>
                        {template.variables[part.key]?.default ||
                          template.variables[part.key]?.options?.[0] ||
                          part.key}
                      </strong>
                    </button>
                    <div className="instruction-part-tools">
                      <button
                        onClick={() => setTemplate(movePart(template, index, index - 1))}
                        disabled={index === 0}
                        title="前移"
                      >
                        ←
                      </button>
                      <button
                        onClick={() => setTemplate(movePart(template, index, index + 1))}
                        disabled={index === template.parts.length - 1}
                        title="后移"
                      >
                        →
                      </button>
                      <button
                        onClick={() => setTemplate(removePart(template, index))}
                        title="移除变量"
                      >
                        ×
                      </button>
                    </div>
                  </div>
                ),
              )}
            </div>
          )}

          <div className="instruction-preview">
            <span>实时预览</span>
            <p>{preview || "模板尚未生成有效指令"}</p>
            <small>{preview.length} / 2000</small>
          </div>
        </main>

        <aside className="instruction-inspector">
          <p className="instruction-eyebrow">VARIABLE INSPECTOR</p>
          {selectedDefinition && selectedVariable ? (
            <>
              <label>
                变量名
                <input value={selectedVariable} readOnly />
              </label>
              <label>
                显示名称
                <input
                  value={selectedDefinition.label}
                  onChange={(event) =>
                    updateVariable(selectedVariable, (definition) => ({
                      ...definition,
                      label: event.target.value,
                    }))
                  }
                />
              </label>
              <label>
                候选值，每行一个
                <textarea
                  value={selectedDefinition.options.join("\n")}
                  onChange={(event) => {
                    const options = event.target.value.split("\n");
                    updateVariable(selectedVariable, (definition) => ({
                      ...definition,
                      options,
                      default: options.includes(definition.default || "")
                        ? definition.default
                        : options[0] || null,
                    }));
                  }}
                />
              </label>
              <label>
                默认值
                <select
                  value={selectedDefinition.default || selectedDefinition.options[0] || ""}
                  onChange={(event) =>
                    updateVariable(selectedVariable, (definition) => ({
                      ...definition,
                      default: event.target.value,
                    }))
                  }
                >
                  {selectedDefinition.options.map((option, index) => (
                    <option key={`${option}:${index}`} value={option}>
                      {option || `（空候选值 ${index + 1}）`}
                    </option>
                  ))}
                </select>
              </label>
              <CloneButton
                danger
                onClick={() => {
                  const index = template.parts.findIndex(
                    (part) => part.type === "variable" && part.key === selectedVariable,
                  );
                  if (index >= 0) setTemplate(removePart(template, index));
                  setSelectedVariable(null);
                }}
              >
                从模板移除变量
              </CloneButton>
            </>
          ) : (
            <p className="instruction-inspector-empty">
              选择一个变量标签以编辑显示名称、候选值和默认值。
            </p>
          )}
        </aside>
      </div>

    </section>
  );
}
