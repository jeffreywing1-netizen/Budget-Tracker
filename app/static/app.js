// ---------- helpers ----------

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  if (res.status === 204) return null;
  return res.json();
}

function fmtMoney(n) {
  const v = Math.abs(n).toFixed(2);
  return (n < 0 ? "-$" : "$") + v;
}

function amountClass(n) {
  return n < 0 ? "negative" : "positive";
}

function el(tag, attrs, children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children || []) e.appendChild(c);
  return e;
}

const MONTH_NAMES = ["January","February","March","April","May","June",
                      "July","August","September","October","November","December"];

// ---------- global state ----------

let CATEGORIES = [];
let ACCOUNTS = [];
let GROUPS = [];
let yearChart = null;
let monthExpenseBarChart = null;
let yearExpenseBarChart = null;

// Category filter (Month/Year views): which category ids are currently "shown".
// Shared across both views. New categories default to selected the first time they're seen.
let categoryFilterSelection = new Set();
let categoryFilterSeenIds = new Set();

let importCurrentFile = null;
let importCurrentMapping = null;

// ---------- tabs ----------

document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
  });
});

// ---------- bootstrap ----------

async function loadCategories() {
  CATEGORIES = await api("/api/categories");
  const selects = ["rule-category", "txn-category"];
  for (const id of selects) {
    const sel = document.getElementById(id);
    const keepFirst = id === "txn-category";
    sel.innerHTML = keepFirst ? '<option value="">All</option>' : "";
    for (const c of CATEGORIES) {
      sel.appendChild(el("option", { value: c.id, text: c.name }));
    }
  }
  for (const c of CATEGORIES) {
    if (c.is_transfer) continue;
    if (!categoryFilterSeenIds.has(c.id)) {
      categoryFilterSeenIds.add(c.id);
      categoryFilterSelection.add(c.id);
    }
  }
}

async function loadCategoryGroups() {
  GROUPS = await api("/api/category_groups");
  const sel = document.getElementById("cat-group");
  sel.innerHTML = '<option value="">(none)</option>';
  for (const g of GROUPS) {
    sel.appendChild(el("option", { value: g.id, text: g.name }));
  }
}

async function loadAccounts() {
  ACCOUNTS = await api("/api/accounts");
  const selects = ["import-account", "txn-account", "rc-run-account", "rc-account"];
  for (const id of selects) {
    const sel = document.getElementById(id);
    const keepFirst = id === "txn-account" || id === "rc-run-account";
    sel.innerHTML = keepFirst ? '<option value="">All</option>' : "";
    for (const a of ACCOUNTS) {
      sel.appendChild(el("option", { value: a.id, text: a.name }));
    }
  }
}

function populateYearSelects() {
  const now = new Date();
  const currentYear = now.getFullYear();
  const years = [];
  for (let y = currentYear + 1; y >= currentYear - 6; y--) years.push(y);

  for (const id of ["month-year", "year-year", "budget-year"]) {
    const sel = document.getElementById(id);
    sel.innerHTML = "";
    for (const y of years) sel.appendChild(el("option", { value: y, text: y }));
    sel.value = currentYear;
  }

  for (const id of ["month-month", "budget-month"]) {
    const monthSel = document.getElementById(id);
    monthSel.innerHTML = "";
    MONTH_NAMES.forEach((name, i) => {
      monthSel.appendChild(el("option", { value: i + 1, text: name }));
    });
    monthSel.value = now.getMonth() + 1;
  }
}

// ---------- Import tab ----------

document.getElementById("new-account-toggle").addEventListener("click", () => {
  document.getElementById("new-account-form").classList.toggle("hidden");
});

