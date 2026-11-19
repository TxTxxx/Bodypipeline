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
  private listeners = new Set<() => void>();
  private actions: NavigationActions | null = null;
  private visibility: NavigationVisibility = { excluded: [], flagged: null };
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  publish(state: NavigationState) {
    this.state = state;
    this.listeners.forEach((listener) => listener());
  }
  connect(actions: NavigationActions) {
    this.actions = actions;
    return () => {
      if (this.actions === actions) {
        this.actions = null;
        this.publish({ ...this.state, ready: false, loading: true });
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
