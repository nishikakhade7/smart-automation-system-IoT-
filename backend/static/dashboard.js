// dashboard.js - Smart Classroom Control Center.
// Live data:   /api/status + /api/consumption every 2.5 s (no overlapping requests).
// Slow data:   /api/timetable every 60 s, /api/sms-log every 15 s.
// Analytics:   /api/analytics only when the section is opened, the range changes or
//              "Refresh" is pressed - never on the 2.5 s poll.
// The page never decides anything itself: it shows what the server sent.

const POLL_MS = 2500;
const TIMETABLE_MS = 60000;
const SMS_LOG_MS = 15000;
const FETCH_TIMEOUT_MS = 5000;

const $ = (id) => document.getElementById(id);
const state = { status: null, serverOk: null, espOnline: null, timetable: [], analyticsRange: "today",
                analyticsLoaded: false, overridePending: false, smsArmed: false, smsCooldownUntil: 0 };

// ---------- helpers ----------

async function getJSON(url, options = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  try {
    const resp = await fetch(url, { cache: "no-store", ...options, signal: ctrl.signal });
    let data = null;
    try { data = await resp.json(); } catch (e) { data = null; }
    return { ok: resp.ok, status: resp.status, data };
  } finally {
    clearTimeout(timer);
  }
}

function text(id, value) { const el = $(id); if (el) el.textContent = value; }
function show(id, visible) { const el = $(id); if (el) el.hidden = !visible; }
function announce(msg) { const el = $("announce"); el.textContent = ""; setTimeout(() => (el.textContent = msg), 50); }
function kwh(n) { return n == null ? "–" : fmt(n, Math.abs(n) < 0.1 ? 4 : 3); }   // small values stay visible
function fmt(n, digits = 1) { return n == null || Number.isNaN(n) ? "–" : Number(n).toFixed(digits); }
function pill(id, kind, label) {
  const el = $(id);
  el.className = "pill " + kind;
  el.replaceChildren();
  const dot = document.createElement("span"); dot.className = "dot";
  const span = document.createElement("span"); span.textContent = label;
  el.append(dot, span);
}
function hhmmToMin(hhmm) { const [h, m] = hhmm.split(":").map(Number); return h * 60 + m; }
function nowMin(s) { const t = s.time.split(" ")[1]; return hhmmToMin(t.slice(0, 5)); }
function minutesLeft(s, until) {
  let left = hhmmToMin(until) - nowMin(s);
  if (left < 0) left += 24 * 60;
  return left;
}
function setScale(fillId, value, min, max) {
  const pct = value == null ? 0 : Math.max(0, Math.min(100, (100 * (value - min)) / (max - min)));
  $(fillId).style.width = pct + "%";
}
// side "left": the label ends at its marker; "right": it starts there - so two
// close thresholds never print on top of each other.
function placeMark(markId, labelId, value, min, max, label, side) {
  const pct = Math.max(0, Math.min(100, (100 * (value - min)) / (max - min)));
  $(markId).style.left = "calc(" + pct + "% - 1px)";
  const l = $(labelId);
  l.style.left = pct + "%";
  l.style.transform = side === "left" ? "translateX(calc(-100% - 4px))" : "translateX(4px)";
  l.textContent = label;
}
function row(cells) {
  const tr = document.createElement("tr");
  cells.forEach((v) => { const td = document.createElement("td"); td.textContent = v; tr.append(td); });
  return tr;
}

// ---------- charts (created once, then only their data changes) ----------

Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
Chart.defaults.color = "#64748B";
Chart.defaults.borderColor = "#E2E8F0";
const C = { blue: "#2563EB", green: "#16A34A", amber: "#D97706", sky: "#0EA5E9", grid: "#EEF2F7" };

const tooltip = { backgroundColor: "#0F172A", titleColor: "#E2E8F0", bodyColor: "#FFFFFF", padding: 10,
                  cornerRadius: 8, displayColors: true, boxPadding: 4 };
function baseOptions(unit) {
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { display: false }, tooltip: { ...tooltip,
      callbacks: { label: (c) => " " + c.dataset.label + ": " + fmt(c.parsed.y, unit === "kWh" ? 4 : 1) + " " + unit } } },
    scales: {
      x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 8 } },
      y: { beginAtZero: true, grid: { color: C.grid }, border: { display: false }, ticks: { maxTicksLimit: 5 },
           title: { display: true, text: unit } },
    },
  };
}