document.getElementById("na-save").addEventListener("click", async () => {
  const name = document.getElementById("na-name").value.trim();
  if (!name) return alert("Enter an account name");
  const payload = {
    name,
    institution: document.getElementById("na-institution").value,
    account_type: document.getElementById("na-type").value,
    last4: document.getElementById("na-last4").value.trim() || null,
  };
  try {
    const created = await api("/api/accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    document.getElementById("na-name").value = "";
    document.getElementById("na-last4").value = "";
    document.getElementById("new-account-form").classList.add("hidden");
    await loadAccounts();
    document.getElementById("import-account").value = created.id;
    await loadUploadTracker();
  } catch (e) {
    alert("Couldn't save account: " + e.message);
  }
});

function resetImportCards() {
  document.getElementById("mapping-card").classList.add("hidden");
  document.getElementById("preview-card").classList.add("hidden");
}

document.querySelectorAll('input[name="amount-mode"]').forEach(radio => {
  radio.addEventListener("change", () => {
    const single = document.querySelector('input[name="amount-mode"]:checked').value === "single";
    document.getElementById("map-single").classList.toggle("hidden", !single);
    document.getElementById("map-split").classList.toggle("hidden", single);
  });
});

function populateMappingForm(headers, sampleRows) {
  const selects = ["map-date", "map-desc", "map-amount", "map-debit", "map-credit"];
  for (const id of selects) {
    const sel = document.getElementById(id);
    sel.innerHTML = "";
    sel.appendChild(el("option", { value: "", text: "(none)" }));
    for (const h of headers) sel.appendChild(el("option", { value: h, text: h }));
  }

  const table = document.getElementById("sample-table");
  table.innerHTML = "";
  const headRow = el("tr", {}, headers.map(h => el("th", { text: h })));
  table.appendChild(el("thead", {}, [headRow]));
  const tbody = el("tbody", {}, sampleRows.map(row =>
    el("tr", {}, row.map(cell => el("td", { text: cell })))
  ));
  table.appendChild(tbody);

  document.getElementById("mapping-card").classList.remove("hidden");
  document.getElementById("preview-card").classList.add("hidden");
}

function renderBreakdownTable(tableEl, breakdown) {
  tableEl.innerHTML = "";
  const head = el("tr", {}, [
    el("th", { text: "Category" }), el("th", { text: "Amount" }), el("th", { text: "Count" }),
  ]);
  tableEl.appendChild(el("thead", {}, [head]));
  const tbody = el("tbody");
  for (const c of breakdown) {
    const label = (c.name || "Uncategorized") + (c.is_transfer ? " (transfer, not counted)" : "");
    tbody.appendChild(el("tr", {}, [
      el("td", {}, [el("span", { class: "color-dot", style: `background:${c.color}` }), document.createTextNode(label)]),
      el("td", { class: "amount-cell " + amountClass(c.amount), text: fmtMoney(c.amount) }),
      el("td", { text: c.count }),
    ]));
  }
  tableEl.appendChild(tbody);
}

async function runImportPreview(mapping, commit, overrides) {
  const accountId = document.getElementById("import-account").value;
  if (!accountId) { alert("Select or create an account first"); return null; }

  if (!importCurrentFile) {
    const fileInput = document.getElementById("import-file");
    importCurrentFile = fileInput.files[0];
  }
  if (!importCurrentFile) { alert("Choose a file first"); return null; }

  const form = new FormData();
  form.append("file", importCurrentFile);
  form.append("account_id", accountId);
  form.append("commit", commit ? "true" : "false");
  if (mapping) form.append("mapping", JSON.stringify(mapping));
  if (overrides) form.append("overrides", JSON.stringify(overrides));

  return api("/api/import", { method: "POST", body: form });
}

document.getElementById("import-preview-btn").addEventListener("click", async () => {
  importCurrentFile = document.getElementById("import-file").files[0];
  importCurrentMapping = null;
  resetImportCards();
  try {
    const result = await runImportPreview(null, false);
    if (result.status === "needs_mapping") {
      populateMappingForm(result.headers, result.sample_rows);
    } else {
      importCurrentMapping = result.mapping || null;
      showPreviewCard(result);
    }
  } catch (e) {
    alert("Import failed: " + e.message);
  }
});

document.getElementById("mapping-preview-btn").addEventListener("click", async () => {
  const single = document.querySelector('input[name="amount-mode"]:checked').value === "single";
  const mapping = {
    date_col: document.getElementById("map-date").value || null,
    description_col: document.getElementById("map-desc").value || null,
  };
  if (single) {
    mapping.amount_col = document.getElementById("map-amount").value || null;
    mapping.amount_sign = document.getElementById("map-amount-sign").value;
  } else {
    mapping.debit_col = document.getElementById("map-debit").value || null;
    mapping.credit_col = document.getElementById("map-credit").value || null;
  }
  if (!mapping.date_col || !mapping.description_col) {
    alert("Please choose at least the date and description columns");
    return;
  }
  try {
    const result = await runImportPreview(mapping, false);
    importCurrentMapping = mapping;
    showPreviewCard(result);
  } catch (e) {
    alert("Import failed: " + e.message);
  }
});

// Transactions from the preview, held in the browser while the user reviews them. Each is shaped like
// a saved transaction (so the shared table/split-editor code can render it) plus `staged: true`, its
// dedupe `key`, and `applySimilar` if the user asked for a correction to be generalized.
let importStaged = [];
let importDuplicateCount = 0;

function describeStatementPreview(st, hasNewTransactions) {
  if (!st) return null;
  if (st.status === "not_found") {
    return "No statement balances were found in this PDF. Add this statement's balances on the Reconcile tab to check it.";
  }
  return `Statement found: closes ${st.period_end}, new balance ${fmtMoney(st.new_balance)}` +
    (st.previous_balance !== null ? ` (previous ${fmtMoney(st.previous_balance)})` : "") +
    (hasNewTransactions
      ? ". It's saved for the Reconcile tab when you confirm."
      : ". There's nothing new to import, so use \"Read PDF(s)\" on the Reconcile tab to save it.");
}

function showPreviewCard(result) {
  document.getElementById("mapping-card").classList.add("hidden");
  const statementLine = describeStatementPreview(result.statement, (result.transactions || []).length > 0);
  const statementEl = document.getElementById("preview-statement");
  statementEl.textContent = statementLine || "";
  statementEl.classList.toggle("hidden", !statementLine);
  importStaged = (result.transactions || []).map(t => ({
    ...t, staged: true, splits: null, edited: false, applySimilar: false,
  }));
  importDuplicateCount = result.duplicates || 0;
  refreshStagedTable();
  document.getElementById("preview-card").classList.remove("hidden");
  document.getElementById("confirm-import-btn").disabled = importStaged.length === 0;
  document.getElementById("preview-card").scrollIntoView({ behavior: "smooth", block: "start" });
}

function refreshStagedTable() {
  renderTransactionsTable(document.getElementById("preview-txns-table"), importStaged, false, refreshStagedTable);
  updateStagedSummary();
}

function updateStagedSummary() {
  const net = importStaged.reduce((sum, t) => sum + t.amount, 0);
  const edited = importStaged.filter(t => t.edited).length;
  document.getElementById("preview-summary").textContent =
    `${importStaged.length} transaction(s) ready to import, net ${fmtMoney(net)}` +
    (importDuplicateCount ? `; ${importDuplicateCount} duplicate(s) will be skipped` : "") +
    (edited ? `; ${edited} edited by you` : "") + ".";
}

function stagedOverrides() {
  const overrides = {};
  for (const t of importStaged) {
    if (!t.edited) continue;
    overrides[t.key] = {
      category_id: t.category_id,
      splits: t.splits ? t.splits.map(s => ({ category_id: s.category_id, amount: s.amount })) : null,
      apply_similar: t.applySimilar,
    };
  }
  return overrides;
}

function clearImportState() {
  resetImportCards();
  importStaged = [];
  importCurrentFile = null;
  importCurrentMapping = null;
  document.getElementById("import-file").value = "";
}

document.getElementById("cancel-import-btn").addEventListener("click", clearImportState);

document.getElementById("confirm-import-btn").addEventListener("click", async () => {
  try {
    const result = await runImportPreview(importCurrentMapping, true, stagedOverrides());
    clearImportState();
    await loadImportBatches();
    await loadUploadTracker();
    await loadStatements();
    const st = result.statement;
    const statementNote = !st ? "" :
      st.status === "saved" ? `\nStatement closing ${st.period_end} saved for reconciliation.` :
      st.status === "already_on_file" ? `\nStatement closing ${st.period_end} was already on file for reconciliation.` :
      "\nNo statement balances were found in this PDF; add them on the Reconcile tab to check it.";
    alert((result.imported > 0
      ? `Imported ${result.imported} transaction(s)` +
        (result.duplicates ? ` (${result.duplicates} duplicate(s) skipped).` : ".")
      : `No new transactions imported. ${result.duplicates} duplicate(s) skipped.`) + statementNote);
  } catch (e) {
    alert("Import failed: " + e.message);
  }
});

async function loadUploadTracker() {
  const batches = await api("/api/import_batches");
  const now = new Date();
  const currentMonth = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;

  const table = document.getElementById("upload-tracker-table");
  table.innerHTML = "";
  table.appendChild(el("thead", {}, [el("tr", {}, [
    el("th", { text: "Account" }), el("th", { text: "Uploaded this month?" }), el("th", { text: "Last upload" }),
  ])]));
  const tbody = el("tbody");
  for (const a of ACCOUNTS) {
    const acctBatches = batches.filter(b => b.account_id === a.id);
    const uploadedThisMonth = acctBatches.some(b => (b.imported_at || "").slice(0, 7) === currentMonth);
    const lastImport = acctBatches.length ? acctBatches.map(b => b.imported_at).sort().slice(-1)[0] : null;

    tbody.appendChild(el("tr", {}, [
      el("td", { text: a.name }),
      el("td", {
        class: "amount-cell " + (uploadedThisMonth ? "positive" : "negative"),
        text: uploadedThisMonth ? "Uploaded" : "Not yet",
      }),
      el("td", { text: lastImport ? lastImport.slice(0, 10) : "never" }),
    ]));
  }
  table.appendChild(tbody);
}

// ---------- shared: expense/income split + sorted bar chart ----------

function splitCategories(byCategory) {
  // Backend already orders by amount ASC, so expenses (most negative first) come out
  // largest-spend-first for free. Income is re-sorted descending (largest first) here.
  const passesFilter = (c) => categoryFilterSelection.has(c.category_id);
  const expenses = byCategory.filter(c => c.amount < 0 && !c.is_transfer && passesFilter(c));
  const income = byCategory.filter(c => c.amount > 0 && !c.is_transfer && passesFilter(c))
    .sort((a, b) => b.amount - a.amount);
  return { expenses, income };
}

function txnPassesCategoryFilter(t) {
  if (t.splits && t.splits.length) {
    return t.splits.some(s => categoryFilterSelection.has(s.category_id));
  }
  return categoryFilterSelection.has(t.category_id);
}

async function onCategoryFilterChanged() {
  await Promise.all([loadMonthView(), loadYearView()]);
}

function renderCategoryFilterPanel(containerId) {
  const container = document.getElementById(containerId);
  container.innerHTML = "";

  const byGroup = {};
  const ungrouped = [];
  for (const c of CATEGORIES) {
    if (c.is_transfer) continue;
    if (c.group_id) {
      (byGroup[c.group_id] = byGroup[c.group_id] || []).push(c);
    } else {
      ungrouped.push(c);
    }
  }

  function makeGroupBlock(label, cats, groupColor) {
    const allSelected = cats.every(c => categoryFilterSelection.has(c.id));

    const groupLabelChildren = [];
    if (groupColor) groupLabelChildren.push(el("span", { class: "color-dot", style: `background:${groupColor}` }));
    groupLabelChildren.push(document.createTextNode(label));
    const groupLabel = el("div", {
      class: "filter-group-label" + (allSelected ? "" : " deselected"),
    }, groupLabelChildren);
    groupLabel.addEventListener("click", () => {
      const stillAllSelected = cats.every(c => categoryFilterSelection.has(c.id));
      for (const c of cats) {
        if (stillAllSelected) categoryFilterSelection.delete(c.id);
        else categoryFilterSelection.add(c.id);
      }
      onCategoryFilterChanged();
    });

    const catLabels = cats.map(c => {
      const selected = categoryFilterSelection.has(c.id);
      const catLabel = el("div", { class: "filter-category-label" + (selected ? "" : " deselected") }, [
        el("span", { class: "color-dot", style: `background:${c.color}` }), document.createTextNode(c.name),
      ]);
      catLabel.addEventListener("click", () => {
        if (categoryFilterSelection.has(c.id)) categoryFilterSelection.delete(c.id);
        else categoryFilterSelection.add(c.id);
        onCategoryFilterChanged();
      });
      return catLabel;
    });

    return el("div", { class: "filter-group" }, [groupLabel, ...catLabels]);
  }

  for (const g of GROUPS) {
    const cats = byGroup[g.id] || [];
    if (cats.length === 0) continue;
    container.appendChild(makeGroupBlock(g.name, cats, g.color));
  }
  if (ungrouped.length) {
    container.appendChild(makeGroupBlock("Ungrouped", ungrouped));
  }
}

function wireFilterButtons(allBtnId, noneBtnId) {
  document.getElementById(allBtnId).addEventListener("click", () => {
    categoryFilterSelection = new Set(CATEGORIES.filter(c => !c.is_transfer).map(c => c.id));
    onCategoryFilterChanged();
  });
  document.getElementById(noneBtnId).addEventListener("click", () => {
    categoryFilterSelection = new Set();
    onCategoryFilterChanged();
  });
}
wireFilterButtons("month-filter-all", "month-filter-none");
wireFilterButtons("year-filter-all", "year-filter-none");

function renderExpenseBarChart(canvasId, existingChart, expenseCats) {
  const ctx = document.getElementById(canvasId).getContext("2d");
  if (existingChart) existingChart.destroy();
  return new Chart(ctx, {
    type: "bar",
    data: {
      labels: expenseCats.map(c => c.name || "Uncategorized"),
      datasets: [{
        data: expenseCats.map(c => Math.abs(c.amount)),
        backgroundColor: expenseCats.map(c => c.color || "#999"),
      }],
    },
    options: {
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true } },
    },
  });
}

