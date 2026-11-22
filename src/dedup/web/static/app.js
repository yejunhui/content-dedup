"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const ICONS = {
  image: "🖼️",
  video: "🎬",
  audio: "🎵",
  document: "📄",
  text: "📝",
  archive: "🗜️",
  binary: "📦",
};

const KIND_LABELS = {
  exact: "完全重复",
  "image:phash": "图片相似",
  "image:dhash": "图片相似",
  "text:simhash": "文本相似",
  "document:simhash": "文档相似",
  "video:phash_seq": "视频相似",
  "audio:chromaprint": "音频相似",
  "archive:manifest": "压缩包内容相同",
};

const MODE_LABELS = {
  all: "标准模式",
  exact: "仅完全重复",
  fingerprint: "相似识别",
};

const FALLBACK_TYPES = [
  { name: "image", label: "图片", icon: "🖼️", description: "照片、截图、设计图", default: true },
  { name: "video", label: "视频", icon: "🎬", description: "电影、短片、录屏", default: true },
  { name: "audio", label: "音频", icon: "🎵", description: "音乐、录音、播客", default: true },
  { name: "document", label: "文档", icon: "📄", description: "PDF、Office、电子书", default: true },
  { name: "text", label: "文本", icon: "📝", description: "文本、代码、Markdown", default: true },
  { name: "archive", label: "压缩包", icon: "🗜️", description: "ZIP、TAR、GZ 等", default: true },
  { name: "binary", label: "其他文件", icon: "📦", description: "未知类型，仅精确去重", default: false },
];

const state = {
  config: null,
  typeInfo: FALLBACK_TYPES,
  typeSelection: new Set(["image", "video", "audio", "document", "text", "archive"]),
  mode: "all",
  allowRoot: null,
  reports: [],
  report: null,
  reportId: null,
  keepers: new Map(),
  selected: new Map(),
  moved: new Set(),
  lastApply: null,
  activeJobId: null,
  pollTimer: null,
  view: "scan",
  filter: { search: "", kind: "" },
  groupLimit: 200,
  searchTimer: null,
  showMoved: false,
  polling: false,
};

let toastTimer = null;
let activeModalClose = null;

document.addEventListener("DOMContentLoaded", init);

async function init() {
  bindEvents();
  try {
    await loadConfig();
  } catch (error) {
    toast(`无法加载配置：${error.message}`, "error");
    renderTypeChips();
    updateModeUI();
  }
  try {
    await refreshReports();
  } catch (error) {
    renderRecentError(error.message);
    renderHistoryError(error.message);
  }
  showView("scan");
}

/* ------------------------------------------------------------------ data */

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  let data = null;
  try {
    data = await response.json();
  } catch (_error) {
    data = null;
  }
  if (!response.ok) {
    throw new Error((data && data.error) || `请求失败（HTTP ${response.status}）`);
  }
  return data;
}

async function loadConfig() {
  const config = await api("/api/config");
  state.config = config;
  state.typeInfo = config.types && config.types.length ? config.types : FALLBACK_TYPES;
  state.typeSelection = new Set((config.defaults && config.defaults.types) || []);
  state.mode = (config.defaults && config.defaults.mode) || "all";
  state.allowRoot = config.allow_root || null;

  renderTypeChips();
  updateModeUI();
  const workers = $("#workers");
  const minSize = $("#min-size");
  if (config.defaults) {
    if (config.defaults.workers) workers.value = config.defaults.workers;
    if (config.defaults.min_size !== undefined) minSize.value = config.defaults.min_size;
  }
  if (state.allowRoot) {
    $("#scan-root").value = state.allowRoot;
    $("#allow-root-hint").textContent = `服务端已将可访问范围限制在：${state.allowRoot}`;
  }
}

async function refreshReports() {
  const data = await api("/api/reports");
  state.reports = data.reports || [];
  renderRecent();
  renderHistory();
}

function renderRecentError(message) {
  $("#recent-list").innerHTML = `<p class="muted">${esc(message)}</p>`;
}

function renderHistoryError(message) {
  $("#history-list").innerHTML = `<div class="empty"><p>${esc(message)}</p></div>`;
}

function renderRecent() {
  const container = $("#recent-list");
  if (!state.reports.length) {
    container.innerHTML = `<p class="muted">还没有扫描记录。选择文件夹并开始扫描后，报告会出现在这里。</p>`;
    return;
  }
  container.innerHTML = state.reports
    .slice(0, 5)
    .map((report) => {
      const when = formatDate(report.generated_at || report.mtime_ns);
      const mode = MODE_LABELS[report.mode] || report.mode || "";
      return `<div class="recent-item" data-report-id="${esc(report.id)}" role="button" tabindex="0">
        <div class="recent-main">
          <div class="recent-root" title="${esc(report.root)}">${esc(report.root)}</div>
          <div class="recent-meta">${esc(mode)} · ${esc(when)}</div>
        </div>
        <div class="recent-stats">
          <strong>${esc(String(report.groups || 0))} 组 · ${esc(report.reclaimable_human || formatBytes(report.reclaimable_bytes || 0))}</strong>
          <span>${esc(String(report.duplicate_files || 0))} 个重复文件</span>
        </div>
      </div>`;
    })
    .join("");
  container.querySelectorAll("[data-report-id]").forEach((item) => {
    item.addEventListener("click", () => openReport(item.dataset.reportId));
    item.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openReport(item.dataset.reportId);
      }
    });
  });
}

