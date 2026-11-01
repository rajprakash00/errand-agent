(() => {
  "use strict";

  const config = JSON.parse(document.getElementById("app-config").textContent || "{}");

  const el = {
    chatPanel: document.getElementById("chat-panel"),
    chatHead: document.getElementById("chat-head"),
    chatHeading: document.getElementById("chat-heading"),
    chatContext: document.getElementById("chat-context"),
    chatScroll: document.getElementById("chat-scroll"),
    emptyState: document.getElementById("empty-state"),
    messages: document.getElementById("messages"),
    turnActions: document.getElementById("turn-actions"),
    thinking: document.getElementById("thinking"),
    stateChip: document.getElementById("state-chip"),
    stateLabel: document.getElementById("state-label"),
    newErrand: document.getElementById("new-errand"),
    memoryButton: document.getElementById("memory-button"),
    memoryCount: document.getElementById("memory-count"),
    demoButton: document.getElementById("demo-button"),
    demoState: document.getElementById("demo-state"),
    demoDialog: document.getElementById("demo-dialog"),
    demoClose: document.getElementById("demo-close"),
    demoSubtitle: document.getElementById("demo-subtitle"),
    demoNote: document.getElementById("demo-note"),
    demoList: document.getElementById("demo-list"),
    memoryDialog: document.getElementById("memory-dialog"),
    memoryClose: document.getElementById("memory-close"),
    memorySubtitle: document.getElementById("memory-subtitle"),
    memoryScroll: document.getElementById("memory-scroll"),
    memoryEmpty: document.getElementById("memory-empty"),
    memoryList: document.getElementById("memory-list"),
    memoryNote: document.getElementById("memory-note"),
    memoryAreas: document.getElementById("memory-areas"),
    scenarioStrip: document.getElementById("scenario-strip"),
    composer: document.getElementById("composer"),
    composerField: document.getElementById("composer-field"),
    composerFinished: document.getElementById("composer-finished"),
    composerFinishedCopy: document.getElementById("composer-finished-copy"),
    input: document.getElementById("composer-input"),
    send: document.getElementById("send-button"),
    traceScroll: document.getElementById("trace-scroll"),
    traceEmpty: document.getElementById("trace-empty"),
    traceList: document.getElementById("trace-list"),
    traceRun: document.getElementById("trace-run"),
    traceDownload: document.getElementById("trace-download"),
    traceCount: document.getElementById("trace-count"),
    traceAnnouncer: document.getElementById("trace-announcer"),
    streamStatus: document.getElementById("stream-status"),
    streamLabel: document.getElementById("stream-label"),
    jumpLatest: document.getElementById("jump-latest"),
    tabChat: document.getElementById("tab-chat"),
    tabTrace: document.getElementById("tab-trace"),
    mobileTraceCount: document.getElementById("mobile-trace-count"),
    toast: document.getElementById("toast"),
  };

  const state = {
    runId: null,
    snapshot: null,
    busy: false,
    es: null,
    poll: null,
    seenSeq: new Set(),
    traceCount: 0,
    messageIds: new Set(),
    actionsSignature: null,
    countdown: null,
    toastTimer: null,
    mobileView: "chat",
    chatPinned: true,
    memory: null,
    memoryBusy: false,
    demo: null,
    demoBusy: false,
  };

  const timeFormatter = new Intl.DateTimeFormat("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZone: "Asia/Kolkata",
  });

  const slotFormatter = new Intl.DateTimeFormat("en-IN", {
    weekday: "short",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Kolkata",
  });

  const timeOnlyFormatter = new Intl.DateTimeFormat("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Kolkata",
  });

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  /* -- startup ----------------------------------------------------------- */

  function renderScenario() {
    const isDemo = config.scenario && config.scenario !== "happy";
    if (!isDemo && !config.startup_notice) {
      el.scenarioStrip.hidden = true;
      el.scenarioStrip.replaceChildren();
      return;
    }
    el.scenarioStrip.hidden = false;
    el.scenarioStrip.replaceChildren();

    const dot = document.createElement("span");
    dot.className = "scenario-dot";
    dot.setAttribute("aria-hidden", "true");

    const copy = document.createElement("p");
    copy.className = "scenario-copy";

    if (isDemo) {
      const lead = document.createElement("strong");
      lead.textContent = "Demo scenario. ";
      copy.appendChild(lead);
      copy.appendChild(
        document.createTextNode(
          config.scenario_summary || "A deterministic fault is available in this run."
        )
      );
    }

    if (config.startup_notice) {
      if (isDemo) copy.appendChild(document.createTextNode(" "));
      copy.appendChild(document.createTextNode(cleanNotice(config.startup_notice)));
    }

    el.scenarioStrip.append(dot, copy);
  }

  function bindEvents() {
    document.querySelectorAll("[data-prompt]").forEach((button) => {
      button.addEventListener("click", () => startErrand(button.dataset.prompt || ""));
    });

    el.composer.addEventListener("submit", (event) => {
      event.preventDefault();
      submitComposer();
    });

    el.input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        submitComposer();
      }
    });

    el.input.addEventListener("input", autoGrow);

    el.newErrand.addEventListener("click", newErrand);
    document.getElementById("new-errand-finished").addEventListener("click", newErrand);

    el.memoryButton.addEventListener("click", openMemory);
    el.memoryClose.addEventListener("click", closeMemory);
    el.memoryDialog.addEventListener("click", (event) => {
      if (event.target === el.memoryDialog) closeMemory();
    });
    el.memoryList.addEventListener("click", onMemoryClick);
    el.memoryList.addEventListener("submit", onMemorySubmit);

    el.demoButton.addEventListener("click", openDemo);
    el.demoClose.addEventListener("click", closeDemo);
    el.demoDialog.addEventListener("click", (event) => {
      if (event.target === el.demoDialog) closeDemo();
    });
    el.demoList.addEventListener("click", onDemoClick);

    el.tabChat.addEventListener("click", () => setMobileView("chat"));
    el.tabTrace.addEventListener("click", () => setMobileView("trace"));

    el.chatScroll.addEventListener(
      "scroll",
      () => {
        state.chatPinned = isNearBottom(el.chatScroll);
      },
      { passive: true }
    );

    el.traceScroll.addEventListener("scroll", () => {
      if (isNearBottom(el.traceScroll)) el.jumpLatest.hidden = true;
    });

    window.addEventListener("resize", () => {
      if (state.chatPinned) scrollChatToBottom(true);
      if (state.mobileView === "trace") scrollTraceToBottom(true);
    });

    el.jumpLatest.addEventListener("click", () => {
      scrollTraceToBottom();
      el.jumpLatest.hidden = true;
    });

    el.turnActions.addEventListener("click", (event) => {
      const target = event.target.closest("[data-action]");
      if (!target || target.disabled) return;
      const action = target.dataset.action;
      if (action === "choose" || action === "reviews" || action === "quick") {
        const value = target.dataset.value || "";
        if (value) sendMessage(value);
      } else if (action === "approve") {
        sendMessage("yes");
      } else if (action === "decline") {
        sendMessage("no");
      } else if (action === "new") {
        newErrand();
      }
    });
  }

  /* -- API --------------------------------------------------------------- */

  async function requestJSON(url, body) {
    const options = {
      method: body === undefined ? "GET" : "POST",
      cache: "no-store",
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    };
    let response;
    try {
      response = await fetch(url, options);
    } catch {
      throw new Error("The local server is not answering. Check that it is still running.");
    }
    let data = null;
    try {
      data = await response.json();
    } catch {
      data = null;
    }
    if (!response.ok) {
      const detail = data && data.detail;
      throw new Error(
        typeof detail === "string" ? detail : `The request failed (${response.status}).`
      );
    }
    return data;
  }

  async function startErrand(text) {
    const message = text.trim();
    if (!message || state.busy) return;
    setBusy(true);
    try {
      const data = await requestJSON("/api/errands", { message });
      resetRunChrome();
      state.runId = data.run_id;
      applySnapshot(data, { resetTrace: true });
      await syncTrace(data.run_id);
      if (!isDone(data)) connectStream(data);
      void loadMemory({ silent: true });
    } catch (error) {
      showToast(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function sendMessage(text) {
    const message = text.trim();
    if (!message || !state.runId || state.busy) return;
    const runId = state.runId;
    setBusy(true);
    try {
      const data = await requestJSON(`/api/errands/${runId}/messages`, { message });
      if (state.runId !== runId) return;
      applySnapshot(data);
      await syncTrace(runId);
      if (isDone(data)) {
        closeStream();
        updateStreamStatus("complete", "Complete");
      } else if (!state.es && !state.poll) {
        connectStream(data);
      }
      el.input.value = "";
      autoGrow();
      void loadMemory({ silent: true });
    } catch (error) {
      showToast(error.message);
      await refreshSnapshot();
    } finally {
      setBusy(false);
    }
  }

  async function refreshSnapshot() {
    if (!state.runId) return;
    try {
      const data = await requestJSON(`/api/errands/${state.runId}`);
      applySnapshot(data);
    } catch {
      // The toast already tells the user the request failed.
    }
  }

  /* -- memory panel ------------------------------------------------------ */

  async function loadMemory({ silent = false } = {}) {
    try {
      const data = await requestJSON("/api/memory");
      state.memory = data;
      renderMemory();
    } catch (error) {
      console.error("Memory panel failed to load", error);
      if (!silent) showToast(error.message);
    }
  }

  function openMemory() {
    if (!el.memoryDialog.open) el.memoryDialog.showModal();
    if (state.memory) {
      renderMemory();
    } else {
      el.memoryEmpty.hidden = true;
      el.memoryList.hidden = true;
      el.memoryNote.hidden = false;
      el.memoryNote.textContent = "Loading saved preferences…";
    }
    void loadMemory();
  }

  function closeMemory() {
    if (el.memoryDialog.open) el.memoryDialog.close();
  }

  function renderMemory() {
    const data = state.memory;
    if (!data) return;
    const count = Number(data.count) || 0;
    const pending = Number(data.pending_count) || 0;

    el.memoryCount.textContent = String(count);
    el.memoryCount.hidden = count === 0;
    el.memoryButton.classList.toggle("has-pending", pending > 0);
    el.memorySubtitle.textContent =
      count === 0
        ? `Saved for ${data.user_id}. Nothing saved yet.`
        : `${count} saved for ${data.user_id}${pending ? ` · ${pending} pending` : ""}.`;

    el.memoryEmpty.hidden = count !== 0;
    el.memoryList.hidden = count === 0;
    el.memoryList.replaceChildren(...(data.facts || []).map(memoryFactRow));

    el.memoryAreas.replaceChildren(
      ...(data.areas || []).map((area) => {
        const option = document.createElement("option");
        option.value = area;
        return option;
      })
    );

    el.memoryNote.hidden = pending === 0;
    el.memoryNote.textContent =
      pending > 0
        ? "Pending inferences are not used. Confirm one to apply it to your next search."
        : "";
  }

  function memoryBadge(label, className) {
    const badge = document.createElement("span");
    badge.className = `memory-badge ${className}`;
    badge.textContent = label;
    return badge;
  }

  function memoryActionButton(label, action, key, className = "button button-ghost button-sm") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.dataset.memoryAction = action;
    button.dataset.key = key;
    button.textContent = label;
    return button;
  }

  function memoryFactRow(fact) {
    const item = document.createElement("li");
    item.className = "memory-fact";
    item.dataset.key = fact.key;
    item.dataset.status = fact.status;
    if (fact.pending) item.classList.add("is-pending");

    const top = document.createElement("div");
    top.className = "memory-fact-top";

    const heading = document.createElement("h3");
    heading.className = "memory-fact-key";
    heading.textContent = fact.key_label;

    const badges = document.createElement("div");
    badges.className = "memory-badges";
    badges.appendChild(
      memoryBadge(fact.provenance_label, `memory-badge-${fact.provenance}`)
    );
    badges.appendChild(
      fact.pending
        ? memoryBadge("Pending", "memory-badge-pending")
        : memoryBadge("Active", "memory-badge-active")
    );
    top.append(heading, badges);

    const value = document.createElement("p");
    value.className = "memory-fact-value";
    value.textContent = fact.value_label;

    const hint = document.createElement("p");
    hint.className = "memory-fact-hint";
    hint.textContent = fact.key_hint;

    item.append(top, value, hint);

    if (fact.pending) {
      const note = document.createElement("p");
      note.className = "memory-fact-note";
      note.textContent =
        "Inferred from your message. This is not used in a search until you confirm it.";
      item.appendChild(note);
    }

    const actions = document.createElement("div");
    actions.className = "memory-actions";
    if (fact.pending) {
      const confirm = memoryActionButton(
        "Confirm",
        "confirm",
        fact.key,
        "button button-primary button-sm"
      );
      confirm.setAttribute("aria-label", `Confirm ${fact.key_label.toLowerCase()}`);
      actions.appendChild(confirm);
    }
    const correct = memoryActionButton("Correct", "edit", fact.key);
    correct.setAttribute("aria-label", `Correct ${fact.key_label.toLowerCase()}`);
    const forget = memoryActionButton(
      "Forget",
      "forget",
      fact.key,
      "button button-danger button-sm"
    );
    forget.setAttribute("aria-label", `Forget ${fact.key_label.toLowerCase()}`);
    actions.append(correct, forget);
    item.appendChild(actions);

    item.appendChild(memoryEditor(fact));
    item.appendChild(memoryForgetConfirm(fact));
    return item;
  }

  function memoryEditor(fact) {
    const form = document.createElement("form");
    form.className = "memory-edit";
    form.dataset.key = fact.key;
    form.hidden = true;

    const label = document.createElement("label");
    label.className = "memory-edit-label";
    label.htmlFor = `memory-edit-${fact.key}`;
    label.textContent =
      fact.key === "earliest_time"
        ? "New earliest time"
        : fact.key === "preferred_provider_id"
          ? "New provider"
          : "New area";

    let field;
    if (fact.key === "preferred_provider_id") {
      field = document.createElement("select");
      for (const provider of (state.memory && state.memory.providers) || []) {
        const option = document.createElement("option");
        option.value = provider.id;
        option.textContent = provider.name;
        if (provider.id === fact.value) option.selected = true;
        field.appendChild(option);
      }
    } else if (fact.key === "earliest_time") {
      field = document.createElement("input");
      field.type = "time";
      field.step = "60";
      field.value = fact.value || "";
    } else {
      field = document.createElement("input");
      field.type = "text";
      field.setAttribute("list", "memory-areas");
      field.maxLength = 120;
      field.autocomplete = "off";
      field.value = fact.value || "";
    }
    field.id = `memory-edit-${fact.key}`;
    field.name = "value";
    field.className = "memory-edit-input";
    field.required = true;

    const controls = document.createElement("div");
    controls.className = "memory-edit-controls";
    const save = document.createElement("button");
    save.type = "submit";
    save.className = "button button-primary button-sm";
    save.textContent = "Save";
    controls.append(save, memoryActionButton("Cancel", "cancel-edit", fact.key));

    form.append(label, field, controls);
    return form;
  }

  function memoryForgetConfirm(fact) {
    const block = document.createElement("div");
    block.className = "memory-forget";
    block.hidden = true;

    const copy = document.createElement("p");
    copy.className = "memory-forget-copy";
    copy.textContent =
      "Forget this fact? The value is deleted and never used in a new search.";

    const actions = document.createElement("div");
    actions.className = "memory-forget-actions";
    const forgetForever = memoryActionButton(
      "Forget permanently",
      "forget-confirm",
      fact.key,
      "button button-danger button-sm"
    );
    forgetForever.setAttribute(
      "aria-label",
      `Forget ${fact.key_label.toLowerCase()} permanently`
    );
    actions.append(
      forgetForever,
      memoryActionButton("Keep", "cancel-forget", fact.key)
    );

    block.append(copy, actions);
    return block;
  }

  function onMemoryClick(event) {
    const target = event.target.closest("[data-memory-action]");
    if (!target || state.memoryBusy) return;
    const action = target.dataset.memoryAction;
    const key = target.dataset.key;
    const item = target.closest(".memory-fact");
    if (!key || !item) return;

    if (action === "edit") {
      showMemoryEditor(item);
    } else if (action === "cancel-edit") {
      hideMemoryEditor(item);
    } else if (action === "forget") {
      showMemoryForget(item);
    } else if (action === "cancel-forget") {
      hideMemoryForget(item);
    } else if (action === "confirm") {
      void runMemoryAction(key, "confirm");
    } else if (action === "forget-confirm") {
      void runMemoryAction(key, "forget");
    }
  }

  function onMemorySubmit(event) {
    const form = event.target.closest(".memory-edit");
    if (!form) return;
    event.preventDefault();
    if (state.memoryBusy) return;
    const field = form.querySelector(".memory-edit-input");
    const value = field ? String(field.value).trim() : "";
    if (!value) {
      showToast("Enter a new value first.");
      if (field) field.focus();
      return;
    }
    void runMemoryCorrection(form.dataset.key, value);
  }

  function showMemoryEditor(item) {
    const actions = item.querySelector(".memory-actions");
    const form = item.querySelector(".memory-edit");
    const forget = item.querySelector(".memory-forget");
    if (!form) return;
    if (actions) actions.hidden = true;
    if (forget) forget.hidden = true;
    form.hidden = false;
    item.classList.add("is-editing");
    const field = form.querySelector(".memory-edit-input");
    if (field) {
      field.focus();
      if (field.select) field.select();
    }
  }

  function hideMemoryEditor(item) {
    const form = item.querySelector(".memory-edit");
    const actions = item.querySelector(".memory-actions");
    if (form) form.hidden = true;
    if (actions) actions.hidden = false;
    item.classList.remove("is-editing");
  }

  function showMemoryForget(item) {
    const actions = item.querySelector(".memory-actions");
    const form = item.querySelector(".memory-edit");
    const forget = item.querySelector(".memory-forget");
    if (!forget) return;
    if (actions) actions.hidden = true;
    if (form) form.hidden = true;
    forget.hidden = false;
    item.classList.add("is-forgetting");
    const confirm = forget.querySelector('[data-memory-action="forget-confirm"]');
    if (confirm) confirm.focus();
  }

  function hideMemoryForget(item) {
    const forget = item.querySelector(".memory-forget");
    const actions = item.querySelector(".memory-actions");
    if (forget) forget.hidden = true;
    if (actions) actions.hidden = false;
    item.classList.remove("is-forgetting");
  }

  async function runMemoryAction(key, action) {
    if (state.memoryBusy) return;
    setMemoryBusy(true);
    try {
      const data = await requestJSON(
        `/api/memory/${encodeURIComponent(key)}/${action}`,
        { run_id: memoryRunId() }
      );
      state.memory = data.memory;
      renderMemory();
      const label = data.fact ? data.fact.key_label : "Preference";
      showToast(
        action === "confirm"
          ? `${label} confirmed. It will be used in your next search.`
          : `${label} forgotten. It will not be used again.`
      );
      afterMemoryChange();
    } catch (error) {
      showToast(error.message);
    } finally {
      setMemoryBusy(false);
    }
  }

  async function runMemoryCorrection(key, value) {
    if (state.memoryBusy) return;
    setMemoryBusy(true);
    try {
      const data = await requestJSON(
        `/api/memory/${encodeURIComponent(key)}/correct`,
        { value, run_id: memoryRunId() }
      );
      state.memory = data.memory;
      renderMemory();
      const label = data.fact ? data.fact.key_label : "Preference";
      const shown = data.fact ? data.fact.value_label : value;
      showToast(`${label} corrected to ${shown}. The next search will use it.`);
      afterMemoryChange();
    } catch (error) {
      showToast(error.message);
    } finally {
      setMemoryBusy(false);
    }
  }

  function setMemoryBusy(busy) {
    state.memoryBusy = busy;
    el.memoryDialog.setAttribute("aria-busy", String(busy));
    el.memoryList.querySelectorAll("button, input, select").forEach((node) => {
      node.disabled = busy;
    });
  }

  function memoryRunId() {
    if (!state.runId || !state.snapshot || isDone(state.snapshot)) return null;
    return state.runId;
  }

  function afterMemoryChange() {
    if (state.runId && state.snapshot && !isDone(state.snapshot)) {
      void syncTrace(state.runId);
    }
  }

  /* -- demo controls ----------------------------------------------------- */

  async function loadDemo({ silent = false } = {}) {
    try {
      const data = await requestJSON("/api/demo");
      state.demo = data;
      syncDemoConfig(data);
      renderDemo();
    } catch (error) {
      if (!silent) showToast(error.message);
    }
  }

  function syncDemoConfig(data) {
    config.scenario = data.scenario;
    config.scenario_summary = data.summary;
    config.startup_notice = data.notice || null;
    el.demoState.hidden = !data.active;
    el.demoButton.classList.toggle("has-pending", Boolean(data.active));
    renderScenario();
  }

  function openDemo() {
    if (!el.demoDialog.open) el.demoDialog.showModal();
    if (state.demo) renderDemo();
    void loadDemo();
  }

  function closeDemo() {
    if (el.demoDialog.open) el.demoDialog.close();
  }

  function renderDemo() {
    const data = state.demo;
    el.demoSubtitle.textContent =
      data && data.active
        ? `Armed: ${data.label}. ${data.summary}`
        : "Arm a deterministic fault, then run the errand.";
    el.demoNote.hidden = !(data && data.notice);
    el.demoNote.textContent = data && data.notice ? cleanNotice(data.notice) : "";
    if (!data) return;
    el.demoList.replaceChildren(
      ...(data.scenarios || []).map((scenario) => demoRow(scenario, data.scenario))
    );
  }

  function demoRow(scenario, activeKey) {
    const item = document.createElement("li");
    item.className = "demo-item";
    const active = scenario.key === activeKey;
    if (active) item.classList.add("is-active");

    const head = document.createElement("div");
    head.className = "demo-item-head";
    const title = document.createElement("h3");
    title.className = "demo-item-title";
    title.textContent = scenario.label;
    head.appendChild(title);
    if (active) {
      const badge = document.createElement("span");
      badge.className = "memory-badge memory-badge-active";
      badge.textContent = "Armed";
      head.appendChild(badge);
    }

    const summary = document.createElement("p");
    summary.className = "demo-item-summary";
    summary.textContent = scenario.summary;

    const hint = document.createElement("p");
    hint.className = "demo-item-hint";
    hint.textContent = scenario.hint;

    const actions = document.createElement("div");
    actions.className = "demo-item-actions";

    const arm = document.createElement("button");
    arm.type = "button";
    arm.className = "button button-ghost button-sm";
    arm.dataset.demoAction = "arm";
    arm.dataset.demoKey = scenario.key;
    arm.textContent = active ? "Re-arm" : "Arm";
    arm.disabled = state.demoBusy;

    const start = document.createElement("button");
    start.type = "button";
    start.className = "button button-primary button-sm";
    start.dataset.demoAction = "start";
    start.dataset.demoKey = scenario.key;
    start.textContent = "Arm & start";
    start.disabled = state.demoBusy;

    actions.append(arm, start);
    item.append(head, summary, hint, actions);
    return item;
  }

  function onDemoClick(event) {
    const target = event.target.closest("[data-demo-action]");
    if (!target || target.disabled || state.demoBusy) return;
    const key = target.dataset.demoKey;
    if (!key) return;
    const start = target.dataset.demoAction === "start";
    void armScenario(key, { start });
  }

  async function armScenario(key, { start = false } = {}) {
    if (state.demoBusy) return;
    state.demoBusy = true;
    renderDemo();
    try {
      const data = await requestJSON("/api/demo/scenario", { scenario: key });
      const spec = (data.scenarios || []).find((item) => item.key === key);
      state.demo = data;
      syncDemoConfig(data);
      renderDemo();
      showToast(
        data.active
          ? `${data.label} armed. ${data.summary}`
          : "Happy path armed. No fault is injected."
      );
      if (start && spec) {
        closeDemo();
        newErrand();
        await startErrand(spec.prompt);
      }
    } catch (error) {
      showToast(error.message);
    } finally {
      state.demoBusy = false;
      renderDemo();
    }
  }

  async function syncTrace(runId) {
    if (!runId) return;
    try {
      const data = await requestJSON(`/api/errands/${runId}/trace`);
      if (state.runId !== runId) return;
      for (const event of data.events || []) {
        appendTraceEvent(event, { animate: false });
      }
      if (state.snapshot && isDone(state.snapshot)) {
        closeStream();
        updateStreamStatus("complete", "Complete");
      }
    } catch {
      // The live stream is the primary path; a catch-up failure is silent.
    }
  }

  /* -- snapshot rendering ------------------------------------------------ */

  function applySnapshot(data, { resetTrace = false } = {}) {
    state.snapshot = data;
    state.runId = data.run_id;

    el.emptyState.hidden = true;
    el.newErrand.hidden = false;
    el.chatHead.hidden = false;
    el.chatHeading.textContent = errandTitle(data);
    el.chatContext.textContent = errandContext(data);
    el.stateChip.dataset.state = data.state;
    el.stateLabel.textContent = data.state_label;

    el.traceRun.textContent = data.run_id;
    el.traceDownload.href = data.trace.download_url;
    el.traceDownload.hidden = false;

    if (resetTrace) resetTracePane();

    renderMessages(data);
    renderActions(data);
    updateComposer(data);
    updateMobileTraceCount();

    const last = data.messages && data.messages[data.messages.length - 1];
    if (
      last &&
      last.role === "assistant" &&
      /^(?:Untrusted reviews|Which provider's reviews|Please reply with the number)/i.test(
        last.content
      )
    ) {
      window.requestAnimationFrame(() => scrollLastMessageIntoView(true));
    }

    if (isDone(data)) {
      closeStream();
      updateStreamStatus("complete", "Complete");
    }
  }

  function renderMessages(data) {
    const nearBottom = state.chatPinned || isNearBottom(el.chatScroll);
    for (const message of data.messages || []) {
      if (state.messageIds.has(message.id)) continue;
      state.messageIds.add(message.id);
      el.messages.appendChild(
        message.role === "user" ? userMessage(message) : assistantMessage(message)
      );
    }
    if (nearBottom) scrollChatToBottom();
  }

  function userMessage(message) {
    const node = document.createElement("article");
    node.className = "message message-user";
    const body = document.createElement("p");
    body.textContent = message.content;
    node.appendChild(body);
    return node;
  }

  function assistantMessage(message) {
    const node = document.createElement("article");
    node.className = "message message-assistant";

    const mark = document.createElement("span");
    mark.className = "agent-mark";
    mark.setAttribute("aria-hidden", "true");

    node.append(mark, messageBody(message.content));
    return node;
  }

  function messageBody(text) {
    const body = document.createElement("div");
    body.className = "message-body";

    const lines = String(text)
      .replace(/\r\n/g, "\n")
      .split("\n")
      .map((line) =>
        line
          .replace(/\s*Here is the (?:new )?exact approval:\s*/i, " ")
          .trimEnd()
      )
      .filter((line) => !/^Trace(?: file)?:\s/i.test(line))
      .filter((line) => !isRedundantLine(line));

    let paragraph = null;
    let quote = null;

    const flushParagraph = () => {
      if (paragraph) {
        body.appendChild(paragraph);
        paragraph = null;
      }
    };

    const flushQuote = () => {
      if (quote) {
        body.appendChild(quote);
        quote = null;
      }
    };

    for (const rawLine of lines) {
      const line = rawLine.trimEnd();
      if (line.startsWith(">")) {
        flushParagraph();
        if (!quote) {
          quote = document.createElement("div");
          quote.className = "message-quote";
        }
        const quoteLine = line.replace(/^>\s?/, "");
        const paragraphNode = document.createElement("p");
        paragraphNode.className = quoteLine.startsWith("—")
          ? "quote-credit"
          : "quote-text";
        paragraphNode.textContent = quoteLine;
        quote.appendChild(paragraphNode);
        continue;
      }

      flushQuote();

      if (!line) {
        flushParagraph();
        continue;
      }

      if (!paragraph) paragraph = document.createElement("p");
      if (paragraph.childNodes.length) paragraph.appendChild(document.createElement("br"));
      paragraph.appendChild(document.createTextNode(line));
    }

    flushParagraph();
    flushQuote();

    if (!body.childNodes.length) {
      const empty = document.createElement("p");
      empty.textContent = "…";
      body.appendChild(empty);
    }

    return body;
  }

  function isRedundantLine(line) {
    if (!line) return false;
    if (/^Reply with the number of (?:the|a) provider/i.test(line)) return true;
    if (/^\d+\.\s.+—\s₹/.test(line)) return true;
    if (
      /^(?:Action|Provider|Slot|Price|Hold expires|Approval expires|Refund|Booking ID):\s/.test(
        line
      )
    ) {
      return true;
    }
    if (/^Reply "yes" to approve or "no" to decline\.$/i.test(line)) return true;
    return false;
  }

  function renderActions(data) {
    const signature = JSON.stringify({
      state: data.state,
      action: data.action,
      options: data.options.map((option) => [
        option.provider_id,
        option.price_inr,
        option.slots.map((slot) => slot.iso).join(","),
      ]),
      approval: data.approval ? data.approval.proposal_id : null,
      booking: data.booking ? data.booking.id : null,
      quick: data.quick_replies,
      notice: data.notice,
      terminal: data.terminal,
    });
    if (signature === state.actionsSignature) return;
    clearCountdown();
    state.actionsSignature = signature;
    el.turnActions.replaceChildren();

    if (data.notice) el.turnActions.appendChild(systemNote(data.notice));

    if (data.approval) {
      el.turnActions.appendChild(approvalCard(data));
    } else if (data.options && data.options.length) {
      el.turnActions.appendChild(optionsBlock(data));
    } else if (isDone(data) || data.state === "needs_human") {
      el.turnActions.appendChild(receiptBlock(data));
    } else if (data.quick_replies && data.quick_replies.length) {
      el.turnActions.appendChild(quickReplies(data.quick_replies));
    }

    if (el.turnActions.childNodes.length) {
      if (data.options && data.options.length) {
        state.chatPinned = false;
        el.turnActions.scrollIntoView({
          block: "start",
          behavior: reducedMotion.matches ? "auto" : "smooth",
        });
      } else if (data.approval || data.state === "awaiting_approval") {
        scrollChatToBottom();
      } else if (isNearBottom(el.chatScroll)) {
        scrollChatToBottom();
      }
    }
  }

  function systemNote(text) {
    const note = document.createElement("div");
    note.className = "system-note";
    const dot = document.createElement("span");
    dot.className = "system-note-dot";
    dot.setAttribute("aria-hidden", "true");
    const copy = document.createElement("p");
    copy.className = "system-note-copy";
    copy.textContent = cleanNotice(text);
    note.append(dot, copy);
    return note;
  }

  function cleanNotice(text) {
    return String(text || "").replace(/^\[(?:scenario|demo)\]\s*/i, "");
  }

  function optionsBlock(data) {
    const block = document.createElement("section");
    block.className = "option-block";
    block.setAttribute("aria-label", "Provider options");

    const heading = document.createElement("h3");
    heading.className = "actions-heading option-heading";
    heading.textContent = "Choose a provider";

    const note = document.createElement("p");
    note.className = "actions-note option-note";
    note.textContent =
      "These are search quotes. The exact held price appears on the approval card before anything is booked.";

    const list = document.createElement("div");
    list.className = "option-list";
    for (const option of data.options) list.appendChild(optionRow(option));

    block.append(heading, note, list);
    return block;
  }

  function optionRow(option) {
    const row = document.createElement("article");
    row.className = "option";

    const main = document.createElement("div");
    main.className = "option-main";

    const name = document.createElement("h3");
    name.className = "option-name";
    name.textContent = option.name;

    const meta = document.createElement("p");
    meta.className = "option-meta";

    const price = document.createElement("span");
    price.className = "option-price";
    price.textContent = option.price_label;

    const rating = document.createElement("span");
    rating.className = "option-rating";
    rating.innerHTML =
      '<svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor" aria-hidden="true"><path d="m6 1 1.5 3.1 3.4.5-2.45 2.4.58 3.4L6 8.8 2.97 10.4l.58-3.4L1.1 4.6l3.4-.5Z"/></svg>';
    rating.appendChild(document.createTextNode(option.rating_label));

    meta.append(price, rating);

    const slots = document.createElement("p");
    slots.className = "option-slots";
    const shown = option.slots.slice(0, 3).map((slot) => slot.label);
    const extra = option.slots.length - shown.length;
    slots.textContent =
      shown.length > 0
        ? `Next: ${shown.join(" · ")}${extra > 0 ? ` · +${extra} more` : ""}`
        : "No free slots in this window.";

    main.append(name, meta, slots);

    if (option.attributes.length) {
      const attrs = document.createElement("div");
      attrs.className = "option-attrs";
      for (const attribute of option.attributes) {
        const chip = document.createElement("span");
        chip.className = "attr";
        chip.textContent = attribute;
        attrs.appendChild(chip);
      }
      main.appendChild(attrs);
    }

    const actions = document.createElement("div");
    actions.className = "option-actions";

    const choose = document.createElement("button");
    choose.type = "button";
    choose.className = "button button-primary button-sm";
    choose.dataset.action = "choose";
    choose.dataset.value = String(option.index);
    choose.textContent = "Choose";
    choose.setAttribute("aria-label", `Choose ${option.name}`);

    const reviews = document.createElement("button");
    reviews.type = "button";
    reviews.className = "button button-ghost button-sm";
    reviews.dataset.action = "reviews";
    reviews.dataset.value = option.review_prompt;
    reviews.textContent = "Reviews";
    reviews.setAttribute("aria-label", `Show reviews for ${option.name}`);

    actions.append(choose, reviews);
    row.append(main, actions);
    return row;
  }

  function approvalCard(data) {
    const approval = data.approval;
    const card = document.createElement("section");
    card.className = "approval";
    card.dataset.proposalId = approval.proposal_id;
    card.setAttribute(
      "aria-label",
      `Approval needed: ${approval.action_label} with ${approval.provider_name}`
    );

    const strip = document.createElement("div");
    strip.className = "approval-strip";
    const stripLabel = document.createElement("span");
    stripLabel.className = "approval-strip-label";
    stripLabel.setAttribute("aria-live", "polite");
    stripLabel.textContent = "Approval needed";
    const countdown = document.createElement("span");
    countdown.className = "approval-countdown";
    countdown.setAttribute("aria-hidden", "true");
    strip.append(stripLabel, countdown);

    const body = document.createElement("div");
    body.className = "approval-body";

    const provider = document.createElement("p");
    provider.className = "approval-provider";
    provider.textContent = approval.provider_name;

    const sub = document.createElement("p");
    sub.className = "approval-sub";
    sub.textContent = "Review the exact action before the agent runs it.";

    const facts = document.createElement("dl");
    facts.className = "approval-facts";

    facts.appendChild(fact("Action", approval.action_label));
    facts.appendChild(fact("Price", approval.price_label, "fact-price"));
    facts.appendChild(fact("Slot", approval.slot_label, "fact-wide"));
    facts.appendChild(fact("Expires", approval.expires_label, "fact-wide"));

    const note = document.createElement("p");
    note.className = "approval-note";
    note.textContent =
      "The approval binds this provider, slot, and price. If any of them changes, the old approval is void and the agent asks again.";

    const ref = document.createElement("p");
    ref.className = "approval-ref";
    ref.textContent = `Approval ref ${approval.binding_short}`;
    ref.title = approval.binding_hash;

    const actions = document.createElement("div");
    actions.className = "approval-actions";

    const approve = document.createElement("button");
    approve.type = "button";
    approve.className = "button button-primary";
    approve.dataset.action = "approve";
    approve.textContent =
      approval.action === "cancel"
        ? "Approve cancellation"
        : approval.action === "reschedule"
          ? "Approve reschedule"
          : "Approve and book";
    approve.dataset.defaultLabel = approve.textContent;

    const decline = document.createElement("button");
    decline.type = "button";
    decline.className = "button button-ghost";
    decline.dataset.action = "decline";
    decline.textContent = "Decline";

    actions.append(approve, decline);
    body.append(provider, sub, facts, note, ref, actions);
    card.append(strip, body);

    startCountdown(approval.expires_at, countdown, card, stripLabel);
    return card;
  }

  function fact(label, value, extraClass = "") {
    const wrapper = document.createElement("div");
    wrapper.className = `fact ${extraClass}`.trim();
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = value;
    wrapper.append(term, description);
    return wrapper;
  }

  function startCountdown(iso, node, card, label) {
    clearCountdown();
    const target = new Date(iso).getTime();
    const tick = () => {
      const remaining = target - Date.now();
      const approve = card.querySelector('[data-action="approve"]');
      if (remaining <= 0) {
        node.textContent = "Expired";
        card.classList.add("is-expired");
        label.textContent = "Approval expired";
        if (approve) approve.textContent = "Request a fresh hold";
        clearCountdown();
        return;
      }
      card.classList.remove("is-expired");
      label.textContent = "Approval needed";
      if (approve && approve.dataset.defaultLabel) {
        approve.textContent = approve.dataset.defaultLabel;
      }
      const totalSeconds = Math.ceil(remaining / 1000);
      const minutes = Math.floor(totalSeconds / 60);
      const seconds = totalSeconds % 60;
      node.textContent = `${minutes}:${String(seconds).padStart(2, "0")} left`;
    };
    tick();
    state.countdown = window.setInterval(tick, 1000);
  }

  function clearCountdown() {
    if (state.countdown) {
      window.clearInterval(state.countdown);
      state.countdown = null;
    }
  }

  function quickReplies(replies) {
    const row = document.createElement("div");
    row.className = "quick-replies";
    row.setAttribute("aria-label", "Quick replies");
    for (const reply of replies) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "quick-reply";
      button.dataset.action = "quick";
      button.dataset.value = reply;
      button.textContent = reply;
      row.appendChild(button);
    }
    return row;
  }

  function receiptBlock(data) {
    const booking = data.booking;
    const done = isDone(data);
    const cancelled = data.state === "cancelled";
    const booked = data.state === "completed" && data.action === "book";
    const moved = data.state === "completed" && data.action === "reschedule";

    const card = document.createElement("section");
    card.className = "receipt";
    if (booked || moved) card.classList.add("receipt-complete");
    else if (cancelled) card.classList.add("receipt-cancelled");
    else card.classList.add("receipt-ended");

    const top = document.createElement("div");
    top.className = "receipt-top";

    const icon = document.createElement("span");
    icon.className = "receipt-icon";
    if (!booked && !moved) icon.classList.add("receipt-icon-neutral");
    icon.innerHTML =
      booked || moved
        ? '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m3.5 8.2 3 3 6-6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'
        : '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4 8h8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';

    const title = document.createElement("h3");
    title.className = "receipt-title";
    if (booked) title.textContent = "Booked and verified";
    else if (moved) title.textContent = "Booking moved and verified";
    else if (cancelled) title.textContent = "Booking cancelled and verified";
    else if (data.state === "limit_reached") title.textContent = "Stopped at a safety limit";
    else if (data.state === "needs_human") title.textContent = "This errand is parked";
    else title.textContent = "The run stopped safely";

    top.append(icon, title);
    card.appendChild(top);

    if (booking && (booked || moved || cancelled)) {
      const body = document.createElement("div");
      body.className = "receipt-body";
      body.appendChild(receiptRow("Provider", booking.provider_name));
      body.appendChild(receiptRow("Slot", booking.slot_label));
      body.appendChild(receiptRow("Price", booking.price_label));
      if (cancelled) body.appendChild(receiptRow("Refund", "₹0, full refund"));
      body.appendChild(receiptRow("Booking ID", booking.id));
      card.appendChild(body);
    } else {
      const copy = document.createElement("p");
      copy.className = "receipt-copy";
      copy.textContent = done
        ? "No success was recorded for this run. The exact reason and the final trace event are in the transcript and the trace pane."
        : "No booking is waiting on this errand. Start a new errand when you are ready.";
      card.appendChild(copy);
    }

    const actions = document.createElement("div");
    actions.className = "receipt-actions";

    if (state.runId) {
      const download = document.createElement("a");
      download.className = "button button-quiet button-sm";
      download.href = `/api/errands/${state.runId}/trace/download`;
      download.download = `${state.runId}.jsonl`;
      download.textContent = "Download trace";
      actions.appendChild(download);
    }

    const restart = document.createElement("button");
    restart.type = "button";
    restart.className = booked || moved || cancelled ? "button button-ink button-sm" : "button button-primary button-sm";
    restart.dataset.action = "new";
    restart.textContent = "New errand";
    actions.appendChild(restart);

    card.appendChild(actions);
    return card;
  }

  function receiptRow(label, value) {
    const row = document.createElement("div");
    row.className = "receipt-row";
    const name = document.createElement("span");
    name.textContent = label;
    const content = document.createElement("strong");
    content.textContent = value;
    row.append(name, content);
    return row;
  }

  function updateComposer(data) {
    const done = isDone(data);
    el.composer.classList.toggle("is-finished", done);
    el.composerFinished.hidden = !done;
    el.composerFinishedCopy.textContent =
      data.state === "needs_human"
        ? "This errand is parked."
        : "This errand is finished.";
    el.input.disabled = state.busy || done;
    el.send.disabled = state.busy || done;
    if (!done) {
      const placeholders = {
        collecting: "Reply with the missing detail…",
        options_ready: "Pick a provider above, or type its number…",
        awaiting_approval: "Approve with the card, or reply yes or no…",
      };
      el.input.placeholder =
        placeholders[data.state] || "Message the errand agent…";
    }
  }

  function isDone(data) {
    return Boolean(data && (data.terminal || data.state === "needs_human"));
  }

  function errandTitle(data) {
    if (data.action === "cancel") return "Cancel a booking";
    if (data.action === "reschedule") return "Move a booking";
    const service = data.slots && data.slots.service;
    const area = data.slots && data.slots.area;
    if (service && area) return `${titleCase(service)} in ${area}`;
    if (service) return titleCase(service);
    if (area) return `Errand in ${area}`;
    return "New errand";
  }

  function errandContext(data) {
    if (data.options && data.options.length) {
      return [data.options.length + " providers", data.slots && data.slots.time_label]
        .filter(Boolean)
        .join(" · ");
    }
    if (data.state === "collecting") {
      const labels = { service: "service", area: "area", time_window: "time window" };
      const missing = data.missing_slot ? labels[data.missing_slot] : null;
      return missing ? `Add the ${missing} to continue` : "Collecting the missing details";
    }
    if (data.state === "awaiting_approval") {
      const verb =
        data.action === "cancel"
          ? "cancelling"
          : data.action === "reschedule"
            ? "moving the booking"
            : "booking";
      return `One approval left before ${verb}`;
    }
    if (data.state === "completed") return "Booked and verified";
    if (data.state === "cancelled") return "Cancelled and verified";
    if (data.state === "needs_human") return "Needs attention";
    if (data.state === "limit_reached") return "Stopped at a safety limit";
    if (data.state === "failed") return "Run ended safely";
    return data.state_label;
  }

  function titleCase(value) {
    const text = String(value || "");
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  /* -- busy and scroll --------------------------------------------------- */

  function setBusy(busy) {
    state.busy = busy;
    el.composer.classList.toggle("is-busy", busy);
    el.chatPanel.setAttribute("aria-busy", String(busy));
    el.thinking.hidden = !busy;
    const done = state.snapshot ? isDone(state.snapshot) : false;
    el.input.disabled = busy || done;
    el.send.disabled = busy || done;
    el.turnActions.querySelectorAll("button").forEach((button) => {
      button.disabled = busy;
    });
    document.querySelectorAll(".prompt").forEach((button) => {
      button.disabled = busy;
    });
    if (busy) {
      state.chatPinned = true;
      scrollChatToBottom(true);
    }
  }

  function autoGrow() {
    el.input.style.height = "auto";
    el.input.style.height = `${Math.min(el.input.scrollHeight, 120)}px`;
  }

  function submitComposer() {
    const text = el.input.value.trim();
    if (!text || state.busy) return;
    if (isDone(state.snapshot)) return;
    if (!state.runId) startErrand(text);
    else sendMessage(text);
  }

  function isNearBottom(node, threshold = 100) {
    return node.scrollHeight - node.scrollTop - node.clientHeight < threshold;
  }

  function scrollLastMessageIntoView(instant = false) {
    const last = el.messages.lastElementChild;
    if (!last) return;
    last.scrollIntoView({
      block: "end",
      behavior: instant || reducedMotion.matches ? "auto" : "smooth",
    });
    state.chatPinned = true;
  }

  function scrollChatToBottom(instant = false) {
    const behavior = instant || reducedMotion.matches ? "auto" : "smooth";
    el.chatScroll.scrollTo({ top: el.chatScroll.scrollHeight, behavior });
    state.chatPinned = true;
  }

  function scrollTraceToBottom(instant = false) {
    const behavior = instant || reducedMotion.matches ? "auto" : "smooth";
    el.traceScroll.scrollTo({ top: el.traceScroll.scrollHeight, behavior });
  }

  /* -- trace ------------------------------------------------------------- */

  function resetTracePane() {
    el.traceList.replaceChildren();
    el.traceEmpty.hidden = false;
    el.traceCount.textContent = "0 events";
    state.seenSeq.clear();
    state.traceCount = 0;
    el.jumpLatest.hidden = true;
    updateMobileTraceCount();
  }

  function resetRunChrome() {
    closeStream();
    clearCountdown();
    if (state.poll) {
      window.clearInterval(state.poll);
      state.poll = null;
    }
    state.messageIds.clear();
    state.actionsSignature = null;
    el.messages.replaceChildren();
    el.turnActions.replaceChildren();
    el.emptyState.hidden = true;
    el.chatHead.hidden = false;
    el.newErrand.hidden = false;
    el.composer.classList.remove("is-finished", "is-busy");
    el.composerFinished.hidden = true;
    el.input.disabled = false;
    el.send.disabled = false;
    el.input.value = "";
    autoGrow();
    updateMobileTraceCount();
  }

  function newErrand() {
    if (state.busy) return;
    closeStream();
    if (state.poll) {
      window.clearInterval(state.poll);
      state.poll = null;
    }
    clearCountdown();
    state.runId = null;
    state.snapshot = null;
    state.messageIds.clear();
    state.seenSeq.clear();
    state.traceCount = 0;
    state.actionsSignature = null;
    state.chatPinned = true;

    el.messages.replaceChildren();
    el.turnActions.replaceChildren();
    el.emptyState.hidden = false;
    el.chatHead.hidden = true;
    el.newErrand.hidden = true;
    el.composer.classList.remove("is-finished", "is-busy");
    el.composerFinished.hidden = true;
    el.input.disabled = false;
    el.send.disabled = false;
    el.input.value = "";
    el.input.placeholder = "Describe the job, area, or reply here…";
    autoGrow();

    resetTracePane();
    el.traceRun.textContent = "No run yet";
    el.traceDownload.hidden = true;
    el.traceDownload.removeAttribute("href");
    updateStreamStatus("idle", "Waiting");
    setMobileView("chat");
    el.input.focus();
  }

  function connectStream(data) {
    if (!data || !data.trace) return;
    closeStream();

    if (!("EventSource" in window)) {
      updateStreamStatus("live", "Polling");
      state.poll = window.setInterval(async () => {
        await syncTrace(state.runId);
        if (state.snapshot && isDone(state.snapshot)) {
          window.clearInterval(state.poll);
          state.poll = null;
          updateStreamStatus("complete", "Complete");
        }
      }, 1500);
      return;
    }

    if (isDone(data)) {
      updateStreamStatus("complete", "Complete");
      return;
    }

    const runId = data.run_id;
    updateStreamStatus("reconnecting", "Connecting");
    const source = new EventSource(data.trace.events_url);
    state.es = source;

    source.onopen = () => {
      if (state.runId === runId) updateStreamStatus("live", "Live");
    };

    source.addEventListener("trace", (message) => {
      if (state.runId !== runId) return;
      let event;
      try {
        event = JSON.parse(message.data);
      } catch {
        return;
      }
      appendTraceEvent(event);
      if (event.type === "run_finished") {
        closeStream();
        updateStreamStatus("complete", "Complete");
      }
    });

    source.onerror = () => {
      if (state.runId !== runId) return;
      if (state.snapshot && isDone(state.snapshot)) {
        closeStream();
        updateStreamStatus("complete", "Complete");
      } else {
        updateStreamStatus("reconnecting", "Reconnecting");
      }
    };
  }

  function closeStream() {
    if (state.es) {
      state.es.close();
      state.es = null;
    }
  }

  function appendTraceEvent(event, { animate = true } = {}) {
    const seq = Number(event.seq);
    if (!Number.isFinite(seq) || state.seenSeq.has(seq)) return;
    state.seenSeq.add(seq);

    const nearBottom = isNearBottom(el.traceScroll);
    const [kindLabel, kind] = kindFor(event);
    const item = document.createElement("li");
    item.className = "trace-event";
    if (!animate) item.classList.add("no-animation");
    item.dataset.kind = kind;

    const top = document.createElement("div");
    top.className = "trace-event-top";

    const sequence = document.createElement("span");
    sequence.className = "trace-seq";
    sequence.textContent = String(seq).padStart(2, "0");

    const time = document.createElement("span");
    time.className = "trace-time";
    time.textContent = formatTime(event.ts);

    const label = document.createElement("span");
    label.className = "trace-kind";
    label.textContent = kindLabel;

    top.append(sequence, time, label);

    const summary = document.createElement("p");
    summary.className = "trace-summary";
    summary.textContent = eventSummary(event);

    const details = document.createElement("details");
    details.className = "trace-details";
    const detailsLabel = document.createElement("summary");
    detailsLabel.textContent = "Raw event";
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(event, null, 2);
    details.append(detailsLabel, pre);

    item.append(top, summary, details);
    el.traceList.appendChild(item);

    state.traceCount += 1;
    el.traceCount.textContent = `${state.traceCount} event${state.traceCount === 1 ? "" : "s"}`;
    el.traceEmpty.hidden = true;
    updateMobileTraceCount();

    if (nearBottom) {
      scrollTraceToBottom(true);
      el.jumpLatest.hidden = true;
    } else {
      el.jumpLatest.hidden = false;
    }

    if (animate) {
      el.traceAnnouncer.textContent = `${kindLabel}: ${summary.textContent}`;
    }
  }

  function kindFor(event) {
    const type = event.type;
    if (type === "state_transition") return ["State", "state"];
    if (type === "tool_call") return ["Tool", "tool"];
    if (type === "model_call") return ["Model", "model"];
    if (type === "approval_requested") return ["Approval", "approval"];
    if (type === "approval_decided") return ["Decision", "decision"];
    if (type === "reconciliation") return ["Reconcile", "reconcile"];
    if (type === "run_finished") {
      const failed = event.payload && ["failed", "limit_reached"].includes(event.payload.state);
      return ["Finished", failed ? "error" : "finished"];
    }
    if (type === "run_started") return ["Run", "run"];
    if (type === "user_message") return ["Message", "message"];
    if (type === "reviews_shown") return ["Reviews", "reviews"];
    if (type === "budget") return ["Limit", "error"];
    if (String(type).startsWith("memory")) return ["Memory", "memory"];
    return [humanize(type), "other"];
  }

  function eventSummary(event) {
    const payload = event.payload || {};
    switch (event.type) {
      case "run_started":
        return `Run started for ${payload.user_id || "the user"} · ${payload.action || "book"}`;
      case "user_message":
        return quote(payload.text);
      case "state_transition":
        return [payload.from, "→", payload.to, payload.reason ? `· ${payload.reason}` : ""]
          .filter(Boolean)
          .join(" ");
      case "model_call": {
        const parts = [payload.role || "model"];
        if (payload.kind) parts.push(payload.kind);
        if (payload.model) parts.push(payload.model);
        if (payload.excerpt) parts.push(quote(payload.excerpt, 72));
        return parts.filter(Boolean).join(" · ");
      }
      case "tool_call": {
        const name = payload.name || "tool";
        if (payload.error) {
          const attempt = payload.attempt > 1 ? ` · attempt ${payload.attempt}` : "";
          return `${name} · ${payload.error}${attempt}`;
        }
        const result = toolResult(payload);
        const latency = event.latency_ms != null ? `${event.latency_ms} ms` : "";
        return [name, result, latency].filter(Boolean).join(" · ");
      }
      case "approval_requested":
        return [
          payload.provider_name,
          formatPrice(payload.quoted_price_inr),
          slotRange(payload.slot_start, payload.slot_end),
        ]
          .filter(Boolean)
          .join(" · ");
      case "approval_decided":
        return [
          payload.decision || "decision",
          payload.reason,
          payload.binding_hash ? shortHash(payload.binding_hash) : "",
        ]
          .filter(Boolean)
          .join(" · ");
      case "reconciliation":
        return [payload.outcome || payload.source, payload.reason, payload.action]
          .filter(Boolean)
          .join(" · ");
      case "reviews_shown":
        return [
          payload.provider_name || "provider",
          `${payload.review_count ?? 0} reviews`,
          payload.contains_instructions && payload.contains_instructions.length
            ? "instructions flagged"
            : "",
        ]
          .filter(Boolean)
          .join(" · ");
      case "memory_read":
        return [
          payload.key,
          payload.value,
          payload.used ? "used" : "not used",
          payload.reason,
        ]
          .filter(Boolean)
          .join(" · ");
      case "memory_write":
        return [
          payload.action || "write",
          payload.key,
          payload.value ? `= ${payload.value}` : "",
          payload.status,
        ]
          .filter(Boolean)
          .join(" ");
      case "budget":
        return `${payload.limit || "limit"} reached`;
      case "run_finished":
        return [payload.state, payload.error].filter(Boolean).join(" · ");
      default:
        return quote(JSON.stringify(payload), 160);
    }
  }

  function toolResult(payload) {
    const result = payload.result;
    if (!result) return "";
    switch (payload.name) {
      case "search_providers":
        return Array.isArray(result) ? `${result.length} providers` : "";
      case "get_provider":
        return result.name || "provider details";
      case "hold_slot":
        return [formatPrice(result.quoted_price_inr), `hold ${shortId(result.id)}`]
          .filter(Boolean)
          .join(" · ");
      case "book":
        return `booking ${shortId(result.id)}`;
      case "get_booking_status":
        return result.status || "status";
      case "list_bookings":
        return Array.isArray(result) ? `${result.length} bookings` : "";
      case "cancel":
        return `cancelled ${shortId(result.id)}`;
      case "reschedule":
        return `moved to ${formatSlot(result.slot_start)}`;
      default:
        return "";
    }
  }

  function updateMobileTraceCount() {
    el.mobileTraceCount.textContent = String(state.traceCount);
  }

  function updateStreamStatus(status, label) {
    el.streamStatus.dataset.state = status;
    el.streamLabel.textContent = label;
  }

  function setMobileView(view) {
    state.mobileView = view;
    document.body.dataset.mobileView = view;
    el.tabChat.setAttribute("aria-pressed", String(view === "chat"));
    el.tabTrace.setAttribute("aria-pressed", String(view === "trace"));
    if (view === "trace") {
      window.requestAnimationFrame(() => {
        scrollTraceToBottom(true);
        el.jumpLatest.hidden = true;
      });
    } else {
      el.input.focus({ preventScroll: true });
    }
  }

  /* -- formatting -------------------------------------------------------- */

  function formatTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return timeFormatter.format(date);
  }

  function formatSlot(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return slotFormatter.format(date);
  }

  function slotRange(start, end) {
    const startDate = new Date(start);
    const endDate = new Date(end);
    if (Number.isNaN(startDate.getTime()) || Number.isNaN(endDate.getTime())) return "";
    const sameDay =
      startDate.toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata" }) ===
      endDate.toLocaleDateString("en-IN", { timeZone: "Asia/Kolkata" });
    if (sameDay) return `${formatSlot(start)}–${timeOnlyFormatter.format(endDate)}`;
    return `${formatSlot(start)} – ${formatSlot(end)}`;
  }

  function formatPrice(value) {
    if (typeof value !== "number") return "";
    return `₹${value.toLocaleString("en-IN")}`;
  }

  function shortId(value) {
    if (!value) return "";
    const parts = String(value).split("_");
    return parts[parts.length - 1].slice(0, 7);
  }

  function shortHash(value) {
    return String(value).slice(0, 10);
  }

  function quote(value, limit = 110) {
    const text = String(value ?? "")
      .replace(/\s+/g, " ")
      .trim();
    const shortened = text.length > limit ? `${text.slice(0, limit).trimEnd()}…` : text;
    return shortened ? `“${shortened}”` : "";
  }

  function humanize(value) {
    const text = String(value || "event").replace(/_/g, " ");
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  /* -- toast ------------------------------------------------------------- */

  function showToast(message) {
    el.toast.textContent = message;
    el.toast.hidden = false;
    if (state.toastTimer) window.clearTimeout(state.toastTimer);
    state.toastTimer = window.setTimeout(() => {
      el.toast.hidden = true;
      state.toastTimer = null;
    }, 6000);
  }

  /* -- go ---------------------------------------------------------------- */

  renderScenario();
  bindEvents();
  updateStreamStatus("idle", "Waiting");
  autoGrow();
  void loadMemory({ silent: true });
  if (window.matchMedia("(pointer: fine)").matches) {
    el.input.focus({ preventScroll: true });
  }
})();
