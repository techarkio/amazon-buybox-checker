"use strict";
// Page logic: collect ASINs and states, start a job, follow it, and show its results three ways: an overview of
// the whole list (winner, products to look at, losses by state, a paged product list), "by product" (pick one
// ASIN, see where its buybox is held), and one product in detail. The URL hash names the job and the view, so a
// copied link opens the same page. All text that comes from Amazon pages is inserted with textContent, never as HTML.

const MAP_DATA_URL = "https://cdn.jsdelivr.net/npm/us-atlas@3.0.1/states-albers-10m.json";
const MAP_WIDTH = 975;
const MAP_HEIGHT = 610;
const POLL_MS = 2500;
const SEARCH_DELAY_MS = 250;
const LIST_PER_PAGE = 25;
const PICK_PER_PAGE = 30;
const LARGE_LIST = 50; // from this many ASINs, Core + growth is picked for you (unless you chose states)
const ALL_STATES = 50;
const APP_HEADER = { "X-Buybox-Checker": "1" };
// Same rules as asin_input.py: a whole link (only its /dp/ product path counts, never numbers in its
// query string such as qid=1792485189), or a bare ASIN / ISBN-10 outside links.
const TOKEN_PATTERN = /(?:https?:\/\/|www\.|amazon\.)[^\s,;"'<>]+|(?<![A-Z0-9])(B0[A-Z0-9]{8}|\d{9}[\dX])(?![A-Z0-9])/gi;
const LINK_ASIN_PATTERN = /\/(?:dp|gp\/product|gp\/aw\/d|product)\/([A-Z0-9]{10})(?![A-Z0-9])/i;
const STATUS_LABELS = {
  ok: "Checked", not_found: "Not found", blocked: "Blocked by Amazon", timeout: "Timed out",
  zip_mismatch: "Wrong zipcode", error: "Error", not_checked: "Not checked",
};
const STATES_STORAGE_KEY = "buybox-checker.states"; // this browser remembers the last states picked
const STORE_STORAGE_KEY = "buybox-checker.store"; // and the answer to "Is this your store?"
const SELLER_COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)"];
const ATTENTION_GROUPS = ["other", "split", "none", "failed"];
// Small north-eastern states (and Hawaii) are labelled outside the map, with a line to the state.
const CALLOUTS = {
  VT: [855, 100, 866, 106], NH: [942, 152, 938, 152], MA: [942, 175, 938, 175], RI: [942, 198, 938, 198],
  CT: [942, 221, 938, 221], NJ: [942, 244, 938, 244], DE: [942, 267, 938, 267], MD: [942, 290, 938, 290],
  HI: [345, 568, 342, 572],
};
const PAGE_SECTIONS = new Set(["main", "contact"]); // in-page anchors: never a check id in the hash
const LABEL_AT = { FL: [798, 546], LA: [566, 478], MI: [688, 188], CA: [80, 292], KY: [692, 322], VA: [798, 306], ID: [200, 160] };

const app = {
  config: null, statesByCode: new Map(), presets: [], selected: new Set(), stateMode: "all",
  userPickedStates: false, autoPicked: false, fileText: "", parsed: { asins: [], duplicates: 0, skipped: [] },
  store: loadStore(), job: null, pollTimer: null, route: { view: "overview", asin: null },
  list: { group: null, touched: false, q: "", sort: "risk", page: 1, data: null, token: 0 },
  pick: { filter: "all", q: "", page: 1, data: null, asin: null, token: 0 },
  product: { data: null, token: 0 }, nav: null, shapesPromise: null, maps: new Map(),
};
const $ = (id) => document.getElementById(id);

document.addEventListener("DOMContentLoaded", init);

async function init() {
  try {
    app.config = await fetchJson("/api/config");
  } catch {
    showMessage("Could not reach the checker's server. Is buybox-checker-web still running?");
    return;
  }
  app.config.states.forEach((s) => app.statesByCode.set(s.code, s));
  setupStates();
  setupInput();
  setupResults();
  app.shapesPromise = loadShapes().catch(() => null);
  window.addEventListener("hashchange", onHashChange);
  // The hash names the check and view, so section links scroll instead of changing it.
  document.querySelectorAll("[data-section]").forEach((link) => link.addEventListener("click", scrollToSection));
  resumeJob();
}

function scrollToSection(event) {
  event.preventDefault();
  const target = $(event.currentTarget.dataset.section);
  target.scrollIntoView({ behavior: prefersMotion() ? "smooth" : "auto", block: "start" });
  target.focus({ preventScroll: true });
}

// ---------- Products to check ----------

function setupInput() {
  $("asin-text").addEventListener("input", updatePreview);
  $("asin-file").addEventListener("change", readChosenFile);
  $("file-clear").addEventListener("click", clearFile);
  $("check-form").addEventListener("submit", startJob);
  const max = app.config.max_asins;
  $("limit-count").textContent = `${fmt(max)} ${plural(max, "ASIN")}`;
  $("contact-limit").textContent = fmt(max);
  document.querySelectorAll(".contact-only").forEach((node) => { node.hidden = !app.config.contact_techark; });
  $("upload-limit").textContent = `(up to ${fmt(max)})`;
  $("check-first").textContent = `Check the first ${fmt(max)}`;
  $("check-first").addEventListener("click", () => startJob(null, app.parsed.asins.slice(0, max)));
  updatePreview();
}

function asinsIn(text) {
  const found = [];
  for (const token of text.matchAll(TOKEN_PATTERN)) {
    const asin = token[1] || (LINK_ASIN_PATTERN.exec(token[0]) || [])[1];
    if (asin) found.push(asin.toUpperCase());
  }
  return found;
}

// Count what the text holds: unique ASINs, duplicates removed, and lines with no ASIN at all.
function parseInput() {
  const all = [];
  const skipped = [];
  for (const line of `${$("asin-text").value}\n${app.fileText}`.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const found = asinsIn(trimmed);
    if (found.length) all.push(...found);
    else skipped.push(trimmed);
  }
  const asins = [...new Set(all)];
  return { asins, duplicates: all.length - asins.length, skipped };
}

function updatePreview() {
  app.parsed = parseInput();
  const { asins, duplicates, skipped } = app.parsed;
  const max = app.config ? app.config.max_asins : Infinity;
  const summary = $("input-summary");
  const parts = asins.length ? [el("b", {}, `${fmt(asins.length)} ${plural(asins.length, "ASIN")}`), " found"] : ["No ASINs yet"];
  if (duplicates) parts.push(` · ${fmt(duplicates)} ${plural(duplicates, "duplicate")} removed`);
  if (skipped.length) parts.push(` · ${fmt(skipped.length)} ${plural(skipped.length, "line")} without an ASIN`);
  if (asins.length > max) parts.push(` · at most ${fmt(max)} per check`);
  summary.replaceChildren(...parts);
  summary.classList.toggle("too-many", asins.length > max);

  $("input-details").hidden = asins.length < 2 && !skipped.length;
  $("asin-list-details").hidden = asins.length < 2;
  $("asin-list-summary").textContent = `Show the ${fmt(asins.length)} ASINs`;
  $("asin-list").replaceChildren(...asins.slice(0, 1000).map((asin) => el("li", {}, asin)));
  $("skipped-details").hidden = !skipped.length;
  $("skipped-summary").textContent = `See the ${fmt(skipped.length)} ${plural(skipped.length, "line")} without an ASIN`;
  $("skipped-list").replaceChildren(...skipped.slice(0, 50).map((line) => el("li", {}, line.slice(0, 80))));
  autoPickStates();
  updateEstimate();
}

async function readChosenFile() {
  const file = $("asin-file").files[0];
  app.fileText = file ? await file.text() : "";
  $("file-name").textContent = file ? file.name : "";
  $("file-clear").hidden = !file;
  updatePreview();
}

function clearFile() {
  $("asin-file").value = "";
  return readChosenFile();
}

// ---------- States ----------
// The presets come from the server (core markets, core + growth). Picking states by hand that match a preset
// selects that preset; any other selection is "Custom". A long list gets Core + growth unless you chose states.