const powerChart = new Chart($("power-chart"), {
  type: "line",
  data: { labels: [], datasets: [{ label: "Power", data: [], borderColor: C.blue, borderWidth: 2,
    backgroundColor: "rgba(37, 99, 235, 0.08)", fill: true, stepped: true, pointRadius: 0, pointHoverRadius: 4 }] },
  options: baseOptions("W"),
});
powerChart.options.scales.y.suggestedMax = 10;

const charts = {};
function analyticsCharts() {
  if (charts.energy) return charts;
  charts.energy = new Chart($("c-energy"), { type: "bar",
    data: { labels: [], datasets: [{ label: "Energy", data: [], backgroundColor: C.blue, borderRadius: 4, maxBarThickness: 36 }] },
    options: baseOptions("kWh") });
  const stacked = baseOptions("min");
  stacked.scales.x.stacked = true; stacked.scales.y.stacked = true;
  stacked.plugins.legend = { display: true, position: "bottom", labels: { boxWidth: 12, boxHeight: 12 } };
  stacked.plugins.tooltip.callbacks.label = (c) => " " + c.dataset.label + ": " + c.parsed.y + " min";
  charts.light = new Chart($("c-light"), { type: "bar",
    data: { labels: [], datasets: [
      { label: "In class time", data: [], backgroundColor: C.blue, borderRadius: 4, maxBarThickness: 36 },
      { label: "Outside class time", data: [], backgroundColor: C.amber, borderRadius: 4, maxBarThickness: 36 }] },
    options: stacked });
  const tempOpts = baseOptions("°C");
  tempOpts.scales.y.beginAtZero = false;
  tempOpts.plugins.legend = { display: true, position: "bottom", labels: { boxWidth: 12, boxHeight: 12 } };
  charts.temp = new Chart($("c-temp"), { type: "line",
    data: { labels: [], datasets: [
      { label: "Average", data: [], borderColor: C.blue, borderWidth: 2, pointRadius: 3, spanGaps: true },
      { label: "Maximum", data: [], borderColor: "#93C5FD", borderWidth: 2, borderDash: [4, 3], pointRadius: 2, spanGaps: true },
      { label: "Fan on (28 °C)", data: [], borderColor: C.amber, borderWidth: 1, borderDash: [6, 4], pointRadius: 0 }] },
    options: tempOpts });
  charts.hum = new Chart($("c-hum"), { type: "line",
    data: { labels: [], datasets: [{ label: "Humidity", data: [], borderColor: C.sky, borderWidth: 2, pointRadius: 3,
      spanGaps: true, fill: true, backgroundColor: "rgba(14, 165, 233, 0.08)" }] },
    options: baseOptions("%") });
  applyChartTheme();
  return charts;
}
// ---------- dark mode (optional, remembered) ----------

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function applyChartTheme() {
  const tick = cssVar("--text-2"), grid = cssVar("--border");
  Chart.defaults.color = tick;
  for (const ch of [powerChart, ...Object.values(charts)]) {
    for (const axis of Object.values(ch.options.scales || {})) {
      if (axis.ticks) axis.ticks.color = tick;
      if (axis.title) axis.title.color = tick;
      if (axis.grid && axis.grid.display !== false) axis.grid.color = grid;
    }
    if (ch.options.plugins.legend && ch.options.plugins.legend.labels) ch.options.plugins.legend.labels.color = tick;
    ch.update("none");
  }
}
function setTheme(dark) {
  if (dark) document.documentElement.dataset.theme = "dark"; else delete document.documentElement.dataset.theme;
  const btn = $("theme-btn");
  btn.setAttribute("aria-pressed", String(dark));
  btn.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
  btn.querySelector("use").setAttribute("href", dark ? "#i-sun" : "#i-moon");
  try { localStorage.setItem("theme", dark ? "dark" : "light"); } catch (e) { /* storage unavailable: works anyway */ }
  applyChartTheme();
}
$("theme-btn").addEventListener("click", () => setTheme(document.documentElement.dataset.theme !== "dark"));

function setChart(chart, labels, datasets) {
  chart.data.labels = labels;
  datasets.forEach((d, i) => (chart.data.datasets[i].data = d));
  chart.update("none");
}

// ---------- live rendering ----------

function renderHeader(s) {
  const [date, time] = s.time.split(" ");
  text("clock", time);
  text("date", s.day_name + ", " + date);
  const badge = $("demo-badge");
  badge.hidden = !s.demo;
  if (s.demo) badge.querySelector("span").textContent = "Demo clock " + s.demo.speed + "×" +
    (s.demo.mock_pir ? " · simulated occupancy" : "");
}

