(() => {
  const root = document.documentElement;
  const pageRoot = root.dataset.biRoot || "";
  const enPanel = document.querySelector(".wasm-bi-panel-en");
  const zhPanel = document.querySelector(".wasm-bi-panel-zh");
  const viewButtons = [...document.querySelectorAll("[data-bi-view-button]")];
  const syncButton = document.querySelector("[data-bi-sync]");
  const searchInput = document.querySelector("[data-bi-search]");
  const results = document.querySelector("[data-bi-results]");
  let syncing = false;
  let syncEnabled = localStorage.getItem("wasm-bi-sync") !== "off";

  function setView(view) {
    root.dataset.biView = view;
    localStorage.setItem("wasm-bi-view", view);
    viewButtons.forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.biViewButton === view));
    });
  }

  const storedView = localStorage.getItem("wasm-bi-view");
  const initialView = storedView || (matchMedia("(max-width: 760px)").matches ? "zh" : "both");
  setView(initialView);
  viewButtons.forEach((button) => button.addEventListener("click", () => setView(button.dataset.biViewButton)));

  function updateSyncButton() {
    if (!syncButton) return;
    syncButton.setAttribute("aria-pressed", String(syncEnabled));
    syncButton.textContent = syncEnabled ? "同步滚动·开" : "同步滚动·关";
  }

  function mirrorScroll(source, target) {
    if (!syncEnabled || syncing || !source || !target) return;
    const sourceRange = source.scrollHeight - source.clientHeight;
    const targetRange = target.scrollHeight - target.clientHeight;
    if (sourceRange <= 0 || targetRange <= 0) return;
    syncing = true;
    target.scrollTop = (source.scrollTop / sourceRange) * targetRange;
    requestAnimationFrame(() => { syncing = false; });
  }

  enPanel?.addEventListener("scroll", () => mirrorScroll(enPanel, zhPanel), { passive: true });
  zhPanel?.addEventListener("scroll", () => mirrorScroll(zhPanel, enPanel), { passive: true });
  syncButton?.addEventListener("click", () => {
    syncEnabled = !syncEnabled;
    localStorage.setItem("wasm-bi-sync", syncEnabled ? "on" : "off");
    updateSyncButton();
  });
  updateSyncButton();

  function closeResults() {
    results?.classList.remove("is-open");
  }

  function escapeHtml(value) {
    return value.replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[char]);
  }

  if (searchInput && results) {
    let index = [];
    fetch(`${pageRoot}_bilingual/search-index.json`)
      .then((response) => response.json())
      .then((data) => { index = data; })
      .catch(() => { searchInput.placeholder = "搜索索引加载失败"; });

    searchInput.addEventListener("input", () => {
      const query = searchInput.value.trim().toLocaleLowerCase();
      if (!query) {
        closeResults();
        results.innerHTML = "";
        return;
      }
      const matches = index.filter((item) => item.haystack.includes(query)).slice(0, 18);
      results.innerHTML = matches.length
        ? matches.map((item) => `
          <a class="wasm-bi-result" href="${pageRoot}${escapeHtml(item.path)}${item.anchor || ""}">
            <strong>${escapeHtml(item.en)}</strong>
            <span>${escapeHtml(item.zh)}</span>
          </a>`).join("")
        : `<div class="wasm-bi-result">未找到 / No result</div>`;
      results.classList.add("is-open");
    });

    searchInput.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeResults();
      if (event.key === "Enter") results.querySelector("a")?.click();
    });
    document.addEventListener("click", (event) => {
      if (!event.target.closest(".wasm-bi-search-wrap")) closeResults();
    });
  }

  function revealHash() {
    if (!location.hash) return;
    const id = decodeURIComponent(location.hash.slice(1));
    const enTarget = document.getElementById(id);
    const zhTarget = document.getElementById(`zh-${id}`);
    enTarget?.scrollIntoView({ block: "start" });
    if (zhTarget && zhPanel) zhPanel.scrollTop = zhTarget.offsetTop - 20;
  }
  addEventListener("hashchange", revealHash);
  requestAnimationFrame(revealHash);
})();