function setupStates() {
  const allCodes = app.config.states.map((s) => s.code);
  app.presets = [{ key: "all", name: "All 50 states", codes: allCodes }, ...app.config.presets,
    { key: "custom", name: "Custom", codes: null }];

  const group = $("state-presets");
  for (const preset of app.presets) {
    const input = el("input", { type: "radio", name: "state-preset", value: preset.key });
    input.addEventListener("change", () => choosePreset(preset.key));
    group.append(el("label", { className: "preset" }, input, el("span", { className: "preset-name" }, preset.name)));
  }

  const tiered = new Set(app.config.tiers.flatMap((tier) => tier.codes));
  const groups = [...app.config.tiers, { name: "Long tail", codes: allCodes.filter((code) => !tiered.has(code)) }];
  const container = $("state-groups");
  for (const stateGroup of groups) {
    const toggle = el("button", { className: "link-button group-toggle", type: "button" });
    toggle.addEventListener("click", () => toggleGroup(stateGroup.codes));
    toggle.dataset.codes = stateGroup.codes.join(" ");
    const chips = el("div", { className: "chips" });
    for (const code of stateGroup.codes) {
      const info = app.statesByCode.get(code);
      const chip = el("button", { className: "chip", type: "button", "data-code": code }, el("b", {}, code), el("span", {}, info.name));
      chip.title = `${info.name} · zipcode ${info.zipcode}`;
      chip.addEventListener("click", () => toggleState(code));
      chips.append(chip);
    }
    container.append(el("div", { className: "state-group", role: "group", "aria-label": stateGroup.name },
      el("div", { className: "group-head" }, el("span", { className: "group-title" }, stateGroup.name), toggle), chips));
  }
  $("states-all").addEventListener("click", () => choosePreset("all"));
  $("states-none").addEventListener("click", () => { markPicked(); setSelection([]); });
  $("use-all-states").addEventListener("click", () => choosePreset("all"));
  setSelection(loadSavedStates() || allCodes, { save: false });
}

function markPicked() {
  app.userPickedStates = true;
  app.autoPicked = false;
}

function choosePreset(key) {
  markPicked();
  const preset = app.presets.find((p) => p.key === key);
  if (preset.codes) {
    setSelection(preset.codes);
  } else {
    app.stateMode = "custom";
    $("state-picker").open = true;
    syncStates();
  }
}

function toggleState(code) {
  markPicked();
  const next = new Set(app.selected);
  next.has(code) ? next.delete(code) : next.add(code);
  setSelection([...next]);
}

function toggleGroup(codes) {
  markPicked();
  const allIn = codes.every((code) => app.selected.has(code));
  const next = new Set(app.selected);
  codes.forEach((code) => (allIn ? next.delete(code) : next.add(code)));
  setSelection([...next]);
}

function setSelection(codes, { save = true } = {}) {
  app.selected = new Set(codes);
  app.stateMode = presetFor(codes) || "custom";
  if (save) saveStates();
  syncStates();
}

function presetFor(codes) {
  const set = new Set(codes);
  const match = app.presets.find((p) => p.codes && p.codes.length === set.size && p.codes.every((c) => set.has(c)));
  return match ? match.key : null;
}

function autoPickStates() {
  const coreGrowth = app.presets.find((p) => p.key === "core-growth");
  if (coreGrowth && app.parsed.asins.length >= LARGE_LIST && !app.userPickedStates && app.stateMode === "all") {
    app.autoPicked = true;
    setSelection(coreGrowth.codes, { save: false });
  }
  $("large-list-note").hidden = !(app.autoPicked && app.stateMode === "core-growth");
}

function syncStates() {
  const count = app.selected.size;
  document.querySelectorAll('input[name="state-preset"]').forEach((input) => { input.checked = input.value === app.stateMode; });
  document.querySelectorAll(".chip").forEach((chip) => chip.setAttribute("aria-pressed", String(app.selected.has(chip.dataset.code))));
  document.querySelectorAll(".group-toggle").forEach((toggle) => {
    toggle.textContent = toggle.dataset.codes.split(" ").every((code) => app.selected.has(code)) ? "Remove all" : "Add all";
  });
  $("state-picker-summary").textContent = count === 0 ? "Choose states" : `See or change the ${count} selected ${plural(count, "state")}`;
  $("state-count").textContent = `${count} of ${app.statesByCode.size} selected`;
  $("large-list-note").hidden = !(app.autoPicked && app.stateMode === "core-growth");
  updateEstimate();
}

function loadSavedStates() {
  try {
    const codes = JSON.parse(localStorage.getItem(STATES_STORAGE_KEY) || "null");
    const valid = Array.isArray(codes) ? codes.filter((code) => app.statesByCode.has(code)) : [];
    return valid.length ? valid : null;
  } catch {
    return null;
  }
}

function saveStates() {
  try {
    localStorage.setItem(STATES_STORAGE_KEY, JSON.stringify([...app.selected]));
  } catch {
    // storage blocked: the selection just isn't remembered
  }
}

// ---------- Estimate and start ----------

function checkMinutes(checks) {
  const sessions = Math.max(1, app.config.default_sessions + app.config.remote_sessions); // sized by the server
  return (checks * app.config.seconds_per_check) / sessions / 60;
}

function updateEstimate() {
  if (!app.config || !app.presets.length) return;
  const states = app.selected.size;
  const count = app.parsed.asins.length;
  const main = $("check-estimate");
  const more = $("check-estimate-more");
  more.textContent = "";
  syncStartButtons();
  const max = app.config.max_asins;
  if (states === 0) {
    main.replaceChildren("Pick at least one state.");
  } else if (count > max) {
    main.replaceChildren(el("b", {}, `${fmt(count)} ASINs found.`), ` Remove ${fmt(count - max)}, or check the first ${fmt(max)}.`);
    if (app.config.contact_techark) {
      const contact = el("a", { href: "#contact", "data-section": "contact" }, "contact TECHARK");
      contact.addEventListener("click", scrollToSection);
      more.replaceChildren(`Only ${fmt(max)} ASINs are checked at a time. To check all of them, `, contact, ".");
    } else {
      more.textContent = `Only ${fmt(max)} ASINs are checked at a time. Split the list into several checks.`;
    }
  } else if (count === 0) {
    main.replaceChildren(el("b", {}, `${states} ${plural(states, "state")}`), " selected · add products to see how long the check takes");
  } else {
    const checks = count * states;
    main.replaceChildren(el("b", {}, `${fmt(count)} ${plural(count, "ASIN")} × ${states} ${plural(states, "state")} = ${fmt(checks)} ${plural(checks, "check")}`),
      checkMinutes(checks) < 1 ? " · " : " · about ", el("b", {}, formatMinutes(checkMinutes(checks))));
    const notes = [];
    const allMinutes = checkMinutes(count * ALL_STATES);
    if (states < ALL_STATES) notes.push(`All 50 states would be ${fmt(count * ALL_STATES)} checks, ${allMinutes < 1 ? "" : "about "}${formatMinutes(allMinutes)}.`);
    if (checkMinutes(checks) > 3) notes.push("You can close the page once the check starts.");
    more.textContent = notes.join(" ");
  }
}

const overLimit = () => app.parsed.asins.length > app.config.max_asins;

// Above the limit, "Check buybox" gives way to "Check the first N"; both wait while a start is in flight.
function syncStartButtons() {
  const over = overLimit();
  $("start-button").hidden = over;
  $("check-first").hidden = !over;
  $("start-button").disabled = Boolean(app.starting);
  $("check-first").disabled = Boolean(app.starting);
}

