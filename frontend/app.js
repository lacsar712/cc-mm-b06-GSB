const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const rows = document.querySelector("#rows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const searchMsg = document.querySelector("#search-msg");
const fragmentInput = document.querySelector("#fragment");
const viewList = document.querySelector("#view-list");
const viewDossier = document.querySelector("#view-dossier");
const dossierRows = document.querySelector("#dossier-rows");
const thOp = document.querySelector("#th-op");

let appliedFragment = "";
let activeDossierId = null;

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]),
  );
}

function levelClass(level) {
  return level === "报警" ? "alarm" : "ok";
}

function paint(list) {
  if (role === "writer") {
    rows.innerHTML = list
      .map(
        (r) =>
          `<tr><td>${esc(r.site)}</td><td>${r.ch4_pct}</td>` +
          `<td class="${levelClass(r.level)}">${esc(r.level)}</td><td>${esc(r.note)}</td>` +
          `<td><input data-id="${r.id}" class="rename-site" value="${esc(r.site)}" />` +
          `<button data-id="${r.id}" class="rename-go" type="button">改正</button></td></tr>`,
      )
      .join("");
  } else {
    rows.innerHTML = list
      .map(
        (r) =>
          `<tr><td>${esc(r.site)}</td><td>${r.ch4_pct}</td>` +
          `<td class="${levelClass(r.level)}">${esc(r.level)}</td><td>${esc(r.note)}</td>` +
          `<td class="muted">只读</td></tr>`,
      )
      .join("");
  }
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "请求失败");
  return data;
}

function showApp() {
  loginBox.hidden = true;
  appBox.hidden = false;
  document.querySelector("#who").textContent = role === "writer" ? "检查员" : "旁观";
  document.querySelector("#out").hidden = false;
  form.hidden = role !== "writer";
  thOp.hidden = role !== "writer";
  connect();
  load();
  loadDossiers();
}

async function load() {
  appliedFragment = "";
  fragmentInput.value = "";
  searchMsg.textContent = "";
  paint(await api("/api/readings"));
}

async function refreshCurrent() {
  if (activeDossierId !== null) {
    await openDossier(activeDossierId);
  } else if (appliedFragment) {
    await runSearch(appliedFragment);
  } else {
    paint(await api("/api/readings"));
  }
  await loadDossiers();
}

async function runSearch(fragment) {
  try {
    const list = await api(`/api/readings?site=${encodeURIComponent(fragment)}`);
    searchMsg.textContent = `命中 ${list.length} 条`;
    paint(list);
    return true;
  } catch (err) {
    if (err.message === "无匹配测点") {
      // 查不到时提示无匹配测点，禁止回退成全表
      searchMsg.textContent = "无匹配测点";
      rows.innerHTML = "";
      return false;
    }
    searchMsg.textContent = err.message;
    return false;
  }
}

document.querySelector("#search").onclick = async () => {
  const fragment = fragmentInput.value.trim();
  if (!fragment) {
    searchMsg.textContent = "请输入测点片段";
    return;
  }
  appliedFragment = fragment;
  await runSearch(fragment);
};

document.querySelector("#clear-search").onclick = load;

document.querySelector("#mk-dossier").onclick = async () => {
  const fragment = fragmentInput.value.trim();
  if (!fragment) {
    searchMsg.textContent = "请输入测点片段";
    return;
  }
  try {
    const dossier = await api("/api/dossiers", {
      method: "POST",
      body: JSON.stringify({ fragment }),
    });
    appliedFragment = fragment;
    searchMsg.textContent = `已存卷宗 #${dossier.id}`;
    await loadDossiers();
    await openDossier(dossier.id);
  } catch (err) {
    if (err.message === "无匹配测点") {
      searchMsg.textContent = "无匹配测点，未建卷宗";
      rows.innerHTML = "";
    } else {
      searchMsg.textContent = err.message;
    }
  }
};

async function loadDossiers() {
  const list = await api("/api/dossiers");
  const box = document.querySelector("#dossiers");
  box.innerHTML = list
    .map(
      (d) =>
        `<tr><td>#${d.id}</td><td>${esc(d.fragment)}</td><td>${d.item_count}</td>` +
        `<td>${esc(d.created_by)}</td><td><button data-id="${d.id}" class="open-dossier" type="button">打开卷宗</button></td></tr>`,
    )
    .join("");
}

async function openDossier(id) {
  const d = await api(`/api/dossiers/${id}`);
  activeDossierId = id;
  viewList.hidden = true;
  viewDossier.hidden = false;
  document.querySelector("#dossier-title").textContent = `检索卷宗 #${d.id} · 片段「${d.fragment}」`;
  document.querySelector("#dossier-meta").textContent =
    `建立人 ${d.created_by}，存档主键 ${d.items.length} 个；右侧为此刻按同一片段重查的结果`;
  dossierRows.innerHTML = d.items
    .map((it) => {
      const cur = it.current;
      const curId = cur ? cur.id : "—";
      const curSite = cur ? esc(cur.site) : "行已不存在";
      const verdict = it.stale
        ? '<span class="stale">失效</span>'
        : '<span class="valid">仍命中</span>';
      const rowClass = it.stale ? 'class="stale"' : "";
      return (
        `<tr ${rowClass}><td>${it.reading_id}</td><td>${esc(it.archived_site)}</td>` +
        `<td class="gap">${curId}</td><td>${curSite}</td><td class="gap">${verdict}</td></tr>`
      );
    })
    .join("");
}

document.querySelector("#dossier-back").onclick = async () => {
  activeDossierId = null;
  viewDossier.hidden = true;
  viewList.hidden = false;
  await refreshCurrent();
};

document.querySelector("#dossiers").onclick = async (e) => {
  const btn = e.target.closest(".open-dossier");
  if (btn) await openDossier(Number(btn.dataset.id));
};

rows.addEventListener("click", async (e) => {
  const btn = e.target.closest(".rename-go");
  if (!btn) return;
  const id = btn.dataset.id;
  const input = rows.querySelector(`input.rename-site[data-id="${id}"]`);
  try {
    await api(`/api/readings/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ site: input.value }),
    });
    live.textContent = `测点 ${id} 已改正为 ${input.value}`;
  } catch (err) {
    live.textContent = err.message;
  }
});

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = async (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "reading_updated") {
      live.textContent = `测点 ${msg.id} 已改名：${msg.site}`;
    } else {
      live.textContent = `刚推送：${msg.site} ${msg.level}`;
    }
    await refreshCurrent();
  };
}

document.querySelector("#go").onclick = async () => {
  const data = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({
      username: document.querySelector("#user").value,
      password: document.querySelector("#pass").value,
    }),
  });
  token = data.access_token;
  role = data.role;
  localStorage.setItem(tokenKey, token);
  localStorage.setItem("methane_role", role);
  showApp();
};

form.onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/readings", {
      method: "POST",
      body: JSON.stringify({
        site: document.querySelector("#site").value,
        ch4_pct: Number(document.querySelector("#ch4").value),
      }),
    });
  } catch (err) {
    live.textContent = err.message;
  }
};

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();
