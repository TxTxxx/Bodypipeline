"""Install the instruction-template editor into the pinned Visualizer."""

from __future__ import annotations

from pathlib import Path

MARKER = "// bodypipeline: visual instruction editor v1"


def _replace(text: str, before: str, after: str, label: str) -> str:
    if after in text:
        return text
    if text.count(before) != 1:
        raise RuntimeError(
            f"Unexpected Visualizer instruction-editor anchor ({label})"
        )
    return text.replace(before, after, 1)


def patch_instruction_editor(cache: Path):
    source = cache / "source/src"
    page = source / "app/[org]/[dataset]/[episode]/episode-viewer.tsx"
    text = page.read_text()
    if MARKER in text:
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
        + '"在原数据集中批量编辑每个 Episode 的语言指令")}',
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
    page.write_text(text)


__all__ = ["patch_instruction_editor"]