// firstAsins: check only these (the first N of a list that is over the limit).
async function startJob(event, firstAsins = null) {
  if (event) event.preventDefault();
  hideMessage();
  if (!firstAsins && overLimit()) return;
  if (app.selected.size === 0) {
    showMessage("Pick at least one state, or choose All 50 states.");
    return;
  }
  const form = new FormData();
  form.append("text", firstAsins ? firstAsins.join("\n") : $("asin-text").value);
  form.append("states", [...app.selected].join(" "));
  if (!firstAsins && $("asin-file").files[0]) form.append("file", $("asin-file").files[0]);
  app.starting = true;
  syncStartButtons();
  try {
    const response = await fetch("/api/jobs", { method: "POST", body: form, headers: APP_HEADER });
    const data = await response.json();
    if (response.status === 409) {
      showMessage(data.detail, { action: { label: "Watch it", onClick: watchLatest } });
      return;
    }
    if (!response.ok) throw new Error(data.detail || "Could not start the check");
    if (data.skipped_short_links.length) {
      showMessage(`Skipped ${data.skipped_short_links.length} short link(s) (amzn.to / a.co): open them and paste the full amazon.com link.`, { info: true });
    }
    resetViews();
    app.job = data.job;
    location.hash = defaultHash(data.job);
    $("job-section").hidden = false;
    $("job-section").scrollIntoView({ behavior: prefersMotion() ? "smooth" : "auto", block: "start" });
  } catch (error) {
    showMessage(error.message);
  } finally {
    app.starting = false;
    syncStartButtons();
  }
}

async function watchLatest() {
  hideMessage();
  try {
    const job = await fetchJson(api("/api/jobs/latest"));
    resetViews();
    app.job = job;
    location.hash = defaultHash(job);
  } catch {
    showMessage("Could not load the running check.");
  }
}

// Fill the form with a job's products and states, for "run again" and "check the other states".
async function prefillForm(asins, codes) {
  await clearFile();
  $("asin-text").value = asins.join("\n");
  markPicked();
  setSelection(codes);
  $("state-picker").open = app.stateMode === "custom";
  updatePreview();
  $("form-card").scrollIntoView({ behavior: prefersMotion() ? "smooth" : "auto", block: "start" });
  $("start-button").focus({ preventScroll: true });
}

// ---------- Following a job ----------

function setupResults() {
  $("cancel-button").addEventListener("click", cancelJob);
  $("retry-button").addEventListener("click", retryJob);
  $("new-check-button").addEventListener("click", () => {
    $("form-card").scrollIntoView({ behavior: prefersMotion() ? "smooth" : "auto", block: "start" });
    $("asin-text").focus({ preventScroll: true });
  });
  document.querySelectorAll(".copy-link").forEach((button) => button.addEventListener("click", () => copyText(location.href, button)));
  $("store-yes").addEventListener("click", () => answerStore(true));
  $("store-no").addEventListener("click", () => answerStore(false));
  $("list-search").addEventListener("input", debounce(() => {
    app.list.q = $("list-search").value;
    app.list.page = 1;
    refreshList();
  }));
  $("list-sort").addEventListener("change", () => {
    app.list.sort = $("list-sort").value;
    app.list.page = 1;
    refreshList();
  });
  $("pick-search").addEventListener("input", debounce(() => {
    app.pick.q = $("pick-search").value;
    app.pick.page = 1;
    refreshPick();
  }));
}

function resetViews() {
  app.list = { group: null, touched: false, q: "", sort: "risk", page: 1, data: null, token: 0 };
  app.pick = { filter: "all", q: "", page: 1, data: null, asin: null, token: 0 };
  app.product = { data: null, token: 0 };
  app.nav = null;
  $("list-search").value = "";
  $("pick-search").value = "";
  $("list-sort").value = "risk";
}

async function resumeJob() {
  const route = parseHash();
  try {
    if (route.id) {
      app.job = await fetchJson(api(`/api/jobs/${encodeURIComponent(route.id)}`));
    } else {
      app.job = await fetchJson(api("/api/jobs/latest"));
      history.replaceState(null, "", defaultHash(app.job));
    }
  } catch {
    return; // no job yet, or it was deleted: nothing to show
  }
  $("job-section").hidden = false;
  applyRoute();
}

function parseHash() {
  const [id, view, asin] = location.hash.slice(1).split("/");
  if (PAGE_SECTIONS.has(id)) return { id: null, view: "overview", asin: null };
  return { id: id || null, view: view === "product" ? "product" : view === "by-product" ? "byproduct" : "overview", asin: asin || null };
}

function defaultHash(job) {
  return job.asins.length === 1 ? `#${job.id}/product/${job.asins[0]}` : `#${job.id}`;
}

async function onHashChange() {
  const route = parseHash();
  if (!route.id) return;
  if (!app.job || app.job.id !== route.id) {
    try {
      resetViews();
      app.job = await fetchJson(api(`/api/jobs/${encodeURIComponent(route.id)}`));
      $("job-section").hidden = false;
    } catch {
      showMessage("No check with this link (checks are kept for a while, then removed).");
      return;
    }
  }
  applyRoute();
}

function applyRoute() {
  const route = parseHash();
  const job = app.job;
  if (job.asins.length === 1) route.view = "product";
  if (route.view === "product" && !job.asins.includes(route.asin)) route.asin = job.asins[0];
  app.route = { view: route.view, asin: route.asin };
  renderJob();
  refreshViewData();
  pollSoon();
}

function pollSoon() {
  clearTimeout(app.pollTimer);
  if (!app.job || app.job.status !== "running") return;
  app.pollTimer = setTimeout(refreshJob, POLL_MS);
}

async function refreshJob() {
  const id = app.job.id;
  try {
    const job = await fetchJson(api(`/api/jobs/${id}`));
    if (!app.job || app.job.id !== id) return;
    app.job = job;
    renderJob();
    refreshViewData();
  } catch {
    $("job-meta").textContent = "Lost contact with the server. Is buybox-checker-web still running?";
  }
  pollSoon();
}

function refreshViewData() {
  if (app.route.view === "overview") refreshList();
  else if (app.route.view === "byproduct") refreshPick();
  else loadProduct(app.route.asin);
}

async function cancelJob() {
  $("cancel-button").disabled = true;
  await fetch(`/api/jobs/${app.job.id}/cancel`, { method: "POST", headers: APP_HEADER });
  $("job-meta").textContent = "Stopping after the checks in progress…";
}

async function retryJob() {
  $("retry-button").disabled = true;
  try {
    const response = await fetch(`/api/jobs/${app.job.id}/retry`, { method: "POST", headers: APP_HEADER });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not retry");
    app.job = data;
    renderJob();
    refreshViewData();
    pollSoon();
  } catch (error) {
    showJobError(error.message);
  } finally {
    $("retry-button").disabled = false;
  }
}

// ---------- Your store ----------

// { mine: the user's seller name or "", declined: sellers the user said are not theirs }
function loadStore() {
  try {
    const store = JSON.parse(localStorage.getItem(STORE_STORAGE_KEY) || "null");
    if (store && typeof store.mine === "string" && Array.isArray(store.declined)) return store;
  } catch {
    // storage blocked or unreadable: start with no answer
  }
  return { mine: "", declined: [] };
}

function saveStore() {
  try {
    localStorage.setItem(STORE_STORAGE_KEY, JSON.stringify(app.store));
  } catch {
    // storage blocked: the answer lasts until the page is closed
  }
}

const storeParam = () => app.store.mine;
const isMine = () => Boolean(app.job && app.job.overview.reference_is_store);
const sameSeller = (a, b) => a.trim().toLowerCase() === b.trim().toLowerCase();
const declined = (seller) => app.store.declined.some((name) => sameSeller(name, seller));

async function answerStore(mine) {
  const seller = app.job.overview.reference;
  if (mine) {
    app.store.mine = seller;
    app.store.declined = app.store.declined.filter((name) => !sameSeller(name, seller));
  } else if (!declined(seller)) {
    app.store.declined.push(seller);
  }
  saveStore();
  await reloadJob();
}

async function forgetStore() {
  app.store.mine = "";
  saveStore();
  await reloadJob();
}

async function reloadJob() {
  app.job = await fetchJson(api(`/api/jobs/${app.job.id}`));
  app.list.page = 1;
  renderJob();
  refreshViewData();
}

// ---------- Rendering the job ----------