function renderHistory() {
  const container = $("#history-list");
  if (!state.reports.length) {
    container.innerHTML = `<div class="empty"><div class="empty-icon">🗂️</div><h2>还没有历史报告</h2><p>完成一次扫描后，这里会保留可再次打开的报告。</p></div>`;
    return;
  }
  container.innerHTML = state.reports
    .map((report) => {
      const when = formatDate(report.generated_at || report.mtime_ns);
      const mode = MODE_LABELS[report.mode] || report.mode || "";
      return `<div class="history-item" data-report-id="${esc(report.id)}" role="button" tabindex="0">
        <div>
          <h3 title="${esc(report.root)}">${esc(report.root)}</h3>
          <p>${esc(mode)} · ${esc(when)} · ${esc(report.id)}</p>
        </div>
        <div class="history-stats">
          <strong>${esc(String(report.groups || 0))} 组</strong>
          <span>${esc(report.reclaimable_human || formatBytes(report.reclaimable_bytes || 0))}</span>
        </div>
      </div>`;
    })
    .join("");
  container.querySelectorAll("[data-report-id]").forEach((item) => {
    item.addEventListener("click", () => openReport(item.dataset.reportId));
    item.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openReport(item.dataset.reportId);
      }
    });
  });
}

/* ---------------------------------------------------------------- rendering */

function renderTypeChips() {
  const container = $("#type-chips");
  container.innerHTML = state.typeInfo
    .map((item) => {
      const active = state.typeSelection.has(item.name);
      return `<button type="button" class="chip${active ? " active" : ""}" data-type="${esc(item.name)}" title="${esc(item.description || "")}" aria-pressed="${active}">
        <span class="chip-icon">${esc(item.icon || ICONS[item.name] || "📄")}</span>${esc(item.label || item.name)}
      </button>`;
    })
    .join("");
}

function updateModeUI() {
  $$("#mode-group button").forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === state.mode);
  });
  const info = state.config && state.config.modes ? state.config.modes.find((item) => item.value === state.mode) : null;
  if (state.mode === "exact") {
    $("#mode-hint").textContent = "只比较 SHA-256，速度最快，不会把相似但不完全相同的文件放在一组。";
  } else {
    $("#mode-hint").textContent =
      (info && info.description) || "找出完全重复和内容相似的文件，推荐日常使用。";
  }
}

function renderResults() {
  if (!state.report) return;
  const report = state.report;
  $("#results-root").textContent = `${report.root} · 生成于 ${formatDate(report.generated_at)}`;
  renderSummary();
  renderNotices();
  renderKindOptions();
  renderGroups();
  updateSelectionBar();
}

function renderSummary() {
  const report = state.report;
  const stats = report.stats || {};
  const summary = report.summary || {};
  const moved = state.moved.size;
  const cards = [
    {
      label: "扫描文件",
      value: numberText(stats.files_processed),
      sub: `缓存命中 ${numberText(stats.cache_hits)} · 复用 ${numberText(stats.exact_reused)}`,
    },
    {
      label: "重复组",
      value: numberText(summary.groups),
      sub: `精确 ${numberText(summary.exact_groups)} · 相似 ${numberText(summary.fingerprint_groups)}`,
    },
    {
      label: "重复文件",
      value: numberText(summary.duplicate_files),
      sub: moved ? `本次已移动 ${moved} 个` : "确认后移到回收站",
    },
    {
      label: "可回收空间",
      value: esc(summary.reclaimable_human || formatBytes(summary.reclaimable_bytes || 0)),
      sub: "移动到回收站后释放",
    },
  ];
  $("#summary-grid").innerHTML = cards
    .map(
      (card) => `<div class="summary-card">
        <span class="label">${esc(card.label)}</span>
        <strong class="value">${card.value}</strong>
        <div class="sub">${esc(card.sub)}</div>
      </div>`
    )
    .join("");
}

function renderNotices() {
  const report = state.report;
  const notices = [];
  if (report.mode === "exact") {
    notices.push({
      type: "",
      icon: "ℹ️",
      title: "当前是仅完全重复模式",
      text: "相似但不完全相同的文件没有参与比较。如需识别缩放、重压缩或格式转换后的副本，请使用标准模式重新扫描。",
    });
  }
  const issues = report.fingerprint_issues || [];
  if (issues.length) {
    const total = issues.reduce((sum, item) => sum + (item.count || 0), 0);
    const details = issues
      .slice(0, 3)
      .map((item) => `${adapterLabel(item.adapter)}：${item.message}（${item.count} 个）`)
      .join("；");
    notices.push({
      type: "warning",
      icon: "⚠️",
      title: `有 ${total} 个文件未能生成相似指纹`,
      text: `${details}${issues.length > 3 ? "；…" : ""}。这些文件仍会参与完全重复检测。`,
    });
  }
  const warnings = report.scan_warnings || {};
  if (warnings.total) {
    const items = (warnings.items || []).slice(0, 2).join("；");
    notices.push({
      type: "warning",
      icon: "⚠️",
      title: `扫描中有 ${warnings.total} 条警告`,
      text: items || "部分目录可能因权限不足被跳过。",
    });
  }
  if (state.lastApply) {
    const apply = state.lastApply;
    const parts = [];
    if (apply.moved) parts.push(`已移动 ${apply.moved} 个文件`);
    if (apply.skipped) parts.push(`跳过 ${apply.skipped} 个`);
    if (apply.errors) parts.push(`${apply.errors} 个错误`);
    if (apply.cancelled) {
      notices.push({
        type: "warning",
        icon: "⚠️",
        title: "清理操作已取消",
        text: apply.moved ? `取消前已移动 ${apply.moved} 个文件；它们保留在回收站中。` : "",
      });
    } else {
      notices.push({
        type: apply.errors ? "warning" : "",
        icon: apply.errors ? "⚠️" : "✅",
        title: parts.length ? parts.join("，") : "清理操作完成",
        text: apply.trash ? `回收站：${apply.trash}` : "",
      });
    }
  }
  $("#result-notices").innerHTML = notices
    .map(
      (notice) => `<div class="notice ${notice.type}">
        <span>${notice.icon}</span>
        <div><strong>${esc(notice.title)}</strong>${notice.text ? `<p>${esc(notice.text)}</p>` : ""}</div>
      </div>`
    )
    .join("");
}

