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
