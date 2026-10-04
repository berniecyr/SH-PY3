/* Sighthound Video — Record Viewer client */
(function () {
  "use strict";

  const form = document.getElementById("searchForm");
  const grid = document.getElementById("resultsGrid");
  const resultMeta = document.getElementById("resultMeta");
  const pageLabel = document.getElementById("pageLabel");
  const prevBtn = document.getElementById("prevPage");
  const nextBtn = document.getElementById("nextPage");
  const modal = document.getElementById("modal");
  const player = document.getElementById("player");
  const modalDetails = document.getElementById("modalDetails");
  const enrollBlock = document.getElementById("enrollBlock");
  const enrollName = document.getElementById("enrollName");
  const enrollGender = document.getElementById("enrollGender");
  const enrollBtn = document.getElementById("enrollBtn");
  const enrollStatus = document.getElementById("enrollStatus");
  const ruleSelect = document.getElementById("ruleSelect");
  const ruleNote = document.getElementById("ruleNote");
  const filterOnly = document.getElementById("filterOnly");

  let page = 0;
  let lastCount = 0;
  let currentRecord = null;
  let searchSeq = 0;

  // ---- helpers ----------------------------------------------------------

  function fmtTime(ms) {
    if (!ms) return "—";
    const d = new Date(ms);
    return d.toLocaleString([], {
      year: "numeric", month: "short", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit"
    });
  }

  function fmtDuration(a, b) {
    const s = Math.max(0, Math.round((b - a) / 1000));
    return s < 60 ? s + "s" : Math.floor(s / 60) + "m " + (s % 60) + "s";
  }

  function localToMs(value) {
    // value is a datetime-local string in the browser's local zone.
    if (!value) return null;
    const t = new Date(value).getTime();
    return isNaN(t) ? null : t;
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  const q = encodeURIComponent;

  // Filter results are detections, keyed by object id.  Rule results are
  // clips, and a duration- or region-only rule matches motion rather than a
  // detection row, so they may have no id at all — those fall back to
  // (camera, time), which both /api/thumb and /api/clip accept.
  function thumbUrl(r) {
    if (r.previewMs != null && r.camera) {
      return "/api/thumb?cam=" + q(r.camera) + "&ms=" + q(r.previewMs);
    }
    return "/api/thumb?id=" + q(r.id);
  }

  // Two of the cameras record H.265/HEVC.  Ask this browser whether it can
  // decode it rather than guessing: Firefox cannot at all, and Chrome only
  // with hardware support.  When it cannot, the server transcodes to H.264;
  // when it can, the server still has to retag the stream, because these
  // files are written as "hev1" and browsers want "hvc1".  H.264 clips are
  // unaffected either way.
  const canPlayHevc = (function () {
    try {
      const v = document.createElement("video");
      return ["video/mp4; codecs=\"hvc1.1.6.L93.B0\"",
              "video/mp4; codecs=\"hev1.1.6.L93.B0\""]
        .some(t => v.canPlayType(t) !== "");
    } catch (e) {
      return false;
    }
  })();

  function clipUrl(r) {
    const hevc = canPlayHevc ? "" : "&h264=1";
    if (r.id != null) return "/api/clip?id=" + q(r.id) + hevc;
    const mid = Math.round((r.timeStart + r.timeStop) / 2);
    return "/api/clip?cam=" + q(r.camera || "") + "&ms=" + q(mid) + hevc;
  }

  // ---- session / bootstrap ---------------------------------------------

  async function ensureSession() {
    try {
      const res = await fetch("/api/session");
      const data = await res.json();
      if (!data.authed) { window.location.href = "/login.html"; return false; }
      document.getElementById("whoami").textContent = data.user ? ("Signed in as " + data.user) : "";
      return true;
    } catch (e) {
      window.location.href = "/login.html";
      return false;
    }
  }

  async function loadFacets() {
    try {
      const res = await fetch("/api/facets");
      if (!res.ok) return;
      const f = await res.json();
      const camSel = document.getElementById("cameraSelect");
      (f.cameras || []).forEach(c => {
        const o = document.createElement("option");
        o.value = c; o.textContent = c; camSel.appendChild(o);
      });
      const typeSel = document.getElementById("typeSelect");
      (f.types || []).forEach(t => {
        const o = document.createElement("option");
        o.value = t; o.textContent = t.charAt(0).toUpperCase() + t.slice(1);
        typeSel.appendChild(o);
      });
      const dl = document.getElementById("faceNames");
      (f.faceNames || []).forEach(n => {
        const o = document.createElement("option");
        o.value = n; dl.appendChild(o);
      });
    } catch (e) { /* non-fatal */ }
  }

  async function loadRules() {
    try {
      const res = await fetch("/api/rules");
      if (!res.ok) return;
      const data = await res.json();
      (data.rules || []).forEach(r => {
        const o = document.createElement("option");
        o.value = r.name;
        // Mirror the desktop rule strip: a clock marks a rule with a custom
        // schedule, struck through when that schedule can never run.
        let label = r.name;
        if (r.scheduleState === true) label += " ⏱";
        else if (r.scheduleState === false) label += " ⏱ (never runs)";
        if (!r.isDefault && !r.enabled) label += " (disabled)";
        o.textContent = label;
        ruleSelect.appendChild(o);
      });
    } catch (e) { /* non-fatal: the filters still work */ }
  }

  // A rule brings its own notion of what to match, so the detection filters
  // stand down while one is selected.  Camera and date range still apply.
  function syncRuleMode() {
    const on = !!ruleSelect.value;
    ruleNote.classList.toggle("on", on);
    filterOnly.classList.toggle("standby", on);
    filterOnly.querySelectorAll("input, select").forEach(el => {
      el.disabled = on;
    });
  }

  async function loadEnrollNames() {
    try {
      const res = await fetch("/api/enrollnames");
      if (!res.ok) return;
      const data = await res.json();
      const dl = document.getElementById("enrollNames");
      dl.innerHTML = "";
      (data.people || []).forEach(p => {
        const o = document.createElement("option");
        o.value = p.name; dl.appendChild(o);
      });
    } catch (e) { /* non-fatal */ }
  }

  // ---- search -----------------------------------------------------------

  function buildQuery() {
    const fd = new FormData(form);
    const p = new URLSearchParams();
    const add = (k, v) => { if (v !== null && v !== undefined && v !== "") p.set(k, v); };

    // When a rule is selected the detection filters are disabled, so FormData
    // omits them and only rule/camera/range/limit reach the server.
    add("rule", fd.get("rule"));
    add("q", fd.get("q"));
    add("camera", fd.get("camera"));
    add("type", fd.get("type"));
    add("subType", fd.get("subType"));
    add("faceName", fd.get("faceName"));
    if (fd.get("hasFace")) add("hasFace", "1");
    add("gender", fd.get("gender"));
    add("ageMin", fd.get("ageMin"));
    add("ageMax", fd.get("ageMax"));
    if (fd.get("nudity")) add("nudity", "1");

    const confPct = parseInt(fd.get("minConfPct") || "0", 10);
    if (confPct > 0) add("minConf", (confPct / 100).toFixed(2));

    // Movement threshold, in px at 1280x720 -- the same units the desktop
    // search filter and the camera setting use.
    const travelPx = parseInt(fd.get("minTravelPx") || "0", 10);
    if (travelPx > 0) add("minTravel", travelPx);

    const fromMs = localToMs(fd.get("from"));
    const toMs = localToMs(fd.get("to"));
    if (fromMs !== null) add("startMs", fromMs);
    if (toMs !== null) add("endMs", toMs);

    const limit = parseInt(fd.get("limit") || "100", 10);
    add("limit", limit);
    add("offset", page * limit);
    return { qs: p.toString(), limit };
  }

  async function runSearch() {
    const { qs, limit } = buildQuery();
    // Searches now fire on every change, and a rule search takes far longer
    // than a filter search, so replies can arrive out of order.  Only the
    // newest request is allowed to paint.
    const seq = ++searchSeq;
    grid.innerHTML = '<div class="empty">Searching…</div>';
    try {
      const res = await fetch("/api/search?" + qs);
      if (res.status === 401) { window.location.href = "/login.html"; return; }
      const data = await res.json();
      if (seq !== searchSeq) return;
      if (data.error) {
        grid.innerHTML = '<div class="empty">' + esc(data.error) + "</div>";
        resultMeta.textContent = "";
        prevBtn.disabled = page === 0;
        nextBtn.disabled = true;
        return;
      }
      render(data.results || [], limit, data.total);
    } catch (e) {
      if (seq !== searchSeq) return;
      grid.innerHTML = '<div class="empty">Search failed. Is the back end running?</div>';
    }
  }

  function render(rows, limit, total) {
    lastCount = rows.length;
    const byRule = !!ruleSelect.value;
    grid.innerHTML = "";
    if (!rows.length) {
      grid.innerHTML = '<div class="empty">' + (byRule
        ? "No clips matched this rule in that range."
        : "No records match these filters.") + "</div>";
    } else {
      const frag = document.createDocumentFragment();
      rows.forEach(r => frag.appendChild(card(r)));
      grid.appendChild(frag);
    }
    // A rule search counts its whole result set before paging, so it can say
    // exactly how many there are; the filter search only knows this page.
    if (typeof total === "number") {
      resultMeta.textContent = total
        ? (total + (total === 1 ? " clip" : " clips")) : "";
      nextBtn.disabled = (page + 1) * limit >= total;
    } else {
      resultMeta.textContent = rows.length
        ? (rows.length + (rows.length === limit ? "+ records" : " record" + (rows.length === 1 ? "" : "s")))
        : "";
      nextBtn.disabled = rows.length < limit;
    }
    pageLabel.textContent = "Page " + (page + 1);
    prevBtn.disabled = page === 0;
  }

  function card(r) {
    const el = document.createElement("div");
    el.className = "card" + (r.hasClip ? "" : " noclip");

    const tags = [];
    tags.push('<span class="tag type">' + esc(r.subType || r.type || "object") + "</span>");
    if (r.objCount > 1) tags.push('<span class="tag conf">' + r.objCount + " objects</span>");
    if (r.faceName) tags.push('<span class="tag face">' + esc(r.faceName) +
      (r.faceConf ? " " + Math.round(r.faceConf * 100) + "%" : "") + "</span>");
    if (r.gender) tags.push('<span class="tag gender">' + esc(r.gender) +
      (r.age != null ? " · " + r.age : "") + "</span>");
    if (r.nudity) tags.push('<span class="tag nudity">nudity</span>');
    if (r.confidence != null) tags.push('<span class="tag conf">' +
      Math.round(r.confidence * 100) + "%</span>");

    el.innerHTML =
      '<div class="thumb-wrap">' +
        '<img loading="lazy" alt="" src="' + esc(thumbUrl(r)) + '">' +
        (r.hasClip ? '<div class="play-badge">▶</div>' : "") +
      "</div>" +
      '<div class="meta">' +
        '<div class="cam">' + esc(r.camera || "") + "</div>" +
        '<div class="time">' + fmtTime(r.timeStart) + " · " + fmtDuration(r.timeStart, r.timeStop) + "</div>" +
        '<div class="tags">' + tags.join("") + "</div>" +
      "</div>";

    el.addEventListener("click", () => openDetail(r));
    return el;
  }

  // ---- detail / player --------------------------------------------------

  function kv(k, v) {
    if (v == null || v === "") return "";
    return '<div class="kv"><div class="k">' + esc(k) + '</div><div class="v">' + esc(v) + "</div></div>";
  }

  function openDetail(r) {
    modalDetails.innerHTML =
      kv("Camera", r.camera) +
      kv("Type", r.subType ? (r.type + " / " + r.subType) : r.type) +
      kv("Start", fmtTime(r.timeStart)) +
      kv("End", fmtTime(r.timeStop)) +
      kv("Duration", fmtDuration(r.timeStart, r.timeStop)) +
      kv("Detection conf.", r.confidence != null ? Math.round(r.confidence * 100) + "%" : "") +
      kv("Face name", r.faceName) +
      kv("Face match", r.faceConf != null ? Math.round(r.faceConf * 100) + "%" : "") +
      kv("Gender", r.gender) +
      kv("Age", r.age) +
      kv("Nudity", r.nudity ? "yes" : "") +
      kv("Nudity detail", r.nudityDetail) +
      // null means "not measurable" (no motion rows, or a database the back end
      // has not upgraded yet), which kv() drops -- deliberately not shown as 0,
      // since "did not move" and "unknown" are different answers.
      kv("Movement", r.travel != null ? r.travel + " px" : "");

    if (r.hasClip) {
      player.style.display = "";
      player.src = clipUrl(r);
      player.load();
      player.play().catch(() => {});
    } else {
      player.removeAttribute("src");
      player.style.display = "none";
    }

    // Face enrollment: offered for person detections with footage on disk.
    // It enrolls by detection id, so a rule result that matched motion rather
    // than a detection (no id) cannot offer it.
    currentRecord = r;
    enrollStatus.textContent = "";
    enrollStatus.className = "enroll-status";
    if (r.id != null && r.type === "person" && r.hasClip) {
      enrollName.value = r.faceName || "";
      enrollGender.value = r.gender || "";
      enrollBlock.classList.remove("hidden");
    } else {
      enrollBlock.classList.add("hidden");
    }
    modal.classList.remove("hidden");
  }

  async function doEnroll() {
    if (!currentRecord) return;
    const name = enrollName.value.trim();
    if (!name) {
      enrollStatus.textContent = "Enter a person name first.";
      enrollStatus.className = "enroll-status err";
      return;
    }
    enrollBtn.disabled = true;
    enrollStatus.textContent = "Extracting face from footage…";
    enrollStatus.className = "enroll-status";
    try {
      const res = await fetch("/api/enroll", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: currentRecord.id,
          name: name,
          gender: enrollGender.value
        })
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.ok) {
        enrollStatus.textContent = "Added " + data.added + " face crop" +
          (data.added === 1 ? "" : "s") + " for " + data.name +
          " (baseline folder: " + data.folder + "). Cameras pick this up " +
          "within seconds.";
        enrollStatus.className = "enroll-status ok";
        loadEnrollNames();
      } else {
        enrollStatus.textContent = data.error || "Enroll failed.";
        enrollStatus.className = "enroll-status err";
      }
    } catch (e) {
      enrollStatus.textContent = "Cannot reach the server.";
      enrollStatus.className = "enroll-status err";
    } finally {
      enrollBtn.disabled = false;
    }
  }

  enrollBtn.addEventListener("click", doEnroll);

  function closeModal() {
    modal.classList.add("hidden");
    try { player.pause(); } catch (e) {}
    player.removeAttribute("src");
  }

  document.getElementById("modalClose").addEventListener("click", closeModal);
  modal.querySelector(".modal-backdrop").addEventListener("click", closeModal);
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

  // ---- wiring -----------------------------------------------------------

  // Searching is automatic — there is no Search button.  Two events, so that
  // each kind of control fires at the right moment:
  //   change  commits a value (select, checkbox, date picker) -> search at once
  //   input   typing and dragging (text, number, range)       -> search debounced
  // Each control is handled by exactly one of them, so a single interaction
  // never fires two searches (typing then blurring would otherwise do so).
  const kTypingDelay = 400;
  let searchTimer = null;

  function typedControl(el) {
    return el.tagName === "INPUT" &&
           (el.type === "text" || el.type === "number" || el.type === "search");
  }

  function debouncedControl(el) {
    return typedControl(el) || el.type === "range";
  }

  function autoSearch(delay) {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { page = 0; runSearch(); }, delay);
  }

  form.addEventListener("change", e => {
    if (e.target === ruleSelect) syncRuleMode();
    if (debouncedControl(e.target)) return;   // the input handler owns these
    autoSearch(0);
  });

  form.addEventListener("input", e => {
    if (debouncedControl(e.target)) autoSearch(kTypingDelay);
  });

  // No submit button remains, but Enter in a text field still submits.
  form.addEventListener("submit", e => { e.preventDefault(); autoSearch(0); });

  // Read the slider read-outs back off the inputs, so they stay right after a
  // form.reset() without hardcoding what the defaults happen to be.
  function syncSliderLabels() {
    document.getElementById("confVal").textContent =
      document.getElementById("minConfPct").value;
    document.getElementById("travelVal").textContent =
      document.getElementById("minTravelPx").value;
  }

  document.getElementById("clearBtn").addEventListener("click", () => {
    // form.reset() fires neither change nor input, so the delegated auto-search
    // stays quiet and the call below is the only search that runs.
    form.reset();
    syncSliderLabels();
    syncRuleMode();
    page = 0;
    runSearch();
  });

  document.getElementById("minConfPct").addEventListener("input", e => {
    document.getElementById("confVal").textContent = e.target.value;
  });
  document.getElementById("minTravelPx").addEventListener("input", e => {
    document.getElementById("travelVal").textContent = e.target.value;
  });
  prevBtn.addEventListener("click", () => { if (page > 0) { page--; runSearch(); } });
  nextBtn.addEventListener("click", () => { page++; runSearch(); });
  document.getElementById("logoutBtn").addEventListener("click", async () => {
    try { await fetch("/api/logout", { method: "POST" }); } catch (e) {}
    window.location.href = "/login.html";
  });

  // ---- go ---------------------------------------------------------------

  (async function init() {
    if (!(await ensureSession())) return;
    await Promise.all([loadFacets(), loadRules()]);
    syncRuleMode();
    loadEnrollNames();
    runSearch();
  })();
})();