function renderKindOptions() {
  const select = $("#filter-kind");
  const kinds = Array.from(new Set((state.report.groups || []).map((group) => group.kind))).sort();
  if (state.filter.kind && !kinds.includes(state.filter.kind)) state.filter.kind = "";
  select.innerHTML =
    `<option value="">全部类型</option>` +
    kinds
      .map((kind) => `<option value="${esc(kind)}">${esc(groupKindLabel(kind))}</option>`)
      .join("");
  select.value = state.filter.kind;
}

function filteredGroups() {
  const report = state.report;
  if (!report) return [];
  const query = state.filter.search.trim().toLowerCase();
  const groups = [];
  for (const group of report.groups || []) {
    if (state.filter.kind && group.kind !== state.filter.kind) continue;
    const keeper = state.keepers.get(group.id) || group.keeper.path;
    const members = groupMembers(group);
    const active = members.filter((member) => member.path === keeper || !state.moved.has(member.path));
    if (active.length < 2) continue;
    if (query) {
      const haystack = active
        .map((member) => `${member.rel_path || ""} ${member.path || ""}`)
        .join(" ")
        .toLowerCase();
      if (!haystack.includes(query) && !groupKindLabel(group.kind).toLowerCase().includes(query)) {
        continue;
      }
    }
    groups.push(group);
  }
  return groups;
}

function renderGroups() {
  const report = state.report;
  if (!report) return;
  const all = filteredGroups();
  const shown = all.slice(0, state.groupLimit);
  const container = $("#groups");
  container.innerHTML = shown.map((group) => groupCardHtml(group)).join("");
  attachThumbFallbacks();
  if (all.length > shown.length) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn ghost load-more";
    button.textContent = `加载更多（剩余 ${all.length - shown.length} 组）`;
    button.addEventListener("click", () => {
      state.groupLimit += 200;
      renderGroups();
    });
    container.appendChild(button);
  }

  const empty = $("#empty-state");
  const showEmpty = all.length === 0;
  empty.classList.toggle("hidden", !showEmpty);
  if (showEmpty) {
    const totalGroups = (report.groups || []).length;
    const remainingAnywhere = (report.groups || []).some((group) => {
      const keeper = state.keepers.get(group.id) || group.keeper.path;
      return groupMembers(group).some(
        (member) => member.path !== keeper && !state.moved.has(member.path)
      );
    });
    if (totalGroups === 0) {
      $("#empty-title").textContent = "没有找到重复文件";
      $("#empty-text").textContent = "可以尝试标准模式、选择更多内容类型，或换一个目录。";
    } else if (!remainingAnywhere && state.moved.size) {
      $("#empty-title").textContent = "本次重复项已处理完";
      $("#empty-text").textContent =
        "已移动的文件保存在回收站中。勾选“显示已移动”可以查看本次操作的文件。";
    } else {
      $("#empty-title").textContent = "没有符合筛选条件的组";
      $("#empty-text").textContent = "调整搜索关键词、类型筛选，或显示已移动文件。";
    }
  }
  $("#filter-count").textContent = all.length
    ? `显示 ${shown.length} / ${all.length} 组`
    : `共 ${(report.groups || []).length} 组`;
  updateGroupSelects();
  updateSelectionBar();
}

function groupCardHtml(group) {
  const keeper = state.keepers.get(group.id) || group.keeper.path;
  const selectedSet = state.selected.get(group.id) || new Set();
  const members = groupMembers(group);
  let keeperMember = members.find((member) => member.path === keeper);
  if (!keeperMember) keeperMember = members[0];
  const ordered = [keeperMember, ...members.filter((member) => member !== keeperMember)];
  const visible = ordered.filter(
    (member) => member.path === keeper || state.showMoved || !state.moved.has(member.path)
  );
  const activeDuplicates = visible.filter((member) => member.path !== keeper);
  const selectedCount = activeDuplicates.filter((member) => selectedSet.has(member.path)).length;
  const allSelected = activeDuplicates.length > 0 && selectedCount === activeDuplicates.length;
  const exact = (group.duplicates || []).every((item) => item.reason === "exact");
  const remaining = activeDuplicates.reduce((sum, member) => sum + (member.size || 0), 0);
  const rows = visible.map((member) => fileRowHtml(group, member, keeper, selectedSet)).join("");

  return `<article class="group" data-group-id="${esc(group.id)}">
    <header class="group-head">
      <div class="group-title">
        <span class="badge ${exact ? "exact" : "similar"}">${esc(groupKindLabel(group.kind))}</span>
        <h3>${visible.length} 个文件</h3>
        <span class="group-meta">可回收 ${esc(formatBytes(remaining))}</span>
      </div>
      <label class="group-select"><input type="checkbox" class="group-select-input" data-group="${esc(group.id)}" ${allSelected ? "checked" : ""} ${activeDuplicates.length ? "" : "disabled"}> 选择本组</label>
    </header>
    <div class="file-list">${rows}</div>
  </article>`;
}