function renderJob() {
  const job = app.job;
  const view = app.route.view;
  renderCheckbar(job);
  renderProgress(job);
  renderStorePrompt(job);
  renderViewSwitch(job);
  $("overview-view").hidden = view !== "overview";
  $("byproduct-view").hidden = view !== "byproduct";
  $("product-view").hidden = view !== "product";
  if (view === "overview") renderOverview(job);
}

function statesLabel(codes) {
  const preset = presetFor(codes);
  return preset ? app.presets.find((p) => p.key === preset).name : `${codes.length} ${plural(codes.length, "state")}`;
}

function renderCheckbar(job) {
  const products = job.asins.length;
  $("job-title").textContent = `${fmt(products)} ${plural(products, "product")} · ${statesLabel(job.states)}`;
  const counts = `${fmt(job.done)} of ${fmt(job.total)} checks done`;
  const text = {
    running: `Started ${timeOf(job.started_at)} · ${counts}`,
    done: `Finished ${timeOf(job.finished_at)} · ${counts}${job.retryable ? `, ${fmt(job.retryable)} could not be checked` : ""}`,
    cancelled: `Stopped ${timeOf(job.finished_at)} · ${counts}`,
    failed: `Stopped by an error · ${counts}`,
    interrupted: `Interrupted · ${counts}`,
  }[job.status] || counts;
  const meta = [text];
  if (isMine()) {
    const change = el("button", { className: "link-button", type: "button" }, "change");
    change.addEventListener("click", forgetStore);
    meta.push(" · your store: ", el("b", {}, job.overview.reference), " · ", change);
  }
  $("job-meta").replaceChildren(...meta);
  $("cancel-button").hidden = job.status !== "running";
  $("cancel-button").disabled = false;
  $("retry-button").hidden = job.status === "running" || !job.retryable;
  $("retry-button").textContent = `Retry ${fmt(job.retryable)} failed ${plural(job.retryable, "check")}`;
  $("csv-link").hidden = job.done === 0;
  $("csv-link").href = `/api/jobs/${job.id}/csv`;
  if (job.error) showJobError(job.error);
  else $("job-error").hidden = true;
}

function showJobError(text) {
  $("job-error").textContent = text;
  $("job-error").hidden = false;
}

function renderProgress(job) {
  const running = job.status === "running";
  $("progress-panel").hidden = !running;
  if (!running) return;
  const run = job.run.total ? job.run : { done: job.done, total: job.total };
  const pct = run.total ? Math.floor((100 * Math.min(run.done, run.total)) / run.total) : 0;
  const retrying = run.total < job.total;
  $("progress-title").textContent = `${retrying ? "Retrying failed checks" : "Checking"}… ${pct}% done`;
  $("progress-bar").style.width = `${pct}%`;
  $("progress").setAttribute("aria-valuenow", String(pct));
  $("tile-products").textContent = `${fmt(job.overview.products_done)} of ${fmt(job.overview.products)}`;
  $("tile-checks").textContent = `${fmt(Math.min(run.done, run.total))} of ${fmt(run.total)}`;
  const eta = job.eta_seconds;
  $("tile-eta").textContent = eta == null ? "–" : eta < 60 ? "under a minute" : `about ${formatMinutes(eta / 60)}`;
}

function renderStorePrompt(job) {
  const ov = job.overview;
  const show = job.asins.length > 1 && Boolean(ov.reference) && !ov.reference_is_store && !declined(ov.reference);
  $("store-prompt").hidden = !show;
  if (show) $("store-name").textContent = ov.reference;
}

function byProductAvailable(job) {
  return job.asins.length > 1 && (job.overview.reference_is_store || job.overview.one_seller_list);
}

function renderViewSwitch(job) {
  const show = byProductAvailable(job) && app.route.view !== "product";
  $("view-switch").hidden = !show;
  $("view-overview").href = `#${job.id}`;
  $("view-byproduct").href = `#${job.id}/by-product`;
  $("view-overview").removeAttribute("aria-current");
  $("view-byproduct").removeAttribute("aria-current");
  if (app.route.view === "overview") $("view-overview").setAttribute("aria-current", "page");
  if (app.route.view === "byproduct") $("view-byproduct").setAttribute("aria-current", "page");
}

function groupLabel(group) {
  const mine = isMine();
  return {
    other: mine ? "Another seller holds every state" : "Another seller in every state",
    split: "Split between sellers", none: "No buybox somewhere", failed: "Could not check every state",
    one: mine ? "You win everywhere" : "Top winner everywhere", pending: "Still being checked", all: "All products",
  }[group];
}

// ---------- Overview ----------

function renderOverview(job) {
  const ov = job.overview;
  if (!app.list.touched) app.list.group = ATTENTION_GROUPS.find((g) => ov.groups[g]) || "all"; // until a tab is picked
  const allClear = ov.all_clear && job.status === "done";
  $("all-clear").hidden = !allClear;
  $("winner-section").hidden = allClear || job.done === 0;
  $("loss-section").hidden = allClear || !ov.reference;
  $("recent-section").hidden = job.status !== "running" || !job.recent.length;
  if (allClear) renderAllClear(job);
  else {
    renderWinner(job);
    renderLosses(job);
  }
  renderAttention(job);
  renderTabs(job);
  renderRecent(job);
}

function renderAllClear(job) {
  const ov = job.overview;
  const products = `${fmt(ov.products)} ${plural(ov.products, "product")}`;
  const states = `${job.states.length} ${plural(job.states.length, "state")}`;
  $("all-clear-title").textContent = isMine()
    ? `You hold the buybox for all ${products} in all ${states}.`
    : `${ov.reference} holds the buybox for all ${products} in all ${states}.`;
  const missing = app.config.states.map((s) => s.code).filter((code) => !job.states.includes(code));
  $("next-more-states").hidden = !missing.length;
  if (missing.length) {
    const checks = job.asins.length * missing.length;
    $("next-more-text").textContent = `Same ${products} in the ${missing.length} states this check skipped: ${fmt(checks)} more checks, about ${formatMinutes(checkMinutes(checks))}.`;
    $("next-more-button").textContent = `Check the other ${missing.length} states`;
    $("next-more-button").onclick = () => prefillForm(job.asins, missing);
  }
  $("next-again-button").onclick = () => prefillForm(job.asins, job.states);
  $("next-csv").href = `/api/jobs/${job.id}/csv`;
}

function sellerColors(sellers, reference, mine) {
  const colors = new Map();
  let slot = mine && !sellers.some((s) => sameSeller(s, reference)) ? 1 : 0;
  for (const seller of sellers) colors.set(seller, slot < SELLER_COLORS.length ? SELLER_COLORS[slot++] : "var(--other)");
  return colors;
}

function renderWinner(job) {
  const ov = job.overview;
  const running = job.status === "running";
  $("hero-label").textContent = `Top buybox winner across ${fmt(ov.products)} ${plural(ov.products, "product")}${running ? ", so far" : ""}`;
  const checks = ov.checks;
  const finished = checks.total - checks.pending;
  $("hero-name").textContent = ov.reference || "Nobody";
  $("hero-badge").hidden = !isMine();
  if (ov.reference) {
    const pct = finished ? Math.round((100 * checks.win) / finished) : 0;
    $("hero-sentence").replaceChildren("holds the buybox in ", el("b", {}, `${pct}% of checks`),
      `: ${fmt(checks.win)} of ${fmt(finished)} product-state pairs${running ? " checked so far" : ""}`);
  } else {
    $("hero-sentence").textContent = "No seller holds the buybox in the states checked so far.";
  }
  const colors = sellerColors(ov.sellers.map((s) => s.seller), ov.reference, isMine());
  const segments = ov.sellers.map((s) => [s.seller, s.checks, colors.get(s.seller), ""]);
  segments.push(["Other sellers", ov.other_sellers_checks, "var(--other)", ""],
    ["No buybox", checks.none, null, "fill-hatch"], ["Not checked", checks.failed, null, "fill-failed"],
    ["Still to check", checks.pending, null, "fill-pending"]);
  renderShare($("share-bar"), $("share-legend"), segments);
}

