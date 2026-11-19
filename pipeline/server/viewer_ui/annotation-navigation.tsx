"use client";

import React, {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";
import {
  AnnotationNavigationStore,
  type EpisodeFilter,
} from "../utils/annotation-navigation-model";
import { EpisodeScoreBadge, type EpisodeScore } from "./episode-scores";

const NavigationContext = createContext<AnnotationNavigationStore | null>(null);
export function AnnotationNavigationProvider({
  repoId,
  children,
}: {
  repoId: string;
  children: React.ReactNode;
}) {
  const store = useMemo(() => new AnnotationNavigationStore(), [repoId]);
  return (
    <NavigationContext.Provider value={store}>
      {children}
    </NavigationContext.Provider>
  );
}
export const useAnnotationNavigation = () => useContext(NavigationContext);

export function AnnotationEpisodeNavigation({
  episodeId,
  scores,
  excluded,
  flagged,
  showFlaggedOnly,
  onShowFlaggedOnlyChange,
  renderActions,
}: {
  episodeId: number;
  scores: Map<number, EpisodeScore>;
  excluded: Set<number>;
  flagged: Set<number>;
  showFlaggedOnly: boolean;
  onShowFlaggedOnlyChange: (value: boolean) => void;
  renderActions: (episode: number) => React.ReactNode;
}) {
  const store = useAnnotationNavigation();
  if (!store) throw new Error("Annotation navigation provider is missing");
  const state = useSyncExternalStore(
    store.subscribe,
    store.getSnapshot,
    store.getSnapshot,
  );
  const [number, setNumber] = useState("");
  const [draft, setDraft] = useState<EpisodeFilter>({});
  const applied = JSON.stringify(state.filter);
  useEffect(() => {
    setDraft(JSON.parse(applied));
  }, [applied]);
  useEffect(() => {
    store.setVisibility(
      [...excluded],
      showFlaggedOnly && flagged.size ? [...flagged] : null,
    );
  }, [store, excluded, flagged, showFlaggedOnly]);
  const disabled = !state.ready || state.loading;
  const run = store.run.bind(store);
  return (
    <section className="annotation-navigation" aria-label="标注数据导航">
      <div className="annotation-nav-heading">
        <h2>Episode 列表</h2>
        <button
          type="button"
          onClick={() => run((a) => a.refresh())}
          disabled={state.loading}
          aria-label="刷新 Episode 列表"
        >
          刷新
        </button>
      </div>
      <form
        className="annotation-nav-jump"
        onSubmit={(event) => {
          event.preventDefault();
          run((a) =>
            a.open(/^\d+$/.test(number.trim()) ? Number(number) : NaN),
          );
        }}
      >
        <label htmlFor="annotation-episode-number">按编号跳转</label>
        <div>
          <input
            id="annotation-episode-number"
            type="number"
            min="0"
            value={number}
            onChange={(e) => setNumber(e.target.value)}
            placeholder="Episode 编号"
          />
          <button disabled={disabled} type="submit">
            跳转
          </button>
        </div>
      </form>
      <details className="annotation-nav-filters">
        <summary>
          筛选
          {state.filter.subtask ||
          (state.filter.review && state.filter.review !== "all") ||
          showFlaggedOnly
            ? " · 已启用"
            : ""}
        </summary>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            run((a) => a.filter(draft));
          }}
        >
          <label>
            整条检查状态
            <select
              aria-label="整条检查状态"
              value={draft.review || "all"}
              onChange={(e) =>
                setDraft({
                  ...draft,
                  review: e.target.value as EpisodeFilter["review"],
                })
              }
            >
              <option value="all">全部</option>
              <option value="unchecked">未检查</option>
              <option value="checked">已检查</option>
            </select>
          </label>
          <label>
            动作标签
            <select
              aria-label="动作标签"
              value={draft.subtask || ""}
              onChange={(e) => setDraft({ ...draft, subtask: e.target.value })}
            >
              <option value="">全部动作</option>
              {state.subtasks.map((step) => (
                <option key={step.id} value={step.id}>
                  {step.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            出现次数
            <input
              aria-label="出现次数"
              type="number"
              min="0"
              value={draft.count || ""}
              onChange={(e) => setDraft({ ...draft, count: e.target.value })}
              placeholder="不限"
              disabled={!draft.subtask}
            />
          </label>
          <label>
            次数依据
            <select
              aria-label="次数依据"
              disabled={!draft.subtask}
              value={draft.basis || "complete"}
              onChange={(e) => setDraft({ ...draft, basis: e.target.value })}
            >
              <option value="complete">只看次数已确定的数据</option>
              <option value="recorded">包含次数尚不确定的数据</option>
            </select>
          </label>
          <p>同一次操作拆成几段仍计一次；无法确定的次数不会当作零次。</p>
          {flagged.size > 0 && (
            <label className="annotation-nav-inline">
              <input
                type="checkbox"
                checked={showFlaggedOnly}
                onChange={(e) => onShowFlaggedOnlyChange(e.target.checked)}
              />
              只看已标记的 Episode
            </label>
          )}
          <div className="annotation-nav-buttons">
            <button disabled={disabled} type="submit">
              应用筛选
            </button>
            <button
              disabled={disabled}
              type="button"
              onClick={() => {
                setDraft({});
                onShowFlaggedOnlyChange(false);
                run((a) => a.filter({}));
              }}
            >
              清除筛选
            </button>
          </div>
        </form>
      </details>
      <div className="annotation-nav-selection">
        <span>
          已选 <strong>{state.selected.length}</strong> 条
        </span>
        <button
          type="button"
          disabled={!state.selected.length || disabled}
          onClick={() => run((a) => a.clearSelection())}
        >
          清空
        </button>
      </div>
      <div className="annotation-nav-buttons">
        <button
          type="button"
          disabled={disabled || !state.rows.length}
          onClick={() => run((a) => a.selectPage())}
        >
          选择本页
        </button>
        <button
          type="button"
          disabled={disabled || !state.resultCount}
          onClick={() => run((a) => a.selectResults())}
        >
          选择筛选结果
        </button>
      </div>
      {state.error && (
        <p className="annotation-nav-error" role="alert">
          {state.error}
        </p>
      )}
      {state.currentOutsideFilter && (
        <p className="annotation-nav-hint">
          当前 Episode {episodeId} 不在筛选结果中，当前画面保留。
        </p>
      )}
      {state.loading ? (
        <p role="status">正在读取列表…</p>
      ) : !state.rows.length ? (
        <p role="status">没有符合条件的 Episode，可清除筛选后查看。</p>
      ) : null}
      <ul className="annotation-nav-list">
        {(state.ready ? state.rows : []).map((row) => (
          <li
            key={row.episode}
            className={row.episode === episodeId ? "current" : ""}
          >
            <input
              type="checkbox"
              aria-label={`选择导出 Episode ${row.episode}`}
              checked={state.selected.includes(row.episode)}
              disabled={disabled}
              onChange={(e) => {
                const checked = e.currentTarget.checked;
                run((a) => a.select(row.episode, checked));
              }}
            />
            <button
              type="button"
              data-annotation-episode={row.episode}
              disabled={disabled}
              aria-current={row.episode === episodeId ? "true" : undefined}
              onClick={() => run((a) => a.open(row.episode))}
            >
              <span>Episode {row.episode}</span>
              <small className={row.complete ? "checked" : ""}>
                {row.complete ? "已检查" : "未检查"} · {row.frames} 帧
              </small>
              {row.counts?.recorded_count != null && (
                <small>
                  已记录 {row.counts.recorded_count} 次 ·{" "}
                  {row.counts.complete ? "次数确定" : "次数不确定"}
                </small>
              )}
            </button>
            <div className="annotation-nav-row-tools">
              <EpisodeScoreBadge score={scores.get(row.episode)} />
              {renderActions(row.episode)}
            </div>
          </li>
        ))}
      </ul>
      <div className="annotation-nav-pagination">
        <button
          aria-label="上一页 Episode"
          disabled={disabled || state.page === 0}
          onClick={() => run((a) => a.page(state.page - 1))}
        >
          ‹
        </button>
        <span>
          {state.page + 1} / {state.pageCount} · {state.resultCount} 条
        </span>
        <button
          aria-label="下一页 Episode"
          disabled={disabled || state.page + 1 >= state.pageCount}
          onClick={() => run((a) => a.page(state.page + 1))}
        >
          ›
        </button>
      </div>
    </section>
  );
}
