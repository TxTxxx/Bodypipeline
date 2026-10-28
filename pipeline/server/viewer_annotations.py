"""Minimal, pinned-source integration with the original annotation workbench."""

from pathlib import Path


def _replace(path, before, after):
    text = path.read_text()
    if after in text:
        return
    if not text.count(before):
        raise RuntimeError(f"Unexpected annotation integration anchor: {path.name}: {before[:70]}")
    path.write_text(text.replace(before, after, 1))


def patch_annotations(cache: Path):
    source = cache / "source/src"
    page = source / "app/[org]/[dataset]/[episode]/episode-viewer.tsx"
    text = page.read_text()
    # Migrate the previous replacement UI without touching user annotation data.
    if "// bodypipeline: server-owned Subtask review v1" in text:
        text = text.replace('// bodypipeline: server-owned Subtask review v1\nimport { SubtaskReview } from "@/components/subtask-review";\n', "")
        text = text.replace("              {!datasetInfo.repoId.startsWith('bp/') && <>\n", "")
        text = text.replace("              </>}\n              <PlaybackBar />", "              <PlaybackBar />")
        text = text.replace('              {datasetInfo.repoId.startsWith("bp/") ? (\n                <SubtaskReview key={`${datasetInfo.repoId}:${episodeId}`} repoId={datasetInfo.repoId} episodeId={episodeId} />\n              ) : (<>\n', "")
        text = text.replace('              </>)}\n            </div>\n          )}\n\n          {activeTab === "statistics"', '            </div>\n          )}\n\n          {activeTab === "statistics"')
        if "SubtaskReview" in text:
            raise RuntimeError("Could not restore original annotation workbench")
        page.write_text(text)

    types = source / "types/language.types.ts"
    _replace(types, "  timestamp: number;", "  timestamp: number;\n  end_timestamp?: number; // Explicit Subtask end, never inferred across gaps.")
    context = source / "context/annotations-context.tsx"
    _replace(context, 'const STORAGE_PREFIX =', 'import { useAnnotationReview } from "../utils/use-annotation-review";\n\nconst STORAGE_PREFIX =')
    _replace(context, 'import { useAnnotationReview }', 'import { insertReviewAtoms } from "../utils/annotation-review-client";\nimport { useAnnotationReview }')
    _replace(context, "  saving: boolean;", "  saving: boolean;\n  reviewLoading: boolean;\n  reviewError: string;\n  reviewReady: boolean;\n  reviewFps: number;\n  exportReview: () => Promise<{ output_dir: string; persistent_rows: number; event_rows: number }>;")
    _replace(context, "  const backendEnabled = isAnnotateBackendEnabled();", "  const serverReview = useAnnotationReview(atoms, setAtoms, setSelectedIdxState);\n  const managed = !!ident.repoId?.startsWith('bp/');\n  const backendEnabled = managed || isAnnotateBackendEnabled();")
    _replace(context, '  const savedSnapshotRef = useRef<string>("[]");', '  const savedSnapshotRef = useRef<string>("[]");\n  const episodeRequest = useRef(0);')
    _replace(context, '      setEpisodeId(newEpisodeId);', '      const request = ++episodeRequest.current;\n      setEpisodeId(newEpisodeId);')
    _replace(context, '            if (remoteAtoms && remoteAtoms.length > 0)', '            if (request === episodeRequest.current && remoteAtoms && remoteAtoms.length > 0)')
    _replace(context, '          .then(setFrameTimestamps)\n          .catch(() => setFrameTimestamps([]));', '          .then(timestamps => { if (request === episodeRequest.current) setFrameTimestamps(timestamps); })\n          .catch(() => { if (request === episodeRequest.current) setFrameTimestamps([]); });')
    _replace(context, "      setSelectedIdxState(null);\n\n      // Hydrate", "      setSelectedIdxState(null);\n      serverReview.load(newIdent.repoId || '', newEpisodeId);\n      if (newIdent.repoId?.startsWith('bp/')) {\n        setFrameTimestamps(initialFrameTimestamps ?? []);\n        return;\n      }\n\n      // Hydrate")
    _replace(context, "    if (episodeId == null) return;", "    if (episodeId == null || ident.repoId?.startsWith('bp/')) return;")
    _replace(context, "    setAtoms((prev) => [...prev, atom]);", "    if (managed && !serverReview.ready) return;\n    setAtoms((prev) => managed && serverReview.review ? insertReviewAtoms(prev, [atom], serverReview.review) : [...prev, atom]);")
    _replace(context, "  }, []);\n\n  const addAtoms", "  }, [managed, serverReview]);\n\n  const addAtoms")
    _replace(context, "    setAtoms((prev) => [...prev, ...newAtoms]);", "    if (managed && !serverReview.ready) return;\n    setAtoms((prev) => managed && serverReview.review ? insertReviewAtoms(prev, newAtoms, serverReview.review) : [...prev, ...newAtoms]);")
    _replace(context, "  }, []);\n\n  const updateAtom", "  }, [managed, serverReview]);\n\n  const updateAtom")
    _replace(context, "        next[index] = { ...next[index], ...updates };", "        if (managed && !serverReview.ready) return prev;\n        const changed = { ...next[index], ...updates };\n        next[index] = managed ? serverReview.normalize(changed) : changed;")
    _replace(context, "    [],\n  );\n\n  const deleteAtom", "    [managed, serverReview],\n  );\n\n  const deleteAtom")
    _replace(context, '  const deleteAtom = useCallback((atom: LanguageAtom) => {', '  const deleteAtom = useCallback((atom: LanguageAtom) => {\n    if (managed && !serverReview.ready) return;')
    _replace(context, '  }, []);\n\n  const resetAtoms', '  }, [managed, serverReview]);\n\n  const resetAtoms')
    _replace(context, '  const resetAtoms = useCallback(() => {', '  const resetAtoms = useCallback(() => {\n    if (managed && !serverReview.ready) return;')
    _replace(context, '  }, []);\n\n  const setPendingDraw', '  }, [managed, serverReview]);\n\n  const setPendingDraw')
    _replace(context, '    if (episodeId == null) return { ok: false, error: "no episode" };', '    if (episodeId == null) return { ok: false, error: "no episode" };\n    if (managed) return serverReview.save();')
    _replace(context, "  }, [atoms, episodeId, ident]);\n\n  const value", "  }, [atoms, episodeId, ident, managed, serverReview]);\n\n  const value")
    _replace(context, "      dirty,\n      saving,\n      setEpisode,", "      dirty: managed ? serverReview.dirty : dirty,\n      saving: managed ? serverReview.saving : saving,\n      reviewLoading: managed && serverReview.loading,\n      reviewReady: !managed || serverReview.ready,\n      reviewError: managed ? serverReview.error : '',\n      reviewFps: serverReview.review?.fps || 30,\n      exportReview: serverReview.exportDataset,\n      setEpisode,")
    _replace(context, "      snap,\n    ],", "      snap, managed, serverReview,\n    ],")

    panel = source / "components/annotations-panel.tsx"
    _replace(panel, "    ident,\n  } = useAnnotations();", "    ident, reviewLoading, reviewReady, reviewError, exportReview,\n  } = useAnnotations();")
    _replace(panel, '  const [exportStatus, setExportStatus]', '  const [exportBusy, setExportBusy] = useState(false);\n  const [exportStatus, setExportStatus]')
    _replace(panel, '    setExportStatus("Saving dataset…");', '    setExportStatus("Saving dataset…");\n    setExportBusy(true);')
    _replace(panel, '    }\n  };\n\n  const selectedAtom', '    } finally { setExportBusy(false); }\n  };\n\n  const selectedAtom')
    _replace(panel, "    if (!isAnnotateBackendEnabled()) {", "    if (!ident.repoId?.startsWith('bp/') && !isAnnotateBackendEnabled()) {")
    _replace(panel, "      const r = await apiExport(ident);", "      const r = ident.repoId?.startsWith('bp/') ? await exportReview() : await apiExport(ident);")
    _replace(panel, "disabled={saving || !dirty}", "disabled={saving || !dirty || !reviewReady}")
    _replace(panel, "disabled={!backendEnabled}", "disabled={!backendEnabled || !reviewReady || saving || exportBusy || (ident.repoId?.startsWith('bp/') && dirty)}")
    _replace(panel, '      {exportStatus && <div className="save-status">{exportStatus}</div>}', '''      {reviewLoading && <div className="save-status">正在加载 AI Subtask 和手动标注…</div>}
      {reviewError && <div className="save-status" role="alert">{reviewError} <button onClick={() => window.location.reload()}>重新加载</button></div>}
      {exportStatus && <div className="save-status">{exportStatus}</div>}''')
    _replace(panel, '      <section className="annotation-composer">', '''      {ident.repoId?.startsWith('bp/') && <div className="save-status">
        保存 Episode 会保存服务端草稿；保存数据集会生成独立修订副本，不改原数据。
        手动标注随副本保存为 sidecar，尚未写回 language Parquet 列。
      </div>}
      <section className="annotation-composer">''')
    _replace(panel, "  const { snap } = useAnnotations();", "  const { snap, reviewFps } = useAnnotations();")
    _replace(panel, "      {/* Content / role-specific fields */}", '''      {atom.style === "subtask" && atom.end_timestamp != null && (
        <div className="field"><label className="field-label">结束时间 (s，不包含该边界)</label>
          <input type="number" step={1 / reviewFps} min={atom.timestamp + 1 / reviewFps}
            value={atom.end_timestamp} onChange={e => {
              if (e.target.value && Number.isFinite(Number(e.target.value))) onChange({ end_timestamp: Number(e.target.value) });
            }} /></div>
      )}
      {/* Content / role-specific fields */}''')

    timeline = source / "components/annotations-timeline.tsx"
    _replace(timeline, '  kind: "edge" | "playhead" | "create";', '  kind: "edge" | "playhead" | "create";\n  edgeField?: "timestamp" | "end_timestamp";')
    _replace(timeline, "const { atoms, addAtom, updateAtom, snap, selectAtom } = useAnnotations();", "const { atoms, addAtom, updateAtom, snap, selectAtom, reviewFps } = useAnnotations();")
    _replace(timeline, "        k + 1 < subWithIdx.length ? subWithIdx[k + 1].a.timestamp : duration;", "        a.end_timestamp ?? (k + 1 < subWithIdx.length ? subWithIdx[k + 1].a.timestamp : duration);")
    _replace(timeline, "    const sub = lanes.subWithIdx;", '''    const sub = lanes.subWithIdx;
    const span = sub[spanK];
    if (span.a.end_timestamp != null) {
      const frame = 1 / reviewFps;
      setDrag({ kind: "edge", atomIdx: span.i, edgeField: side === "l" ? "timestamp" : "end_timestamp",
        prevTs: side === "l" ? (sub[spanK - 1]?.a.end_timestamp ?? 0) : span.a.timestamp + frame,
        nextTs: side === "l" ? span.a.end_timestamp - frame : (sub[spanK + 1]?.a.timestamp ?? duration) });
      return;
    }''')
    _replace(timeline, "        updateAtom(drag.atomIdx, { timestamp: snapped });", "        if (drag.edgeField) {\n          const value = Math.max(lower, Math.min(upper, Math.round(ts * reviewFps) / reviewFps));\n          updateAtom(drag.atomIdx, { [drag.edgeField]: value });\n        } else updateAtom(drag.atomIdx, { timestamp: snapped });")
    _replace(timeline, "[drag, duration, seek, setIsPlaying, snap, updateAtom]", "[drag, duration, seek, setIsPlaying, snap, updateAtom, reviewFps]")
    _replace(timeline, "      timestamp: snap(pendingCreate.start),", "      timestamp: snap(pendingCreate.start),\n      end_timestamp: Math.round(pendingCreate.end * reviewFps) / reviewFps,")
    _replace(timeline, "{k + 1 < lanes.subtask.length && (", "{(s.atom.end_timestamp != null || k + 1 < lanes.subtask.length) && (")