// segments: [label, count, color, className]
function renderShare(bar, legend, segments) {
  const shown = segments.filter((segment) => segment[1] > 0);
  bar.replaceChildren(...shown.map(([, count, color, className]) => {
    const span = el("span", { className });
    span.style.flex = String(count);
    if (color) span.style.background = color;
    return span;
  }));
  bar.setAttribute("aria-label", shown.map(([label, count]) => `${label} ${fmt(count)}`).join(", "));
  legend.replaceChildren(...shown.map(([label, count, color, className]) => {
    const swatch = el("span", { className: `swatch ${className}` });
    if (color) swatch.style.background = color;
    return el("span", { className: "legend-item" }, swatch, `${label} ${fmt(count)}`);
  }));
}

function renderAttention(job) {
  const groups = [...ATTENTION_GROUPS, "one"];
  if (job.overview.groups.pending) groups.push("pending");
  $("attention-list").replaceChildren(...groups.map((group) => {
    const count = job.overview.groups[group];
    const card = el("button", { className: `attention-card${count ? "" : " zero"}`, type: "button" },
      el("b", {}, fmt(count)), el("span", {}, groupLabel(group)));
    card.setAttribute("aria-pressed", String(app.list.group === group));
    card.addEventListener("click", () => {
      setListGroup(group);
      $("list-section").scrollIntoView({ behavior: prefersMotion() ? "smooth" : "auto", block: "start" });
    });
    return card;
  }));
}

function lossLevels(max) {
  const b1 = Math.max(1, Math.floor(max / 3));
  const b2 = Math.max(b1 + 1, Math.floor((2 * max) / 3));
  return {
    level: (n) => (n === 0 ? 0 : n <= b1 ? 1 : n <= b2 ? 2 : 3),
    labels: ["none", b1 === 1 ? "1" : `1–${b1}`, b2 === b1 + 1 ? `${b2}` : `${b1 + 1}–${b2}`, `${b2 + 1}+`],
  };
}

async function renderLosses(job) {
  const ov = job.overview;
  $("loss-title").textContent = isMine() ? "Where you lose the most" : `Where ${ov.reference} loses the most`;
  const byState = new Map(ov.losses.map((loss) => [loss.state, loss]));
  const max = Math.max(0, ...ov.losses.map((loss) => loss.lost));
  const { level, labels } = lossLevels(max);
  $("loss-legend").replaceChildren(
    el("div", { className: "ramp" }, ...[0, 1, 2, 3].map((i) => { const s = el("span"); s.style.background = `var(--loss-${i})`; return s; })),
    el("div", { className: "ramp-labels" }, ...labels.map((label) => el("span", {}, label))));
  const worst = [...ov.losses].filter((loss) => loss.lost > 0).sort((a, b) => b.lost - a.lost).slice(0, 3);
  $("loss-list").replaceChildren(...(worst.length ? worst.map((loss) => el("p", {},
    el("span", {}, stateName(loss.state)),
    el("span", {}, el("b", {}, fmt(loss.lost)), loss.main_taker ? ` · mostly to ${loss.main_taker}` : " · no buybox"))) : [el("p", {}, "No products lost in any state.")]));
  const map = await getMap("loss-map");
  if (!map) return;
  paintMap(map, (code) => {
    const loss = byState.get(code);
    if (!loss) return null;
    const i = level(loss.lost);
    return { color: `var(--loss-${i})`, ink: `var(--loss-${i}-ink)`, text: `${fmt(loss.lost)} products lost` };
  }, (code) => {
    const loss = byState.get(code);
    if (!loss) return null;
    return [stateName(code), [["Products lost", fmt(loss.lost)], ["Mostly to", loss.main_taker]]];
  });
}

function renderRecent(job) {
  if ($("recent-section").hidden) return;
  $("recent-list").replaceChildren(...job.recent.map((product) => el("div", { className: "recent-row" },
    el("span", { className: "name" }, productLink(product), el("span", { className: "asin" }, product.asin)),
    strip(product, "small"), el("span", { className: "summary" }, cellSummary(product)))));
}

function renderTabs(job) {
  const groups = [...ATTENTION_GROUPS, "one"];
  if (job.overview.groups.pending) groups.push("pending");
  groups.push("all");
  $("list-tabs").replaceChildren(...groups.map((group) => {
    const count = group === "all" ? job.overview.products : job.overview.groups[group];
    const tab = el("button", { className: "tab", type: "button", role: "tab" }, groupLabel(group), " ", el("b", {}, fmt(count)));
    tab.setAttribute("aria-selected", String(app.list.group === group));
    tab.addEventListener("click", () => setListGroup(group));
    return tab;
  }));
}

function setListGroup(group) {
  app.list.group = group;
  app.list.touched = true;
  app.list.page = 1;
  renderAttention(app.job);
  renderTabs(app.job);
  refreshList();
}

async function refreshList() {
  if (!app.job || app.route.view !== "overview") return;
  if (!app.list.group) app.list.group = "all";
  const token = ++app.list.token;
  const { group, q, sort, page } = app.list;
  try {
    const data = await fetchJson(api(`/api/jobs/${app.job.id}/products`, { group, q, sort, page, per_page: LIST_PER_PAGE }));
    if (token !== app.list.token) return;
    app.list.data = data;
    app.nav = { asins: data.asins, label: groupLabel(group) };
    renderList();
  } catch {
    // the next poll or click tries again
  }
}

function renderList() {
  const data = app.list.data;
  const states = app.job.states;
  $("strip-head").textContent = `${states.length} ${plural(states.length, "state")} · ${states[0]} → ${states[states.length - 1]}`;
  const body = $("product-table").querySelector("tbody");
  if (!data.items.length) {
    body.replaceChildren(el("tr", {}, el("td", { colSpan: 5, className: "empty" }, app.list.q ? "No product matches this search." : "No products in this group.")));
  } else {
    body.replaceChildren(...data.items.map((product) => {
      const top = product.top_seller;
      const topCount = product.cells.filter((cell) => cell.seller === top).length;
      const dot = el("span", { className: `swatch ${top && sameSeller(top, app.job.overview.reference) ? "k-win" : "k-other"}` });
      return el("tr", {},
        el("th", { scope: "row" }, productLink(product), el("span", { className: "asin" }, product.asin)),
        el("td", {}, top ? el("span", { className: "dot-name" }, dot, el("b", {}, top), el("span", { className: "muted" }, ` ${topCount}/${product.total}`)) : "Nobody"),
        el("td", {}, strip(product)),
        el("td", { className: "muted" }, everyoneElse(product)),
        el("td", { className: "num" }, product.price || "–"));
    }));
  }
  renderPager($("list-pager"), data, (page) => { app.list.page = page; refreshList(); });
}

function renderPager(box, data, goTo) {
  if (data.total === 0) {
    box.replaceChildren();
    return;
  }
  const first = (data.page - 1) * data.per_page + 1;
  const last = Math.min(data.total, data.page * data.per_page);
  const prev = el("button", { className: "button secondary small", type: "button" }, "Previous");
  const next = el("button", { className: "button secondary small", type: "button" }, "Next");
  prev.disabled = data.page <= 1;
  next.disabled = data.page >= data.pages;
  prev.addEventListener("click", () => goTo(data.page - 1));
  next.addEventListener("click", () => goTo(data.page + 1));
  box.replaceChildren(el("span", { className: "muted" }, `Showing ${fmt(first)}–${fmt(last)} of ${fmt(data.total)}`),
    el("span", { className: "buttons" }, prev, next));
}

function productLink(product) {
  const link = el("a", { className: "product-link", href: `#${app.job.id}/product/${product.asin}` }, product.title || product.asin);
  return product.not_found ? el("span", {}, link, el("span", { className: "flag" }, "Not found on Amazon")) : link;
}

function strip(product, size = "") {
  const box = el("span", { className: `strip ${size}`, role: "img" });
  box.setAttribute("aria-label", `${product.title || product.asin}: ${cellSummary(product)}`);
  box.append(...product.cells.map((cell) => {
    const span = el("span", { className: `k-${cell.kind}` });
    span.title = `${stateName(cell.state)}: ${describeCell(cell)}`;
    return span;
  }));
  return box;
}

