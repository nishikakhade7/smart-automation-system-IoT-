// dashboard.js - Polls the backend API every 2.5 seconds and updates the dashboard.
// /api/status      -> status cards, reason, classes, sensors
// /api/consumption -> today's kWh, power chart, power table
// /api/timetable   -> timetable table (refreshed every 60 s)

const POLL_MS = 2500;
const TIMETABLE_MS = 60000;

const $ = (id) => document.getElementById(id);

// ---------- small helpers ----------

function setBadge(id, text, kind) {
  // kind: "on" (green), "off" (grey), "bad" (red), "info" (blue)
  const el = $(id);
  el.textContent = text;
  el.className = "badge badge-" + kind;
}

async function getJSON(url) {
  const resp = await fetch(url, { cache: "no-store" });
  if (!resp.ok) throw new Error(url + " -> HTTP " + resp.status);
  return resp.json();
}

function showServerError(isError) {
  $("server-banner").classList.toggle("hidden", !isError);
}

// ---------- status cards ----------

function updateStatus(s) {
  const [date, time] = s.time.split(" ");
  $("clock").textContent = time;
  $("date").textContent = s.day_name + ", " + date;

  // Light / fan = the last command actually sent to the ESP32
  setBadge("light-badge", s.command.light ? "ON" : "OFF", s.command.light ? "on" : "off");
  setBadge("fan-badge", s.command.fan ? "ON" : "OFF", s.command.fan ? "on" : "off");
  $("reason").textContent = s.command.reason || "-";

  // Room
  setBadge("room-badge", s.occupied ? "Occupied" : "Empty", s.occupied ? "on" : "off");
  $("last-motion").textContent = s.last_motion
    ? "Last motion " + s.last_motion + " (hold " + s.hold_min + " min)"
    : "No motion yet";

  // ESP32 connection
  if (s.esp_online) {
    setBadge("esp-badge", "Online", "on");
    $("esp-seen").textContent = "Data " + s.seconds_since_data + " s ago";
  } else {
    setBadge("esp-badge", "Offline", "bad");
    $("esp-seen").textContent = s.seconds_since_data === null
      ? "No data received yet"
      : "Last data " + Math.round(s.seconds_since_data) + " s ago (ESP32 may be in fail-safe)";
  }

  // Classes
  if (s.current_class) {
    const c = s.current_class;
    setBadge("class-now-badge", "Class now", "info");
    $("class-now").textContent = c.subject + "  " + c.start + "-" + c.end;
  } else {
    setBadge("class-now-badge", "No class", "off");
    $("class-now").textContent = " ";
  }
  if (s.next_class) {
    const n = s.next_class;
    const when = s.next_class_days_ahead === 0 ? "Today"
      : s.next_class_days_ahead === 1 ? "Tomorrow" : n.day_name;
    $("class-next").textContent = when + " " + n.start + "-" + n.end + "  " + n.subject;
  } else {
    $("class-next").textContent = "None in the timetable";
  }

  // Sensors (latest reading)
  const r = s.latest || {};
  $("temp").textContent = r.temp == null ? "--" : Math.round(r.temp);
  $("hum").textContent = r.hum == null ? "--" : Math.round(r.hum);
  $("ldr").textContent = r.ldr == null ? "--" : Math.round(r.ldr);
  $("power-now").textContent = (r.power_w == null ? 0 : Math.round(r.power_w)) + " W";
  $("settings").textContent =
    "Lead time " + s.lead_min + " min  ·  Hold " + s.hold_min + " min  ·  Last reading " + (r.time || "-");

  highlightTimetable(s);
}

// ---------- power chart ----------

// Draws a thin vertical line at the hovered point (crosshair).
const crosshair = {
  id: "crosshair",
  afterDatasetsDraw(chart) {
    const active = chart.tooltip && chart.tooltip.getActiveElements();
    if (!active || !active.length) return;
    const x = active[0].element.x;
    const { top, bottom } = chart.chartArea;
    const ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = "#c3c2b7";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, bottom);
    ctx.stroke();
    ctx.restore();
  },
};

