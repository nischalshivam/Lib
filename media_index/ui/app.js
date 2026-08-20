/* The Movie Editor, wired to the tool.
 *
 * The design decides how this looks; this decides what is true. Every number
 * on screen is read from the same files the menu already writes — nothing
 * here keeps its own copy of anything, so the browser and the terminal can
 * be open at once and neither can be wrong about the other.
 *
 * The style helpers (navStyle, chipStyle, segStyle …) are ported from the
 * design's own script, unchanged in what they produce. They live here rather
 * than being evaluated out of the design file because a design is a drawing:
 * it should never be able to decide what the tool does.
 */
(function () {
  "use strict";

  var state = {
    nav: "Library",
    theme: localStorage.getItem("me.theme") || "light",
    collapsed: localStorage.getItem("me.collapsed") === "1",
    filter: "all",
    loading: true,
    failed: "",
    library: null,

    // --- New Video ---------------------------------------------------
    form: load("me.form", {
      title: "", script: "", audio: "", name: "", out: "",
      preset: "auto", quality: "1080", pace: "normal", clip: 4.0,
      timings: "", timingsFrom: "", cast: "", narration: "", mode: "balanced",
      clues: "",
    }),
    cast: null,             // what the chosen cast folder holds
    castError: "",
    clues: null,            // what the chosen clue script offers, unchecked
    clueError: "",
    narration: null,        // the clean narration script, if one was given
    narrationError: "",
    script: null,           // what the chosen script says about itself
    scriptError: "",
    audio: null,
    srcOpen: false,
    task: null,             // the check or build that is running / just ran
    panelDismissed: false,

    picker: null,           // {kind, target, path, data}

    // --- Library ------------------------------------------------------
    addOpen: false,
    addRoot: "",
    addLook: null,          // what look_at_folder said
    addError: "",
    libTask: null,          // a scan/index that is running

    // --- Editor ------------------------------------------------------
    edFolder: localStorage.getItem("me.edFolder") || "",
    edBuild: null,          // what /api/build says about that folder
    edError: "",
    edNote: "",             // the last thing an edit said, good or bad
    edNoteBad: false,
    sel: null,              // {scene, file}
    find: null,             // {busy, query, error, data}
  };

  var screens = "";
  var where = null;
  var timer = null;

  // Bumped whenever what the form means changes. An update that leaves
  // yesterday's script, voiceover and folder sitting in the boxes looks
  // exactly like a form somebody filled in — and the first thing anyone
  // does with a filled-in form is press the button.
  var FORM_VERSION = "4";

  function load(key, fallback) {
    try {
      if (localStorage.getItem("me.formVersion") !== FORM_VERSION) {
        localStorage.setItem("me.formVersion", FORM_VERSION);
        localStorage.removeItem(key);
        return fallback;
      }
      var kept = JSON.parse(localStorage.getItem(key) || "null");
      return kept ? Object.assign({}, fallback, kept) : fallback;
    } catch (e) { return fallback; }
  }

  function setState(patch) {
    Object.keys(patch).forEach(function (k) { state[k] = patch[k]; });
    draw();
  }

  // A text field holds its own value; redrawing on every keystroke would
  // only take the caret away from whoever is typing. Remembered, not drawn.
  function setQuiet(patch) {
    Object.keys(patch).forEach(function (k) { state[k] = patch[k]; });
    remember();
  }

  function remember() {
    try { localStorage.setItem("me.form", JSON.stringify(state.form)); }
    catch (e) { /* a full or private-mode store is not worth an error */ }
  }

  /* ------------------------------------------------------------- fetching */

  function body(r) {
    return r.json().then(function (data) {
      if (!r.ok) throw new Error((data && data.error) || ("HTTP " + r.status));
      return data;
    });
  }

  function get(url) { return fetch(url).then(body); }

  function post(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(body);
  }

  function loadLibrary() {
    setState({ loading: true, failed: "" });
    return get("/api/titles").then(function (data) {
      // The title is chosen, not assumed. Filling it in silently means a
      // build can run against the wrong series without anyone having
      // touched the field — and with two titles in the library that is a
      // coin toss nobody was asked to call.
      setState({ library: data, loading: false });
    }).catch(function (err) {
      setState({ loading: false, failed: String(err.message || err) });
    });
  }

  /* ------------------------------------------------------- what runs, runs */

  function watch(task) {
    setState({ task: task, panelDismissed: false });
    if (timer) clearInterval(timer);
    if (task.status !== "running") return;
    timer = setInterval(function () {
      get("/api/task?id=" + encodeURIComponent(task.id)).then(function (now) {
        setState({ task: now });
        if (now.status !== "running" && now.status !== "queued") {
          clearInterval(timer);
          timer = null;
        }
      }).catch(function () {
        clearInterval(timer);
        timer = null;                   // the server went away; stop asking
      });
    }, 2500);
  }

  function spec() {
    var f = state.form;
    var chosen = titleNamed(f.title);
    return {
      name: f.name || "video",
      script: f.script,
      audio: f.audio,
      out: f.out,
      db: chosen ? chosen.db : "",
      clip_seconds: parseFloat(f.clip) || 4.0,
      pace: f.pace,
      quality: f.quality,
      preset: f.preset,
      title: f.title,
      timings: f.timings,
      cast: f.cast,
      narration: f.narration,
      mode: f.mode,
      clues: f.clues,
    };
  }

  function missingField() {
    var f = state.form;
    if (!f.title) return "Pehle title chuno";
    if (!f.script) return "Script chuno";
    if (!f.out) return "Output folder do";
    if (state.scriptError) return "Script padhi nahi ja rahi";
    return "";
  }

  function run(url, extra) {
    var why = missingField();
    if (why) { setState({ task: { status: "failed", error: why, kind: "check", lines: [] } }); return; }
    post(url, Object.assign(spec(), extra || {}))
      .then(watch)
      .catch(function (err) {
        setState({ task: { status: "failed", kind: "check", lines: [],
                           error: String(err.message || err) } });
      });
  }

  /* -------------------------------------------------- styles, from design */

  function navStyle(name) {
    var active = state.nav === name, c = state.collapsed;
    return "display:flex; align-items:center; gap:10px; border-radius:8px; cursor:pointer; user-select:none; font-size:13px; white-space:nowrap; overflow:hidden; transition:background .12s ease, color .12s ease; "
      + (c ? "padding:9px 0; justify-content:center; " : "padding:7px 10px; ")
      + (active
        ? "background:var(--accent-soft); color:var(--accent); font-weight:600;"
        : "color:var(--muted); font-weight:450;");
  }

  function chipStyle(name) {
    var active = state.filter === name;
    return "display:flex; align-items:center; gap:6px; padding:7px 14px; border-radius:999px; font-size:12.5px; cursor:pointer; user-select:none; transition:all .13s ease; "
      + (active
        ? "background:var(--accent); color:var(--on-accent); font-weight:550;"
        : "background:var(--raised); color:var(--muted); font-weight:500;");
  }

  function segStyle(name) {
    var active = state.theme === name;
    return "flex:1; display:flex; align-items:center; justify-content:center; gap:6px; padding:6px 0; border-radius:8px; font-size:12px; cursor:pointer; user-select:none; transition:all .15s ease; "
      + (active
        ? "background:var(--surface); color:var(--text); font-weight:600; box-shadow:var(--shadow-sm);"
        : "color:var(--muted); font-weight:500;");
  }

  function seg2(on) {
    return "flex:1; text-align:center; padding:7px 14px; border-radius:8px; font-size:12.5px; cursor:pointer; user-select:none; white-space:nowrap; transition:all .14s ease; "
      + (on ? "background:var(--surface); color:var(--text); font-weight:600; box-shadow:var(--shadow-sm);"
            : "color:var(--muted); font-weight:500;");
  }

  function presetCard(on) {
    return "flex:1; padding:14px 15px; border-radius:12px; cursor:pointer; user-select:none; transition:all .15s ease; border:1.5px solid "
      + (on ? "var(--accent); background:var(--accent-soft); box-shadow:var(--shadow-sm);"
            : "var(--border); background:var(--surface);");
  }

  var SECONDARY = "background:var(--surface); border:1px solid var(--border-strong); color:var(--text); font-size:13px; font-weight:550; padding:9px 14px; border-radius:9px; cursor:pointer; white-space:nowrap; transition:background .15s ease;";

  // The four status colours the design defines. A tone is decided once, so
  // the badge, its dot and the tile always agree.
  var TONES = { ready: "ok", partial: "busy", attention: "warn", empty: "muted" };

  function badgeStyle(tone) {
    var base = "display:inline-flex; align-items:center; gap:6px; font-size:11.5px; font-weight:550; padding:4px 10px; border-radius:999px; white-space:nowrap; ";
    if (tone === "muted") {
      return base + "color:var(--muted); background:var(--raised); border:1px solid var(--border);";
    }
    return base + "color:var(--" + tone + "); background:var(--" + tone
      + "-soft); border:1px solid var(--" + tone + "-line);";
  }

  function dot(tone) {
    return "width:6px; height:6px; flex:0 0 6px; border-radius:50%; background:"
      + (tone === "muted" ? "var(--muted)" : "var(--" + tone + ")") + ";";
  }

  function initialsOf(name) {
    var words = String(name || "?").split(/\s+/).filter(Boolean);
    if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
    return (words[0][0] + words[words.length - 1][0]).toUpperCase();
  }

  /* ------------------------------------------------------------ view model */

  function allTitles() { return (state.library || {}).titles || []; }

  function titleNamed(name) {
    return allTitles().filter(function (t) { return t.name === name; })[0] || null;
  }

  function titleView(t) {
    var tone = TONES[t.status] || "muted";
    var counted = t.kind === "movie"
      ? (t.size === "—" ? "not indexed" : t.size)
      : (t.files + " episode" + (t.files === 1 ? "" : "s")
         + (t.size === "—" ? "" : " · " + t.size));
    return {
      name: t.name, kind: t.kind, detail: t.detail,
      media_root: t.media_root || "—",
      counted: counted,
      files: t.files, indexed: t.indexed, missing: t.missing,
      percent: t.files ? Math.round(t.indexed * 100 / t.files) : 0,
      partial: t.indexed > 0 && t.indexed < t.files,
      hasNoSubs: t.no_subs.length > 0,
      noSubsList: t.no_subs.join(", "),
      hasMissing: t.missing > 0,
      initials: initialsOf(t.name),
      initialsStyle: "width:40px; height:40px; flex:0 0 40px; border-radius:11px; display:flex; align-items:center; justify-content:center; font-size:13px; font-weight:650; letter-spacing:-0.02em; "
        + (tone === "muted"
          ? "background:var(--raised); color:var(--muted);"
          : "background:var(--" + tone + "-soft); color:var(--" + tone + ");"),
      badgeStyle: badgeStyle(tone),
      dotStyle: "width:5px; height:5px; border-radius:50%; background:"
        + (tone === "muted" ? "var(--muted)" : "var(--" + tone + ")") + ";",
    };
  }

  function visible(rows) {
    var f = state.filter;
    return rows.filter(function (t) {
      if (f === "series") return t.kind === "series";
      if (f === "movie") return t.kind === "movie";
      if (f === "issue") return t.status !== "ready";
      return true;
    });
  }

  function baseName(path) {
    return String(path || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop();
  }

  function clock(seconds) {
    var s = Math.max(0, Math.round(seconds || 0));
    var m = Math.floor(s / 60);
    return m ? m + "m " + ("0" + (s % 60)).slice(-2) + "s" : s + "s";
  }

  /* ------------------------------------------------------------ the picker */

  /* Ask the machine to open its own file dialog. Falling back to the page's
   * own folder list only when it cannot — a Python without tkinter, or a
   * dialog that failed to appear. What must never happen is a Choose button
   * that does nothing. */
  function choosePath(kind, target) {
    var at = state.form[target] || localStorage.getItem("me.browsedAt") || "";
    setState({ picking: target });
    get("/api/pick?kind=" + encodeURIComponent(kind)
        + "&path=" + encodeURIComponent(at))
      .then(function (got) {
        setState({ picking: "" });
        if (!got.available) { openPicker(kind, target); return; }
        if (got.path) accept(target, got.path);
      })
      .catch(function () {
        setState({ picking: "" });
        openPicker(kind, target);
      });
  }

  function openPicker(kind, target) {
    // Where this field already points, then wherever the picker was last
    // left. Somebody choosing a script and then an output folder is almost
    // always working in the same corner of one drive, and starting them at
    // their home folder every time makes them walk back each time.
    var at = state.form[target] || localStorage.getItem("me.browsedAt") || "";
    setState({ picker: { kind: kind, target: target, path: at, data: null } });
    walk(at);
  }

  function walk(path) {
    get("/api/browse?kind=" + encodeURIComponent(state.picker.kind)
        + "&path=" + encodeURIComponent(path || ""))
      .then(function (data) {
        if (!state.picker) return;      // closed while the answer was coming
        state.picker.path = data.path;
        state.picker.data = data;
        try { localStorage.setItem("me.browsedAt", data.path); }
        catch (e) { /* a full store must not stop the picker working */ }
        draw();
      })
      .catch(function (err) {
        if (!state.picker) return;
        state.picker.data = { error: String(err.message || err),
                              folders: [], files: [], drives: [] };
        draw();
      });
  }

  function accept(target, path) {
    state.form[target] = path;
    remember();
    if (target === "script") readScript(path);
    else if (target === "audio") readAudio(path);
    else if (target === "cast") lookAtCast();
    else if (target === "narration") readNarration();
    else if (target === "clues") readClues();
    else draw();
  }

  function readClues() {
    var path = state.form.clues;
    remember();
    if (!path) { setState({ clues: null, clueError: "" }); return; }
    get("/api/clues?path=" + encodeURIComponent(path))
      .then(function (data) { setState({ clues: data, clueError: "" }); })
      .catch(function (err) {
        setState({ clues: null, clueError: String(err.message || err) });
      });
  }

  function readNarration() {
    var path = state.form.narration;
    remember();
    if (!path) { setState({ narration: null, narrationError: "" }); return; }
    get("/api/narration?path=" + encodeURIComponent(path))
      .then(function (data) { setState({ narration: data, narrationError: "" }); })
      .catch(function (err) {
        setState({ narration: null,
                   narrationError: String(err.message || err) });
      });
  }

  function lookAtCast() {
    var path = state.form.cast;
    remember();
    if (!path) { setState({ cast: null, castError: "" }); return; }
    get("/api/cast?path=" + encodeURIComponent(path))
      .then(function (data) { setState({ cast: data, castError: "" }); })
      .catch(function (err) {
        setState({ cast: null, castError: String(err.message || err) });
      });
  }

  function choose(path) {
    var target = state.picker.target;
    setState({ picker: null });
    accept(target, path);
  }

  function readScript(path) {
    setState({ script: null, scriptError: "" });
    get("/api/script?path=" + encodeURIComponent(path))
      .then(function (data) {
        // A name for the video, if there is not one already: the file's own
        // is a better first guess than an empty box.
        if (!state.form.name) {
          state.form.name = baseName(path).replace(/\.(json|txt)$/i, "");
        }
        // The script's own scene ranges, put in the box rather than applied
        // behind the page. They are the model's guesses — on a real script
        // one of them was ten minutes wide — so they belong somewhere a
        // person can see them and fix the two that matter.
        //
        // Never over a line somebody typed. Replaced only when the box is
        // empty, or still holds exactly what the LAST script filled in, so
        // choosing a different script does not leave stale times behind.
        var f = state.form;
        if (data.timings && (!f.timings || f.timings === f.timingsFrom)) {
          f.timings = data.timings;
          f.timingsFrom = data.timings;
        }
        remember();
        setState({ script: data });
      })
      .catch(function (err) {
        setState({ scriptError: String(err.message || err) });
      });
  }

  function readAudio(path) {
    setState({ audio: null });
    get("/api/audio?path=" + encodeURIComponent(path))
      .then(function (data) { setState({ audio: data }); })
      .catch(function (err) {
        setState({ audio: { error: String(err.message || err) } });
      });
  }

  function pickerScope() {
    var p = state.picker;
    if (!p) {
      return { pickerOpen: false, pickerRows: [], pickerDrives: [],
               pickerPath: "", pickerTitle: "",
               pickerError: "",
               pickerEmpty: false, pickerEmptyWhy: "", pickerHint: "",
               pickingFolder: false, hasDrives: false,
               closePicker: function () {},
               pickerUpGo: function () {}, useThisFolder: function () {},
               goToTyped: function () {} };
    }
    var d = p.data || { folders: [], files: [], drives: [] };
    var rows = [];
    (d.folders || []).forEach(function (f) {
      rows.push({ name: f.name, meta: "", icon: "▸",
        iconStyle: "width:16px; color:var(--faint); font-size:11px;",
        go: function () { walk(f.path); } });
    });
    (d.files || []).forEach(function (f) {
      rows.push({ name: f.name, icon: "•",
        meta: f.size > 1024 * 1024
          ? (f.size / 1024 / 1024).toFixed(1) + " MB"
          : Math.max(1, Math.round(f.size / 1024)) + " KB",
        iconStyle: "width:16px; color:var(--accent); font-size:11px;",
        go: function () { choose(f.path); } });
    });
    var folderMode = p.kind === "folder";
    return {
      pickerOpen: true,
      pickerTitle: { script: "Script chuno", audio: "Voiceover chuno",
                     folder: "Folder chuno" }[p.kind] || "Chuno",
      pickerPath: p.path || "",
      pickerRows: rows,
      pickerDrives: (d.drives || []).map(function (dr) {
        return { name: dr.name, go: function () { walk(dr.path); } };
      }),
      hasDrives: (d.drives || []).length > 1,
      pickerError: d.error || "",
      pickerEmpty: !d.error && rows.length === 0 && !!p.data,
      pickerEmptyWhy: folderMode ? "Is folder me aur folder nahi hai."
        : "Is folder me is tarah ki koi file nahi hai.",
      pickerHint: folderMode
        ? "Folder me jao, phir neeche wala button dabao."
        : "File pe click karo.",
      pickingFolder: folderMode,
      upBtn: "font-size:12px; font-weight:550; color:var(--muted); background:var(--raised); padding:7px 12px; border-radius:8px; cursor:pointer; white-space:nowrap;",
      pickerUpGo: function () { if (d.up) walk(d.up); },
      goToTyped: function (ev) { walk(ev.target.value.trim()); },
      useThisFolder: function () { choose(p.path); },
      closePicker: function () { setState({ picker: null }); },
    };
  }

  /* --------------------------------------------------------- the Library */

  function watchLibrary(task) {
    setState({ libTask: task, addOpen: false });
    var tick = setInterval(function () {
      get("/api/task?id=" + encodeURIComponent(task.id)).then(function (now) {
        setState({ libTask: now });
        if (now.status !== "running" && now.status !== "queued") {
          clearInterval(tick);
          loadLibrary();            // the counts on screen just changed
        }
      }).catch(function () { clearInterval(tick); });
    }, 1500);
  }

  function startIndexing(root, force) {
    post("/api/library/index", { root: root, force: !!force })
      .then(watchLibrary)
      .catch(function (err) {
        setState({ addError: String(err.message || err) });
      });
  }

  function libraryScope() {
    var t = state.libTask;
    var look = state.addLook;
    var running = !!t && (t.status === "running" || t.status === "queued");
    var rows = [];
    if (look) {
      rows.push({ ok: true, text: look.files + " video file(s) mile",
                  detail: (look.shows || []).map(function (p) {
                    return p[0] + " (" + p[1] + ")"; }).join(", ") });
      rows.push({ ok: look.subtitled === look.files,
                  text: look.subtitled + " episodes — subtitles theek" });
      if ((look.bitmap_subs || []).length) {
        rows.push({ warn: true,
                    text: look.bitmap_subs.length + " episodes — subtitle "
                          + "image-based hai (.srt chahiye)",
                    detail: look.bitmap_subs.join(", ") });
      }
      if ((look.missing_subs || []).length) {
        rows.push({ bad: true,
                    text: look.missing_subs.length + " episodes — subtitle "
                          + "hai hi nahi",
                    detail: look.missing_subs.join(", ") });
      }
    }
    return {
      openAdd: function () {
        setState({ addOpen: true, addLook: null, addError: "" });
      },
      closeAdd: function () { setState({ addOpen: false }); },
      addOpen: state.addOpen,
      addRoot: state.addRoot,
      setAddRoot: function (ev) {
        setState({ addRoot: ev.target.value.trim(), addLook: null });
      },
      pickAddRoot: function () {
        get("/api/pick?kind=folder&path="
            + encodeURIComponent(state.addRoot || ""))
          .then(function (r) {
            if (!r.available) { openPicker("folder", "out"); return; }
            if (r.path) setState({ addRoot: r.path, addLook: null });
          })
          .catch(function () { openPicker("folder", "out"); });
      },
      runLook: function () {
        setState({ addError: "", addLook: null });
        post("/api/library/look", { root: state.addRoot })
          .then(function (data) { setState({ addLook: data }); })
          .catch(function (err) {
            setState({ addError: String(err.message || err) });
          });
      },
      startIndex: function () {
        if (!look) return;
        startIndexing(state.addRoot, false);
      },
      lookBtn: SECONDARY,
      indexBtn: "background:var(--accent); color:var(--on-accent); font-size:13px; font-weight:600; padding:9px 16px; border-radius:9px; white-space:nowrap; "
        + (look ? "cursor:pointer; box-shadow:var(--shadow-sm);"
                : "opacity:.4; pointer-events:none;"),
      addNotChecked: !look,
      addChecked: !!look,
      addError: state.addError,
      addRows: rows.map(function (r) {
        var tint = r.bad ? "bad" : (r.warn ? "warn" : (r.ok ? "ok" : "warn"));
        return { text: r.text, detail: r.detail || "",
                 icon: r.bad ? "✗" : (r.warn ? "!" : (r.ok ? "✓" : "!")),
                 mark: "flex:0 0 16px; text-align:center; font-size:12px; font-weight:700; color:var(--" + tint + ");" };
      }),
      addEstimate: look
        ? ("Picture index me lagega: lagbhag " + Math.max(1, Math.round(look.minutes / 60))
           + " ghanta. Raat bhar chhod do — beech me band ho jaaye to dobara "
           + "chalane pe wahin se shuru hoga.")
        : "",

      libBusy: !!t,
      libState: running ? "WORKING" : (t && t.status === "failed" ? "FAILED" : "DONE"),
      libBadge: badgeStyle(running ? "busy" : (t && t.status === "failed" ? "bad" : "ok")),
      libDot: dot(running ? "busy" : (t && t.status === "failed" ? "bad" : "ok")),
      libStage: t ? (t.error || t.stage || "") : "",
      libElapsed: t ? clock(t.seconds) : "",
      libCount: (t && t.scenes_total)
        ? ("episode " + t.scenes_done + " / " + t.scenes_total) : "",
      libBar: (running && !(t && t.scenes_total))
        ? "height:100%; border-radius:99px; background:linear-gradient(90deg,var(--border) 0%,var(--busy) 50%,var(--border) 100%); background-size:220px 100%; animation:shimmer 1.1s linear infinite;"
        : "width:" + (t ? t.percent : 0) + "%; height:100%; background:var(--busy); border-radius:99px; transition:width .4s ease;",
      libLines: t ? (t.lines || []) : [],
      rowBtn: "padding:6px 10px; border-radius:8px; font-size:12px; font-weight:500; color:var(--muted); cursor:pointer; transition:all .12s ease;",
    };
  }

  /* ---------------------------------------------------------- the Editor */

  function loadFolder(path) {
    var at = (path || "").trim();
    if (!at) { setState({ edBuild: null, edError: "" }); return; }
    localStorage.setItem("me.edFolder", at);
    setState({ edFolder: at, edError: "", edBuild: null });
    get("/api/build?out=" + encodeURIComponent(at))
      .then(function (data) { setState({ edBuild: data, sel: null }); })
      .catch(function (err) {
        setState({ edError: String(err.message || err) });
      });
  }

  function selected() {
    var b = state.edBuild, s = state.sel;
    if (!b || !s) return null;
    for (var i = 0; i < b.scenes.length; i++) {
      if (b.scenes[i].scene !== s.scene) continue;
      for (var j = 0; j < b.scenes[i].items.length; j++) {
        if (b.scenes[i].items[j].file === s.file) {
          return { scene: b.scenes[i], item: b.scenes[i].items[j] };
        }
      }
    }
    return null;
  }

  function edit(url, payload, said) {
    post(url, Object.assign({ out: state.edFolder }, payload))
      .then(function () {
        setState({ edNote: said, edNoteBad: false });
        loadFolder(state.edFolder);
      })
      .catch(function (err) {
        setState({ edNote: String(err.message || err), edNoteBad: true });
      });
  }

  function search(query) {
    var got = selected();
    if (!got) return;
    setState({ find: { busy: true, query: query, error: "", data: null,
                       chosen: -1 } });
    post("/api/alternatives", {
      out: state.edFolder, scene: got.scene.scene, file: got.item.file,
      query: query,
    }).then(function (data) {
      setState({ find: { busy: false, query: data.query, error: "",
                         data: data, chosen: -1 } });
    }).catch(function (err) {
      setState({ find: { busy: false, query: query, chosen: -1,
                         error: String(err.message || err), data: null } });
    });
  }

  function editorScope() {
    var b = state.edBuild;
    var got = selected();
    var f = state.find;
    var cands = (f && f.data && f.data.candidates) || [];
    var best = cands.reduce(function (m, c) { return Math.max(m, c.score); }, 0);

    return {
      onEditor: state.nav === "Editor",
      edFolder: state.edFolder,
      setEdFolder: function (ev) { loadFolder(ev.target.value); },
      pickEdFolder: function () {
        var at = state.edFolder || localStorage.getItem("me.browsedAt") || "";
        get("/api/pick?kind=folder&path=" + encodeURIComponent(at))
          .then(function (r) {
            if (!r.available) { openPicker("folder", "out"); return; }
            if (r.path) loadFolder(r.path);
          })
          .catch(function () { openPicker("folder", "out"); });
      },
      exportBigBtn: "display:flex; align-items:center; gap:7px; background:var(--accent); color:var(--on-accent); font-size:13px; font-weight:600; padding:10px 18px; border-radius:9px; cursor:pointer; white-space:nowrap; box-shadow:var(--shadow-sm);"
        + (b ? "" : " opacity:.45; pointer-events:none;"),
      startRender: function () {
        post("/api/render", { out: state.edFolder,
                              audio: (b && b.audio) || "" })
          .then(function (task) { setState({ nav: "New Video" }); watch(task); })
          .catch(function (err) {
            setState({ edNote: String(err.message || err), edNoteBad: true });
          });
      },

      edHeadline: b
        ? (b.video || "video") + " · " + clock(b.total_seconds) + " · "
          + b.scenes.length + " scenes"
        : "Ek bani hui video ka folder do.",
      edEmpty: !b,
      edEmptyTitle: state.edError ? "Ye folder khula nahi" : "Koi video khuli nahi",
      edEmptyWhy: state.edError
        || "Upar folder ka path daalo — wahi jo New Video me output folder tha. Ya New Video se ek video banao.",
      edHasScenes: !!b && b.scenes.length > 0,
      edCounts: b ? Object.keys(b.counts).sort().map(function (k) {
        var tone = { anchor: "ok", verified: "busy", picture: "busy",
                     interpolated: "warn", paced: "warn", filler: "muted",
                     chosen: "accent" }[k] || "muted";
        return { label: k + " " + b.counts[k], style: badgeStyle(
                   tone === "accent" ? "busy" : tone),
                 dot: dot(tone === "accent" ? "busy" : tone) };
      }) : [],

      edScenes: b ? b.scenes.map(function (sc) {
        return {
          label: "scene " + ("00" + sc.scene).slice(-3),
          narration: sc.narration || sc.note || "—",
          span: clock(sc.start) + " → " + clock(sc.end),
          bare: sc.items.length === 0,
          items: sc.items.map(function (it) {
            var on = state.sel && state.sel.scene === sc.scene
                     && state.sel.file === it.file;
            var tone = { anchor: "ok", verified: "busy", picture: "busy",
                         interpolated: "warn", paced: "warn", filler: "muted",
                         chosen: "ok" }[it.placed_by] || "muted";
            return {
              // #t makes the browser seek one frame in and actually paint
              // it. Without it a <video> thumbnail is a grey rectangle, and
              // a wall of grey rectangles is the opposite of the point.
              url: it.kind === "video" ? it.url + "#t=0.1" : it.url,
              at: clock(it.source_start || 0),
              isVideo: it.kind === "video", isImage: it.kind !== "video",
              hold: (it.duration || 0).toFixed(1) + "s",
              placed_by: it.placed_by || "?",
              style: "position:relative; flex:0 0 132px; width:132px; border-radius:9px; overflow:hidden; cursor:pointer; transition:box-shadow .13s ease; border:"
                + (on ? "2px solid var(--accent); box-shadow:0 0 0 3px var(--accent-soft);"
                      : "1px solid var(--border-strong);"),
              badge: "position:absolute; top:4px; left:4px; font-size:9px; font-weight:650; padding:1.5px 5px; border-radius:4px; "
                + (tone === "muted"
                   ? "color:var(--muted); background:var(--raised);"
                   : "color:var(--" + tone + "); background:var(--" + tone
                     + "-soft); border:1px solid var(--" + tone + "-line);"),
              pick: function () {
                setState({ sel: { scene: sc.scene, file: it.file },
                           edNote: "" });
              },
            };
          }),
        };
      }) : [],

      edPicked: !!got,
      sel: got ? {
        scene: got.scene.scene,
        narration: got.scene.narration || "—",
        url: got.item.url,
        isVideo: got.item.kind === "video",
        isImage: got.item.kind !== "video",
        source: got.item.source || "—",
        at: "at " + clock(got.item.source_start || 0),
        duration: (got.item.duration || 0).toFixed(1),
        placed_by: got.item.placed_by || "?",
        badge: badgeStyle({ anchor: "ok", verified: "busy", picture: "busy",
                            interpolated: "warn", paced: "warn", filler: "muted",
                            chosen: "ok" }[got.item.placed_by] || "muted"),
      } : { scene: "", narration: "", url: "", source: "", at: "",
            duration: "", placed_by: "", badge: "",
            isVideo: false, isImage: false },
      setDuration: function (ev) {
        edit("/api/edit", { scene: got.scene.scene, file: got.item.file,
                            duration: parseFloat(ev.target.value) },
             "duration badal di");
      },
      removeShot: function () {
        edit("/api/edit", { scene: got.scene.scene, file: got.item.file,
                            remove: true }, "shot hata diya");
      },
      edNote: state.edNote,
      edNoteStyle: "margin-top:14px; padding:10px 12px; border-radius:9px; font-size:11.5px; line-height:1.6; "
        + (state.edNoteBad
           ? "background:var(--bad-soft); border:1px solid var(--bad-line); color:var(--bad);"
           : "background:var(--ok-soft); border:1px solid var(--ok-line); color:var(--ok);"),

      // --- Find another ---------------------------------------------
      openFind: function () { search(got.scene.narration || ""); },
      closeFind: function () { setState({ find: null }); },
      findOpen: !!f,
      findBusy: !!f && f.busy,
      findError: (f && f.error) || "",
      findQuery: (f && f.query) || "",
      runFind: function (ev) { search(ev.target.value); },
      searchAgain: function () {
        var box = document.querySelector('[data-keep="findQuery"]');
        search(box ? box.value : (f && f.query) || "");
      },
      findWhere: f && f.data
        ? f.data.episode + " · " + f.data.searched + " frames dekhe"
        : "",
      findHasResults: cands.length > 0,
      findNone: !!f && !f.busy && !f.error && !!f.data && cands.length === 0,
      candidates: cands.map(function (c, n) {
        var on = f.chosen === n;
        return {
          url: "/file?out=" + encodeURIComponent(state.edFolder)
               + "&rel=" + encodeURIComponent(f.data.folder + "/" + c.file),
          at: clock(c.at),
          current: !!c.current,
          bar: Math.max(6, Math.round((best ? c.score / best : 0) * 100)),
          tone: c.confidence === "high" ? "ok"
                : (c.confidence === "medium" ? "busy" : "muted"),
          style: "border-radius:10px; overflow:hidden; cursor:pointer; background:var(--surface); transition:box-shadow .12s ease; border:"
            + (on ? "2px solid var(--accent); box-shadow:0 0 0 3px var(--accent-soft);"
                  : "1px solid var(--border-strong);"),
          pick: function () { f.chosen = n; draw(); },
        };
      }),
      useLabel: (f && f.chosen >= 0) ? "Use this shot" : "Ek shot chuno",
      useBtn: "font-size:12.5px; font-weight:600; padding:9px 16px; border-radius:9px; white-space:nowrap; "
        + ((f && f.chosen >= 0)
           ? "background:var(--accent); color:var(--on-accent); cursor:pointer; box-shadow:var(--shadow-sm);"
           : "background:var(--raised); color:var(--faint); cursor:not-allowed;"),
      useChosen: function () {
        if (!f || f.chosen < 0) return;
        var at = cands[f.chosen].at;
        setState({ find: null });
        edit("/api/replace", { scene: got.scene.scene, file: got.item.file,
                               at: at }, "shot badal diya — " + clock(at));
      },
    };
  }

  /* --------------------------------------------------------- the New Video */

  var PRESETS = [
    { key: "auto", name: "Auto", why: "Script ke mood se khud chunta hai", rec: true },
    { key: "cinematic", name: "Cinematic", why: "Lambe shots, teal-orange" },
    { key: "tense", name: "Tense", why: "Chhote cuts, high contrast" },
    { key: "documentary", name: "Documentary", why: "Slow push, flat grade" },
  ];
  var PACES = [["calm", "Calm"], ["normal", "Normal"],
               ["quick", "Quick"], ["rapid", "Rapid"]];

  var VERDICT = { READY: ["ok", "OK"], GAPS: ["warn", "GAPS"],
                  BLOCKED: ["bad", "BLOCKED"] };

  function newVideoScope() {
    var f = state.form;
    var t = state.task;
    var report = (t && t.report && t.report.verdict) ? t.report : null;
    var ev = (report && report.evidence) || {};

    // One segment of the "what is this footage resting on" bar.
    function bar(part, whole, tone) {
      var share = whole ? (part || 0) * 100 / whole : 0;
      return "width:" + share.toFixed(1) + "%; background:var(--" + tone
             + "); transition:width .2s ease;";
    }
    var running = !!t && (t.status === "running" || t.status === "queued");
    var chosen = titleNamed(f.title);
    var tone = chosen ? (TONES[chosen.status] || "muted") : "muted";

    var verdictTone = "busy", verdictLabel = "WORKING", verdictWhy = t ? (t.stage || "") : "";
    if (report) {
      var v = VERDICT[report.verdict] || ["busy", report.verdict];
      verdictTone = v[0];
      verdictLabel = v[1];
      verdictWhy = report.percent + "% shots placeable · "
        + report.beats + " scenes, " + report.shots + " shots";
    }
    if (t && t.status === "failed") { verdictTone = "bad"; verdictLabel = "FAILED"; }
    if (t && t.kind === "build" && t.status === "done") {
      verdictTone = "ok"; verdictLabel = "BUILT"; verdictWhy = t.stage || "";
    }

    return {
      onNewVideo: state.nav === "New Video",

      pickedName: f.title || "title chuno",
      pickedDetail: chosen ? chosen.detail
        : ((state.library && (state.library.titles || []).length)
           ? "yahan click karo" : "Library me jaake ek banao"),
      pickedDot: dot(tone),
      srcOpen: state.srcOpen,
      toggleSrc: function () { setState({ srcOpen: !state.srcOpen }); },
      srcOptions: allTitles().map(function (row) {
        var on = row.name === f.title;
        return {
          name: row.name, detail: row.detail,
          dotStyle: dot(TONES[row.status] || "muted"),
          rowStyle: "display:flex; align-items:center; gap:10px; padding:9px 11px; border-radius:8px; cursor:pointer;"
            + (on ? " background:var(--accent-soft);" : ""),
          nameStyle: "flex:1; font-size:13px;" + (on ? " font-weight:550; color:var(--accent);" : ""),
          pick: function () { f.title = row.name; remember(); setState({ srcOpen: false }); },
        };
      }),

      pathInput: "flex:1; min-width:0; box-sizing:border-box; background:var(--surface); border:1px solid var(--border-strong); border-radius:9px; padding:9px 12px; color:var(--text); font-size:12px; outline:none; font-family:'Cascadia Code', Consolas, monospace;",
      scriptPath: f.script,
      setScript: function (ev) { accept("script", ev.target.value.trim()); },
      pickScript: function () { choosePath("script", "script"); },
      scriptOk: !!state.script && !state.scriptError,
      script: state.script || { beats: 0, shots: 0 },
      scriptEpisodes: state.script
        ? (state.script.episodes.length
            ? state.script.episodes.slice(0, 8).join(", ")
              + (state.script.episodes_total > 8
                 ? " +" + (state.script.episodes_total - 8) : "")
            : (state.script.titles || []).join(", ") || "koi episode named nahi")
        : "",
      scriptError: state.scriptError,
      scriptNote: state.script ? (state.script.note || "") : "",
      // Whose photos to gather, read straight from the chosen script. Shown
      // the moment the script is picked so the cast folder can be built
      // before the video is, not discovered missing halfway through.
      castNeededShow: !!(state.script && state.script.cast_needed
                         && (state.script.cast_needed.main || []).length),
      castNeeded: (state.script && state.script.cast_needed
                   ? (state.script.cast_needed.main || []) : []).map(
        function (c) { return c.name; }),
      castNeededPhotos: (state.script && state.script.cast_needed
                         && state.script.cast_needed.photos_each) || 7,

      audioPath: f.audio,
      setAudio: function (ev) { accept("audio", ev.target.value.trim()); },
      audioLength: state.audio
        ? (state.audio.error ? "padhi nahi ja rahi: " + state.audio.error
                             : clock(state.audio.seconds))
        : "",
      pickAudio: function () { choosePath("audio", "audio"); },

      timingsText: f.timings,
      setTimings: function (ev) { f.timings = ev.target.value; setQuiet({}); },
      // Counted here rather than asked of the server: the box is read as it
      // is typed, and a round trip per keystroke to be told "3 lines" is a
      // round trip nobody needed.
      timingsCount: (function () {
        var n = (f.timings || "").split("\n").filter(function (line) {
          return /\d+\s*[:x]\s*\d+/i.test(line) && /\d+\s*:\s*\d+/.test(line);
        }).length;
        return n ? n + " episode ki timing di hai" : "";
      })(),

      castPath: f.cast,
      setCast: function (ev) { f.cast = ev.target.value.trim(); lookAtCast(); },
      pickCast: function () { choosePath("folder", "cast"); },
      castSummary: !!(state.cast && (state.cast.people || []).length),
      castPeople: state.cast
        ? (state.cast.people || []).map(function (p) {
            return p.name + " · " + p.images;
          })
        : [],
      castError: state.castError,

      cluePath: f.clues,
      setClues: function (ev) {
        f.clues = ev.target.value.trim();
        readClues();
      },
      pickClues: function () { choosePath("script", "clues"); },
      clueFacts: state.clues
        ? state.clues.clues + " clue · " + state.clues.lines + " dialogue line"
          + " · " + state.clues.bracketed + " scene dono taraf se bandhe"
        : "",
      // Said separately from the count, because the count looks healthy
      // either way. A clue with no line is a clue that cannot be checked
      // against anything, and a script full of them buys nothing at all.
      clueWeak: (function () {
        var c = state.clues;
        if (!c || !c.clues) return "";
        var mute = c.clues - c.with_dialogue;
        if (mute * 5 <= c.clues) return "";
        return mute + " clue me koi dialogue nahi hai (" + c.clues + " me se)"
             + " — inse kuch nahi milega. Claude se dobara maango: har scene"
             + " ke pehle aur baad wali line yaad karke bhare.";
      })(),
      clueError: state.clueError,

      narrationPath: f.narration,
      setNarration: function (ev) {
        f.narration = ev.target.value.trim();
        readNarration();
      },
      pickNarration: function () { choosePath("narration", "narration"); },
      narrationFacts: state.narration
        ? state.narration.words + " words — voiceover isi se time hoga"
        : "",
      narrationError: state.narrationError,

      videoTitle: f.name,
      setVideoTitle: function (ev) { f.name = ev.target.value; setQuiet({}); },
      outFolder: f.out,
      setOutFolder: function (ev) { f.out = ev.target.value; setQuiet({}); },
      pickOut: function () { choosePath("folder", "out"); },

      presets: PRESETS.map(function (p) {
        return { name: p.name, why: p.why, recommended: !!p.rec,
                 style: presetCard(f.preset === p.key),
                 pick: function () { f.preset = p.key; remember(); draw(); } };
      }),
      modes: [["balanced", "Balanced", "Poori video bharti hai. Silent shots par Gemini asli frame dhoondhta hai. Ye default hai."],
              ["strict", "Strict", "Sirf dialogue-pakki footage. Baaki har shot BLACK CARD — aadhi video khaali ho sakti hai."],
              ["draft", "Draft", "Sab bhar do, kamzor bhi. Rough cut ke liye."]]
        .map(function (m) {
          return { key: m[0], name: m[1], why: m[2],
                   style: presetCard(f.mode === m[0]),
                   pick: function () { f.mode = m[0]; remember(); draw(); } };
        }),
      q1080: seg2(f.quality === "1080"),
      q4k: seg2(f.quality === "4k"),
      is4k: f.quality === "4k",
      set1080: function () { f.quality = "1080"; remember(); draw(); },
      set4k: function () { f.quality = "4k"; remember(); draw(); },
      paces: PACES.map(function (p) {
        return { name: p[1], style: seg2(f.pace === p[0]),
                 pick: function () { f.pace = p[0]; remember(); draw(); } };
      }),
      clipSeconds: f.clip,
      setClip: function (ev) { f.clip = ev.target.value; setQuiet({}); },

      checkBtn: SECONDARY + (running ? " opacity:.5; pointer-events:none;" : ""),
      exportBtn: "color:var(--muted); font-size:12.5px; font-weight:550; padding:9px 13px; border-radius:9px; cursor:pointer; white-space:nowrap; transition:all .14s ease;"
        + (running ? " opacity:.5; pointer-events:none;" : ""),
      buildBtn: "display:flex; align-items:center; gap:7px; background:var(--accent); color:var(--on-accent); font-size:13px; font-weight:600; padding:10px 16px; border-radius:9px; cursor:pointer; white-space:nowrap; box-shadow:var(--shadow-sm); transition:background .15s ease;"
        + (running ? " opacity:.5; pointer-events:none;" : ""),
      clearForm: function () {
        state.form = { title: "", script: "", audio: "", name: "", out: "",
                       timings: "", timingsFrom: "", cast: "", narration: "",
                       mode: "balanced",
                       preset: "auto", quality: "1080", pace: "normal",
                       clip: 4.0 };
        remember();
        setState({ script: null, scriptError: "", audio: null, task: null });
      },
      runCheck: function () { run("/api/check"); },
      buildEditor: function () { run("/api/build", { after: "editor" }); },
      buildExport: function () { run("/api/build", { after: "export" }); },

      noPanel: !t || state.panelDismissed,
      panelOpen: !!t && !state.panelDismissed,
      closePanel: function () { setState({ panelDismissed: true }); },
      verdictStyle: badgeStyle(verdictTone),
      verdictLabel: verdictLabel,
      verdictWhy: verdictWhy,
      taskRunning: running,
      // Pre-flight knows how much work there is only once it has read the
      // script, so for the first half-minute there is no percentage to show.
      // A bar frozen at zero is how a tool that is working looks broken, so
      // that stretch gets a moving one instead of a still one.
      barStyle: (running && !(t && t.scenes_total))
        ? "height:100%; border-radius:99px; background:linear-gradient(90deg,var(--border) 0%,var(--busy) 50%,var(--border) 100%); background-size:220px 100%; animation:shimmer 1.1s linear infinite;"
        : "width:" + (t ? t.percent : 0) + "%; height:100%; background:var(--busy); border-radius:99px; transition:width .3s ease;",
      taskStage: t ? (t.stage || "chal raha hai…") : "",
      taskElapsed: t ? clock(t.seconds) : "",
      taskFailed: !!t && (t.status === "failed" || t.status === "blocked"),
      taskError: t ? (t.error || t.stage || "") : "",
      hasReport: !!report,
      reportChecks: (report ? report.checks : []).map(function (c) {
        var tint = c.ok ? "ok" : (c.fatal ? "bad" : "warn");
        return { name: c.name, detail: c.detail,
                 icon: c.ok ? "✓" : (c.fatal ? "✗" : "!"),
                 mark: "flex:0 0 16px; text-align:center; font-size:12px; font-weight:700; color:var(--" + tint + ");" };
      }),
      clueNote: report ? (report.clue_note || "") : "",
      hasEvidence: !!(report && report.evidence && report.evidence.total),
      evExact: ev.exact || 0,
      evBetween: ev.between || 0,
      evLoose: ev.loose || 0,
      evExactBar: bar(ev.exact, ev.total, "ok"),
      evBetweenBar: bar(ev.between, ev.total, "warn"),
      evLooseBar: bar(ev.loose, ev.total, "muted"),
      // Below this the video is mostly the right episode and not much else,
      // and somebody about to spend forty minutes rendering should be told
      // BEFORE, not by watching it afterwards.
      evWeak: !!ev.total && (ev.loose / ev.total) > 0.25,
      evWeakWhy: !ev.total ? "" : Math.round(ev.loose * 100 / ev.total)
        + "% shots ke liye sirf episode pata hai, moment nahi. Ye clips "
        + "random lagengi. Niche jo timings maangi gayi hain wo bhar do, ya "
        + "script me un runs ke liye quoted lines add karwao.",

      hasLearnedTiming: !!report && (report.learned_timing || []).length > 0,
      learnedTiming: report ? (report.learned_timing || []) : [],
      useLearned: function () {
        // The derived lines replace only the episodes they cover. A line
        // somebody typed for a run that quoted nothing is the one thing
        // here that was not worked out, and it must survive being helped.
        var mine = {}, order = [];
        (f.timings || "").split("\n").forEach(function (line) {
          var key = (line.match(/s\d{1,2}\s*e\d{1,3}/i) || [""])[0].toUpperCase();
          if (!key) return;
          if (!(key in mine)) order.push(key);
          mine[key] = line.trim();
        });
        (report.learned_timing || []).forEach(function (t) {
          var key = (t.line.match(/S\d{1,2}E\d{1,3}/i) || [""])[0].toUpperCase();
          if (!key) return;
          if (!(key in mine)) order.push(key);
          mine[key] = t.line;
        });
        f.timings = order.map(function (k) { return mine[k]; }).join("\n");
        f.timingsFrom = "";
        remember();
        setState({ edNote: "" });
      },
      hasNeedsTiming: !!report && (report.needs_timing || []).length > 0,
      needsTiming: report ? (report.needs_timing || []) : [],
      hasWeak: !!report && (report.weak_scenes || []).length > 0,
      weakScenes: report ? report.weak_scenes : [],
      builtOk: !!t && t.kind === "build" && t.status === "done",
      openBuilt: function () {
        loadFolder((t && t.out) || state.form.out);
        setState({ nav: "Editor" });
      },
      hasLines: !!t && (t.lines || []).length > 0,
      taskLines: t ? (t.lines || []) : [],
    };
  }

  /* ----------------------------------------------------------------- scope */

  var STUB = {
    "Queue": "Yahan saari videos ki list hogi — jo ban rahi hai aur jo ban chuki.",
    "Settings": "Folders, default quality, ffmpeg ka path.",
  };

  function scope() {
    var lib = state.library || { titles: [], counts: {}, databases: [], root: "" };
    var go = function (name) {
      return function () { setState({ nav: name }); };
    };
    var rows = visible(lib.titles || []);
    var first = (lib.databases || [])[0] || "";
    var home = lib.root || first.replace(/[\\/][^\\/]*$/, "");
    var count = (lib.titles || []).length;

    var common = {
      theme: state.theme,
      collapsed: state.collapsed,
      expanded: !state.collapsed,
      logoTitle: state.collapsed ? "Sidebar kholo" : "Sidebar chhota karo",
      toggleSidebar: function () {
        localStorage.setItem("me.collapsed", state.collapsed ? "0" : "1");
        setState({ collapsed: !state.collapsed });
      },
      toggleTheme: function () { setTheme(state.theme === "dark" ? "light" : "dark"); },
      setLight: function () { setTheme("light"); },
      setDark: function () { setTheme("dark"); },
      lightBtn: segStyle("light"),
      darkBtn: segStyle("dark"),
      footer: (home || "—") + " · " + count + (count === 1 ? " title" : " titles"),

      sidebarStyle: "width:" + (state.collapsed ? 64 : 228) + "px; flex:0 0 "
        + (state.collapsed ? 64 : 228)
        + "px; background:var(--sidebar); border-right:1px solid var(--border); display:flex; flex-direction:column; transition:width .18s ease, flex-basis .18s ease; overflow:hidden;",
      sideGroupStyle: "padding:" + (state.collapsed ? "6px 10px 0 10px" : "6px 12px 0 12px")
        + "; display:flex; flex-direction:column; gap:2px;",
      sideGroup2Style: "padding:" + (state.collapsed ? "12px 10px 0 10px" : "14px 12px 0 12px")
        + "; display:flex; flex-direction:column; gap:2px;",
      groupLabel: "padding:8px 8px 6px 8px; font-size:10px; font-weight:650; letter-spacing:0.1em; color:var(--faint); white-space:nowrap; "
        + (state.collapsed ? "display:none;" : ""),
      soonItem: "display:flex; align-items:center; gap:10px; border-radius:8px; font-size:13px; color:var(--faint); opacity:.6; cursor:not-allowed; white-space:nowrap; overflow:hidden; "
        + (state.collapsed ? "padding:9px 0; justify-content:center;" : "padding:7px 10px;"),
      labelStyle: state.collapsed ? "display:none;" : "",

      navNewVideo: navStyle("New Video"), navQueue: navStyle("Queue"),
      navEditor: navStyle("Editor"), navLibrary: navStyle("Library"),
      navSettings: navStyle("Settings"),
      goNewVideo: go("New Video"), goQueue: go("Queue"), goEditor: go("Editor"),
      goLibrary: go("Library"), goSettings: go("Settings"),

      onLibrary: state.nav === "Library",
      onStub: state.nav !== "Library" && state.nav !== "New Video"
              && state.nav !== "Editor",
      stubTitle: state.nav,
      stubWhy: STUB[state.nav] || "Abhi ban raha hai.",
      stubGoLabel: "Library kholo",
      stubGo: go("Library"),

      loading: state.loading,
      failed: state.failed,
      ready: !state.loading && !state.failed,
      nothing: !state.loading && !state.failed && rows.length === 0,
      emptyWhy: count
        ? "Is filter me koi title nahi. Upar 'All' dabao."
        : "Koi library nahi mili. start.bat kholo, 8 se media folder set karo, phir 4 aur L chalao.",
      refresh: function () { loadLibrary(); },

      counts: {
        all: (lib.counts || {}).all || 0,
        series: (lib.counts || {}).series || 0,
        movies: (lib.counts || {}).movies || 0,
        attention: (lib.counts || {}).attention || 0,
      },
      chipAll: chipStyle("all"), chipSeries: chipStyle("series"),
      chipMovie: chipStyle("movie"), chipIssue: chipStyle("issue"),
      filterAll: function () { setState({ filter: "all" }); },
      filterSeries: function () { setState({ filter: "series" }); },
      filterMovie: function () { setState({ filter: "movie" }); },
      filterIssue: function () { setState({ filter: "issue" }); },

      // Stops a click inside a panel from reaching the backdrop behind it,
      // which closes on purpose. Defined once, in the scope every screen
      // shares: when it lived in the picker's own scope, the closed-picker
      // stub quietly replaced it with a no-op and every other panel started
      // dismissing itself the moment it was touched.
      swallow: function (ev) { ev.stopPropagation(); },

      shown: rows.map(function (t) {
        var view = titleView(t);
        view.update = function () { startIndexing(t.media_root, false); };
        view.rebuild = function () { startIndexing(t.media_root, true); };
        return view;
      }),
      databases: lib.databases || [],
      chooseBtn: SECONDARY,
    };

    return Object.assign(common, libraryScope(), newVideoScope(),
                         editorScope(), pickerScope());
  }

  function applyTheme(name) {
    // The page background, so the strip outside the app matches. Set on the
    // document rather than in the design's stylesheet, which only knows
    // about the element carrying data-theme.
    document.documentElement.style.background =
      name === "dark" ? "#0b0e13" : "#f6f7f9";
    setState({ theme: name });
  }

  function setTheme(name) {
    // Only a click writes the preference. Applying the current one at
    // start-up must not, or the tool records a choice nobody made.
    localStorage.setItem("me.theme", name);
    applyTheme(name);
  }

  /* ----------------------------------------------------------------- start */

  var pending = false;

  function selecting() {
    // Text being selected inside the app. Redrawing throws the selection
    // away, and a log that redraws once a second is a log nobody can copy
    // a line out of — which is exactly when someone most wants to.
    var sel = window.getSelection && window.getSelection();
    return !!(sel && !sel.isCollapsed && sel.rangeCount
              && where && where.contains(sel.anchorNode));
  }

  function draw() {
    if (!where || !screens) return;
    if (selecting()) {
      if (!pending) {
        pending = true;
        // Try again once the selection is let go, so nothing is lost —
        // only postponed.
        document.addEventListener("mouseup", function again() {
          document.removeEventListener("mouseup", again);
          pending = false;
          setTimeout(draw, 60);
        });
      }
      return;
    }
    window.DCX.render(where, screens, scope());
  }

  /* A file dragged onto the page arrives as its name and its CONTENTS —
   * never its path, which is the browser refusing on purpose. So the
   * contents are sent to the server, written down somewhere real, and that
   * path is used. Same outcome, different road, and the only road that
   * works when a file is somewhere awkward to navigate to. */
  function dropped(file) {
    var target = /\.(json|txt)$/i.test(file.name) ? "script"
               : (/\.(m4a|mp3|wav|aac|flac|ogg|mp4)$/i.test(file.name)
                  ? "audio" : "");
    if (!target) {
      setState({ scriptError: file.name + " — ye na script hai na voiceover. "
                              + ".json / .txt ya .m4a / .mp3 / .wav chahiye." });
      return;
    }
    var reader = new FileReader();
    reader.onload = function () {
      post("/api/upload", { name: file.name, data: reader.result })
        .then(function (saved) { accept(target, saved.path); })
        .catch(function (err) {
          setState({ scriptError: String(err.message || err) });
        });
    };
    reader.onerror = function () {
      setState({ scriptError: file.name + " padhi nahi ja saki" });
    };
    reader.readAsDataURL(file);
  }

  function acceptDrops() {
    ["dragenter", "dragover"].forEach(function (name) {
      document.addEventListener(name, function (ev) {
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "copy";
      });
    });
    document.addEventListener("drop", function (ev) {
      ev.preventDefault();
      var files = ev.dataTransfer && ev.dataTransfer.files;
      if (!files || !files.length) return;
      setState({ nav: "New Video" });
      for (var i = 0; i < Math.min(files.length, 2); i++) dropped(files[i]);
    });
  }

  function start() {
    where = document.getElementById("app");
    acceptDrops();
    fetch("/ui/design").then(function (r) { return r.text(); })
      .then(function (text) {
        var style = document.createElement("style");
        style.textContent = window.DCX.unwrap(text).styles;
        document.head.appendChild(style);
      }).catch(function () {
        // The design file is how the page gets its colours. Without it every
        // var(--x) is empty and the screen is unreadable — better to say so
        // than to show white text on white.
        document.body.innerHTML =
          '<pre style="font:13px monospace; padding:30px; color:#b00">'
          + "shared/design/Movie Editor.dc.html nahi mili.\n"
          + "Design ke bina page ke colours hi nahi hai." + "</pre>";
        throw new Error("no design");
      })
      .then(function () {
        return fetch("/ui/screens").then(function (r) { return r.text(); });
      })
      .then(function (text) {
        screens = text;
        applyTheme(state.theme);
        if (state.edFolder) loadFolder(state.edFolder);
        if (state.form.script) readScript(state.form.script);
        if (state.form.audio) readAudio(state.form.audio);
        return loadLibrary();
      })
      .catch(function () { /* already reported on the page */ });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