function describeCell(cell) {
  if (cell.seller) return `${cell.seller}${cell.price ? `, ${cell.price}` : ""}`;
  return { none: "no buybox", failed: STATUS_LABELS[cell.status] || "could not check", pending: "not checked yet" }[cell.kind] || "";
}

function cellSummary(product) {
  const sellers = new Map();
  let none = 0;
  let failed = 0;
  let pending = 0;
  for (const cell of product.cells) {
    if (cell.seller) sellers.set(cell.seller, (sellers.get(cell.seller) || 0) + 1);
    else if (cell.kind === "none") none += 1;
    else if (cell.kind === "failed") failed += 1;
    else pending += 1;
  }
  const parts = [...sellers].sort((a, b) => b[1] - a[1]).map(([seller, count]) => `${seller} ${count}`);
  if (none) parts.push(`no buybox ${none}`);
  if (failed) parts.push(`${failed} not checked`);
  if (pending) parts.push(`${pending} waiting`);
  return parts.join(" · ");
}

function everyoneElse(product) {
  const groups = new Map();
  for (const cell of product.cells) {
    if (cell.seller === product.top_seller && cell.seller) continue;
    const key = cell.seller || { none: "No buybox", failed: "Not checked", pending: "Waiting" }[cell.kind];
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(cell.state);
  }
  return [...groups].map(([key, codes]) => `${key}: ${codes.join(", ")}`).join(" · ") || "–";
}

// ---------- By product ----------

async function refreshPick() {
  if (!app.job || app.route.view !== "byproduct") return;
  const token = ++app.pick.token;
  const { filter, q, page } = app.pick;
  try {
    const data = await fetchJson(api(`/api/jobs/${app.job.id}/products`, {
      group: filter === "lose" ? "attention" : "all", q, sort: "risk", page, per_page: PICK_PER_PAGE,
    }));
    if (token !== app.pick.token) return;
    app.pick.data = data;
    if (!app.pick.asin || !data.asins.includes(app.pick.asin)) app.pick.asin = data.items.length ? data.items[0].asin : null;
    app.nav = { asins: data.asins, label: filter === "lose" ? "Products you lose somewhere" : "By product" };
    renderPickList();
    if (app.pick.asin) loadPick(app.pick.asin);
  } catch {
    // the next poll or click tries again
  }
}

function renderPickList() {
  const data = app.pick.data;
  const filters = [["all", "All products"], ["lose", isMine() ? "You lose somewhere" : "Lost somewhere"]];
  $("pick-filters").replaceChildren(...filters.map(([key, label]) => {
    const button = el("button", { className: "tab", type: "button" }, label);
    button.setAttribute("aria-pressed", String(app.pick.filter === key));
    if (app.pick.filter === key) button.setAttribute("aria-selected", "true");
    button.addEventListener("click", () => { app.pick.filter = key; app.pick.page = 1; refreshPick(); });
    return button;
  }));
  if (!data.items.length) {
    $("pick-items").replaceChildren(el("p", { className: "empty" }, app.pick.q ? "No product matches. Try part of the ASIN." : "No products here."));
  } else {
    $("pick-items").replaceChildren(...data.items.map((product) => {
      const bar = el("i");
      bar.style.width = `${Math.round((100 * product.held) / Math.max(1, product.total))}%`;
      const item = el("button", { className: "pick-item", type: "button", role: "option" },
        el("span", { className: "text" }, el("span", { className: "title" }, product.title || product.asin), el("span", { className: "asin" }, product.asin)),
        el("span", { className: "score" }, `${product.held}/${product.total}`, el("span", { className: "mini-bar" }, bar)));
      item.setAttribute("aria-selected", String(product.asin === app.pick.asin));
      item.addEventListener("click", () => {
        app.pick.asin = product.asin;
        renderPickList();
        loadPick(product.asin);
      });
      return item;
    }));
  }
  renderPager($("pick-pager"), data, (page) => { app.pick.page = page; refreshPick(); });
}

async function loadPick(asin) {
  const token = ++app.product.token;
  let detail;
  try {
    detail = await fetchJson(api(`/api/jobs/${app.job.id}/products/${asin}`));
  } catch {
    return;
  }
  if (token !== app.product.token) return;
  const product = detail.product;
  const reference = detail.reference;
  const counts = product.counts;
  const who = isMine() ? "You hold" : `${reference || "Nobody"} holds`;
  $("pick-asin").replaceChildren(`${product.asin} · `, amazonLink(product.asin));
  $("pick-title").textContent = product.title || product.asin;
  $("pick-open").href = `#${app.job.id}/product/${product.asin}`;
  const checked = product.total - counts.pending;
  $("pick-headline").textContent = counts.win === 0 ? `${isMine() ? "You don't hold" : `${reference} doesn't hold`} the buybox in any of the ${checked} checked states.`
    : counts.win === product.total ? `${who} the buybox in all ${product.total} states.`
      : `${who} the buybox in ${counts.win} of ${product.total} states.`;
  $("pick-legend").replaceChildren(
    legendItem("var(--series-1)", "", `${isMine() ? "You hold it" : reference} (${counts.win})`),
    legendItem("var(--lost)", "", `Another seller (${counts.other})`),
    legendItem(null, "fill-hatch", `No buybox (${counts.none})`),
    legendItem(null, "fill-failed", `Could not check (${counts.failed})`),
    ...(counts.pending ? [legendItem(null, "fill-pending", `Not checked yet (${counts.pending})`)] : []));
  const cells = new Map(product.cells.map((cell) => [cell.state, cell]));
  const rows = new Map(detail.rows.map((row) => [row.state, row]));
  const you = product.cells.filter((cell) => cell.kind === "win");
  const lost = product.cells.filter((cell) => cell.kind === "other");
  const other = product.cells.filter((cell) => ["none", "failed", "pending"].includes(cell.kind));
  $("pick-you").replaceChildren(...(you.length ? [el("div", { className: "code-chips" }, ...you.map((cell) => el("span", {}, cell.state))),
    el("p", { className: "muted" }, `Buybox price: ${product.your_price || "–"}`)] : [el("p", { className: "muted" }, "None of the checked states.")]));
  $("pick-lost").replaceChildren(...(lost.length ? lost.map((cell) => el("p", { className: "row" },
    el("span", {}, el("b", {}, stateName(cell.state)), ` · ${cell.seller}`), el("span", {}, cell.price))) : [el("p", { className: "muted" }, "No other seller holds it anywhere.")]));
  $("pick-other").replaceChildren(...(other.length ? other.map((cell) => el("p", { className: "row" },
    el("span", {}, el("b", {}, stateName(cell.state)), ` · ${describeCell(cell)}`))) : [el("p", { className: "muted" }, "Every state has a buybox and was checked.")]));
  const map = await getMap("pick-map");
  if (!map || token !== app.product.token) return;
  paintMap(map, (code) => kindPaint(cells.get(code)), (code) => rowTooltip(code, rows.get(code)));
}

function kindPaint(cell) {
  if (!cell) return null;
  const paint = {
    win: { color: "var(--series-1)", ink: "#FFFFFF" }, other: { color: "var(--lost)", ink: "#FFFFFF" },
    none: { color: "hatch", ink: "var(--ink)" }, failed: { color: "var(--critical-wash)", ink: "var(--ink)", failed: true },
    pending: { color: "var(--pending)", ink: "var(--ink-2)" },
  }[cell.kind];
  return { ...paint, text: describeCell(cell) };
}

// ---------- One product ----------

async function loadProduct(asin) {
  const token = ++app.product.token;
  let detail;
  try {
    detail = await fetchJson(api(`/api/jobs/${app.job.id}/products/${asin}`));
  } catch {
    return;
  }
  if (token !== app.product.token || app.route.view !== "product") return;
  app.product.data = detail;
  renderProduct(detail, token);
}