function renderHealth(s) {
  const esp = $("h-esp");
  esp.textContent = s.esp_online ? "Online" : "Offline";
  esp.style.color = s.esp_online ? "var(--ok-text)" : "var(--err-text)";
  text("h-age", s.seconds_since_data == null ? "never" : (s.seconds_since_data < 60 ? s.seconds_since_data.toFixed(1) + " s ago"
    : Math.round(s.seconds_since_data / 60) + " min ago"));
  text("h-room", !s.esp_online ? "Unknown" : (s.occupied ? "Occupied" : "Empty"));
}

function renderDevice(prefix, s, isOn, reason, override) {
  const card = $(prefix + "-card");
  if (!s.esp_online) {
    card.className = "card device unknown span-4";
    text(prefix + "-state", "No data");
    text(prefix + "-reason", "ESP32 offline – the real state is unknown. If it lost the server it is in fail-safe (light follows motion, fan off).");
    text(prefix + "-mode", ""); text(prefix + "-confirm", "");
    return;
  }
  card.className = "card device span-4 " + (isOn ? "on" : "off");
  text(prefix + "-state", isOn ? "ON" : "OFF");
  text(prefix + "-reason", reason || s.command.reason || "–");
  text(prefix + "-mode", override ? "Manual until " + override.until + " (" + minutesLeft(s, override.until) + " min left)" : "Automatic");
  let confirm = "Command sent " + (s.command_time || "");
  const amps = (s.latest || {}).current_a;
  if (isOn && prefix === "light") {
    confirm += amps != null && amps > s.noise_a ? " · current detected (" + fmt(amps, 2) + " A)" : " · not physically confirmed";
  } else if (isOn) {
    confirm += " · not physically confirmed";
  }
  text(prefix + "-confirm", confirm);
}

function renderOverride(s) {
  const btn = $("override-btn");
  const ov = s.overrides.light;
  const isOn = s.command.light;
  btn.classList.toggle("on", s.esp_online && isOn);
  text("override-label", !s.esp_online ? "Unavailable" : (isOn ? "ON" : "OFF"));
  btn.setAttribute("aria-label", "Watchman switch, currently " + (isOn ? "on" : "off") + (ov ? ", manual" : ", automatic"));
  btn.disabled = state.overridePending || !s.esp_online || !state.serverOk;
  text("override-mode", ov ? "Manual · until " + ov.until + " (" + minutesLeft(s, ov.until) + " min left)" : "Automatic");
  let help;
  if (!s.esp_online) help = "The ESP32 is offline, so the switch cannot reach the relay.";
  else if (ov) help = "Press again to return to automatic. Manual mode also ends by itself after " + s.override_min + " min.";
  else help = "Press to switch " + (isOn ? "OFF" : "ON") + " manually (max " + s.override_min + " min). Press again to return to automatic.";
  text("override-help", help);
}

function renderRoomAndClass(s) {
  const room = $("room-state");
  room.textContent = !s.esp_online ? "Unknown" : (s.occupied ? "Occupied" : "Empty");
  room.style.color = !s.esp_online ? "var(--text-3)" : (s.occupied ? "var(--ok-text)" : "var(--text)");
  text("room-sub", (s.last_motion ? "Last motion " + s.last_motion : "No motion recorded") +
    " · counts as occupied for " + s.hold_min + " min after the last motion");
  text("room-demo", s.demo && s.demo.mock_pir ? "Demo: occupancy is simulated, the real PIR is ignored." : "");
  const c = s.current_class, n = s.next_class;
  text("class-now", c ? c.subject + " · " + c.start + "–" + c.end : "No class right now");
  if (n) {
    const when = s.next_class_days_ahead === 0 ? "Today" : s.next_class_days_ahead === 1 ? "Tomorrow" : n.day_name;
    text("class-next", when + " " + n.start + "–" + n.end + " · " + n.subject);
  } else text("class-next", "No upcoming class in the timetable");
}