function fileRowHtml(group, member, keeper, selectedSet) {
  const isKeeper = member.path === keeper;
  const isMoved = state.moved.has(member.path);
  const isSelected = !isKeeper && !isMoved && selectedSet.has(member.path);
  const reason = isKeeper ? "保留此文件" : reasonText(member);
  const reasonClass = member.reason === "exact" ? "exact" : isKeeper ? "" : "similar";
  const icon = ICONS[member.adapter] || "📄";
  const relative = member.rel_path || member.path || "";
  const name = baseName(member.path);
  const meta = `${formatBytes(member.size || 0)}<span>${esc(formatDate(member.mtime_ns))}</span><span class="reason ${reasonClass}">${esc(isMoved ? "已移动" : reason)}</span>`;
  return `<div class="file-row ${isKeeper ? "is-keeper" : ""} ${isMoved ? "is-moved" : ""}">
    <label class="keep-choice">
      <input type="radio" name="keep-${esc(group.id)}" value="${esc(member.path)}" data-keep="${esc(member.path)}" data-group="${esc(group.id)}" ${isKeeper ? "checked" : ""} ${isMoved ? "disabled" : ""}>
      保留
    </label>
    ${thumbHtml(member, icon)}
    <div class="file-info">
      <div class="file-name" title="${esc(member.path)}">${esc(name)}</div>
      <div class="file-path" title="${esc(member.path)}">${esc(relative)}</div>
      <div class="file-meta">${meta}</div>
    </div>
    <label class="move-choice">
      <input type="checkbox" class="dup-check" data-group="${esc(group.id)}" data-path="${esc(member.path)}" ${isSelected ? "checked" : ""} ${isKeeper || isMoved ? "disabled" : ""}>
      移走
    </label>
  </div>`;
}

function thumbHtml(member, icon) {
  if (state.moved.has(member.path)) {
    return `<div class="thumb">${icon}</div>`;
  }
  if (member.adapter === "image" || member.adapter === "video") {
    const videoClass = member.adapter === "video" ? " video" : "";
    const url = `/api/thumb?path=${encodeURIComponent(member.path)}&amp;size=128`;
    return `<div class="thumb${videoClass}"><img class="thumb-img" loading="lazy" decoding="async" alt="" src="${url}" data-icon="${esc(icon)}"></div>`;
  }
  return `<div class="thumb">${icon}</div>`;
}

function attachThumbFallbacks() {
  $$("#groups img.thumb-img").forEach((image) => {
    image.addEventListener(
      "error",
      () => {
        const parent = image.parentElement;
        const icon = image.dataset.icon || "📄";
        image.remove();
        parent.textContent = icon;
      },
      { once: true }
    );
  });
}

function updateGroupSelects() {
  $$(".group").forEach((card) => {
    const groupId = card.dataset.groupId;
    const group = groupById(groupId);
    if (!group) return;
    const keeper = state.keepers.get(groupId) || group.keeper.path;
    const selectedSet = state.selected.get(groupId) || new Set();
    const inputs = Array.from(card.querySelectorAll(".dup-check:not(:disabled)"));
    const checkedCount = inputs.filter((input) => selectedSet.has(input.dataset.path) || input.checked).length;
    const box = card.querySelector(".group-select-input");
    if (!box) return;
    box.checked = inputs.length > 0 && checkedCount === inputs.length;
    box.indeterminate = checkedCount > 0 && checkedCount < inputs.length;
    box.disabled = inputs.length === 0;
  });
}

function updateSelectionBar() {
  if (!state.report) return;
  const stats = selectionStats();
  $("#selected-count").textContent = String(stats.count);
  $("#selected-size").textContent = formatBytes(stats.bytes);
  const bar = $("#selection-bar");
  bar.classList.toggle("hidden", !(state.view === "results" && stats.count > 0));
  $$("#apply-selected, #apply-bottom").forEach((button) => {
    button.disabled = stats.count === 0;
  });
  $("#apply-selected").textContent = stats.count ? `移到回收站（${stats.count}）` : "移到回收站";
}

function selectionStats() {
  let count = 0;
  let bytes = 0;
  if (!state.report) return { count, bytes };
  for (const group of state.report.groups || []) {
    const keeper = state.keepers.get(group.id) || group.keeper.path;
    const selectedSet = state.selected.get(group.id) || new Set();
    for (const member of groupMembers(group)) {
      if (member.path === keeper || state.moved.has(member.path)) continue;
      if (selectedSet.has(member.path)) {
        count += 1;
        bytes += member.size || 0;
      }
    }
  }
  return { count, bytes };
}

/* ---------------------------------------------------------------- selection */

function changeKeeper(groupId, path) {
  const group = groupById(groupId);
  if (!group || state.moved.has(path)) return;
  const oldKeeper = state.keepers.get(groupId) || group.keeper.path;
  if (oldKeeper === path) return;
  const selectedSet = state.selected.get(groupId) || new Set();
  state.keepers.set(groupId, path);
  selectedSet.delete(path);
  if (!state.moved.has(oldKeeper)) selectedSet.add(oldKeeper);
  state.selected.set(groupId, selectedSet);
  const scrollY = window.scrollY;
  renderGroups();
  window.scrollTo({ top: scrollY });
}

function setGroupSelection(groupId, select) {
  const group = groupById(groupId);
  if (!group) return;
  const keeper = state.keepers.get(groupId) || group.keeper.path;
  const selectedSet = state.selected.get(groupId) || new Set();
  for (const member of groupMembers(group)) {
    if (member.path === keeper || state.moved.has(member.path)) continue;
    if (select) selectedSet.add(member.path);
    else selectedSet.delete(member.path);
  }
  state.selected.set(groupId, selectedSet);
  syncGroupCheckboxes(groupId);
  updateGroupSelects();
  updateSelectionBar();
}

function syncGroupCheckboxes(groupId) {
  const selectedSet = state.selected.get(groupId) || new Set();
  $$(`.dup-check[data-group="${cssEscape(groupId)}"]`).forEach((input) => {
    if (!input.disabled) input.checked = selectedSet.has(input.dataset.path);
  });
}