async function renderProduct(detail, token) {
  const job = app.job;
  const product = detail.product;
  const rows = new Map(detail.rows.map((row) => [row.state, row]));
  const many = job.asins.length > 1;

  const crumbs = [];
  if (many) {
    crumbs.push(el("a", { href: `#${job.id}` }, `${fmt(job.asins.length)} products`), el("span", { "aria-hidden": "true" }, "/"));
    if (app.nav && app.nav.label) crumbs.push(el("a", { href: `#${job.id}` }, app.nav.label), el("span", { "aria-hidden": "true" }, "/"));
  }
  crumbs.push(el("span", { className: "current" }, product.title || product.asin));
  $("crumbs").replaceChildren(...crumbs);
  const order = app.nav && app.nav.asins.includes(product.asin) ? app.nav.asins : null;
  $("product-nav").hidden = !many || !order;
  if (order) {
    const i = order.indexOf(product.asin);
    $("product-position").textContent = `${fmt(i + 1)} of ${fmt(order.length)}`;
    setNavLink($("product-prev"), i > 0 ? order[i - 1] : null);
    setNavLink($("product-next"), i < order.length - 1 ? order[i + 1] : null);
  }

  const checkedAt = detail.rows.map((row) => row.checked_at || "").sort().pop() || "";
  $("product-title").textContent = product.title || product.asin;
  $("product-meta").replaceChildren(`${product.asin}${checkedAt ? ` · checked ${timeOf(checkedAt)}` : ""} · `, amazonLink(product.asin),
    product.not_found ? " · not found on Amazon" : "");

  const counts = new Map();
  for (const cell of product.cells) if (cell.seller) counts.set(cell.seller, (counts.get(cell.seller) || 0) + 1);
  const sellers = [...counts.keys()].sort((a, b) => (sameSeller(a, detail.reference) ? -1 : sameSeller(b, detail.reference) ? 1 : counts.get(b) - counts.get(a)));
  const colors = sellerColors(sellers, detail.reference, detail.reference_is_store);
  const top = product.top_seller;
  const topRows = detail.rows.filter((row) => top && row.sold_by === top && row.has_buybox === "yes");
  $("p-name").textContent = top || "Nobody";
  $("p-trophy").style.background = top ? colors.get(top) : "var(--other)";
  $("p-badge").hidden = !(top && detail.reference_is_store && sameSeller(top, detail.reference));
  $("p-sentence").replaceChildren(...(top ? ["holds the buybox in ", el("b", {}, `${counts.get(top)} of ${product.total} ${plural(product.total, "state")}`)]
    : ["No seller holds the buybox in the checked states."]));
  $("p-price").textContent = priceRange(topRows.map((row) => row.price)) || "–";
  $("p-ships").textContent = mostCommon(topRows.map((row) => row.ships_from)) || "–";
  $("p-seller-id").textContent = mostCommon(topRows.map((row) => row.seller_id)) || "–";
  const c = product.counts;
  renderShare($("p-share-bar"), $("p-share-legend"), [
    ...sellers.map((seller) => [seller, counts.get(seller), colors.get(seller), ""]),
    ["No buybox", c.none, null, "fill-hatch"], ["Could not check", c.failed, null, "fill-failed"], ["Not checked yet", c.pending, null, "fill-pending"]]);

  const side = sellers.filter((seller) => seller !== top).map((seller) => {
    const states = product.cells.filter((cell) => cell.seller === seller);
    return sideCard(colors.get(seller), "", seller, `${states.length} ${plural(states.length, "state")}`,
      `${states.map((cell) => stateName(cell.state)).join(", ")} · ${priceRange(states.map((cell) => cell.price)) || "no price"}`);
  });
  const none = product.cells.filter((cell) => cell.kind === "none");
  const failed = product.cells.filter((cell) => cell.kind === "failed");
  side.push(sideCard(null, "fill-hatch", "No buybox", `${none.length} ${plural(none.length, "state")}`,
    none.length ? `${none.map((cell) => stateName(cell.state)).join(", ")} · no Add to Cart button, only "See all buying options"` : "Every checked state has a seller in the buybox."));
  side.push(sideCard(null, "fill-failed", "Could not check", `${failed.length}`,
    failed.length ? failed.map((cell) => `${stateName(cell.state)} (${STATUS_LABELS[cell.status] || cell.status})`).join(", ") : "Every state loaded."));
  $("p-side").replaceChildren(...side);

  const missing = ALL_STATES - job.states.length;
  $("p-legend").replaceChildren(...sellers.map((seller) => legendItem(colors.get(seller), "", `${seller} (${counts.get(seller)})`)),
    ...(c.none ? [legendItem(null, "fill-hatch", `No buybox (${c.none})`)] : []),
    ...(c.failed ? [legendItem(null, "fill-failed", `Could not check (${c.failed})`)] : []),
    ...(c.pending ? [legendItem(null, "fill-pending", `Not checked yet (${c.pending})`)] : []),
    ...(missing ? [legendItem("var(--excluded)", "", `Not in this check (${missing})`)] : []));
  renderStateTable(job.states, rows, colors);

  const map = await getMap("product-map");
  if (!map || token !== app.product.token) return;
  const cells = new Map(product.cells.map((cell) => [cell.state, cell]));
  paintMap(map, (code) => {
    const cell = cells.get(code);
    if (!cell) return null;
    if (cell.seller) return { color: colors.get(cell.seller), ink: "#FFFFFF", text: describeCell(cell) };
    return kindPaint(cell);
  }, (code) => rowTooltip(code, rows.get(code)));
}

function setNavLink(link, asin) {
  link.href = asin ? `#${app.job.id}/product/${asin}` : "#";
  link.setAttribute("aria-disabled", String(!asin));
}

function sideCard(color, className, title, count, text) {
  const swatch = el("span", { className: `swatch ${className}` });
  if (color) swatch.style.background = color;
  return el("div", { className: "side-card" },
    el("div", { className: "head" }, el("span", { className: "dot-name" }, swatch, title), el("span", { className: "count" }, count)),
    el("p", {}, text));
}

function legendItem(color, className, text) {
  const swatch = el("span", { className: `swatch ${className}` });
  if (color) swatch.style.background = color;
  return el("span", { className: "legend-item" }, swatch, text);
}

function renderStateTable(states, rows, colors) {
  const body = $("results-table").querySelector("tbody");
  body.replaceChildren(...states.map((code) => {
    const info = app.statesByCode.get(code);
    const row = rows.get(code);
    const seller = el("span", { className: "dot-name" });
    if (row && isWinner(row)) {
      const swatch = el("span", { className: "swatch" });
      swatch.style.background = colors.get(row.sold_by) || "var(--other)";
      seller.append(swatch, row.sold_by || "Unknown seller");
    }
    return el("tr", {},
      el("th", { scope: "row" }, `${info.name} (${code})`),
      el("td", {}, info.zipcode),
      el("td", {}, statusBadge(row)),
      el("td", {}, seller),
      el("td", {}, row ? row.ships_from || "" : ""),
      el("td", { className: "num" }, row ? row.price || "" : ""),
      el("td", {}, row ? row.delivery_message || "" : ""),
      el("td", {}, row ? (isFailed(row) ? row.error || "" : row.availability || "") : ""));
  }));
}

const isWinner = (row) => row.status === "ok" && row.has_buybox === "yes";
const isFailed = (row) => row.status !== "ok";

function statusBadge(row) {
  if (!row) return el("span", { className: "muted" }, "Waiting…");
  if (isFailed(row)) return el("span", { className: "badge failed" }, STATUS_LABELS[row.status] || row.status);
  return isWinner(row) ? el("span", { className: "badge ok" }, "Buybox") : el("span", { className: "badge none" }, "No buybox");
}

function rowTooltip(code, row) {
  const info = app.statesByCode.get(code);
  const details = [["Zipcode", `${info.zipcode} (${info.city})`]];
  if (!row) details.push(["Result", "not checked yet"]);
  else if (isFailed(row)) details.push(["Result", STATUS_LABELS[row.status] || row.status], ["Error", row.error]);
  else if (isWinner(row)) details.push(["Sold by", row.sold_by], ["Price", row.price], ["Ships from", row.ships_from], ["Delivery", row.delivery_message]);
  else details.push(["Result", "no buybox"], ["Availability", row.availability]);
  return [`${info.name} (${code})`, details];
}

