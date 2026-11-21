/** Shared navigation state for the Viewer sidebar and annotation controller. */
export interface EpisodeRow {
  episode: number;
  revision: number;
  frames: number;
  complete: boolean;
  counts?: { recorded_count: number | null; complete: boolean };
}
export interface EpisodeFilter {
  subtask?: string;
  count?: string;
  basis?: string;
  review?: "all" | "checked" | "unchecked";
  template_signature?: string;
}
export interface NavigationState {
  dataset: string;
  episode: number;
  ready: boolean;
  loading: boolean;
  error: string;
  rows: EpisodeRow[];
  total: number;
  resultCount: number;
  page: number;
  pageCount: number;
  selected: number[];
  filter: EpisodeFilter;
  subtasks: { id: string; name: string }[];
  currentOutsideFilter: boolean;
}
export interface NavigationActions {
  open(episode: number): unknown;
  filter(filter: EpisodeFilter): unknown;
  select(episode: number, checked: boolean): unknown;
  selectPage(): unknown;
  selectResults(): unknown;
  clearSelection(): unknown;
  page(page: number): unknown;
  refresh(): unknown;
  visibilityChanged(): unknown;
}
export interface NavigationVisibility {
  excluded: number[];
  flagged: number[] | null;
}
let lastNavigation:
  | { state: NavigationState; visibility: NavigationVisibility }
  | undefined;
/** The review button and its look-ahead must agree, including skipped rows. */
export function nextUncheckedEpisode(rows: EpisodeRow[], episode: number) {
  return rows.find((row) => row.episode > episode && !row.complete);
}
export function nextPrefetchEpisode(rows: EpisodeRow[], episode: number) {
  return (
    nextUncheckedEpisode(rows, episode) ||
    rows.find((row) => row.episode > episode)
  );
}
export function reconcileNavigationRow(
  rows: EpisodeRow[],
  saved: EpisodeRow,
  filter: EpisodeFilter,
) {
  const remaining = rows.filter((row) => row.episode !== saved.episode);
  const count = filter.count;
  const matches =
    !filter.subtask ||
    count === undefined ||
    count === "" ||
    (saved.counts?.recorded_count === Number(count) &&
      (filter.basis === "recorded" || saved.counts.complete));
  if (matches) remaining.push(saved);
  return remaining.sort((a, b) => a.episode - b.episode);
}
export function filterNavigationRows(
  rows: EpisodeRow[],
  filter: EpisodeFilter,
  visibility: NavigationVisibility,
): EpisodeRow[] {
  const excluded = new Set(visibility.excluded),
    flagged = visibility.flagged === null ? null : new Set(visibility.flagged);
  return rows.filter(
    (row) =>
      !excluded.has(row.episode) &&
      (!flagged || flagged.has(row.episode)) &&
      (filter.review === "checked"
        ? row.complete
        : filter.review === "unchecked"
          ? !row.complete
          : true),
  );
}
export class AnnotationNavigationStore {
  private state: NavigationState = {
    dataset: "",
    episode: 0,
    ready: false,
    loading: true,
    error: "",
    rows: [],
    total: 0,
    resultCount: 0,
    page: 0,
    pageCount: 1,
    selected: [],
    filter: {},
    subtasks: [],
    currentOutsideFilter: false,
  };
  private readonly serverState = this.state;
  private listeners = new Set<() => void>();
  private actions: NavigationActions | null = null;
  private visibility: NavigationVisibility = { excluded: [], flagged: null };
  constructor(dataset?: string) {
    if (
      typeof window !== "undefined" &&
      dataset &&
      lastNavigation?.state.dataset === dataset
    ) {
      this.state = {
        ...lastNavigation.state,
        ready: false,
        currentOutsideFilter: false,
      };
      this.visibility = lastNavigation.visibility;
    }
  }
  getSnapshot = () => this.state;
  getServerSnapshot = () => this.serverState;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  publish(state: NavigationState) {
    this.state = state;
    if (
      typeof window !== "undefined" &&
      state.dataset &&
      (!state.loading || state.rows.length)
    )
      lastNavigation = { state, visibility: this.visibility };
    this.listeners.forEach((listener) => listener());
  }
  connect(actions: NavigationActions) {
    this.actions = actions;
    return () => {
      if (this.actions === actions) {
        this.actions = null;
        this.publish({ ...this.state, ready: false });
      }
    };
  }
  run(task: (actions: NavigationActions) => unknown) {
    const actions = this.actions;
    if (!actions) return;
    if (this.state.error) this.publish({ ...this.state, error: "" });
    const failed = (error: unknown) => {
      if (this.actions === actions)
        this.publish({
          ...this.state,
          error: error instanceof Error ? error.message : String(error),
        });
    };
    try {
      Promise.resolve(task(actions)).catch(failed);
    } catch (error) {
      failed(error);
    }
  }

  getVisibility() {
    return this.visibility;
  }
  setVisibility(excluded: number[], flagged: number[] | null) {
    const next = {
      excluded: [...excluded].sort((a, b) => a - b),
      flagged: flagged?.slice().sort((a, b) => a - b) ?? null,
    };
    if (JSON.stringify(next) === JSON.stringify(this.visibility)) return;
    this.visibility = next;
    this.actions?.visibilityChanged();
  }
}