const powerChart = new Chart($("power-chart"), {
  type: "line",
  data: {
    labels: [],
    datasets: [{
      label: "Power (W)",
      data: [],
      borderColor: "#3987e5",
      backgroundColor: "rgba(57, 135, 229, 0.15)",
      borderWidth: 2,
      fill: true,
      stepped: true,          // power jumps when a relay switches
      pointRadius: 0,
      pointHoverRadius: 5,
      pointHoverBackgroundColor: "#3987e5",
      pointHoverBorderColor: "#1a1a19",
      pointHoverBorderWidth: 2,
    }],
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },     // one series: the title names it
      tooltip: {
        backgroundColor: "#0d0d0d",
        borderColor: "rgba(255,255,255,0.15)",
        borderWidth: 1,
        titleColor: "#c3c2b7",
        bodyColor: "#f0efec",
        displayColors: false,
        callbacks: { label: (ctx) => Math.round(ctx.parsed.y) + " W" },
      },
    },
    scales: {
      x: {
        ticks: { color: "#898781", maxTicksLimit: 6, maxRotation: 0 },
        grid: { display: false },
        border: { color: "#383835" },
      },
      y: {
        beginAtZero: true,
        suggestedMax: 120,
        ticks: { color: "#898781", maxTicksLimit: 5 },
        grid: { color: "#2c2c2a" },
        border: { display: false },
      },
    },
  },
  plugins: [crosshair],
});

function updateConsumption(c) {
  $("kwh").textContent = c.kwh_today.toFixed(3);

  const samples = c.samples;
  powerChart.data.labels = samples.map((p) => p.time);
  powerChart.data.datasets[0].data = samples.map((p) => (p.power_w == null ? 0 : p.power_w));
  powerChart.update("none");

  // Table view of the last 10 readings (newest first)
  $("power-table").innerHTML = samples.slice(-10).reverse()
    .map((p) => "<tr><td class='pr-6 text-2'>" + p.time + "</td><td class='text-right'>" +
                Math.round(p.power_w || 0) + "</td></tr>")
    .join("");
}

// ---------- timetable ----------

let timetableRows = [];   // [{slot, tr}] so we can re-highlight without rebuilding

function buildTimetable(slots) {
  const body = $("timetable");
  body.innerHTML = "";
  timetableRows = [];
  if (!slots.length) {
    body.innerHTML = "<tr><td colspan='4' class='py-2 muted'>No classes in the timetable.</td></tr>";
    return;
  }
  for (const slot of slots) {
    const tr = document.createElement("tr");
    tr.className = "border-t";
    tr.style.borderColor = "var(--border)";
    tr.innerHTML =
      "<td class='py-2 pr-4 text-2'>" + slot.day_name + "</td>" +
      "<td class='py-2 pr-4'>" + slot.start + "</td>" +
      "<td class='py-2 pr-4'>" + slot.end + "</td>" +
      "<td class='py-2'></td>";
    tr.lastChild.textContent = slot.subject;    // textContent: subject is plain text
    body.appendChild(tr);
    timetableRows.push({ slot, tr });
  }
}

function highlightTimetable(s) {
  const now = s.current_class;
  for (const { slot, tr } of timetableRows) {
    const isNow = now && slot.day === now.day && slot.start === now.start && slot.end === now.end;
    tr.classList.toggle("row-now", Boolean(isNow));
    tr.classList.toggle("row-today", slot.day_name === s.day_name);
  }
}

// ---------- polling loop ----------

async function refresh() {
  try {
    const [status, consumption] = await Promise.all([
      getJSON("/api/status"),
      getJSON("/api/consumption"),
    ]);
    updateStatus(status);
    updateConsumption(consumption);
    showServerError(false);
  } catch (err) {
    console.log("[dashboard] refresh failed:", err);
    showServerError(true);
  }
}

async function refreshTimetable() {
  try {
    buildTimetable(await getJSON("/api/timetable"));
  } catch (err) {
    console.log("[dashboard] timetable failed:", err);
  }
}

refreshTimetable().then(refresh);
setInterval(refresh, POLL_MS);
setInterval(refreshTimetable, TIMETABLE_MS);