// ---------- Maps ----------

async function loadShapes() {
  const us = await fetchJson(MAP_DATA_URL); // pre-projected with geoAlbersUsa, 975 x 610
  const codeByName = new Map(app.config.states.map((s) => [s.name, s.code]));
  const path = d3.geoPath();
  return topojson.feature(us, us.objects.states).features
    .filter((feature) => codeByName.has(feature.properties.name))
    .map((feature) => ({ code: codeByName.get(feature.properties.name), d: path(feature), centroid: path.centroid(feature) }));
}

// One map per box, drawn once; paintMap colours it for each view.
async function getMap(id) {
  if (app.maps.has(id)) return app.maps.get(id);
  const shapes = await app.shapesPromise;
  if (app.maps.has(id)) return app.maps.get(id);
  const box = $(id);
  if (!shapes) {
    box.replaceChildren(el("p", { className: "muted map-message" }, "The map needs internet access to cdn.jsdelivr.net. The lists still work."));
    app.maps.set(id, null);
    return null;
  }
  const map = { hatchId: `hatch-${id}`, paths: new Map(), labels: new Map(), lines: new Map(), describe: null };
  const svg = svgEl("svg", { viewBox: `0 0 ${MAP_WIDTH} ${MAP_HEIGHT}`, role: "group", "aria-label": "Map of US states" });
  svg.append(hatchPattern(map.hatchId));
  const labels = svgEl("g");
  for (const shape of shapes) {
    const path = svgEl("path", { d: shape.d, class: "state", tabindex: "0", "data-code": shape.code });
    path.addEventListener("mousemove", (event) => showTooltip(map, shape.code, event.clientX, event.clientY));
    path.addEventListener("mouseleave", hideTooltip);
    path.addEventListener("focus", () => {
      const rect = path.getBoundingClientRect();
      showTooltip(map, shape.code, rect.left + rect.width / 2, rect.top + rect.height / 2);
    });
    path.addEventListener("blur", hideTooltip);
    map.paths.set(shape.code, path);
    svg.append(path);
    const callout = CALLOUTS[shape.code];
    if (callout) {
      const line = svgEl("line", { x1: shape.centroid[0], y1: shape.centroid[1], x2: callout[2], y2: callout[3], class: "map-line" });
      map.lines.set(shape.code, line);
      labels.append(line);
    }
    const [x, y] = callout ? callout : LABEL_AT[shape.code] || shape.centroid;
    const text = svgEl("text", { x, y, class: `map-label${callout ? " outside" : ""}` });
    text.textContent = shape.code;
    map.labels.set(shape.code, text);
    labels.append(text);
  }
  svg.append(labels);
  box.replaceChildren(svg);
  app.maps.set(id, map);
  return map;
}

// paint(code) -> {color ("hatch" for no buybox), ink, failed, text} or null when the state is not in the check.
function paintMap(map, paint, describe) {
  map.describe = describe;
  for (const [code, path] of map.paths) {
    const spec = paint(code);
    const name = app.statesByCode.get(code).name;
    path.style.fill = !spec ? "var(--excluded)" : spec.color === "hatch" ? `url(#${map.hatchId})` : spec.color;
    path.classList.toggle("failed", Boolean(spec && spec.failed));
    path.setAttribute("aria-label", `${name}: ${spec ? spec.text : "not in this check"}`);
    const label = map.labels.get(code);
    const outside = Boolean(CALLOUTS[code]);
    label.style.display = spec ? "" : "none";
    label.style.fill = outside ? "var(--ink)" : spec ? spec.ink : "";
    const line = map.lines.get(code);
    if (line) line.style.display = spec ? "" : "none";
  }
}

function hatchPattern(id) {
  const defs = svgEl("defs");
  const pattern = svgEl("pattern", { id, width: 7, height: 7, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" });
  const background = svgEl("rect", { width: 7, height: 7 });
  background.style.fill = "var(--hatch-bg)";
  const line = svgEl("line", { x1: 0, y1: 0, x2: 0, y2: 7, "stroke-width": 2 });
  line.style.stroke = "var(--hatch-line)";
  pattern.append(background, line);
  defs.append(pattern);
  return defs;
}

function showTooltip(map, code, clientX, clientY) {
  const info = map.describe && map.describe(code);
  if (!info) return;
  const [title, details] = info;
  const list = el("dl");
  for (const [term, value] of details) if (value) list.append(el("dt", {}, term), el("dd", {}, value));
  const tooltip = $("tooltip");
  tooltip.replaceChildren(el("strong", {}, title), list);
  tooltip.hidden = false;
  const x = Math.min(clientX + 14, window.innerWidth - tooltip.offsetWidth - 8);
  const y = Math.min(clientY + 14, window.innerHeight - tooltip.offsetHeight - 8);
  tooltip.style.left = `${Math.max(8, x)}px`;
  tooltip.style.top = `${Math.max(8, y)}px`;
}

function hideTooltip() {
  $("tooltip").hidden = true;
}

// ---------- Small helpers ----------

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${response.status} ${url}`);
  return response.json();
}

// API path with the user's store and other query parameters.
function api(path, params = {}) {
  const query = new URLSearchParams();
  const store = storeParam();
  if (store) query.set("store", store);
  for (const [key, value] of Object.entries(params)) if (value !== "" && value != null) query.set(key, String(value));
  const text = query.toString();
  return text ? `${path}?${text}` : path;
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key in node && !key.includes("-")) node[key] = value;
    else node.setAttribute(key, value);
  }
  node.append(...children.filter((child) => child !== null && child !== undefined && child !== ""));
  return node;
}

function svgEl(tag, attributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
}

function amazonLink(asin) {
  return el("a", { href: `https://www.amazon.com/dp/${asin}`, target: "_blank", rel: "noopener noreferrer" }, "open on Amazon");
}

function showMessage(text, { info = false, action = null } = {}) {
  const message = $("form-message");
  const parts = [el("span", {}, text)];
  if (action) {
    const button = el("button", { className: "link-button", type: "button" }, action.label);
    button.addEventListener("click", action.onClick);
    parts.push(button);
  }
  message.replaceChildren(...parts);
  message.classList.toggle("info", info);
  message.hidden = false;
}

function hideMessage() {
  $("form-message").hidden = true;
}

async function copyText(text, button) {
  const label = button.textContent;
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = "Copied";
  } catch {
    button.textContent = "Copy failed";
  }
  setTimeout(() => { button.textContent = label; }, 1600);
}

function debounce(fn) {
  let timer = null;
  return () => {
    clearTimeout(timer);
    timer = setTimeout(fn, SEARCH_DELAY_MS);
  };
}

const prefersMotion = () => !matchMedia("(prefers-reduced-motion: reduce)").matches;
const fmt = (n) => Number(n || 0).toLocaleString("en-US");
const plural = (n, word) => (n === 1 ? word : `${word}s`);
const timeOf = (iso) => (iso ? iso.slice(11, 16) : "");
const stateName = (code) => (app.statesByCode.get(code) || { name: code }).name;

function formatMinutes(minutes) {
  if (minutes < 1) return "under a minute";
  if (minutes < 60) return `${Math.ceil(minutes)} min`;
  const rounded = Math.round(minutes);
  return `${Math.floor(rounded / 60)} h ${String(rounded % 60).padStart(2, "0")} min`;
}

function priceValue(price) {
  return Number(String(price).replace(/[$,]/g, "")) || 0;
}

function priceRange(prices) {
  const distinct = [...new Set(prices.filter(Boolean))].sort((a, b) => priceValue(a) - priceValue(b));
  if (!distinct.length) return "";
  return distinct.length === 1 ? distinct[0] : `${distinct[0]}–${distinct[distinct.length - 1]}`;
}

function mostCommon(values) {
  const counts = new Map();
  for (const value of values) if (value) counts.set(value, (counts.get(value) || 0) + 1);
  return [...counts].sort((a, b) => b[1] - a[1]).map(([value]) => value)[0] || "";
}