// ---------- Month tab ----------

async function loadMonthView() {
  const year = parseInt(document.getElementById("month-year").value, 10);
  const month = parseInt(document.getElementById("month-month").value, 10);
  const summary = await api(`/api/summary/month?year=${year}&month=${month}`);

  renderCategoryFilterPanel("month-filter-body");

  const { expenses, income } = splitCategories(summary.by_category);
  const filteredSpend = expenses.reduce((s, c) => s + c.amount, 0);
  const filteredIncome = income.reduce((s, c) => s + c.amount, 0);

  document.getElementById("month-spend").textContent = fmtMoney(filteredSpend);
  document.getElementById("month-income").textContent = fmtMoney(filteredIncome);
  document.getElementById("month-transfers").textContent = fmtMoney(summary.transfers_total);

  renderBreakdownTable(document.getElementById("month-breakdown"), expenses);
  renderBreakdownTable(document.getElementById("month-income-breakdown"), income);

  monthExpenseBarChart = renderExpenseBarChart("month-expense-bar", monthExpenseBarChart, expenses);

  const txns = await api(`/api/transactions?year=${year}&month=${month}`);
  const filteredTxns = txns.filter(txnPassesCategoryFilter);
  renderTransactionsTable(document.getElementById("month-transactions"), filteredTxns, false, loadMonthView);
}

document.getElementById("month-year").addEventListener("change", loadMonthView);
document.getElementById("month-month").addEventListener("change", loadMonthView);

// ---------- Year tab ----------

async function loadYearView() {
  const year = parseInt(document.getElementById("year-year").value, 10);
  const summary = await api(`/api/summary/year?year=${year}`);

  renderCategoryFilterPanel("year-filter-body");

  const { expenses, income } = splitCategories(summary.by_category);
  const filteredSpend = expenses.reduce((s, c) => s + c.amount, 0);
  const filteredIncome = income.reduce((s, c) => s + c.amount, 0);

  document.getElementById("year-spend").textContent = fmtMoney(filteredSpend);
  document.getElementById("year-income").textContent = fmtMoney(filteredIncome);
  document.getElementById("year-transfers").textContent = fmtMoney(summary.transfers_total);

  renderBreakdownTable(document.getElementById("year-breakdown"), expenses);
  renderBreakdownTable(document.getElementById("year-income-breakdown"), income);

  yearExpenseBarChart = renderExpenseBarChart("year-expense-bar", yearExpenseBarChart, expenses);

  const txns = await api(`/api/transactions?year=${year}`);
  const filteredTxns = txns.filter(txnPassesCategoryFilter);
  renderTransactionsTable(document.getElementById("year-transactions"), filteredTxns, true, loadYearView);

  const catNames = {};
  const catColors = {};
  for (const c of summary.by_category) {
    if (c.amount < 0 && !c.is_transfer && categoryFilterSelection.has(c.category_id)) {
      catNames[c.category_id] = c.name || "Uncategorized"; catColors[c.category_id] = c.color || "#999";
    }
  }
  const months = Array.from({ length: 12 }, (_, i) => String(i + 1).padStart(2, "0"));
  const datasets = Object.keys(catNames).map(catId => {
    const perMonth = months.map(m => {
      const entry = summary.by_month.find(e => e.month === m && String(e.category_id) === String(catId));
      return entry ? Math.abs(Math.min(entry.amount, 0)) : 0;
    });
    return { label: catNames[catId], data: perMonth, backgroundColor: catColors[catId] };
  });

  const ctx = document.getElementById("year-chart").getContext("2d");
  if (yearChart) yearChart.destroy();
  yearChart = new Chart(ctx, {
    type: "bar",
    data: { labels: MONTH_NAMES.map(m => m.slice(0, 3)), datasets },
    options: {
      responsive: true,
      scales: { x: { stacked: true }, y: { stacked: true } },
      plugins: { legend: { position: "bottom" } },
    },
  });
}

document.getElementById("year-year").addEventListener("change", loadYearView);

// ---------- Budget tab ----------

let budgetMonthlyChart = null;
let budgetAnnualChart = null;
let budgetMonthlyEntries = [];
let budgetAnnualEntries = [];

function diffClass(diff) {
  if (Math.abs(diff) < 0.01) return "muted";
  return diff > 0 ? "negative" : "positive"; // over budget = red, under budget = green
}

function renderBudgetChart(canvasId, existingChart, entries, sortBy) {
  const sorted = [...entries].sort((a, b) => sortBy === "budget"
    ? b.saved_amount - a.saved_amount
    : (b.actual - b.saved_amount) - (a.actual - a.saved_amount));
  const ctx = document.getElementById(canvasId).getContext("2d");
  if (existingChart) existingChart.destroy();
  return new Chart(ctx, {
    type: "bar",
    data: {
      labels: sorted.map(e => e.name),
      datasets: [
        { label: "Budget", data: sorted.map(e => e.saved_amount), backgroundColor: "#b0b0b0" },
        { label: "Actual", data: sorted.map(e => e.actual), backgroundColor: sorted.map(e => e.color) },
      ],
    },
    options: {
      plugins: { legend: { position: "bottom" } },
      scales: { y: { beginAtZero: true } },
    },
  });
}

function renderBudgetTable(tableId, entries, isAnnual) {
  const table = document.getElementById(tableId);
  table.innerHTML = "";
  const headerCells = [
    el("th", { text: "Category" }),
    el("th", { text: "Suggested" }),
    el("th", { text: isAnnual ? "Annual Budget" : "Budget" }),
  ];
  if (isAnnual) headerCells.push(el("th", { text: "Amortized /mo" }));
  headerCells.push(el("th", { text: "Actual" }), el("th", { text: "Difference" }));
  table.appendChild(el("thead", {}, [el("tr", {}, headerCells)]));

  const tbody = el("tbody");
  for (const entry of entries) {
    const input = el("input", { type: "number", step: "0.01", style: "width:100px" });
    input.value = entry.saved_amount;
    input.dataset.categoryId = entry.category_id;

    const diffCell = el("td", { class: "amount-cell" });
    const amortCell = isAnnual ? el("td", { text: fmtMoney(entry.amortized_monthly) }) : null;

    const recompute = () => {
      const val = parseFloat(input.value) || 0;
      const diff = entry.actual - val;
      diffCell.textContent = fmtMoney(diff);
      diffCell.className = "amount-cell " + diffClass(diff);
      if (isAnnual && amortCell) amortCell.textContent = fmtMoney(val / 12);
      updateBudgetSummary();
    };
    input.addEventListener("input", recompute);

    const cells = [
      el("td", {}, [el("span", { class: "color-dot", style: `background:${entry.color}` }), document.createTextNode(entry.name)]),
      el("td", { text: fmtMoney(entry.suggested) }),
      el("td", {}, [input]),
    ];
    if (isAnnual) cells.push(amortCell);
    cells.push(el("td", { text: fmtMoney(entry.actual) }));
    cells.push(diffCell);

    tbody.appendChild(el("tr", {}, cells));
    recompute();
  }
  table.appendChild(tbody);
}

