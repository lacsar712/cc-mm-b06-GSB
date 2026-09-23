const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const rows = document.querySelector("#rows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const qInput = document.querySelector("#q");
const searchMsg = document.querySelector("#searchMsg");
const dossierList = document.querySelector("#dossierList");
const dossierView = document.querySelector("#dossierView");

let lastList = [];
let currentQuery = "";

function paint(list) {
  lastList = list;
  rows.innerHTML = list
    .map((r) => {
      const ops = role === "writer" ? `<button data-rename="${r.id}">改名</button>` : "";
      return `<tr><td>${r.site}</td><td>${r.ch4_pct}</td><td class="${r.level === "报警" ? "alarm" : "ok"}">${r.level}</td><td>${r.note}</td><td>${ops}</td></tr>`;
    })
    .join("");
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
  document.querySelector("#who").textContent = role === "writer" ? "检查员" : "查看";
  document.querySelector("#out").hidden = false;
  form.hidden = role !== "writer";
  connect();
  reload();
  loadDossiers();
}

async function reload() {
  if (currentQuery) {
    await runSearch(currentQuery);
  } else {
    paint(await api("/api/readings"));
  }
}

async function runSearch(fragment) {
  const list = await api(`/api/readings/search?q=${encodeURIComponent(fragment)}`);
  paint(list);
  // 无命中只提示，绝不明里暗里回退成全表
  searchMsg.textContent = list.length ? `「${fragment}」命中 ${list.length} 条` : "无匹配测点";
  return list;
}

async function loadDossiers() {
  const list = await api("/api/dossiers");
  dossierList.innerHTML =
    list
      .map(
        (d) =>
          `<li><button data-open="${d.id}">打开</button> 卷宗 #${d.id} · 片段「${d.fragment}」 · 存档 ${d.hit_ids.length} 个主键 · ${d.created_by}</li>`,
      )
      .join("") || "<li>暂无卷宗</li>";
}

async function openDossier(id) {
  try {
    const d = await api(`/api/dossiers/${id}`);
    dossierView.hidden = false;
    document.querySelector("#dvTitle").textContent = `卷宗 #${d.id} · 片段「${d.fragment}」`;
    document.querySelector("#dvMeta").textContent = `由 ${d.created_by} 建于 ${d.created_at}`;
    document.querySelector("#archivedRows").innerHTML = d.archived
      .map(
        (a) =>
          `<tr><td>${a.reading_id}</td><td>${a.site === null ? "（记录已删除）" : a.site}</td><td class="${a.status === "失效" ? "stale" : "fresh"}">${a.status}</td></tr>`,
      )
      .join("");
    document.querySelector("#currentRows").innerHTML =
      d.current
        .map((r) => `<tr><td>${r.id}</td><td>${r.site}</td><td class="fresh">命中</td></tr>`)
        .join("") || `<tr><td colspan="3">无匹配测点</td></tr>`;
  } catch (err) {
    searchMsg.textContent = err.message;
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const row = JSON.parse(ev.data);
    live.textContent = row.kind === "update" ? `已改正：${row.site} ${row.level}` : `刚推送：${row.site} ${row.level}`;
    reload();
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

document.querySelector("#doSearch").onclick = async () => {
  const fragment = qInput.value.trim();
  if (!fragment) {
    searchMsg.textContent = "请输入测点名片段";
    return;
  }
  try {
    currentQuery = fragment;
    await runSearch(fragment);
  } catch (err) {
    searchMsg.textContent = err.message;
  }
};

document.querySelector("#showAll").onclick = async () => {
  currentQuery = "";
  qInput.value = "";
  searchMsg.textContent = "";
  paint(await api("/api/readings"));
};

document.querySelector("#saveDossier").onclick = async () => {
  const fragment = qInput.value.trim();
  if (!fragment) {
    searchMsg.textContent = "请输入测点名片段";
    return;
  }
  try {
    const d = await api("/api/dossiers", {
      method: "POST",
      body: JSON.stringify({ fragment }),
    });
    searchMsg.textContent = `已建卷宗 #${d.id}，存档主键 ${d.hit_ids.length} 个`;
    await loadDossiers();
    openDossier(d.id);
  } catch (err) {
    searchMsg.textContent = err.message;
  }
};

dossierList.onclick = (e) => {
  const id = e.target.dataset && e.target.dataset.open;
  if (id) openDossier(id);
};

rows.onclick = async (e) => {
  const id = e.target.dataset && e.target.dataset.rename;
  if (!id) return;
  const row = lastList.find((r) => String(r.id) === String(id));
  const name = prompt("改正测点名", row ? row.site : "");
  if (!name || !name.trim()) return;
  try {
    await api(`/api/readings/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ site: name.trim() }),
    });
    await reload();
  } catch (err) {
    live.textContent = err.message;
  }
};

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();
