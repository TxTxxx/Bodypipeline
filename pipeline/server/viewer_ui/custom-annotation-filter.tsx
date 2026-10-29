"use client";
import { useEffect, useState } from "react";
import { reviewRequest } from "../utils/annotation-review-client";
import type { EpisodeSummary, Scheme } from "../utils/custom-annotation-model";
interface Listing {
  episodes: EpisodeSummary[];
  scheme: Scheme | null;
}
export function CustomAnnotationFilter({
  repoId,
  onFilter,
}: {
  repoId: string;
  onFilter: (ids: number[] | null) => void;
}) {
  const [listing, setListing] = useState<Listing | null>(null),
    [error, setError] = useState(""),
    [status, setStatus] = useState(""),
    [type, setType] = useState(""),
    [field, setField] = useState(""),
    [value, setValue] = useState(""),
    [page, setPage] = useState(0),
    [ids, setIds] = useState<number[] | null>(null);
  const base = `/api/manual-annotations/${encodeURIComponent(repoId.slice(3))}`;
  useEffect(() => {
    let live = true;
    const refresh = () =>
      reviewRequest<Listing>(base)
        .then((data) => {
          if (live) {
            setListing(data);
            setError("");
          }
        })
        .catch((e) => {
          if (live) setError(String(e.message || e));
        });
    void refresh();
    window.addEventListener("bp-annotations-saved", refresh);
    return () => {
      live = false;
      window.removeEventListener("bp-annotations-saved", refresh);
    };
  }, [base]);
  useEffect(() => {
    const filters: Record<string, string> = {};
    if (status) filters.status = status;
    if (type) filters.type_id = type;
    if (field) {
      filters.field_id = field;
      filters.value = value;
    }
    window.dispatchEvent(
      new CustomEvent("bp-annotation-filter", {
        detail: { repo: repoId, filters },
      }),
    );
    if (!status && !type && !field) {
      setIds(null);
      setPage(0);
      return;
    }
    let live = true;
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (type) params.set("type_id", type);
    if (field) {
      params.set("field_id", field);
      params.set("value", value);
    }
    reviewRequest<{ episodes: EpisodeSummary[] }>(`${base}/episodes?${params}`)
      .then((data) => {
        if (live) {
          setIds(data.episodes.map((e) => e.episode_index));
          setPage(0);
          setError("");
        }
      })
      .catch((e) => {
        if (live) {
          setError(e.message);
          setIds([]);
        }
      });
    return () => {
      live = false;
    };
  }, [base, status, type, field, value, listing]);
  useEffect(() => {
    onFilter(ids === null ? null : ids.slice(page * 20, (page + 1) * 20));
  }, [ids, page, onFilter]);
  if (!repoId.startsWith("bp/")) return null;
  const fields =
    listing?.scheme?.types
      .filter((t) => t.scope === "episode")
      .flatMap((t) =>
        t.fields
          .filter((f) => ["single", "multi", "bool"].includes(f.dtype))
          .map((f) => ({ ...f, typeId: t.id, typeLabel: t.label || t.id })),
      ) || [];
  return (
    <section
      aria-label="标注筛选"
      style={{
        border: "1px solid #40516b",
        borderRadius: 6,
        padding: 8,
        margin: "10px 0",
        fontSize: 12,
      }}
    >
      <strong>标注进度与筛选</strong>
      {listing && (
        <p>
          {listing.episodes.filter((e) => e.status === "completed").length} /{" "}
          {listing.episodes.length} 已完成
        </p>
      )}
      <label>
        完成状态
        <select
          style={{ display: "block", width: "100%" }}
          value={status}
          onChange={(e) => setStatus(e.target.value)}
        >
          <option value="">全部状态</option>
          <option value="not_started">未开始</option>
          <option value="draft">草稿</option>
          <option value="completed">已完成</option>
        </select>
      </label>
      <label>
        包含类型
        <select
          style={{ display: "block", width: "100%" }}
          value={type}
          onChange={(e) => setType(e.target.value)}
        >
          <option value="">全部类型</option>
          {listing?.scheme?.types
            .filter((t) => t.scope !== "episode")
            .map((t) => (
              <option key={t.id} value={t.id}>
                {t.label || t.id}
              </option>
            ))}
        </select>
      </label>
      <label>
        Episode 字段
        <select
          style={{ display: "block", width: "100%" }}
          value={field}
          onChange={(e) => {
            setField(e.target.value);
            setValue("");
          }}
        >
          <option value="">不按字段筛选</option>
          {fields.map((f) => (
            <option key={`${f.typeId}.${f.id}`} value={`${f.typeId}.${f.id}`}>
              {f.typeLabel} / {f.label || f.id}
            </option>
          ))}
        </select>
      </label>
      {field && (
        <label>
          值
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="存储值（布尔填 true / false）"
          />
        </label>
      )}
      {error && <p role="alert">{error}</p>}
      {ids !== null && (
        <div>
          <p>
            匹配 {ids.length} 段 · 第 {page + 1} 页
          </p>
          <button disabled={page === 0} onClick={() => setPage(page - 1)}>
            上一页
          </button>{" "}
          <button
            disabled={(page + 1) * 20 >= ids.length}
            onClick={() => setPage(page + 1)}
          >
            下一页
          </button>
          <p>当前列表已按标注筛选，原分页仅用于全部数据。</p>
        </div>
      )}
    </section>
  );
}