function syncAllCheckboxes() {
  $$(".dup-check").forEach((input) => {
    if (input.disabled) return;
    const selectedSet = state.selected.get(input.dataset.group) || new Set();
    input.checked = selectedSet.has(input.dataset.path);
  });
  updateGroupSelects();
}

function selectAllFiltered() {
  const groups = filteredGroups();
  let count = 0;
  for (const group of groups) {
    const keeper = state.keepers.get(group.id) || group.keeper.path;
    const selectedSet = state.selected.get(group.id) || new Set();
    for (const member of groupMembers(group)) {
      if (member.path === keeper || state.moved.has(member.path)) continue;
      if (!selectedSet.has(member.path)) {
        selectedSet.add(member.path);
        count += 1;
      }
    }
    state.selected.set(group.id, selectedSet);
  }
  syncAllCheckboxes();
  updateSelectionBar();
  toast(count ? `已选择 ${count} 个重复文件` : "当前筛选下没有可选择的重复文件");
}

function clearAllSelections() {
  for (const group of (state.report && state.report.groups) || []) {
    state.selected.set(group.id, new Set());
  }
  syncAllCheckboxes();
  updateSelectionBar();
}

function buildSelections() {
  const selections = [];
  if (!state.report) return selections;
  for (const group of state.report.groups || []) {
    const keeper = state.keepers.get(group.id) || group.keeper.path;
    const selectedSet = state.selected.get(group.id) || new Set();
    const duplicates = Array.from(selectedSet).filter(
      (path) => path !== keeper && !state.moved.has(path)
    );
    if (duplicates.length) {
      selections.push({ group_id: group.id, keeper, duplicates });
    }
  }
  return selections;
}

function openApplyConfirm() {
  const selections = buildSelections();
  const stats = selectionStats();
  if (!selections.length || !stats.count) {
    toast("请先勾选要移走的重复文件");
    return;
  }
  const report = state.report;
  const safety = (report.settings && report.settings.safety) || {};
  let trash = String(safety.trash_dir || ".dedup-trash");
  if (!trash.startsWith("/")) {
    trash = `${String(report.root || "").replace(/\/+$/, "")}/${trash}`;
  }
  trash = `${trash}/<时间戳>`;
  const html = `<div class="modal-backdrop">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="apply-title">
      <div class="modal-head">
        <h2 id="apply-title">确认移动到回收站</h2>
        <button type="button" class="icon-btn" data-close aria-label="关闭">×</button>
      </div>
      <div class="modal-body">
        <div class="confirm-summary">
          <div class="confirm-stat"><span>将移动文件</span><strong>${esc(String(stats.count))} 个</strong></div>
          <div class="confirm-stat"><span>预计释放空间</span><strong>${esc(formatBytes(stats.bytes))}</strong></div>
          <div class="confirm-stat"><span>涉及重复组</span><strong>${esc(String(selections.length))} 组</strong></div>
        </div>
        <p class="hint">文件会按原目录结构移动到：<span class="trash-path">${esc(trash)}</span></p>
        <label class="confirm-check">
          <input type="checkbox" id="apply-confirm-check">
          <span>我确认这些文件可以移动到回收站。此操作不会永久删除文件，但移动后需要手动恢复。</span>
        </label>
        <div class="confirm-warning">移动前会再次校验文件大小、修改时间和 SHA-256。文件已变化的项目会自动跳过。</div>
      </div>
      <div class="modal-foot">
        <button type="button" class="btn ghost" data-close>取消</button>
        <button type="button" class="btn primary" id="apply-confirm-btn" disabled>确认移动</button>
      </div>
    </div>
  </div>`;
  const modal = mountModal(html);
  const checkbox = modal.root.querySelector("#apply-confirm-check");
  const confirm = modal.root.querySelector("#apply-confirm-btn");
  checkbox.addEventListener("change", () => {
    confirm.disabled = !checkbox.checked;
  });
  confirm.addEventListener("click", () => {
    if (!checkbox.checked) return;
    modal.close();
    startApply(selections);
  });
}

async function startApply(selections) {
  try {
    const job = await api("/api/apply", {
      method: "POST",
      body: JSON.stringify({
        report_id: state.reportId,
        selections,
        dry_run: false,
        verify: true,
      }),
    });
    showProgress(job);
    pollJob(job.id);
  } catch (error) {
    toast(error.message, "error");
  }
}

function handleApplyDone(summary) {
  state.activeJobId = null;
  state.lastApply = summary;
  const moved = (summary.items || []).map((item) => item.source).filter(Boolean);
  for (const path of moved) {
    state.moved.add(path);
    for (const selectedSet of state.selected.values()) selectedSet.delete(path);
  }
  showView("results");
  renderResults();
  if (summary.cancelled) {
    toast("操作已取消；已经移动的文件保留在回收站中", "error");
  } else if (summary.errors) {
    toast(`完成，但有 ${summary.errors} 个错误。已移动 ${summary.moved || 0} 个文件`, "error");
  } else if (summary.skipped) {
    toast(`已移动 ${summary.moved || 0} 个文件，跳过 ${summary.skipped} 个已变化的文件`);
  } else {
    toast(`已移动 ${summary.moved || 0} 个文件到回收站`);
  }
}

/* ------------------------------------------------------------------ scan */

