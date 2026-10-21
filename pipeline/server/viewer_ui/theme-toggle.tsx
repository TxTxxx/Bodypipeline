export function ThemeToggle() {
  return (
    <button
      type="button"
      className="theme-toggle"
      data-theme-toggle
      aria-label="切换深夜与浅色模式"
      title="切换深夜与浅色模式"
    >
      <svg
        width="15"
        height="15"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        aria-hidden="true"
      >
        <path d="M20.8 13.1A9 9 0 0 1 10.9 3.2 9 9 0 1 0 20.8 13.1Z" />
      </svg>
      <span className="theme-label-light">浅色模式</span>
      <span className="theme-label-dark">深夜模式</span>
    </button>
  );
}