function updateBudgetSummary() {
  let monthlyBudget = 0, monthlyActual = 0, annualBudget = 0, annualActual = 0;
  document.querySelectorAll("#budget-monthly-table input[data-category-id]").forEach(input => {
    const entry = budgetMonthlyEntries.find(e => String(e.category_id) === input.dataset.categoryId);
    monthlyBudget += parseFloat(input.value) || 0;
    if (entry) monthlyActual += entry.actual;
  });
  // Annual categories count at 1/12 of their annual figure (budget and trailing-12-month actual alike).
  document.querySelectorAll("#budget-annual-table input[data-category-id]").forEach(input => {
    const entry = budgetAnnualEntries.find(e => String(e.category_id) === input.dataset.categoryId);
    annualBudget += (parseFloat(input.value) || 0) / 12;
    if (entry) annualActual += entry.actual / 12;
  });

  const setRow = (prefix, budget, actual) => {
    document.getElementById(`bs-${prefix}-budget`).textContent = fmtMoney(budget);
    document.getElementById(`bs-${prefix}-actual`).textContent = fmtMoney(actual);
    const diff = actual - budget;
    const diffEl = document.getElementById(`bs-${prefix}-diff`);
    diffEl.textContent = fmtMoney(diff);
    diffEl.className = "amount-cell " + diffClass(diff);
  };
  setRow("monthly", monthlyBudget, monthlyActual);
  setRow("annual", annualBudget, annualActual);
  setRow("total", monthlyBudget + annualBudget, monthlyActual + annualActual);
}

async function loadBudgetTab() {
  const year = parseInt(document.getElementById("budget-year").value, 10);
  const month = parseInt(document.getElementById("budget-month").value, 10);
  const data = await api(`/api/budget/data?year=${year}&month=${month}`);

  budgetMonthlyEntries = data.categories.filter(c => c.frequency !== "annual");
  budgetAnnualEntries = data.categories.filter(c => c.frequency === "annual");

  renderBudgetTable("budget-monthly-table", budgetMonthlyEntries, false);
  renderBudgetTable("budget-annual-table", budgetAnnualEntries, true);

  budgetMonthlyChart = renderBudgetChart("budget-monthly-chart", budgetMonthlyChart, budgetMonthlyEntries, "budget");
  budgetAnnualChart = renderBudgetChart("budget-annual-chart", budgetAnnualChart, budgetAnnualEntries, "variance");

  updateBudgetSummary();
}

document.getElementById("budget-year").addEventListener("change", loadBudgetTab);
document.getElementById("budget-month").addEventListener("change", loadBudgetTab);

document.getElementById("budget-lock-btn").addEventListener("click", async () => {
  const year = parseInt(document.getElementById("budget-year").value, 10);
  const month = parseInt(document.getElementById("budget-month").value, 10);
  const entries = [];
  document.querySelectorAll(
    '#budget-monthly-table input[data-category-id], #budget-annual-table input[data-category-id]'
  ).forEach(input => {
    entries.push({ category_id: parseInt(input.dataset.categoryId, 10), amount: parseFloat(input.value) || 0 });
  });
  try {
    const result = await api("/api/budget/data", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ year, month, entries }),
    });
    alert(`Locked in budget for ${MONTH_NAMES[month - 1]} ${year} (${result.saved} categories).`);
    await loadBudgetTab();
  } catch (e) {
    alert("Couldn't save budget: " + e.message);
  }
});

// ---------- Transactions tab ----------