function renderSensors(s) {
  const r = s.latest || {};
  text("sensor-time", r.time ? "Reading at " + r.time : "No readings yet");
  // temperature: scale 15-40 C with the fan thresholds
  const t = r.temp;
  const tValid = t != null && t >= 0 && t <= 50;
  text("temp-v", tValid ? fmt(t, 1) : "–");
  setScale("temp-fill", tValid ? t : null, 15, 40);
  $("temp-fill").style.background = s.fan_rule.hot ? C.amber : C.blue;
  placeMark("temp-m-off", "temp-l-off", s.fan_rule.off_c, 15, 40, "off <" + s.fan_rule.off_c, "left");
  placeMark("temp-m-on", "temp-l-on", s.fan_rule.on_c, 15, 40, "on ≥" + s.fan_rule.on_c, "right");
  text("temp-note", !s.esp_online ? "ESP32 offline – value may be old." :
    t == null ? "No valid DHT11 reading – the fan is switched off after 60 s." :
    !tValid ? "Out of range – reading ignored." :
    s.fan_rule.hot ? "Warm: fan allowed, stays on until below " + s.fan_rule.off_c + " °C." :
    "Below " + s.fan_rule.on_c + " °C: the fan is not needed.");
  // humidity
  const h = r.hum;
  text("hum-v", h == null ? "–" : fmt(h, 0));
  setScale("hum-fill", h, 0, 100);
  text("hum-note", h == null ? "No valid DHT11 reading." : "Shown for information.");
  // daylight
  const d = s.daylight;
  text("ldr-v", d.level == null ? "–" : Math.round(d.level));
  setScale("ldr-fill", d.level, 0, 4095);
  placeMark("ldr-m-on", "ldr-l-on", d.on_at, 0, 4095, "on <" + d.on_at, "left");
  placeMark("ldr-m-off", "ldr-l-off", d.off_at, 0, 4095, "off >" + d.off_at, "right");
  text("ldr-note", (d.higher_is_brighter ? "Higher = brighter. " : "Inverted LDR (shown as 4095 − raw). ") +
    (d.is_day ? "Enough daylight: the light is kept off." : "Not bright enough for daylight saving."));
  // power
  text("power-v", r.power_w == null ? "–" : Math.round(r.power_w));
  text("power-note", s.power_source === "estimated" ? "Estimated from relay state (bulb " + s.light_w + " W while ON)."
    : "Measured with the ACS712 (current × 230 V).");
}

function renderEnergy(c, s) {
  text("kwh-today", kwh(c.kwh_today));
  text("kwh-note", "since midnight");
  text("cost-today", "₹" + fmt(c.kwh_today * s.price_per_kwh, 2));
  text("cost-note", "at ₹" + s.price_per_kwh + " per kWh (assumed)");
  const p = (s.latest || {}).power_w;
  text("power-now", p == null ? "–" : Math.round(p));
  text("power-now-note", s.power_source === "estimated" ? "estimated" : "measured");
  powerChart.data.labels = c.samples.map((x) => x.time);
  powerChart.data.datasets[0].data = c.samples.map((x) => x.power_w);
  powerChart.update("none");
  $("power-table").replaceChildren(...c.samples.slice(-10).reverse().map((x) =>
    row([x.time, x.power_w == null ? "–" : Math.round(x.power_w)])));
}

// ---------- timetable ----------

function renderTimetable(s) {
  const slots = state.timetable;
  const list = $("today-list");
  const todays = slots.filter((x) => x.day_name === s.day_name).sort((a, b) => a.start.localeCompare(b.start));
  const now = nowMin(s);
  if (!todays.length) {
    const li = document.createElement("li"); li.className = "muted"; li.textContent = "No classes scheduled today.";
    list.replaceChildren(li);
  } else {
    let nextMarked = false;
    list.replaceChildren(...todays.map((x) => {
      const li = document.createElement("li");
      const isCurrent = s.current_class && s.current_class.start === x.start && s.current_class.subject === x.subject;
      let status;
      if (isCurrent) { li.className = "current"; status = ["info", "Now"]; }
      else if (hhmmToMin(x.end) <= now) { li.className = "done"; status = ["off", "Done"]; }
      else if (!nextMarked) { nextMarked = true; status = ["ok", "Next"]; }
      else status = ["off", "Later"];
      const time = document.createElement("span"); time.className = "time tabular"; time.textContent = x.start + "–" + x.end;
      const subj = document.createElement("span"); subj.className = "subj"; subj.textContent = x.subject;
      const p = document.createElement("span"); p.className = "pill " + status[0]; p.textContent = status[1];
      li.append(time, subj, p);
      return li;
    }));
  }
  const cur = s.current_class;
  $("week-table").replaceChildren(...slots.map((x) => {
    const tr = row([x.day_name, x.start, x.end, x.subject]);
    if (x.day_name === s.day_name) tr.classList.add("today");
    if (cur && cur.day_name === x.day_name && cur.start === x.start) tr.classList.add("now");
    return tr;
  }));
  $("week-cards").replaceChildren(...slots.map((x) => {
    const card = document.createElement("div"); card.className = "card"; card.style.padding = "10px 12px";
    card.textContent = x.day_name + " · " + x.start + "–" + x.end + " · " + x.subject;
    if (x.day_name === s.day_name) card.style.borderColor = "var(--accent)";
    return card;
  }));
}

