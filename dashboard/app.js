/* ClipForge dashboard — vanilla JS + Supabase (auth, queries, realtime). */
const cfg = window.CF_CONFIG || {};
const sb = supabase.createClient(cfg.url, cfg.anon);
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ago = (t) => { if (!t) return "never"; const s = (Date.now() - new Date(t)) / 1000; return s < 60 ? `${s | 0}s ago` : s < 3600 ? `${(s / 60) | 0}m ago` : `${(s / 3600) | 0}h ago`; };
const hhmm = (t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
let tab = "mission", feed = [], refreshTimer;

// ───── auth ─────
async function boot() {
  const { data } = await sb.auth.getSession();
  if (!data.session) { $("#login").classList.remove("hidden"); return; }
  $("#app").classList.remove("hidden");
  subscribe();
  render();
  refreshTimer = setInterval(() => { if (["mission", "agents"].includes(tab)) render(); }, 15000);
}
$("#loginForm").onsubmit = async (e) => {
  e.preventDefault();
  const { error } = await sb.auth.signInWithPassword({ email: $("#email").value, password: $("#password").value });
  if (error) { $("#loginErr").textContent = error.message; return; }
  location.reload();
};
$("#logout").onclick = async () => { await sb.auth.signOut(); location.reload(); };
$("#tabs").onclick = (e) => {
  const b = e.target.closest("button"); if (!b) return;
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
  tab = b.dataset.tab; render();
};

// ───── helpers ─────
async function setting(key, fallback) { const { data } = await sb.from("settings").select("value").eq("key", key).maybeSingle(); return data ? data.value : fallback; }
async function setSetting(key, value) { await sb.from("settings").upsert({ key, value, updated_at: new Date().toISOString() }); }
async function signed(path) { if (!path) return null; const { data } = await sb.storage.from("clips").createSignedUrl(path, 3600); return data?.signedUrl; }
const pill = (txt, cls = "") => `<span class="pill ${cls}">${esc(txt)}</span>`;

// ───── realtime ─────
function subscribe() {
  sb.channel("live")
    .on("postgres_changes", { event: "INSERT", schema: "public", table: "agent_events" }, (p) => {
      feed.unshift(p.new); feed = feed.slice(0, 200);
      const f = $("#feed"); if (f) f.innerHTML = feedHtml();
    })
    .on("postgres_changes", { event: "*", schema: "public", table: "clips" }, () => { if (["mission", "clips"].includes(tab)) debounceRender(); })
    .on("postgres_changes", { event: "*", schema: "public", table: "agents" }, () => { if (tab === "agents") debounceRender(); })
    .on("postgres_changes", { event: "*", schema: "public", table: "tasks" }, () => { if (tab === "tasks") debounceRender(); })
    .on("postgres_changes", { event: "INSERT", schema: "public", table: "heartbeats" }, () => headerStatus())
    .subscribe();
}
let rt; function debounceRender() { clearTimeout(rt); rt = setTimeout(render, 800); }

async function headerStatus() {
  const { data } = await sb.from("heartbeats").select("ts").order("ts", { ascending: false }).limit(1);
  const t = data?.[0]?.ts; const stale = !t || Date.now() - new Date(t) > 15 * 60000;
  $("#hb").className = `pill ${stale ? "err" : "ok"}`; $("#hb").textContent = `heartbeat ${ago(t)}`;
  const ks = await setting("kill_switch", {});
  $("#killPub").textContent = ks.publishing ? "Resume publishing" : "Pause publishing";
  $("#killPub").className = `small ${ks.publishing ? "ok" : "danger"}`;
}
$("#killPub").onclick = async () => { const ks = await setting("kill_switch", {}); ks.publishing = !ks.publishing; await setSetting("kill_switch", ks); headerStatus(); };

function feedHtml() {
  return feed.map((e) => `<div><span class="t">${hhmm(e.created_at)}</span><span class="a">${esc(e.agent_id)}</span><span class="lv-${e.level}">${esc(e.message)}</span></div>`).join("") || `<p class="muted">No events yet.</p>`;
}

// ───── views ─────
async function render() {
  headerStatus();
  const v = $("#view");
  try { v.innerHTML = await views[tab](); bind[tab]?.(); }
  catch (err) { v.innerHTML = `<div class="card err">Error: ${esc(err.message)}</div>`; }
}

const STAGES = [["cutting", "Cut"], ["awaiting_brief", "Claude brief"], ["editing", "Edit"], ["awaiting_qa", "QA"], ["review", "Your review"], ["approved", "Approved"], ["scheduled", "Scheduled"], ["published", "Published"]];

const views = {
  async mission() {
    const [{ data: clips }, { data: streams }, { data: ev }, bridge, auto, { data: plans }, { count: pend }] = await Promise.all([
      sb.from("clips").select("status").gte("created_at", new Date(Date.now() - 86400000).toISOString()),
      sb.from("streams").select("title,status,concurrent_viewers,streamers(name)").in("status", ["recording", "live"]).order("concurrent_viewers", { ascending: false }).limit(8),
      feed.length ? { data: null } : sb.from("agent_events").select("*").order("created_at", { ascending: false }).limit(100),
      setting("bridge", {}), setting("autopilot_level", 0),
      sb.from("plans").select("*").order("created_at", { ascending: false }).limit(1),
      sb.from("llm_requests").select("id", { count: "exact", head: true }).eq("status", "pending"),
    ]);
    if (ev) feed = ev;
    const counts = {}; (clips || []).forEach((c) => (counts[c.status] = (counts[c.status] || 0) + 1));
    const flow = STAGES.map(([k, l], i) => `${i ? '<span class="arrow">→</span>' : ""}<div class="stage ${counts[k] ? "hot" : ""}"><div class="n">${counts[k] || 0}</div><div class="l">${l}</div></div>`).join("");
    const cState = bridge.state === "working" ? pill("working", "ok") : pill("sleeping", "info");
    const p = plans?.[0];
    return `
      <div class="grid g2" style="margin-bottom:12px">
        <div class="card" style="grid-column:1/-1"><h2>Pipeline (last 24h) · rejected ${counts.rejected || 0}</h2><div class="flow">${flow}</div></div>
      </div>
      <div class="grid g3" style="margin-bottom:12px">
        <div class="card"><h2>Claude (the brain)</h2>${cState} ${pill(`${pend ?? 0} waiting`, pend ? "warn" : "")}
          <p class="muted">Last wake ${ago(bridge.last_wake_at)} · last seen ${ago(bridge.last_seen_at)} · wakes today ${bridge.wakes_today || 0}</p></div>
        <div class="card"><h2>Autopilot</h2>
          <select id="autopilot">${["L0 · I approve everything", "L1 · auto-publish only top clips", "L2 · full auto"].map((t, i) => `<option value="${i}" ${+auto === i ? "selected" : ""}>${t}</option>`).join("")}</select></div>
        <div class="card"><h2>Recording now</h2>${(streams || []).filter((s) => s.status === "recording").map((s) => `<div>🔴 ${esc(s.streamers?.name)} · ${s.concurrent_viewers ?? "?"} viewers</div>`).join("") || '<span class="muted">Nothing recording</span>'}</div>
      </div>
      <div class="grid g2">
        <div class="card"><h2>Live agent feed</h2><div id="feed" class="feed">${feedHtml()}</div></div>
        <div class="card"><h2>Supervisor plan ${p ? `<span class="muted">· ${ago(p.created_at)}</span>` : ""}</h2>
          ${p ? `<p>${esc(p.rationale)}</p><ul>${(p.plan.record || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : '<p class="muted">No plan yet — the heartbeat writes one every 10 min.</p>'}
          <h2 style="margin-top:14px">Live GTA 6 streams found</h2>
          ${(streams || []).map((s) => `<div>${s.status === "recording" ? "🔴" : "⚪"} ${esc(s.streamers?.name)} — ${esc((s.title || "").slice(0, 60))}</div>`).join("") || '<span class="muted">none yet</span>'}
        </div>
      </div>`;
  },

  async clips() {
    const filter = window._clipFilter || "review";
    const q = sb.from("clips").select("*, streamers(name,permission_status)").order("created_at", { ascending: false }).limit(30);
    const { data } = filter === "all" ? await q : await q.eq("status", filter);
    const cards = await Promise.all((data || []).map(async (c) => {
      const url = await signed(c.video_path); const sheet = !url ? await signed(c.sheet_path) : null;
      const media = url ? `<video src="${url}" controls preload="metadata"></video>` : sheet ? `<img src="${sheet}">` : `<div class="muted" style="width:200px">${esc(c.status)}…</div>`;
      return `<div class="card clip" data-id="${c.id}">${media}<div>
        <div class="title" contenteditable="true" data-field="title">${esc(c.title || "(untitled)")}</div>
        <div class="muted" style="margin:6px 0">${esc(c.streamers?.name)} · ${esc(c.category || "")} · ${(c.end_s - c.start_s || 0).toFixed(0)}s</div>
        <div>${pill(c.status, c.status === "rejected" ? "err" : c.status === "review" ? "warn" : "ok")} ${pill(`final ${c.final_score ?? "-"}`)} ${pill(`QA ${c.qa_score ?? "-"}`)} ${pill(`signal ${c.signal_score ?? "-"}`)}
          ${c.streamers && !["granted", "open_policy"].includes(c.streamers.permission_status) ? pill("no permission", "err") : ""}</div>
        ${c.status_reason ? `<p class="muted">${esc(c.status_reason)}</p>` : ""}
        <p class="muted">Hook: ${esc(c.hook_text || "")}</p>
        <div class="actions">
          <button class="ok small" data-act="approve">Approve</button>
          <button class="small ghost" data-act="save">Save title</button>
          <button class="small ghost" data-act="rerender">Re-render</button>
          <button class="small danger" data-act="reject">Reject</button>
        </div></div></div>`;
    }));
    const opts = ["review", "approved", "scheduled", "published", "awaiting_brief", "editing", "awaiting_qa", "rejected", "all"];
    return `<div class="row-form" style="margin-bottom:12px"><select id="clipFilter">${opts.map((o) => `<option ${o === filter ? "selected" : ""}>${o}</option>`).join("")}</select></div>
      <div class="grid">${cards.join("") || '<p class="muted">No clips in this state.</p>'}</div>`;
  },

  async streams() {
    const { data } = await sb.from("streams").select("*, streamers(name)").order("created_at", { ascending: false }).limit(40);
    return `<div class="card" style="margin-bottom:12px"><h2>Record a live stream now</h2>
      <div class="row-form"><input id="recUrl" placeholder="https://www.youtube.com/watch?v=… (live)"><button id="recBtn">Record</button></div></div>
      <div class="card"><table><tr><th>Streamer</th><th>Title</th><th>Status</th><th>Viewers</th><th>Capture</th><th></th></tr>
      ${(data || []).map((s) => `<tr><td>${esc(s.streamers?.name)}</td><td>${esc((s.title || "").slice(0, 70))}</td><td>${pill(s.status, s.status === "recording" ? "ok" : "")}</td>
        <td>${s.concurrent_viewers ?? ""}</td><td>${esc(s.capture_level || "")}</td>
        <td>${s.status === "recording" ? `<button class="small danger" data-stop="${s.id}">Stop</button>` : ""}</td></tr>`).join("")}</table></div>`;
  },

  async agents() {
    const { data } = await sb.from("agents").select("*").order("type");
    return `<div class="grid g3">${(data || []).map((a) => {
      const stale = Date.now() - new Date(a.last_heartbeat) > 120000;
      const st = stale ? "offline" : a.status;
      return `<div class="card agent"><div class="row"><b><span class="dot ${st}"></span>${esc(a.type)}</b>${pill(st, st === "working" ? "ok" : st === "idle" ? "info" : "err")}</div>
        <div class="act">${esc(a.current_action || "")}</div>
        ${(a.next_steps || []).length ? `<div class="muted">Next:</div><ul>${a.next_steps.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
        <p class="muted">${esc(a.id)} · ♥ ${ago(a.last_heartbeat)} · done ${a.stats?.done ?? 0} · failed ${a.stats?.failed ?? 0}</p></div>`;
    }).join("") || '<p class="muted">No agents registered yet — deploy the engine.</p>'}</div>`;
  },

  async tasks() {
    const [{ data }, { data: streamers }] = await Promise.all([
      sb.from("tasks").select("*").order("created_at", { ascending: false }).limit(30),
      sb.from("streamers").select("id,name").order("name")]);
    return `<div class="grid g2" style="margin-bottom:12px">
      <div class="card"><h2>Ask Claude</h2><div class="grid">
        <input id="tTitle" placeholder="Short title (e.g. Rewrite today's titles)">
        <textarea id="tInstr" placeholder="What should Claude do?"></textarea>
        <button id="tBtn">Assign to Claude</button></div></div>
      <div class="card"><h2>Clip a video / VOD section</h2><div class="grid">
        <input id="vUrl" placeholder="https://www.youtube.com/watch?v=…">
        <div class="row-form"><input id="vStart" placeholder="start (sec or mm:ss)"><input id="vEnd" placeholder="end (sec or mm:ss)"></div>
        <select id="vStreamer"><option value="">streamer (optional)</option>${(streamers || []).map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("")}</select>
        <button id="vBtn">Send to Cutter</button></div></div></div>
      <div class="card"><table><tr><th>Task</th><th>Status</th><th>Result</th><th>When</th></tr>
      ${(data || []).map((t) => `<tr><td><b>${esc(t.title)}</b><div class="muted">${esc(t.instruction)}</div></td><td>${pill(t.status, t.status === "done" ? "ok" : "warn")}</td><td style="white-space:pre-wrap">${esc(t.result || "")}</td><td>${ago(t.created_at)}</td></tr>`).join("")}</table></div>`;
  },

  async plan() {
    const [{ data }, { data: hb }] = await Promise.all([
      sb.from("plans").select("*").order("created_at", { ascending: false }).limit(12),
      sb.from("heartbeats").select("*").order("ts", { ascending: false }).limit(6)]);
    return `<div class="grid g2"><div class="card"><h2>Supervisor plans</h2>${(data || []).map((p) => `<div style="padding:8px 0;border-bottom:1px solid var(--line)"><span class="muted">${ago(p.created_at)}</span><p>${esc(p.rationale)}</p><pre class="muted">${esc(JSON.stringify(p.plan.queue || {}, null, 0))}</pre></div>`).join("") || '<p class="muted">none yet</p>'}</div>
      <div class="card"><h2>Heartbeats</h2>${(hb || []).map((h) => `<div class="muted" style="padding:6px 0">${ago(h.ts)} — ${h.summary?.agents_alive ?? "?"} agents alive, ${h.summary?.live_streams ?? 0} live streams, requeued ${h.summary?.requeued ?? 0}</div>`).join("")}</div></div>`;
  },

  async streamers() {
    const { data } = await sb.from("streamers").select("*").order("priority", { ascending: false });
    const perms = ["not_asked", "asked", "granted", "denied", "open_policy"];
    return `<div class="card" style="margin-bottom:12px"><h2>Add streamer</h2><div class="row-form">
      <input id="sName" placeholder="Name"><input id="sUrl" placeholder="https://www.youtube.com/@handle"><input id="sPri" type="number" placeholder="priority 0–100" value="60"><button id="sBtn">Add</button></div></div>
      <div class="card"><table><tr><th>Name</th><th>Permission</th><th>Priority</th><th>Watch</th><th>Facecam</th><th>Weight</th></tr>
      ${(data || []).map((s) => `<tr data-sid="${s.id}"><td><a href="${esc(s.channel_url)}" target="_blank" style="color:var(--text)">${esc(s.name)}</a></td>
        <td><select data-f="permission_status">${perms.map((p) => `<option ${p === s.permission_status ? "selected" : ""}>${p}</option>`).join("")}</select></td>
        <td><input data-f="priority" type="number" value="${s.priority}" style="width:80px"></td>
        <td><input data-f="watch" type="checkbox" ${s.watch ? "checked" : ""} style="width:auto"></td>
        <td class="muted">${s.layout_preset ? "learned" : "—"}</td><td>${s.weight}</td></tr>`).join("")}</table></div>`;
  },

  async settings() {
    const [cap, gap, maxs, { data: rules }] = await Promise.all([setting("daily_post_cap", 10), setting("min_post_gap_minutes", 30), setting("max_parallel_streams", 2),
      sb.from("rules").select("*").eq("active", true).order("version", { ascending: false }).limit(1)]);
    return `<div class="grid g2"><div class="card"><h2>Limits</h2><div class="grid">
      <label>Daily post cap <input id="cap" type="number" value="${cap}"></label>
      <label>Minutes between posts <input id="gap" type="number" value="${gap}"></label>
      <label>Parallel streams recorded <input id="maxs" type="number" value="${maxs}"></label>
      <button id="saveLimits">Save</button>
      <button id="killAll" class="danger">Emergency: pause ALL agents</button></div></div>
      <div class="card"><h2>Rule overrides (merged over clip_rules.json)</h2>
      <textarea id="rulesJson" style="min-height:260px">${esc(JSON.stringify(rules?.[0]?.rules || {}, null, 2))}</textarea>
      <p class="muted">Example: {"detector":{"candidate_threshold":55},"publish":{"require_permission":true}}</p>
      <button id="saveRules">Save as new rules version</button></div></div>`;
  },
};

// ───── event bindings per view ─────
const toSec = (v) => { v = String(v).trim(); if (v.includes(":")) return v.split(":").reduce((a, b) => a * 60 + +b, 0); return +v; };
const vidOf = (u) => { try { const x = new URL(u); return x.searchParams.get("v") || x.pathname.split("/").filter(Boolean).pop(); } catch { return null; } };

const bind = {
  mission() { $("#autopilot").onchange = (e) => setSetting("autopilot_level", +e.target.value); },
  clips() {
    $("#clipFilter").onchange = (e) => { window._clipFilter = e.target.value; render(); };
    document.querySelectorAll(".clip").forEach((el) => {
      const id = el.dataset.id;
      el.querySelector(".actions").onclick = async (e) => {
        const act = e.target.dataset.act; if (!act) return;
        const title = el.querySelector('[data-field="title"]').innerText.trim();
        if (act === "approve") await sb.from("clips").update({ status: "approved", title, status_reason: null }).eq("id", id);
        if (act === "save") await sb.from("clips").update({ title }).eq("id", id);
        if (act === "reject") await sb.from("clips").update({ status: "rejected", status_reason: "rejected by you" }).eq("id", id);
        if (act === "rerender") { await sb.from("clips").update({ status: "editing" }).eq("id", id); await sb.from("jobs").insert({ type: "edit", payload: { clip_id: id }, priority: 90 }); }
        render();
      };
    });
  },
  streams() {
    $("#recBtn").onclick = async () => {
      const url = $("#recUrl").value.trim(); const vid = vidOf(url); if (!vid) return alert("Paste a YouTube watch URL");
      const { data, error } = await sb.from("streams").upsert({ video_id: vid, url: `https://www.youtube.com/watch?v=${vid}`, status: "live" }, { onConflict: "video_id" }).select();
      if (error) return alert(error.message);
      await sb.from("jobs").insert({ type: "record_stream", payload: { stream_id: data[0].id }, priority: 95 });
      render();
    };
    document.querySelectorAll("[data-stop]").forEach((b) => (b.onclick = async () => { await sb.from("streams").update({ status: "stop_requested" }).eq("id", b.dataset.stop); render(); }));
  },
  tasks() {
    $("#tBtn").onclick = async () => {
      const title = $("#tTitle").value.trim(), instruction = $("#tInstr").value.trim(); if (!title || !instruction) return;
      await sb.from("tasks").insert({ title, instruction, assigned_agent: "claude" }); render();
    };
    $("#vBtn").onclick = async () => {
      const url = $("#vUrl").value.trim(), start = toSec($("#vStart").value), end = toSec($("#vEnd").value);
      if (!url || !(end > start)) return alert("URL, start and end (end > start) are required");
      await sb.from("jobs").insert({ type: "clip_vod", payload: { url, start, end, streamer_id: $("#vStreamer").value || null }, priority: 90 });
      await sb.from("tasks").insert({ title: `Clip ${url} ${start}-${end}s`, instruction: "sent to Cutter", assigned_agent: "cutter", status: "queued" });
      render();
    };
  },
  streamers() {
    $("#sBtn").onclick = async () => {
      const name = $("#sName").value.trim(), url = $("#sUrl").value.trim(); if (!name || !url) return;
      const { error } = await sb.from("streamers").insert({ name, channel_url: url, priority: +$("#sPri").value || 50 }); if (error) alert(error.message); render();
    };
    document.querySelectorAll("tr[data-sid] [data-f]").forEach((inp) => (inp.onchange = async () => {
      const f = inp.dataset.f, v = inp.type === "checkbox" ? inp.checked : inp.type === "number" ? +inp.value : inp.value;
      await sb.from("streamers").update({ [f]: v }).eq("id", inp.closest("tr").dataset.sid);
    }));
  },
  settings() {
    $("#saveLimits").onclick = async () => { await setSetting("daily_post_cap", +$("#cap").value); await setSetting("min_post_gap_minutes", +$("#gap").value); await setSetting("max_parallel_streams", +$("#maxs").value); alert("Saved"); };
    $("#killAll").onclick = async () => { const ks = await setting("kill_switch", {}); ks.all = !ks.all; await setSetting("kill_switch", ks); alert(ks.all ? "ALL agents paused" : "Agents resumed"); };
    $("#saveRules").onclick = async () => {
      let r; try { r = JSON.parse($("#rulesJson").value); } catch (e) { return alert("Invalid JSON: " + e.message); }
      await sb.from("rules").update({ active: false }).eq("active", true);
      await sb.from("rules").insert({ rules: r, note: "dashboard edit" }); alert("Rules saved — agents pick them up within 60 s");
    };
  },
};

boot();