async function startScan() {
  const root = $("#scan-root").value.trim();
  if (!root) {
    toast("请先选择要扫描的文件夹", "error");
    return;
  }
  const types = Array.from(state.typeSelection);
  if (!types.length) {
    toast("请至少选择一种内容类型", "error");
    return;
  }
  const limitValue = $("#limit").value.trim();
  const workersValue = $("#workers").value.trim();
  const minSizeValue = $("#min-size").value.trim();
  const payload = {
    root,
    mode: state.mode,
    types,
    workers: workersValue ? Number(workersValue) : undefined,
    min_size: minSizeValue ? Number(minSizeValue) : undefined,
    limit: limitValue ? Number(limitValue) : null,
    follow_symlinks: $("#follow-symlinks").checked,
    refresh: $("#refresh").checked,
  };
  const button = $("#start-scan");
  button.disabled = true;
  button.textContent = "正在启动…";
  try {
    const job = await api("/api/scan", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    showProgress(job);
    pollJob(job.id);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "开始扫描";
  }
}

function showProgress(job) {
  state.activeJobId = job.id;
  showView("progress");
  $("#progress-kind").textContent = job.kind === "apply" ? "清理" : "扫描";
  $("#progress-title").textContent = job.kind === "apply" ? "正在移动到回收站…" : "正在扫描…";
  $("#cancel-job").textContent = "取消";
  $("#cancel-job").disabled = false;
  updateProgress(job);
}

function pollJob(jobId) {
  clearInterval(state.pollTimer);
  state.activeJobId = jobId;
  const tick = async () => {
    if (state.polling) return;
    state.polling = true;
    try {
      let job;
      try {
        job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      } catch (error) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        state.activeJobId = null;
        toast(error.message, "error");
        return;
      }
      updateProgress(job);
      if (!["done", "cancelled", "error"].includes(job.status)) return;
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      try {
        if (job.status === "done") {
          state.activeJobId = null;
          if (job.kind === "scan") {
            if (!job.result || !job.result.report_id) throw new Error("扫描完成但没有生成报告");
            await loadReport(job.result.report_id);
            showView("results");
            toast("扫描完成");
            refreshReports().catch(() => {});
          } else if (job.kind === "apply") {
            handleApplyDone(job.result || {});
          }
        } else if (job.status === "cancelled" && job.kind === "apply" && job.result) {
          handleApplyDone(job.result);
        } else {
          state.activeJobId = null;
          const button = $("#cancel-job");
          button.textContent = "返回扫描";
          button.disabled = false;
          toast(job.error || "操作已取消", "error");
        }
      } catch (error) {
        state.activeJobId = null;
        toast(error.message, "error");
      }
    } finally {
      state.polling = false;
    }
  };
  state.pollTimer = setInterval(tick, 700);
  tick();
}

function updateProgress(job) {
  const progress = job.progress || {};
  const active = ["queued", "running"].includes(job.status);
  const title =
    job.status === "error"
      ? "操作失败"
      : job.status === "cancelled"
        ? "操作已取消"
        : job.status === "done"
          ? "完成"
          : job.kind === "apply"
            ? "正在移动到回收站…"
            : "正在扫描…";
  $("#progress-kind").textContent = job.kind === "apply" ? "清理" : "扫描";
  $("#progress-title").textContent = title;
  const fill = $("#progress-fill");
  fill.style.width = `${Math.max(0, Math.min(100, Number(progress.percent) || 0))}%`;
  fill.classList.toggle("error", job.status === "error");
  fill.classList.toggle("cancelled", job.status === "cancelled");
  $("#progress-message").textContent =
    job.status === "error" ? job.error || "未知错误" : progress.message || "正在处理…";
  $("#progress-count").textContent = progress.total
    ? `${progress.processed || 0} / ${progress.total}`
    : "";
  $("#progress-current").textContent = progress.current || "";
  const cancel = $("#cancel-job");
  if (active) {
    cancel.textContent = "取消";
    cancel.disabled = false;
  } else if (job.status !== "done") {
    cancel.textContent = "返回扫描";
    cancel.disabled = false;
  }
}

async function cancelJob() {
  const jobId = state.activeJobId;
  if (!jobId) {
    showView("scan");
    return;
  }
  const button = $("#cancel-job");
  button.disabled = true;
  button.textContent = "正在取消…";
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
    toast("正在取消，当前文件处理完后停止");
  } catch (error) {
    button.disabled = false;
    button.textContent = "取消";
    toast(error.message, "error");
  }
}

/* ----------------------------------------------------------------- report */

