/** Structured instruction templates used by the Visualizer's visual editor. */

export interface TextPart {
  type: "text";
  text: string;
}

export interface VariablePart {
  type: "variable";
  key: string;
}

export type TemplatePart = TextPart | VariablePart;

export interface VariableDefinition {
  label: string;
  options: string[];
  default?: string | null;
}

export interface InstructionTemplate {
  parts: TemplatePart[];
  variables: Record<string, VariableDefinition>;
}

export interface MigrationWarning {
  kind: "variable_missing" | "option_missing";
  episode_index: number;
  variable: string;
  value: string;
  message: string;
}

export interface SavedEpisodeDraft {
  revision: number;
  source_signature: string;
  selections: Record<string, string>;
  rendered: string;
}

export function appliedInstructionOutcome(
  refreshed: boolean,
  successNotice: string,
): { notice: string; refreshRequired: boolean } {
  return refreshed
    ? { notice: successNotice, refreshRequired: false }
    : { notice: "", refreshRequired: true };
}

export function mergeSavedEpisodeDraft<
  Head extends { revision: number; source_signature: string },
  Episode extends {
    revision: number;
    source_signature: string;
    has_draft: boolean;
    selections: Record<string, string>;
    rendered: string | null;
    valid: boolean;
    error: string | null;
  },
>(
  head: Head,
  episode: Episode,
  saved: SavedEpisodeDraft,
): { head: Head; episode: Episode } {
  const version = {
    revision: saved.revision,
    source_signature: saved.source_signature,
  };
  return {
    head: { ...head, ...version },
    episode: {
      ...episode,
      ...version,
      has_draft: true,
      selections: saved.selections,
      rendered: saved.rendered,
      valid: true,
      error: null,
    },
  };
}

export function defaultSelections(template: InstructionTemplate): Record<string, string> {
  return Object.fromEntries(
    Object.entries(template.variables).map(([key, definition]) => [
      key,
      definition.default || definition.options[0] || "",
    ]),
  );
}

export function renderInstruction(
  template: InstructionTemplate,
  selections: Record<string, string> = {},
): string {
  const chosen = { ...defaultSelections(template), ...selections };
  return template.parts
    .map(part => part.type === "text" ? part.text : chosen[part.key] || `[${part.key}]`)
    .join("")
    .trim();
}

export function selectionsForTemplate(
  template: InstructionTemplate,
  selections: Record<string, string>,
): Record<string, string> {
  const next = defaultSelections(template);
  for (const key of Object.keys(template.variables)) {
    if (selections[key] !== undefined) next[key] = selections[key];
  }
  return next;
}

export function addVariable(
  template: InstructionTemplate,
  key: string,
  definition: VariableDefinition,
): InstructionTemplate {
  if (!/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(key)) throw new Error("变量名只能使用字母、数字和下划线");
  if (template.variables[key] || template.parts.some(part => part.type === "variable" && part.key === key))
    throw new Error(`变量 ${key} 已存在`);
  if (!definition.options.length || definition.options.some(option => !option.trim()))
    throw new Error("变量至少需要一个非空候选值");
  return {
    ...template,
    parts: [...template.parts, { type: "variable", key }],
    variables: { ...template.variables, [key]: definition },
  };
}

export function movePart(template: InstructionTemplate, from: number, to: number): InstructionTemplate {
  if (from === to || from < 0 || to < 0 || from >= template.parts.length || to >= template.parts.length)
    return template;
  const parts = [...template.parts];
  const [part] = parts.splice(from, 1);
  parts.splice(to, 0, part);
  return { ...template, parts };
}

export function updateText(template: InstructionTemplate, index: number, text: string): InstructionTemplate {
  const part = template.parts[index];
  if (!part || part.type !== "text") return template;
  return {
    ...template,
    parts: template.parts.map((item, i) => i === index ? { type: "text", text } : item),
  };
}

export function removePart(template: InstructionTemplate, index: number): InstructionTemplate {
  const part = template.parts[index];
  if (!part) return template;
  const parts = template.parts.filter((_, i) => i !== index);
  if (part.type === "text") return { ...template, parts };
  const stillUsed = parts.some(item => item.type === "variable" && item.key === part.key);
  if (stillUsed) return { ...template, parts };
  const variables = { ...template.variables };
  delete variables[part.key];
  return { parts, variables };
}

export function parseInstructionSource(
  source: string,
  variables: Record<string, VariableDefinition>,
): InstructionTemplate {
  const parts: TemplatePart[] = [];
  let cursor = 0;
  const found = new Set<string>();
  for (const match of source.matchAll(/\{\{([A-Za-z][A-Za-z0-9_]{0,63})\}\}/g)) {
    const start = match.index ?? 0;
    if (start > cursor) parts.push({ type: "text", text: source.slice(cursor, start) });
    parts.push({ type: "variable", key: match[1] });
    found.add(match[1]);
    cursor = start + match[0].length;
  }
  if (cursor < source.length) parts.push({ type: "text", text: source.slice(cursor) });
  if (!found.size) throw new Error("模板源文本至少需要一个 {{变量}}");
  const missing = [...found].filter(key => !variables[key]);
  if (missing.length) throw new Error(`请先配置变量：${missing.join("、")}`);
  const unused = Object.keys(variables).filter(key => !found.has(key));
  if (unused.length) throw new Error(`模板未使用变量：${unused.join("、")}`);
  return { parts, variables };
}

export function sourceText(template: InstructionTemplate): string {
  return template.parts.map(part => part.type === "text" ? part.text : `{{${part.key}}}`).join("");
}

export function migrationWarnings(
  template: InstructionTemplate,
  assignments: Record<string, Record<string, string>>,
): MigrationWarning[] {
  const warnings: MigrationWarning[] = [];
  for (const [rawEpisode, selections] of Object.entries(assignments)) {
    const episode = Number(rawEpisode);
    for (const [key, value] of Object.entries(selections)) {
      const definition = template.variables[key];
      if (!definition) {
        warnings.push({ kind: "variable_missing", episode_index: episode, variable: key, value,
          message: `Episode ${episode} 仍使用已移除变量 ${key}` });
      } else if (!definition.options.includes(value)) {
        warnings.push({ kind: "option_missing", episode_index: episode, variable: key, value,
          message: `Episode ${episode} 的变量 ${key} 仍使用已移除选项“${value}”` });
      }
    }
  }
  return warnings.sort((a, b) => a.episode_index - b.episode_index || a.variable.localeCompare(b.variable));
}
