"""Pinned Visualizer receives a dedicated, repeatable Instruction tab."""

from pathlib import Path

from pipeline.server.viewer_instructions import patch_instruction_editor


def test_instruction_editor_patch_is_idempotent(tmp_path):
    page = Path(
        tmp_path
        / "source/src/app/[org]/[dataset]/[episode]/episode-viewer.tsx"
    )
    page.parent.mkdir(parents=True)
    page.write_text(
        '''"use client";
import { AnnotationsPanel } from "@/components/annotations-panel";
type ActiveTab =
  | "episodes"
  | "annotations"
  | "urdf";
const stored = ["episodes", "annotations", "urdf"];
{renderTab("episodes", "Episodes")}
{renderTab("annotations", "Annotations")}
{(activeTab === "episodes" ||
  activeTab === "annotations" ||
  activeTab === "urdf") && <Sidebar />}
activeTab === "annotations"
  ? (ep) => router.push(`./episode_${ep}`)
  : undefined
{activeTab === "episodes" && (
  <Episodes />
)}
'''
    )
    patch_instruction_editor(tmp_path)
    first = page.read_text()
    patch_instruction_editor(tmp_path)
    assert page.read_text() == first
    assert first.count('import { InstructionEditor }') == 1
    assert '"instructions"' in first
    assert 'renderTab("instructions"' in first
    assert first.count("<InstructionEditor") == 1


def test_instruction_editor_patch_accepts_localized_current_viewer(tmp_path):
    page = Path(
        tmp_path
        / "source/src/app/[org]/[dataset]/[episode]/episode-viewer.tsx"
    )
    page.parent.mkdir(parents=True)
    page.write_text(
        '''"use client";
import { AnnotationsPanel } from "@/components/annotations-panel";
type ActiveTab =
  | "episodes"
  | "annotations"
  | "urdf";
const stored = ["episodes", "annotations", "urdf"];
{renderTab("episodes", "Episode")}
{renderTab("annotations", "标注")}
{(activeTab === "episodes" ||
  activeTab === "annotations" ||
  activeTab === "urdf") && <Sidebar />}
activeTab === "annotations"
  ? (ep) => router.push(`./episode_${ep}`)
  : undefined
{activeTab === "episodes" && (
  <Episodes />
)}
'''
    )
    patch_instruction_editor(tmp_path)
    text = page.read_text()
    assert 'renderTab("instructions", "语言指令"' in text
    assert text.count("<InstructionEditor") == 1


def test_patch_upgrades_existing_instruction_editor_to_inline_episode_edit(
    tmp_path,
):
    page = Path(
        tmp_path
        / "source/src/app/[org]/[dataset]/[episode]/episode-viewer.tsx"
    )
    page.parent.mkdir(parents=True)
    page.write_text(
        '''"use client";
import { AnnotationsPanel } from "@/components/annotations-panel";
// bodypipeline: visual instruction editor v1
import { InstructionEditor } from "@/components/instruction-editor";
type ActiveTab =
  | "episodes"
  | "annotations"
  | "instructions"
  | "urdf";
const stored = ["episodes", "annotations", "instructions", "urdf"];
{renderTab("episodes", "Episode")}
{renderTab("instructions", "语言指令", "edit")}
{activeTab === "instructions" && (
  <InstructionEditor repoId={datasetInfo.repoId} episodeId={episodeId} />
)}
{activeTab === "episodes" && (
  <>
    <ManualTrimPanel />
    {/* Language Instruction */}
    {task && (
      <div className="viewer-instruction panel p-4">
        <p className="text-[10px] uppercase tracking-wide text-slate-500">
          语言指令
        </p>
        <div className="mt-1.5 space-y-0.5 text-sm text-slate-200">
          {task}
        </div>
      </div>
    )}
  </>
)}
'''
    )

    patch_instruction_editor(tmp_path)
    first = page.read_text()
    patch_instruction_editor(tmp_path)
    assert page.read_text() == first
    assert first.count("import { InstructionEditor }") == 1
    assert first.count("import { InlineInstructionEditor }") == 1
    assert first.count("<InstructionEditor") == 1
    assert first.count("<InlineInstructionEditor") == 1
    assert "<InstructionEditor repoId={datasetInfo.repoId} />" in first
    assert (
        "<InstructionEditor repoId={datasetInfo.repoId} episodeId={episodeId} />"
        not in first
    )
    assert "currentInstruction={task}" in first
    assert 'className="viewer-instruction panel p-4"' not in first