async function openReport(reportId) {
  try {
    await loadReport(reportId);
    showView("results");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function loadReport(reportId) {
  const report = await api(`/api/reports/${encodeURIComponent(reportId)}`);
  state.report = report;
  state.reportId = reportId;
  state.moved = new Set();
  state.lastApply = null;
  state.groupLimit = 200;
  state.keepers = new Map();
  state.selected = new Map();
  state.filter = { search: "", kind: "" };
  state.showMoved = false;
  $("#search").value = "";
  $("#filter-kind").value = "";
  $("#show-moved").checked = false;
  for (const group of report.groups || []) {
    state.keepers.set(group.id, group.keeper.path);
    state.selected.set(group.id, new Set((group.duplicates || []).map((member) => member.path)));
  }
  renderResults();
}

/* ------------------------------------------------------------------ cache */

async function loadCache() {
  try {
    const stats = await api("/api/cache");
    const byStatus = stats.by_fingerprint_status || {};
    const cards = [
      { label: "缓存文件", value: numberText(stats.files), sub: "已记录的路径" },
      { label: "指纹可用", value: numberText(byStatus.ok), sub: "可直接复用" },
      { label: "不支持指纹", value: numberText(byStatus.unsupported), sub: "仅精确去重" },
      { label: "指纹失败", value: numberText(byStatus.error), sub: "可尝试重新扫描" },
    ];
    $("#cache-stats").innerHTML = cards
      .map(
        (card) => `<div class="summary-card">
          <span class="label">${esc(card.label)}</span>
          <strong class="value">${card.value}</strong>
          <div class="sub">${esc(card.sub)}</div>
        </div>`
      )
      .join("");
    $("#cache-path").textContent = `数据库位置：${stats.path || "未知"}`;
  } catch (error) {
    toast(error.message, "error");
  }
}

/* ------------------------------------------------------------ directory picker */

async function openBrowse(initialPath) {
  const startPath = initialPath || state.allowRoot || "";
  const html = `<div class="modal-backdrop">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="browse-title">
      <div class="modal-head">
        <h2 id="browse-title">选择文件夹</h2>
        <button type="button" class="icon-btn" data-close aria-label="关闭">×</button>
      </div>
      <div class="modal-body">
        <div class="browse-roots" id="browse-roots"></div>
        <div class="browse-path-row">
          <button type="button" class="btn ghost small" id="browse-up" disabled>↑ 上一级</button>
          <div class="browse-path" id="browse-current">正在加载…</div>
        </div>
        <div class="browse-list" id="browse-list"><div class="browse-empty">正在加载…</div></div>
      </div>
      <div class="modal-foot">
        <div class="browse-foot-left">
          <label class="check"><input type="checkbox" id="browse-hidden"> 显示隐藏目录</label>
        </div>
        <button type="button" class="btn ghost" data-close>取消</button>
        <button type="button" class="btn primary" id="browse-select" disabled>使用此文件夹</button>
      </div>
    </div>
  </div>`;
  const modal = mountModal(html);
  const rootsEl = modal.root.querySelector("#browse-roots");
  const upButton = modal.root.querySelector("#browse-up");
  const currentEl = modal.root.querySelector("#browse-current");
  const listEl = modal.root.querySelector("#browse-list");
  const hiddenInput = modal.root.querySelector("#browse-hidden");
  const selectButton = modal.root.querySelector("#browse-select");
  let currentPath = "";
  let parentPath = null;

  const load = async (path) => {
    listEl.innerHTML = `<div class="browse-empty">正在加载…</div>`;
    try {
      const query = new URLSearchParams();
      if (path) query.set("path", path);
      if (hiddenInput.checked) query.set("hidden", "1");
      const data = await api(`/api/browse?${query.toString()}`);
      currentPath = data.current;
      parentPath = data.parent;
      currentEl.textContent = data.current;
      upButton.disabled = !data.parent;
      upButton.dataset.parent = data.parent || "";
      rootsEl.innerHTML = (data.roots || [])
        .map(
          (root) =>
            `<button type="button" class="btn ghost small" data-root="${esc(root.path)}">${esc(root.label)}</button>`
        )
        .join("");
      rootsEl.querySelectorAll("[data-root]").forEach((button) => {
        button.addEventListener("click", () => load(button.dataset.root));
      });
      if (!data.entries || !data.entries.length) {
        listEl.innerHTML = `<div class="browse-empty">这里没有子文件夹</div>`;
      } else {
        listEl.innerHTML = data.entries
          .map(
            (entry) =>
              `<button type="button" class="browse-entry" data-path="${esc(entry.path)}"><span class="folder">📁</span>${esc(entry.name)}</button>`
          )
          .join("");
        listEl.querySelectorAll("[data-path]").forEach((button) => {
          button.addEventListener("click", () => load(button.dataset.path));
        });
        if (data.truncated) {
          listEl.insertAdjacentHTML(
            "beforeend",
            `<div class="browse-empty">目录过多，仅显示前 2000 个。</div>`
          );
        }
      }
      selectButton.disabled = false;
    } catch (error) {
      currentPath = "";
      parentPath = null;
      upButton.disabled = true;
      selectButton.disabled = true;
      listEl.innerHTML = `<div class="browse-empty">${esc(error.message)}</div>`;
    }
  };

  upButton.addEventListener("click", () => {
    if (parentPath) load(parentPath);
  });
  hiddenInput.addEventListener("change", () => load(currentPath));
  selectButton.addEventListener("click", () => {
    if (!currentPath) return;
    $("#scan-root").value = currentPath;
    modal.close();
  });
  await load(startPath);
}

/* ------------------------------------------------------------------ modals */

function mountModal(html) {
  if (activeModalClose) activeModalClose();
  const root = $("#modal-root");
  root.innerHTML = html;
  const backdrop = root.querySelector(".modal-backdrop");
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    root.innerHTML = "";
    document.removeEventListener("keydown", onKey);
    if (activeModalClose === close) activeModalClose = null;
  };
  const onKey = (event) => {
    if (event.key === "Escape") close();
  };
  document.addEventListener("keydown", onKey);
  root.querySelectorAll("[data-close]").forEach((element) => {
    element.addEventListener("click", close);
  });
  if (backdrop) {
    backdrop.addEventListener("click", (event) => {
      if (event.target === backdrop) close();
    });
  }
  activeModalClose = close;
  return { root, close };
}

/* ------------------------------------------------------------------- views */

function showView(name) {
  state.view = name;
  $$(".view").forEach((view) => view.classList.toggle("hidden", view.id !== `view-${name}`));
  const navName = name === "results" || name === "progress" ? "scan" : name;
  $$(".nav-btn").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === navName);
  });
  if (name === "history") {
    refreshReports().catch((error) => toast(error.message, "error"));
  }
  if (name === "cache") void loadCache();
  if (name !== "results") $("#selection-bar").classList.add("hidden");
  else updateSelectionBar();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* ------------------------------------------------------------------ events */