// ---------- SMS ----------

const SMS_STATUS = {
  delivered: ["ok", "#i-check", "Delivered"], read: ["ok", "#i-check", "Delivered"],
  undelivered: ["err", "#i-x", "Not delivered"], failed: ["err", "#i-x", "Failed"], canceled: ["err", "#i-x", "Canceled"],
  queued: ["wait", "#i-wait", "Accepted by Twilio, not delivered yet"], accepted: ["wait", "#i-wait", "Accepted by Twilio"],
  sending: ["wait", "#i-wait", "Sending"], sent: ["wait", "#i-wait", "Sent, waiting for delivery report"],
};

function renderSms(s) {
  const a = s.alert;
  text("sms-rule", a.configured
    ? "An SMS goes to " + a.to + " when the light has been on for " + a.after_min + " minutes with no class. One SMS per episode."
    : "SMS is not configured: " + a.problems.join("; ") + ".");
  const trial = $("sms-trial");
  trial.hidden = !a.trial_template;
  if (a.trial_template) trial.textContent = "Twilio trial account: the SMS uses Twilio's template “" + a.trial_template +
    "” instead of our text. After an upgrade our own alert text is sent.";
  const box = $("sms-last");
  const m = a.last;
  box.replaceChildren();
  if (!m) {
    box.className = "sms-last";
    const h = document.createElement("div"); h.className = "head"; h.textContent = "No SMS sent yet";
    box.append(h);
  } else {
    const [kind, icon, label] = SMS_STATUS[m.status] || ["wait", "#i-wait", m.status];
    box.className = "sms-last " + kind;
    const h = document.createElement("div"); h.className = "head";
    h.innerHTML = '<svg aria-hidden="true"><use href="' + icon + '"/></svg>';
    const lbl = document.createElement("span"); lbl.textContent = label + (m.kind === "test" ? " · test SMS" : " · alert");
    h.append(lbl);
    const info = document.createElement("div"); info.className = "wrapit"; info.style.fontSize = "13px"; info.style.marginTop = "4px";
    info.textContent = m.time + " · " + m.info + (m.sid ? " · " + m.sid : "");
    box.append(h, info);
  }
  if (!state.smsArmed && Date.now() >= state.smsCooldownUntil) $("sms-test").disabled = !a.configured || !state.serverOk;
}

async function loadSmsLog() {
  try {
    const r = await getJSON("/api/sms-log");
    if (!r.ok) return;
    const list = $("sms-list");
    if (!r.data.length) {
      const li = document.createElement("li"); li.className = "muted"; li.textContent = "No messages yet.";
      list.replaceChildren(li);
      return;
    }
    list.replaceChildren(...r.data.map((m) => {
      const li = document.createElement("li");
      const [kind, , label] = SMS_STATUS[m.status] || ["wait", "", m.status];
      const p = document.createElement("span"); p.className = "pill " + (kind === "wait" ? "warn" : kind); p.textContent = label;
      const t = document.createElement("span"); t.className = "wrapit";
      t.textContent = m.time + " · " + (m.kind === "test" ? "test" : "alert") + " · " + m.info;
      li.append(p, t);
      return li;
    }));
  } catch (e) { /* the live poll shows connection problems */ }
}

function smsFeedback(msg, kind) { const el = $("sms-feedback"); el.className = "feedback " + (kind || ""); el.textContent = msg; }

$("sms-test").addEventListener("click", async () => {
  const btn = $("sms-test");
  const label = btn.querySelector("span");
  if (!state.smsArmed) {                              // step 1: ask for confirmation (it is a real SMS)
    state.smsArmed = true;
    btn.className = "btn danger";
    label.textContent = "Confirm: send 1 real SMS";
    smsFeedback("This sends a real SMS through Twilio. Press again within 6 s to confirm.");
    setTimeout(() => {
      if (state.smsArmed) { state.smsArmed = false; btn.className = "btn primary"; label.textContent = "Send test SMS"; smsFeedback(""); }
    }, 6000);
    return;
  }
  state.smsArmed = false;
  btn.className = "btn primary"; btn.disabled = true; label.textContent = "Sending…";
  try {
    const r = await getJSON("/api/test-alert", { method: "POST" });
    if (r.ok) { smsFeedback("Queued at Twilio. The status below changes to delivered or failed.", "ok"); announce("Test SMS queued"); }
    else if (r.status === 429) smsFeedback("Please wait 30 s between test SMS.", "err");
    else smsFeedback((r.data && r.data.error) || "Could not send the test SMS.", "err");
  } catch (e) {
    smsFeedback("The server did not answer – nothing was sent.", "err");
  }
  state.smsCooldownUntil = Date.now() + 30000;
  const tick = setInterval(() => {
    const left = Math.ceil((state.smsCooldownUntil - Date.now()) / 1000);
    if (left <= 0) { clearInterval(tick); label.textContent = "Send test SMS"; btn.disabled = false; }
    else label.textContent = "Wait " + left + " s";
  }, 1000);
  setTimeout(loadSmsLog, 3000);
});

