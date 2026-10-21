(() => {
  const key = "bodypipeline-theme";
  const cookieTheme = () => document.cookie.split("; ").find((value) => value.startsWith("bodypipeline_theme="))?.split("=")[1];
  const apply = (theme) => {
    document.documentElement.dataset.theme = theme === "light" ? "light" : "dark";
    document.documentElement.style.colorScheme = document.documentElement.dataset.theme;
  };
  let saved;
  try { saved = cookieTheme() || localStorage.getItem(key); } catch (_) { saved = cookieTheme(); }
  apply(saved);
  document.addEventListener("click", (event) => {
    if (!(event.target instanceof Element) || !event.target.closest("[data-theme-toggle]")) return;
    const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    apply(theme);
    try { localStorage.setItem(key, theme); } catch (_) { /* Theme still works without storage. */ }
    document.cookie = `bodypipeline_theme=${theme}; Path=/; Max-Age=31536000; SameSite=Lax`;
  });
  // Cookies share the preference between the workbench and viewer ports.
  window.addEventListener("focus", () => { const theme = cookieTheme(); if (theme) apply(theme); });
  window.addEventListener("storage", (event) => { if (event.key === key) apply(event.newValue); });
})();
