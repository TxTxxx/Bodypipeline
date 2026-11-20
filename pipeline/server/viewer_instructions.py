"""Install the instruction-template editor into the pinned Visualizer."""

from __future__ import annotations

import re
from pathlib import Path

MARKER = "// bodypipeline: visual instruction editor v1"
INLINE_MARKER = "// bodypipeline: inline instruction editor v2"
NAVIGATION_IMPORT = (
    "import {\n"
    "  initialEpisodePage,\n"
    "  pageForEpisode,\n"
    '} from "@/utils/episode-navigation-state";\n'
)
INSTRUCTION_IMPORT = (
    'import { InstructionEditor } from "@/components/instruction-editor";'
)
INLINE_INSTRUCTION_IMPORT = (
    'import { InlineInstructionEditor } '
    'from "@/components/instruction-episode-editor";'
)
LANGUAGE_INSTRUCTION_BLOCK = re.compile(
    r"(?P<indent>[ \t]*)\{/\* Language Instruction \*/\}\s*"
    r"\{task && \(\s*"
    r'<div className="viewer-instruction panel p-4">.*?'
    r"\n[ \t]*\)\}",
    re.DOTALL,
)


def _deduplicate_navigation_import(text: str) -> str:
    first = text.find(NAVIGATION_IMPORT)
    if first < 0:
        return text
    end = first + len(NAVIGATION_IMPORT)
    return text[:end] + text[end:].replace(NAVIGATION_IMPORT, "")


def _replace(text: str, before: str, after: str, label: str) -> str:
    if after in text:
        return text
    if text.count(before) != 1:
        raise RuntimeError(
            f"Unexpected Visualizer instruction-editor anchor ({label})"
        )
    return text.replace(before, after, 1)


def _patch_inline_episode_editor(text: str) -> str:
    if INLINE_MARKER not in text:
        if INSTRUCTION_IMPORT not in text:
            raise RuntimeError(
                "Unexpected Visualizer inline instruction-editor anchor (import)"
            )
        text = text.replace(
            INSTRUCTION_IMPORT,
            INSTRUCTION_IMPORT
            + "\n"
            + INLINE_MARKER
            + "\n"
            + INLINE_INSTRUCTION_IMPORT,
            1,
        )
        if "{/* Language Instruction */}" in text:

            def replacement(match: re.Match[str]) -> str:
                indent = match.group("indent")
                return (
                    f"{indent}{{/* Language Instruction */}}\n"
                    f"{indent}<InlineInstructionEditor\n"
                    f"{indent}  repoId={{datasetInfo.repoId}}\n"
                    f"{indent}  episodeId={{episodeId}}\n"
                    f"{indent}  currentInstruction={{task}}\n"
                    f"{indent}/>"
                )

            text, count = LANGUAGE_INSTRUCTION_BLOCK.subn(
                replacement, text, count=1
            )
            if count != 1:
                raise RuntimeError(
                    "Unexpected Visualizer inline instruction-editor anchor "
                    "(episode card)"
                )
    return re.sub(
        r'<InstructionEditor\b[^>]*?repoId=\{datasetInfo\.repoId\}[^>]*?/>',
        "<InstructionEditor repoId={datasetInfo.repoId} />",
        text,
        count=1,
    )


def patch_instruction_editor(cache: Path):
    source = cache / "source/src"
    page = source / "app/[org]/[dataset]/[episode]/episode-viewer.tsx"
    text = page.read_text()
    text = _deduplicate_navigation_import(text)
    if MARKER in text:
        text = _patch_inline_episode_editor(text)
        page.write_text(text)
        return

    text = _replace(
        text,
        'import { AnnotationsPanel } from "@/components/annotations-panel";',
        'import { AnnotationsPanel } from "@/components/annotations-panel";\n'
        + MARKER
        + '\nimport { InstructionEditor } from "@/components/instruction-editor";',
        "component import",
    )
    text = _replace(
        text,
        'type ActiveTab =\n  | "episodes"\n  | "annotations"',
        'type ActiveTab =\n  | "episodes"\n  | "annotations"\n  | "instructions"',
        "tab type",
    )
    restored = '"annotations",'
    if restored not in text:
        raise RuntimeError(
            "Unexpected Visualizer instruction-editor anchor (restored tab list)"
        )
    text = text.replace(
        restored,
        restored + '\n          "instructions",',
        1,
    )
    episode_tab = next(
        (
            anchor
            for anchor in (
                '{renderTab("episodes", "Episodes")}',
                '{renderTab("episodes", "Episode")}',
            )
            if anchor in text
        ),
        None,
    )
    if episode_tab is None:
        raise RuntimeError(
            "Unexpected Visualizer instruction-editor anchor (tab button)"
        )
    text = text.replace(
        episode_tab,
        episode_tab
        + "\n"
        + '        {renderTab("instructions", "语言指令", '
        + '"编辑数据集共享的语言模板；具体 Episode 指令在 Episode 页面修改")}',
        1,
    )
    sidebar_variants = [
        (
            '(activeTab === "episodes" ||\n'
            '  activeTab === "annotations" ||\n'
            '  activeTab === "urdf")',
            '(activeTab === "episodes" ||\n'
            '  activeTab === "annotations" ||\n'
            '  activeTab === "instructions" ||\n'
            '  activeTab === "urdf")',
        ),
        (
            'activeTab === "annotations" ||\n'
            '          activeTab === "urdf")',
            'activeTab === "annotations" ||\n'
            '          activeTab === "instructions" ||\n'
            '          activeTab === "urdf")',
        ),
    ]
    for before, after in sidebar_variants:
        if after in text:
            break
        if text.count(before) == 1:
            text = text.replace(before, after, 1)
            break
    else:
        raise RuntimeError(
            "Unexpected Visualizer instruction-editor anchor (sidebar)"
        )
    if ': activeTab === "annotations"\n' in text:
        text = text.replace(
            ': activeTab === "annotations"\n',
            ': (activeTab === "annotations" || activeTab === "instructions")\n',
            1,
        )
    elif 'activeTab === "annotations"\n  ?' in text:
        text = text.replace(
            'activeTab === "annotations"\n  ?',
            '(activeTab === "annotations" || activeTab === "instructions")\n  ?',
            1,
        )
    else:
        raise RuntimeError(
            "Unexpected Visualizer instruction-editor anchor (episode navigation)"
        )
    text = _replace(
        text,
        '{activeTab === "episodes" && (',
        '{activeTab === "instructions" && (\n'
        "            <InstructionEditor\n"
        '              key={`${datasetInfo.repoId}:${episodeId}`}\n'
        "              repoId={datasetInfo.repoId}\n"
        "              episodeId={episodeId}\n"
        "            />\n"
        "          )}\n\n"
        '{activeTab === "episodes" && (',
        "instruction panel",
    )
    text = _patch_inline_episode_editor(text)
    page.write_text(text)


__all__ = ["patch_instruction_editor"]