function buildCategoryCell(t, onChanged) {
  const cellContent = el("div", { class: "split-cell" });

  if (t.splits && t.splits.length) {
    const summary = el("div", { class: "split-summary" }, t.splits.map(s =>
      el("div", {}, [
        el("span", { class: "color-dot", style: `background:${s.category_color}` }),
        document.createTextNode(`${s.category_name}: ${fmtMoney(s.amount)}`),
      ])
    ));
    const editBtn = el("button", {
      text: "Edit split", class: "split-edit-btn", type: "button",
      onclick: () => openSplitEditor(t, onChanged),
    });
    cellContent.appendChild(summary);
    cellContent.appendChild(editBtn);
    return cellContent;
  }

  const catSelect = el("select");
  if (!t.staged) catSelect.appendChild(el("option", { value: "", text: "Uncategorized" }));
  for (const c of CATEGORIES) {
    const opt = el("option", { value: c.id, text: c.name });
    if (String(t.category_id) === String(c.id)) opt.selected = true;
    catSelect.appendChild(opt);
  }
  catSelect.addEventListener("change", async () => {
    const applySimilar = confirm("Apply this category to all similar transactions on this account too?");
    if (t.staged) {
      const newCategoryId = parseInt(catSelect.value, 10);
      t.category_id = newCategoryId;
      t.splits = null;
      t.edited = true;
      t.applySimilar = applySimilar;
      if (applySimilar) {
        // Same merchant elsewhere in this upload; rows the user split or already edited are left alone.
        for (const other of importStaged) {
          if (other !== t && !other.edited && other.merchant_token === t.merchant_token) {
            other.category_id = newCategoryId;
          }
        }
      }
      if (onChanged) onChanged();
      return;
    }
    try {
      await api(`/api/transactions/${t.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ category_id: parseInt(catSelect.value, 10), apply_to_similar: applySimilar }),
      });
      if (onChanged) onChanged();
    } catch (e) {
      alert("Couldn't update category: " + e.message);
    }
  });
  const splitBtn = el("button", {
    text: "Split", class: "split-edit-btn", type: "button",
    onclick: () => openSplitEditor(t, onChanged),
  });
  cellContent.appendChild(catSelect);
  cellContent.appendChild(splitBtn);
  return cellContent;
}

function renderTransactionsTable(tableEl, txns, showAccount, onChanged) {
  tableEl.innerHTML = "";
  const headerCells = [
    el("th", { text: "Date" }),
  ];
  if (showAccount) headerCells.push(el("th", { text: "Account" }));
  headerCells.push(el("th", { text: "Description" }), el("th", { text: "Amount" }), el("th", { text: "Category" }));
  tableEl.appendChild(el("thead", {}, [el("tr", {}, headerCells)]));

  const tbody = el("tbody");
  for (const t of txns) {
    const cells = [el("td", { text: t.date })];
    if (showAccount) cells.push(el("td", { text: t.account_name || "" }));
    cells.push(
      el("td", { text: t.description }),
      el("td", { class: "amount-cell " + amountClass(t.amount), text: fmtMoney(t.amount) }),
      el("td", {}, [buildCategoryCell(t, onChanged)]),
    );
    tbody.appendChild(el("tr", {}, cells));
  }
  tableEl.appendChild(tbody);
}

async function loadTransactionsTab() {
  const account_id = document.getElementById("txn-account").value;
  const category_id = document.getElementById("txn-category").value;
  const search = document.getElementById("txn-search").value.trim();
  const dateFrom = document.getElementById("txn-date-from").value;
  const dateTo = document.getElementById("txn-date-to").value;

  const params = new URLSearchParams();
  if (dateFrom) params.set("date_from", dateFrom);
  if (dateTo) params.set("date_to", dateTo);
  if (account_id) params.set("account_id", account_id);
  if (category_id) params.set("category_id", category_id);
  if (search) params.set("search", search);

  const txns = await api("/api/transactions?" + params.toString());
  renderTransactionsTable(document.getElementById("txn-table"), txns, true, loadTransactionsTab);
}

document.getElementById("txn-filter-btn").addEventListener("click", loadTransactionsTab);
document.getElementById("txn-clear-btn").addEventListener("click", () => {
  for (const id of ["txn-account", "txn-category", "txn-search", "txn-date-from", "txn-date-to"]) {
    document.getElementById(id).value = "";
  }
  loadTransactionsTab();
});

// ---------- Split editor ----------

let splitEditorTxn = null;
let splitEditorOnChanged = null;

function makeSplitRow(categoryId, amount) {
  const catSelect = el("select");
  catSelect.appendChild(el("option", { value: "", text: "Choose category..." }));
  for (const c of CATEGORIES) {
    const opt = el("option", { value: c.id, text: c.name });
    if (categoryId != null && String(categoryId) === String(c.id)) opt.selected = true;
    catSelect.appendChild(opt);
  }
  const amountInput = el("input", { type: "number", step: "0.01" });
  amountInput.value = amount != null ? amount : "";
  amountInput.addEventListener("input", updateSplitRemaining);
  catSelect.addEventListener("change", updateSplitRemaining);

  const removeBtn = el("button", {
    text: "×", class: "remove-row-btn", type: "button",
    onclick: () => { row.remove(); updateSplitRemaining(); },
  });

  const row = el("div", { class: "split-row" }, [catSelect, amountInput, removeBtn]);
  return row;
}

function updateSplitRemaining() {
  if (!splitEditorTxn) return;
  const rows = document.querySelectorAll("#split-rows .split-row");
  let sum = 0;
  rows.forEach(row => {
    const input = row.querySelector("input");
    sum += parseFloat(input.value) || 0;
  });
  const remaining = splitEditorTxn.amount - sum;
  const el2 = document.getElementById("split-remaining");
  const balanced = Math.abs(remaining) < 0.01;
  el2.textContent = balanced
    ? `Balanced: ${fmtMoney(sum)} of ${fmtMoney(splitEditorTxn.amount)}`
    : `${fmtMoney(sum)} entered, ${fmtMoney(remaining)} remaining of ${fmtMoney(splitEditorTxn.amount)}`;
  el2.className = balanced ? "hint balanced" : "hint unbalanced";
}

function openSplitEditor(t, onChanged) {
  splitEditorTxn = t;
  splitEditorOnChanged = onChanged;

  document.getElementById("split-modal-desc").textContent =
    `${t.date} — ${t.description} — total ${fmtMoney(t.amount)}`;

  const rowsContainer = document.getElementById("split-rows");
  rowsContainer.innerHTML = "";

  if (t.splits && t.splits.length) {
    for (const s of t.splits) rowsContainer.appendChild(makeSplitRow(s.category_id, s.amount));
  } else {
    rowsContainer.appendChild(makeSplitRow(t.category_id, t.amount));
    rowsContainer.appendChild(makeSplitRow(null, 0));
  }

  updateSplitRemaining();
  document.getElementById("split-modal").classList.remove("hidden");
}

function closeSplitEditor() {
  document.getElementById("split-modal").classList.add("hidden");
  splitEditorTxn = null;
  splitEditorOnChanged = null;
}

document.getElementById("split-add-row-btn").addEventListener("click", () => {
  document.getElementById("split-rows").appendChild(makeSplitRow(null, ""));
});

document.getElementById("split-cancel-btn").addEventListener("click", closeSplitEditor);

document.getElementById("split-save-btn").addEventListener("click", async () => {
  const rows = document.querySelectorAll("#split-rows .split-row");
  const splits = [];
  for (const row of rows) {
    const select = row.querySelector("select");
    const input = row.querySelector("input");
    const categoryId = select.value;
    const amount = parseFloat(input.value);
    if (!categoryId || Number.isNaN(amount)) {
      alert("Every row needs a category and an amount (or remove the row).");
      return;
    }
    splits.push({ category_id: parseInt(categoryId, 10), amount });
  }
  if (splits.length < 2) {
    alert("A split needs at least two categories.");
    return;
  }
  const total = splits.reduce((sum, s) => sum + s.amount, 0);
  if (Math.abs(total - splitEditorTxn.amount) > 0.01) {
    alert(`Split amounts must add up to ${fmtMoney(splitEditorTxn.amount)} (currently ${fmtMoney(total)}).`);
    return;
  }

  if (splitEditorTxn.staged) {
    splitEditorTxn.splits = splits.map(s => {
      const cat = CATEGORIES.find(c => c.id === s.category_id);
      return { ...s, category_name: cat ? cat.name : "Unknown", category_color: cat ? cat.color : "#999999" };
    });
    splitEditorTxn.edited = true;
    const onChanged = splitEditorOnChanged;
    closeSplitEditor();
    if (onChanged) onChanged();
    return;
  }

  try {
    await api(`/api/transactions/${splitEditorTxn.id}/splits`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ splits }),
    });
    const onChanged = splitEditorOnChanged;
    closeSplitEditor();
    if (onChanged) onChanged();
  } catch (e) {
    alert("Couldn't save split: " + e.message);
  }
});

document.getElementById("split-remove-btn").addEventListener("click", async () => {
  if (!confirm("Remove this split and go back to a single category?")) return;
  if (splitEditorTxn.staged) {
    splitEditorTxn.splits = null;
    splitEditorTxn.edited = true;
    const onChanged = splitEditorOnChanged;
    closeSplitEditor();
    if (onChanged) onChanged();
    return;
  }
  try {
    await api(`/api/transactions/${splitEditorTxn.id}/splits`, { method: "DELETE" });
    const onChanged = splitEditorOnChanged;
    closeSplitEditor();
    if (onChanged) onChanged();
  } catch (e) {
    alert("Couldn't remove split: " + e.message);
  }
});

// ---------- Manage tab ----------

async function loadAccountsTable() {
  const table = document.getElementById("accounts-table");
  table.innerHTML = "";
  table.appendChild(el("thead", {}, [el("tr", {}, [
    el("th", { text: "Name" }), el("th", { text: "Institution" }),
    el("th", { text: "Type" }), el("th", { text: "Last 4" }),
  ])]));
  const tbody = el("tbody");
  for (const a of ACCOUNTS) {
    tbody.appendChild(el("tr", {}, [
      el("td", { text: a.name }), el("td", { text: a.institution }),
      el("td", { text: a.account_type }), el("td", { text: a.last4 || "" }),
    ]));
  }
  table.appendChild(tbody);
}

async function loadCategoriesTable() {
  const table = document.getElementById("categories-table");
  table.innerHTML = "";
  table.appendChild(el("thead", {}, [el("tr", {}, [
    el("th", { text: "Name" }), el("th", { text: "Color" }), el("th", { text: "Group" }),
    el("th", { text: "Budget" }), el("th", { text: "" }),
  ])]));
  const tbody = el("tbody");
  for (const c of CATEGORIES) {
    tbody.appendChild(buildCategoryRow(c));
  }
  table.appendChild(tbody);
}

async function loadGroupsTable() {
  const table = document.getElementById("groups-table");
  table.innerHTML = "";
  table.appendChild(el("thead", {}, [el("tr", {}, [
    el("th", { text: "Name" }), el("th", { text: "Color" }), el("th", { text: "Categories" }), el("th", { text: "" }),
  ])]));
  const tbody = el("tbody");
  for (const g of GROUPS) {
    const count = CATEGORIES.filter(c => c.group_id === g.id).length;
    const delBtn = el("button", { text: "Delete", onclick: async () => {
      if (!confirm(`Delete group "${g.name}"? Its categories become ungrouped, not deleted.`)) return;
      try {
        await api(`/api/category_groups/${g.id}`, { method: "DELETE" });
        await loadCategoryGroups();
        await loadCategories();
        await loadGroupsTable();
        await loadCategoriesTable();
      } catch (e) {
        alert("Couldn't delete group: " + e.message);
      }
    }});
    tbody.appendChild(el("tr", {}, [
      el("td", { text: g.name }),
      el("td", {}, [el("span", { class: "color-dot", style: `background:${g.color}` })]),
      el("td", { text: String(count) }), el("td", {}, [delBtn]),
    ]));
  }
  table.appendChild(tbody);
}

document.getElementById("group-add-btn").addEventListener("click", async () => {
  const name = document.getElementById("group-name").value.trim();
  const color = document.getElementById("group-color").value;
  if (!name) return alert("Enter a group name");
  try {
    await api("/api/category_groups", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, color }),
    });
    document.getElementById("group-name").value = "";
    await loadCategoryGroups();
    await loadGroupsTable();
  } catch (e) {
    alert("Couldn't add group: " + e.message);
  }
});

function buildCategoryRow(c) {
  const delBtn = el("button", { text: "Delete", onclick: async () => {
    try {
      await api(`/api/categories/${c.id}`, { method: "DELETE" });
      await loadCategories();
      await loadCategoriesTable();
      await loadRulesTable();
    } catch (e) {
      alert("Couldn't delete category: " + e.message);
    }
  }});
  const editBtn = el("button", { text: "Edit", onclick: () => {
    row.replaceWith(buildCategoryEditRow(c));
  }});

  const mergeSelect = el("select", { style: "font-size:12px;padding:3px;" });
  mergeSelect.appendChild(el("option", { value: "", text: "Merge into..." }));
  for (const other of CATEGORIES) {
    if (other.id === c.id) continue;
    mergeSelect.appendChild(el("option", { value: other.id, text: other.name }));
  }
  mergeSelect.addEventListener("change", async () => {
    const targetId = mergeSelect.value;
    if (!targetId) return;
    const targetName = CATEGORIES.find(o => String(o.id) === targetId).name;
    if (!confirm(`Move everything in "${c.name}" into "${targetName}" and delete "${c.name}"? This can't be undone.`)) {
      mergeSelect.value = "";
      return;
    }
    try {
      const result = await api(`/api/categories/${c.id}/merge_into/${targetId}`, { method: "POST" });
      alert(`Moved ${result.transactions_moved} transaction(s) and ${result.rules_moved} rule(s) into ${targetName}.`);
      await loadCategories();
      await loadCategoriesTable();
      await loadRulesTable();
    } catch (e) {
      alert("Couldn't merge category: " + e.message);
    }
  });

  const nameCell = el("td", {}, [
    document.createTextNode(c.name),
    ...(c.is_transfer ? [el("span", { class: "hint", text: " (transfer)" })] : []),
  ]);
  const row = el("tr", {}, [
    nameCell,
    el("td", {}, [el("span", { class: "color-dot", style: `background:${c.color}` })]),
    el("td", { text: c.group_name || "—" }),
    el("td", { text: c.budget_frequency === "annual" ? "Annual" : "Monthly" }),
    el("td", { style: "display:flex;gap:6px;" }, [mergeSelect, editBtn, delBtn]),
  ]);
  return row;
}

function buildCategoryEditRow(c) {
  const nameInput = el("input", { value: c.name, style: "width:160px" });
  nameInput.value = c.name;
  const colorInput = el("input", { type: "color", style: "padding:2px;width:44px" });
  colorInput.value = c.color;
  const transferCheckbox = el("input", { type: "checkbox" });
  transferCheckbox.checked = !!c.is_transfer;

  const groupSelect = el("select", { style: "font-size:12px;padding:3px;" });
  groupSelect.appendChild(el("option", { value: "", text: "(none)" }));
  for (const g of GROUPS) {
    const opt = el("option", { value: g.id, text: g.name });
    if (c.group_id === g.id) opt.selected = true;
    groupSelect.appendChild(opt);
  }

  const freqSelect = el("select", { style: "font-size:12px;padding:3px;" });
  freqSelect.appendChild(el("option", { value: "monthly", text: "Monthly" }));
  freqSelect.appendChild(el("option", { value: "annual", text: "Annual" }));
  freqSelect.value = c.budget_frequency === "annual" ? "annual" : "monthly";

  const saveBtn = el("button", { text: "Save", class: "primary", onclick: async () => {
    const name = nameInput.value.trim();
    if (!name) return alert("Category name can't be empty");
    try {
      await api(`/api/categories/${c.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name, color: colorInput.value, is_transfer: transferCheckbox.checked,
          group_id: groupSelect.value ? parseInt(groupSelect.value, 10) : null,
          budget_frequency: freqSelect.value,
        }),
      });
      await loadCategories();
      await loadCategoriesTable();
      await loadGroupsTable();
      await loadRulesTable();
    } catch (e) {
      alert("Couldn't save category: " + e.message);
    }
  }});
  const cancelBtn = el("button", { text: "Cancel", onclick: () => {
    row.replaceWith(buildCategoryRow(c));
  }});

  const row = el("tr", {}, [
    el("td", {}, [nameInput]),
    el("td", {}, [colorInput]),
    el("td", {}, [groupSelect]),
    el("td", {}, [freqSelect]),
    el("td", { style: "display:flex;gap:8px;align-items:center;flex-wrap:wrap;" }, [
      el("label", { style: "flex-direction:row;align-items:center;gap:4px;font-size:12px;" }, [
        transferCheckbox, document.createTextNode("Transfer"),
      ]),
      saveBtn, cancelBtn,
    ]),
  ]);
  return row;
}

document.getElementById("cat-add-btn").addEventListener("click", async () => {
  const name = document.getElementById("cat-name").value.trim();
  const color = document.getElementById("cat-color").value;
  const is_transfer = document.getElementById("cat-is-transfer").checked;
  const groupVal = document.getElementById("cat-group").value;
  const budget_frequency = document.getElementById("cat-frequency").value;
  if (!name) return alert("Enter a category name");
  try {
    await api("/api/categories", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name, color, is_transfer, group_id: groupVal ? parseInt(groupVal, 10) : null, budget_frequency,
      }),
    });
    document.getElementById("cat-name").value = "";
    document.getElementById("cat-is-transfer").checked = false;
    document.getElementById("cat-group").value = "";
    document.getElementById("cat-frequency").value = "monthly";
    await loadCategories();
    await loadCategoriesTable();
    await loadGroupsTable();
  } catch (e) {
    alert("Couldn't add category: " + e.message);
  }
});

async function loadRulesTable() {
  const rules = await api("/api/rules");
  const table = document.getElementById("rules-table");
  table.innerHTML = "";
  table.appendChild(el("thead", {}, [el("tr", {}, [
    el("th", { text: "Pattern" }), el("th", { text: "Type" }), el("th", { text: "Category" }),
    el("th", { text: "Priority" }), el("th", { text: "" }),
  ])]));
  const tbody = el("tbody");
  for (const r of rules) {
    const delBtn = el("button", { text: "Delete", onclick: async () => {
      try {
        await api(`/api/rules/${r.id}`, { method: "DELETE" });
        loadRulesTable();
      } catch (e) {
        alert("Couldn't delete rule: " + e.message);
      }
    }});
    const applyBtn = el("button", { text: "Apply to existing", onclick: async () => {
      try {
        const result = await api(`/api/rules/${r.id}/apply`, { method: "POST" });
        alert(`Updated ${result.updated} existing transaction(s) to ${r.category_name}.`);
      } catch (e) {
        alert("Couldn't apply rule: " + e.message);
      }
    }});
    tbody.appendChild(el("tr", {}, [
      el("td", { text: r.pattern }), el("td", { text: r.match_type }),
      el("td", {}, [el("span", { class: "color-dot", style: `background:${r.category_color}` }), document.createTextNode(r.category_name)]),
      el("td", { text: r.priority }), el("td", { style: "display:flex;gap:6px;" }, [applyBtn, delBtn]),
    ]));
  }
  table.appendChild(tbody);
}

document.getElementById("rule-add-btn").addEventListener("click", async () => {
  const pattern = document.getElementById("rule-pattern").value.trim();
  const category_id = parseInt(document.getElementById("rule-category").value, 10);
  const priority = parseInt(document.getElementById("rule-priority").value, 10) || 150;
  const apply_to_existing = document.getElementById("rule-apply-existing").checked;
  if (!pattern || !category_id) return alert("Enter a pattern and choose a category");
  try {
    const result = await api("/api/rules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pattern, category_id, priority, match_type: "contains", apply_to_existing }),
    });
    document.getElementById("rule-pattern").value = "";
    loadRulesTable();
    if (apply_to_existing) {
      alert(`Rule added. Updated ${result.updated} existing transaction(s).`);
    }
  } catch (e) {
    alert("Couldn't add rule: " + e.message);
  }
});

async function loadImportBatches() {
  const batches = await api("/api/import_batches");
  const table = document.getElementById("batches-table");
  table.innerHTML = "";
  table.appendChild(el("thead", {}, [el("tr", {}, [
    el("th", { text: "Date" }), el("th", { text: "Account" }), el("th", { text: "File" }),
    el("th", { text: "Imported" }), el("th", { text: "Skipped" }),
    el("th", { text: "Calculated total" }), el("th", { text: "Statement total" }), el("th", { text: "Difference" }),
    el("th", { text: "" }),
  ])]));
  const tbody = el("tbody");
  for (const b of batches) {
    const undoBtn = el("button", { text: "Undo", onclick: async () => {
      if (!confirm(`Remove the ${b.row_count} transaction(s) from this import?`)) return;
      try {
        await api(`/api/import_batches/${b.id}`, { method: "DELETE" });
        loadImportBatches();
      } catch (e) {
        alert("Couldn't undo import: " + e.message);
      }
    }});

    const reassignSelect = el("select", { style: "font-size:12px;padding:3px;" });
    reassignSelect.appendChild(el("option", { value: "", text: "Reassign to..." }));
    for (const a of ACCOUNTS) {
      if (a.id === b.account_id) continue;
      reassignSelect.appendChild(el("option", { value: a.id, text: a.name }));
    }
    reassignSelect.addEventListener("change", async () => {
      const targetId = reassignSelect.value;
      if (!targetId) return;
      const targetName = ACCOUNTS.find(a => String(a.id) === targetId).name;
      if (!confirm(`Move all ${b.row_count} transaction(s) from "${b.filename}" to ${targetName}?`)) {
        reassignSelect.value = "";
        return;
      }
      try {
        const result = await api(`/api/import_batches/${b.id}/reassign`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ account_id: parseInt(targetId, 10) }),
        });
        alert(`Moved ${result.updated} transaction(s) to ${targetName}.` +
          (result.conflicts ? ` ${result.conflicts} already existed there and were left alone.` : ""));
        await loadImportBatches();
        await Promise.all([loadMonthView(), loadYearView(), loadTransactionsTab()]);
      } catch (e) {
        alert("Couldn't reassign: " + e.message);
      }
    });

    const totalInput = el("input", { type: "number", step: "0.01", placeholder: "type & press Enter", style: "width:110px" });
    if (b.statement_total !== null && b.statement_total !== undefined) totalInput.value = b.statement_total;
    totalInput.addEventListener("keydown", async (ev) => {
      if (ev.key !== "Enter") return;
      const val = totalInput.value.trim();
      try {
        await api(`/api/import_batches/${b.id}/statement_total`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ statement_total: val === "" ? null : parseFloat(val) }),
        });
        await loadImportBatches();
      } catch (e) {
        alert("Couldn't save statement total: " + e.message);
      }
    });

    let diffCell;
    if (b.statement_total === null || b.statement_total === undefined) {
      diffCell = el("td", { class: "hint", text: "—" });
    } else {
      const diff = b.difference;
      const matches = Math.abs(diff) < 0.01;
      diffCell = el("td", {
        class: "amount-cell " + (matches ? "positive" : "negative"),
        text: matches ? "Matches" : fmtMoney(diff),
      });
    }

    tbody.appendChild(el("tr", {}, [
      el("td", { text: b.imported_at }), el("td", { text: b.account_name }),
      el("td", { text: b.filename }), el("td", { text: b.row_count }),
      el("td", { text: b.skipped_count }),
      el("td", { class: "amount-cell " + amountClass(b.calculated_total), text: fmtMoney(b.calculated_total) }),
      el("td", {}, [totalInput]),
      diffCell,
      el("td", { style: "display:flex;gap:6px;" }, [reassignSelect, undoBtn]),
    ]));
  }
  table.appendChild(tbody);
}

// ---------- Reconcile tab ----------

let rcLastRun = null;
let rcEditingId = null;

function rcMarkStale() {
  if (rcLastRun) {
    document.getElementById("rc-run-status").textContent = "Statements changed since the last run -- run again to update the results.";
  }
}

document.getElementById("rc-run-btn").addEventListener("click", async () => {
  const btn = document.getElementById("rc-run-btn");
  const status = document.getElementById("rc-run-status");
  const accountId = document.getElementById("rc-run-account").value;
  btn.disabled = true;
  status.textContent = "Running...";
  try {
    rcLastRun = await api("/api/reconcile/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account_id: accountId ? parseInt(accountId, 10) : null }),
    });
    status.textContent = "";
    renderReconcileResults();
  } catch (e) {
    status.textContent = "";
    alert("Couldn't run the reconciliation: " + e.message);
  } finally {
    btn.disabled = false;
  }
});

document.getElementById("rc-only-flagged").addEventListener("change", () => {
  if (rcLastRun) renderReconcileResults();
});

function moneyOrDash(n) {
  return n === null || n === undefined ? "—" : fmtMoney(n);
}

function renderReconcileResults() {
  const run = rcLastRun;
  document.getElementById("rc-results").classList.remove("hidden");

  const summary = document.getElementById("rc-summary");
  summary.innerHTML = "";
  if (run.statements.length === 0) {
    summary.textContent = "No statements on file yet. Add one below, or read it from a statement PDF.";
  } else {
    summary.appendChild(document.createTextNode(`Checked ${run.statements.length} statement(s) at ${run.ran_at.slice(11)}: `));
    summary.appendChild(el("strong", { class: "amount-cell positive", text: `${run.matched} match` }));
    summary.appendChild(document.createTextNode(", "));
    summary.appendChild(el("strong", { class: run.flagged ? "amount-cell negative" : "", text: `${run.flagged} off` }));
    summary.appendChild(document.createTextNode(", "));
    summary.appendChild(el("strong", { class: run.incomplete ? "rc-warn" : "", text: `${run.incomplete} incomplete` }));
    summary.appendChild(document.createTextNode("."));
  }

  const onlyFlagged = document.getElementById("rc-only-flagged").checked;
  const rows = run.statements.filter(r => !onlyFlagged || r.status !== "match");

  const table = document.getElementById("rc-table");
  table.innerHTML = "";
  if (rows.length === 0) return;
  table.appendChild(el("thead", {}, [el("tr", {}, [
    el("th", { text: "Account" }), el("th", { text: "Statement period" }), el("th", { text: "Balance (previous → new)" }),
    el("th", { text: "Statement change" }), el("th", { text: "Imported change" }), el("th", { text: "Difference" }),
    el("th", { text: "Txns" }), el("th", { text: "Status" }), el("th", { text: "" }),
  ])]));

  const tbody = el("tbody");
  for (const r of rows) {
    const statusCell = r.status === "match"
      ? el("td", { class: "amount-cell positive", text: "Matches" })
      : r.status === "mismatch"
        ? el("td", { class: "amount-cell negative", text: "Off by " + fmtMoney(Math.abs(r.difference)) })
        : el("td", { class: "rc-warn", text: "Incomplete" });

    const detailRow = el("tr", { class: "hidden" }, [el("td", { colspan: "9" })]);
    let detailLoaded = false;
    const toggle = el("button", { type: "button", class: "split-edit-btn", text: "Details", onclick: () => {
      const opening = detailRow.classList.contains("hidden");
      detailRow.classList.toggle("hidden");
      toggle.textContent = opening ? "Hide" : "Details";
      if (opening && !detailLoaded) {
        detailLoaded = true;
        detailRow.firstChild.appendChild(buildReconcileDetail(r));
      }
    }});

    tbody.appendChild(el("tr", { class: r.status === "mismatch" ? "rc-flagged" : r.status === "incomplete" ? "rc-incomplete" : "" }, [
      el("td", { text: r.account_name }),
      el("td", { text: `${r.period_start || "?"} – ${r.period_end}` }),
      el("td", { text: `${moneyOrDash(r.previous_balance)} → ${fmtMoney(r.new_balance)}` }),
      el("td", { text: moneyOrDash(r.statement_change) }),
      el("td", { text: moneyOrDash(r.imported_change) }),
      el("td", { class: r.status === "mismatch" ? "amount-cell negative" : "", text: moneyOrDash(r.difference) }),
      el("td", { text: r.txn_count }),
      statusCell,
      el("td", {}, [toggle]),
    ]));
    tbody.appendChild(detailRow);
  }
  table.appendChild(tbody);
}

function buildReconcileDetail(r) {
  const box = el("div", { class: "rc-detail" });
  for (const note of r.notes) box.appendChild(el("p", { class: "rc-note", text: note }));
  if (r.possible_duplicates.length) {
    box.appendChild(el("p", { class: "rc-note", text: "Transaction(s) whose amount equals the difference:" }));
    for (const d of r.possible_duplicates) {
      box.appendChild(el("div", { class: "rc-dup", text: `${d.date}   ${d.description}   ${fmtMoney(d.amount)}` }));
    }
  }
  if (r.source_file) box.appendChild(el("p", { class: "hint", text: "Statement read from: " + r.source_file }));

  const holder = el("div", { class: "hint", text: "Loading the transactions that were counted..." });
  box.appendChild(holder);
  api(`/api/statements/${r.statement_id}/transactions`).then(res => {
    holder.innerHTML = "";
    if (res.transactions.length === 0) {
      holder.textContent = "No transactions were counted for this statement.";
      return;
    }
    holder.appendChild(el("p", { class: "hint", text: `Transactions counted (${res.transactions.length}):` }));
    const table = el("table", { class: "txn-table" });
    table.appendChild(el("thead", {}, [el("tr", {}, [
      el("th", { text: "Date" }), el("th", { text: "Description" }), el("th", { text: "Amount" }), el("th", { text: "Counted because" }),
    ])]));
    table.appendChild(el("tbody", {}, res.transactions.map(t => el("tr", {}, [
      el("td", { text: t.date }), el("td", { text: t.description }),
      el("td", { class: "amount-cell " + amountClass(t.amount), text: fmtMoney(t.amount) }),
      el("td", { class: "hint", text: t.source === "statement" ? "Listed on the statement PDF" : "Dated within the period" }),
    ]))));
    holder.appendChild(table);
  }).catch(e => { holder.textContent = "Couldn't load the transactions: " + e.message; });
  return box;
}

// -- statements on file --

async function loadStatements() {
  const statements = await api("/api/statements");
  const table = document.getElementById("rc-statements-table");
  table.innerHTML = "";
  if (statements.length === 0) return;
  table.appendChild(el("thead", {}, [el("tr", {}, [
    el("th", { text: "Account" }), el("th", { text: "Start" }), el("th", { text: "Closing" }),
    el("th", { text: "Previous balance" }), el("th", { text: "New balance" }), el("th", { text: "Source" }), el("th", { text: "" }),
  ])]));
  const tbody = el("tbody");
  for (const s of statements) {
    const editBtn = el("button", { type: "button", text: "Edit", onclick: () => startEditStatement(s) });
    const deleteBtn = el("button", { type: "button", text: "Delete", onclick: async () => {
      if (!confirm(`Delete the ${s.account_name} statement closing ${s.period_end}? Your transactions aren't affected.`)) return;
      try {
        await api(`/api/statements/${s.id}`, { method: "DELETE" });
        if (rcEditingId === s.id) resetStatementForm();
        await loadStatements();
        rcMarkStale();
      } catch (e) {
        alert("Couldn't delete: " + e.message);
      }
    }});
    tbody.appendChild(el("tr", {}, [
      el("td", { text: s.account_name }),
      s.period_start ? el("td", { text: s.period_start }) : el("td", { class: "hint", text: "(after previous)" }),
      el("td", { text: s.period_end }),
      s.previous_balance !== null ? el("td", { text: fmtMoney(s.previous_balance) }) : el("td", { class: "hint", text: "(previous new balance)" }),
      el("td", { text: fmtMoney(s.new_balance) }),
      el("td", { class: "hint", text: s.source_file || "entered by hand" }),
      el("td", { style: "display:flex;gap:6px;" }, [editBtn, deleteBtn]),
    ]));
  }
  table.appendChild(tbody);
}

function resetStatementForm() {
  rcEditingId = null;
  for (const id of ["rc-start", "rc-end", "rc-prev", "rc-new"]) document.getElementById(id).value = "";
  document.getElementById("rc-form-title").textContent = "Or enter a statement by hand";
  document.getElementById("rc-save-btn").textContent = "Save statement";
  document.getElementById("rc-cancel-btn").classList.add("hidden");
}

function startEditStatement(s) {
  rcEditingId = s.id;
  document.getElementById("rc-account").value = s.account_id;
  document.getElementById("rc-start").value = s.period_start || "";
  document.getElementById("rc-end").value = s.period_end;
  document.getElementById("rc-prev").value = s.previous_balance === null ? "" : s.previous_balance;
  document.getElementById("rc-new").value = s.new_balance;
  document.getElementById("rc-form-title").textContent = `Editing the ${s.account_name} statement closing ${s.period_end}`;
  document.getElementById("rc-save-btn").textContent = "Update statement";
  document.getElementById("rc-cancel-btn").classList.remove("hidden");
  document.getElementById("rc-form-title").scrollIntoView({ behavior: "smooth", block: "center" });
}

document.getElementById("rc-cancel-btn").addEventListener("click", resetStatementForm);

document.getElementById("rc-save-btn").addEventListener("click", async () => {
  const accountId = document.getElementById("rc-account").value;
  const end = document.getElementById("rc-end").value;
  const newBalance = document.getElementById("rc-new").value.trim();
  if (!accountId) return alert("Choose an account first");
  if (!end) return alert("Enter the statement's closing date");
  if (newBalance === "") return alert("Enter the statement's new balance");
  const prev = document.getElementById("rc-prev").value.trim();
  const body = {
    account_id: parseInt(accountId, 10),
    period_start: document.getElementById("rc-start").value || null,
    period_end: end,
    previous_balance: prev === "" ? null : parseFloat(prev),
    new_balance: parseFloat(newBalance),
  };
  try {
    await api(rcEditingId ? `/api/statements/${rcEditingId}` : "/api/statements", {
      method: rcEditingId ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    resetStatementForm();
    await loadStatements();
    rcMarkStale();
  } catch (e) {
    alert("Couldn't save the statement: " + e.message);
  }
});

document.getElementById("rc-pdf-btn").addEventListener("click", async () => {
  const accountId = document.getElementById("rc-account").value;
  const files = Array.from(document.getElementById("rc-pdf").files);
  if (!accountId) return alert("Choose an account first");
  if (files.length === 0) return alert("Choose one or more statement PDFs first");

  const btn = document.getElementById("rc-pdf-btn");
  const out = document.getElementById("rc-pdf-result");
  btn.disabled = true;
  out.innerHTML = "";
  const line = (text, cls) => out.appendChild(el("div", { class: cls || "hint", text }));
  for (const file of files) {
    const form = new FormData();
    form.append("file", file);
    form.append("account_id", accountId);
    try {
      const r = await api("/api/statements/from_pdf", { method: "POST", body: form });
      const what = r.status === "saved" ? "statement saved" : "statement was already on file (left unchanged)";
      const lines = r.lines_in_pdf === undefined ? "" :
        `; ${r.lines_in_database} of ${r.lines_in_pdf} transaction lines are in the database` +
        (r.lines_identical ? ` (${r.lines_identical} identical line(s) stored once)` : "");
      line(`${file.name}: closing ${r.period_end}, new balance ${fmtMoney(r.new_balance)} -- ${what}${lines}.`,
        r.missing && r.missing.length ? "rc-warn" : "hint");
      for (const m of r.missing || []) line(`      not in the database: ${m.date}  ${m.description}  ${fmtMoney(m.amount)}`, "rc-warn");
      if (r.note) line("      " + r.note);
    } catch (e) {
      line(`${file.name}: ${e.message}`, "rc-warn");
    }
  }
  document.getElementById("rc-pdf").value = "";
  btn.disabled = false;
  await loadStatements();
  rcMarkStale();
});

// ---------- Danger zone: trim old transactions ----------

document.getElementById("trim-preview-btn").addEventListener("click", async () => {
  const before = document.getElementById("trim-before").value;
  if (!before) return alert("Choose a cutoff date first");
  try {
    const result = await api(`/api/transactions/trim_preview?before=${before}`);
    const summaryEl = document.getElementById("trim-preview-summary");
    if (result.count === 0) {
      summaryEl.textContent = `No transactions dated before ${before}.`;
      document.getElementById("trim-preview-table").innerHTML = "";
      document.getElementById("trim-preview-result").classList.remove("hidden");
      document.getElementById("trim-confirm-btn").classList.add("hidden");
      return;
    }
    summaryEl.textContent = `${result.count} transaction(s) dated before ${before} will be permanently deleted:`;
    const table = document.getElementById("trim-preview-table");
    table.innerHTML = "";
    table.appendChild(el("thead", {}, [el("tr", {}, [
      el("th", { text: "Account" }), el("th", { text: "Count" }),
      el("th", { text: "Earliest" }), el("th", { text: "Latest" }),
    ])]));
    const tbody = el("tbody");
    for (const row of result.by_account) {
      tbody.appendChild(el("tr", {}, [
        el("td", { text: row.account_name }), el("td", { text: row.c }),
        el("td", { text: row.earliest }), el("td", { text: row.latest }),
      ]));
    }
    table.appendChild(tbody);
    document.getElementById("trim-preview-result").classList.remove("hidden");
    document.getElementById("trim-confirm-btn").classList.remove("hidden");
    document.getElementById("trim-confirm-btn").dataset.before = before;
    document.getElementById("trim-confirm-btn").dataset.count = result.count;
  } catch (e) {
    alert("Couldn't preview: " + e.message);
  }
});

document.getElementById("trim-confirm-btn").addEventListener("click", async () => {
  const btn = document.getElementById("trim-confirm-btn");
  const before = btn.dataset.before;
  const count = btn.dataset.count;
  if (!confirm(`Permanently delete ${count} transaction(s) dated before ${before}? A backup will be made first, but this can't be undone from within the app.`)) {
    return;
  }
  try {
    const result = await api("/api/transactions/trim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ before }),
    });
    alert(`Deleted ${result.deleted} transaction(s). Backup saved to: ${result.backup_path}`);
    document.getElementById("trim-preview-result").classList.add("hidden");
    document.getElementById("trim-before").value = "";
    await Promise.all([loadMonthView(), loadYearView(), loadTransactionsTab(), loadImportBatches()]);
  } catch (e) {
    alert("Couldn't delete transactions: " + e.message);
  }
});

// ---------- init ----------

(async function init() {
  populateYearSelects();
  try {
    await Promise.all([loadCategoryGroups(), loadAccounts()]);
    await loadCategories();
    await Promise.all([loadMonthView(), loadYearView(), loadBudgetTab(), loadAccountsTable(), loadCategoriesTable(), loadGroupsTable(), loadRulesTable(), loadImportBatches(), loadUploadTracker(), loadStatements()]);
  } catch (e) {
    alert(
      "Couldn't connect to the app's local server (" + e.message + "). " +
      "If you just started the app, wait a few seconds and reload this page."
    );
  }
})();