$("sms-check").addEventListener("click", async () => {
  const btn = $("sms-check");
  btn.disabled = true;
  try {
    const r = await getJSON("/api/sms-refresh", { method: "POST" });
    smsFeedback(r.ok ? (r.data.checking ? "Asking Twilio about " + r.data.checking + " message(s)…"
      : "No message is waiting for a delivery report.") : "Could not check the status.", r.ok ? "ok" : "err");
  } catch (e) { smsFeedback("The server did not answer.", "err"); }
  setTimeout(() => { btn.disabled = false; loadSmsLog(); }, 3000);
});

// ---------- watchman override ----------

$("override-btn").addEventListener("click", async () => {
  if (state.overridePending) return;
  state.overridePending = true;
  const btn = $("override-btn");
  btn.disabled = true;
  text("override-label", "Sending…");
  const fb = $("override-feedback");
  fb.className = "feedback"; fb.textContent = "Waiting for the server…";
  try {
    const r = await getJSON("/api/toggle", { method: "POST" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const m = r.data.mode;
    fb.className = "feedback ok";
    fb.textContent = m === "auto" ? "Back to automatic. The ESP32 follows within about 2 s."
      : "Manual " + m.toUpperCase() + " set. The ESP32 follows within about 2 s.";
    announce(fb.textContent);
  } catch (e) {
    fb.className = "feedback err"; fb.textContent = "The switch did not change – the server did not confirm it.";
    announce(fb.textContent);
  } finally {
    state.overridePending = false;
    setTimeout(refresh, 600);
    setTimeout(refresh, 2600);
  }
});

// ---------- analytics ----------

function kpi(id, value, sub) { text(id, value); text(id + "-s", sub || " "); }

function renderHeatmap(heat) {
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const max = Math.max(1, ...heat.flat());
  const cells = [document.createElement("span")];
  for (let h = 0; h < 24; h++) {
    const l = document.createElement("span"); l.className = "hl"; l.textContent = h % 3 === 0 ? String(h).padStart(2, "0") : "";
    cells.push(l);
  }
  heat.forEach((hours, d) => {
    const lbl = document.createElement("span"); lbl.className = "lbl"; lbl.textContent = days[d]; cells.push(lbl);
    hours.forEach((v, h) => {
      const c = document.createElement("span"); c.className = "c";
      if (v) c.style.background = "rgba(37, 99, 235, " + (0.15 + 0.85 * v / max).toFixed(2) + ")";
      c.title = days[d] + " " + String(h).padStart(2, "0") + ":00 – " + v + " min with motion";
      cells.push(c);
    });
  });
  $("heatmap").replaceChildren(...cells);
  $("heat-legend").replaceChildren(...[0.15, 0.4, 0.65, 1].map((a) => {
    const s = document.createElement("span");
    s.style.cssText = "width:14px;height:14px;border-radius:3px;background:rgba(37,99,235," + a + ")";
    return s;
  }));
  $("t-heat").replaceChildren(...heat.map((hours, d) => {
    const top = hours.map((v, h) => [v, h]).filter((x) => x[0]).sort((a, b) => b[0] - a[0]).slice(0, 3)
      .map((x) => String(x[1]).padStart(2, "0") + ":00 (" + x[0] + ")").join(", ");
    return row([days[d], top || "no motion"]);
  }));
}

function renderAnalytics(a) {
  const label = { today: "Today", "7d": "Last 7 days", "30d": "Last 30 days" }[a.range];
  text("an-range-text", label + " · " + a.from + " to " + a.to + ". Cost and savings are estimates.");
  show("an-empty", !a.has_data);
  show("an-body", a.has_data);
  if (!a.has_data) return;
  const ch = analyticsCharts();
  const e = a.energy, sv = a.savings, oc = a.occupancy, rel = a.reliability;

  let vs = "";
  if (a.range === "today" && e.kwh_yesterday > 0) {
    const pct = Math.round(100 * (e.kwh_today - e.kwh_yesterday) / e.kwh_yesterday);
    vs = (pct <= 0 ? Math.abs(pct) + "% less" : pct + "% more") + " than yesterday";
  }
  kpi("k-energy", kwh(e.kwh_total) + " kWh", vs || label.toLowerCase());
  kpi("k-cost", "₹" + fmt(e.cost_total, 2), "at ₹" + a.assumptions.price_per_kwh + "/kWh");
  kpi("k-saved", sv.saved_vs_college_kwh >= 0 ? kwh(sv.saved_vs_college_kwh) + " kWh" : "−" + kwh(-sv.saved_vs_college_kwh) + " kWh",
    sv.saved_vs_college_kwh >= 0 ? "≈ ₹" + fmt(sv.saved_vs_college_cost, 2) + " vs on " + a.assumptions.college_hours
      : "used more than the college-hours baseline");
  kpi("k-occ", oc.occupied_pct == null ? "–" : fmt(oc.occupied_pct, 0) + "%",
    oc.class_minutes_with_data ? "of " + oc.class_minutes_with_data + " class minutes with data" : "no class time with data");
  kpi("k-up", rel.uptime_pct == null ? "–" : fmt(rel.uptime_pct, 0) + "%", rel.minutes_with_data + " minutes with ESP32 data");

  text("c-energy-sub", "kWh · " + label.toLowerCase());
  setChart(ch.energy, e.per_day.map((x) => x.label), [e.per_day.map((x) => x.kwh)]);
  $("t-energy").replaceChildren(...e.per_day.map((x) => row([x.label, fmt(x.kwh, 4), "₹" + fmt(x.kwh * a.assumptions.price_per_kwh, 2)])));
  const ie = $("i-energy");
  ie.hidden = !vs; ie.textContent = vs ? "Today used " + vs + "." : "";

  const lt = a.lighting;
  text("c-light-sub", "minutes · " + label.toLowerCase());
  setChart(ch.light, lt.per_day.map((x) => x.label), [lt.per_day.map((x) => x.in_class), lt.per_day.map((x) => x.outside)]);
  $("t-light").replaceChildren(...lt.per_day.map((x) => row([x.label, x.in_class, x.outside])));
  const il = $("i-light");
  il.hidden = !lt.outside_minutes;
  il.textContent = lt.outside_minutes ? "The light was on for " + lt.outside_minutes + " min outside class time (watchman switch, " +
    "fail-safe, or demo/test classes that are not in the timetable)." : "";

  const env = a.environment.buckets;
  const envLabels = env.map((b) => b.label);
  text("c-temp-sub", "°C · " + (a.range === "today" ? "per hour" : "per day"));
  text("c-hum-sub", "% · " + (a.range === "today" ? "per hour" : "per day"));
  setChart(ch.temp, envLabels, [env.map((b) => b.temp && b.temp.avg), env.map((b) => b.temp && b.temp.max),
    env.map(() => a.environment.fan_on_c)]);
  setChart(ch.hum, envLabels, [env.map((b) => b.hum && b.hum.avg)]);
  $("t-env").replaceChildren(...env.map((b) => row([b.label, b.temp ? b.temp.min : "–", b.temp ? b.temp.avg : "–",
    b.temp ? b.temp.max : "–", b.hum ? b.hum.avg : "–", b.ldr ? b.ldr.avg : "–"])));
  const noTemp = env.every((b) => !b.temp);
  const it = $("i-temp");
  it.hidden = !(a.environment.minutes_above_fan_on || noTemp);
  it.textContent = noTemp ? "No valid DHT11 readings in this period." :
    "The temperature was at or above " + a.environment.fan_on_c + " °C for " + a.environment.minutes_above_fan_on + " min.";

  text("c-heat-sub", "minutes with motion · " + label.toLowerCase());
  renderHeatmap(oc.heatmap);

  const cl = oc.classes;
  text("a-classes", cl.held + " held with people · " + cl.no_motion.length + " with nobody present · " +
    cl.no_data + " without enough data (ESP32 off).");
  $("a-nomotion").replaceChildren(...cl.no_motion.slice(0, 5).map((x) => {
    const li = document.createElement("li"); li.textContent = "No motion: " + x.date + " " + x.time; return li;
  }));
  const gap = rel.longest_gap_s;
  const gapText = gap >= 3600 ? Math.round(gap / 3600) + " h" : gap >= 60 ? Math.round(gap / 60) + " min" : gap + " s";
  text("a-rel", "Data coverage " + (rel.uptime_pct == null ? "–" : rel.uptime_pct + "%") + " · " + rel.offline_periods +
    " offline periods over " + rel.failsafe_gap_s + " s (ESP32 or server down) · longest gap " + gapText +
    (rel.last_reading ? " · last reading " + rel.last_reading : ""));
  const dec = a.decisions, sms = a.sms;
  const statuses = Object.entries(sms.by_status).map(([k, v]) => v + " " + k).join(", ");
  text("a-dec", (dec.since ? "Light kept off for daylight: " + dec.daylight_off_min + " min · watchman override: " +
    dec.override_min + " min (logged since " + dec.since + "). " : "Decision history starts with the next ESP32 request. ") +
    "SMS: " + sms.total + " (" + sms.alerts + " alerts, " + sms.tests + " tests)" + (statuses ? " – " + statuses : "") + ".");
  text("an-assume", "Assumptions: " + a.assumptions.rated_w + " W rated load, ₹" + a.assumptions.price_per_kwh +
    "/kWh, college hours " + a.assumptions.college_hours + ", occupied = motion within " + a.assumptions.hold_min + " min.");
}

async function loadAnalytics() {
  const btn = $("an-refresh");
  btn.disabled = true;
  try {
    const r = await getJSON("/api/analytics?range=" + state.analyticsRange);
    if (r.ok) { renderAnalytics(r.data); state.analyticsLoaded = true; }
    else text("an-range-text", "Analytics could not be loaded (HTTP " + r.status + ").");
  } catch (e) {
    text("an-range-text", "Analytics could not be loaded – the server did not answer.");
  } finally {
    btn.disabled = false;
  }
}

document.querySelectorAll("[data-range]").forEach((b) => b.addEventListener("click", () => {
  state.analyticsRange = b.dataset.range;
  document.querySelectorAll("[data-range]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  loadAnalytics();
}));
$("an-refresh").addEventListener("click", loadAnalytics);
new IntersectionObserver((entries, obs) => {
  if (entries.some((x) => x.isIntersecting) && !state.analyticsLoaded) { loadAnalytics(); obs.disconnect(); }
}, { rootMargin: "200px" }).observe($("analytics"));

// ---------- polling ----------

function setServer(ok) {
  if (state.serverOk === ok) return;
  const first = state.serverOk === null;
  state.serverOk = ok;
  $("banner").classList.toggle("show", !ok);
  $("main").classList.toggle("is-stale", !ok);
  pill("server-pill", ok ? "ok" : "err", ok ? "Server connected" : "Server unreachable");
  const h = $("h-server");
  h.textContent = ok ? "Connected" : "Unreachable";
  h.style.color = ok ? "var(--ok-text)" : "var(--err-text)";
  if (!first || !ok) announce(ok ? "Connection to the server restored" : "Server unreachable, showing last known values");
}

let inFlight = false;
async function refresh() {
  if (inFlight) return;                       // never overlap requests
  inFlight = true;
  try {
    const [st, co] = await Promise.all([getJSON("/api/status"), getJSON("/api/consumption")]);
    if (!st.ok || !co.ok) throw new Error("bad response");
    const s = st.data;
    state.status = s;
    setServer(true);
    if (state.espOnline !== null && state.espOnline !== s.esp_online) announce(s.esp_online ? "ESP32 back online" : "ESP32 offline");
    state.espOnline = s.esp_online;
    renderHeader(s); renderHealth(s);
    renderDevice("light", s, s.command.light, s.command.light_reason, s.overrides.light);
    renderDevice("fan", s, s.command.fan, s.command.fan_reason, s.overrides.fan);
    renderOverride(s); renderRoomAndClass(s); renderSensors(s); renderEnergy(co.data, s); renderSms(s);
    if (state.timetable.length) renderTimetable(s);
    text("last-refresh", new Date().toLocaleTimeString());
  } catch (e) {
    setServer(false);
    if (state.status) renderOverride(state.status);
  } finally {
    inFlight = false;
  }
}

async function loadTimetable() {
  try {
    const r = await getJSON("/api/timetable");
    if (r.ok) { state.timetable = r.data; if (state.status) renderTimetable(state.status); }
  } catch (e) { /* shown by the live poll */ }
}

setTheme(document.documentElement.dataset.theme === "dark");
loadTimetable().then(refresh);
loadSmsLog();
setInterval(refresh, POLL_MS);
setInterval(loadTimetable, TIMETABLE_MS);
setInterval(loadSmsLog, SMS_LOG_MS);