function bindEvents() {
  $("#brand-home").addEventListener("click", () => showView("scan"));
  $$(".nav-btn").forEach((button) => {
    button.addEventListener("click", () => showView(button.dataset.view));
  });
  $("#browse-btn").addEventListener("click", () => openBrowse($("#scan-root").value.trim()));
  $("#start-scan").addEventListener("click", startScan);
  $("#type-chips").addEventListener("click", (event) => {
    const chip = event.target.closest(".chip");
    if (!chip) return;
    const type = chip.dataset.type;
    if (state.typeSelection.has(type)) state.typeSelection.delete(type);
    else state.typeSelection.add(type);
    chip.classList.toggle("active", state.typeSelection.has(type));
    chip.setAttribute("aria-pressed", state.typeSelection.has(type) ? "true" : "false");
  });
  $("#mode-group").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-mode]");
    if (!button) return;
    state.mode = button.dataset.mode;
    updateModeUI();
  });
  $("#refresh-recent").addEventListener("click", () => {
    refreshReports().catch((error) => toast(error.message, "error"));
  });
  $("#history-refresh").addEventListener("click", () => {
    refreshReports().catch((error) => toast(error.message, "error"));
  });
  $("#cache-refresh").addEventListener("click", () => loadCache());
  $("#cache-prune").addEventListener("click", async () => {
    const button = $("#cache-prune");
    button.disabled = true;
    try {
      const result = await api("/api/cache/prune", { method: "POST", body: "{}" });
      toast(`已清理 ${result.removed || 0} 条失效缓存`);
      await loadCache();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  });

  $("#back-scan").addEventListener("click", () => showView("scan"));
  $("#rescan").addEventListener("click", () => {
    if (state.report && state.report.root) $("#scan-root").value = state.report.root;
    showView("scan");
  });
  $("#apply-selected").addEventListener("click", openApplyConfirm);
  $("#apply-bottom").addEventListener("click", openApplyConfirm);
  $("#clear-selection").addEventListener("click", clearAllSelections);
  $("#select-all").addEventListener("click", selectAllFiltered);
  $("#clear-all").addEventListener("click", clearAllSelections);

  $("#search").addEventListener("input", () => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(() => {
      state.filter.search = $("#search").value;
      state.groupLimit = 200;
      renderGroups();
    }, 130);
  });
  $("#filter-kind").addEventListener("change", () => {
    state.filter.kind = $("#filter-kind").value;
    state.groupLimit = 200;
    renderGroups();
  });
  $("#show-moved").addEventListener("change", () => {
    state.showMoved = $("#show-moved").checked;
    state.groupLimit = 200;
    renderGroups();
  });

  $("#groups").addEventListener("change", (event) => {
    const target = event.target;
    if (target.classList.contains("dup-check")) {
      const groupId = target.dataset.group;
      const path = target.dataset.path;
      const selectedSet = state.selected.get(groupId) || new Set();
      if (target.checked) selectedSet.add(path);
      else selectedSet.delete(path);
      state.selected.set(groupId, selectedSet);
      updateGroupSelects();
      updateSelectionBar();
    } else if (target.classList.contains("group-select-input")) {
      setGroupSelection(target.dataset.group, target.checked);
    } else if (target.matches("[data-keep]")) {
      changeKeeper(target.dataset.group, target.dataset.keep);
    }
  });

  $("#cancel-job").addEventListener("click", cancelJob);
  window.addEventListener("beforeunload", (event) => {
    if (state.activeJobId) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
}

/* ------------------------------------------------------------------ format */

function groupById(groupId) {
  return ((state.report && state.report.groups) || []).find((group) => group.id === groupId) || null;
}

function groupMembers(group) {
  const members = [];
  if (group.keeper) members.push(group.keeper);
  for (const member of group.duplicates || []) members.push(member);
  return members;
}

function groupKindLabel(kind) {
  if (KIND_LABELS[kind]) return KIND_LABELS[kind];
  if (!kind) return "重复";
  const [adapter, reason] = kind.split(":");
  if (reason === "phash" || reason === "dhash") return "图片相似";
  if (reason === "simhash") return `${adapterLabel(adapter)}相似`;
  if (reason === "phash_seq") return "视频相似";
  if (reason === "chromaprint") return "音频相似";
  if (reason === "manifest") return "压缩包内容相同";
  return kind.replace(":", " · ");
}

function reasonText(member) {
  const reason = member.reason || "";
  if (reason === "exact") return "完全相同（SHA-256）";
  if (reason === "phash") return `pHash 距离 ${numberText(member.distance)}`;
  if (reason === "dhash") return `dHash 距离 ${numberText(member.distance)}`;
  if (reason === "simhash") return `SimHash 距离 ${numberText(member.distance)}`;
  if (reason === "phash_seq") return `视频帧差异 ${numberText(member.distance, 1)}`;
  if (reason === "chromaprint") {
    const score = Number(member.score);
    return Number.isFinite(score) ? `音频相似度 ${(score * 100).toFixed(1)}%` : "音频指纹相似";
  }
  if (reason === "manifest") return "内部清单一致";
  return reason || "相似";
}

function adapterLabel(name) {
  const info = state.typeInfo.find((item) => item.name === name);
  return info ? info.label : name || "文件";
}

function baseName(path) {
  const value = String(path || "");
  const parts = value.split(/[\\/]/);
  return parts[parts.length - 1] || value;
}

function numberText(value, digits) {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return String(value);
  if (digits !== undefined) return number.toFixed(digits);
  return Number.isInteger(number) ? String(number) : number.toFixed(2);
}

function formatBytes(value) {
  const bytes = Number(value) || 0;
  if (bytes <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let number = bytes;
  let unit = 0;
  while (number >= 1024 && unit < units.length - 1) {
    number /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? Math.round(number) : number.toFixed(1)} ${units[unit]}`;
}

function formatDate(value) {
  if (value === null || value === undefined || value === "") return "";
  let date;
  if (typeof value === "number") date = new Date(value / 1e6);
  else date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function esc(value) {
  return String(value === null || value === undefined ? "" : value).replace(
    /[&<>"']/g,
    (character) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[character]
  );
}

function cssEscape(value) {
  if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(String(value));
  return String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
}

function toast(message, type = "") {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast${type === "error" ? " error" : ""}`;
  element.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.add("hidden"), 4600);
}
