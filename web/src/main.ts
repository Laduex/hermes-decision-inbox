import "./style.css";
import { gestureAction } from "./gestures";
import type { Card, Decision, Outcome } from "./types";

const app = document.querySelector<HTMLElement>("#app")!;
const toast = document.querySelector<HTMLElement>("#toast")!;
let token = "";
let current: Decision | null = null;
let cardIndex = 0;
let editMode = false;
let viewMode = false;
let pollTimer = 0;

const terminalStatuses = new Set(["COMPLETED", "EXPIRED", "CANCELLED", "ARCHIVED"]);

function statusLabel(status: unknown): string {
  const value = String(status || "");
  if (value === "ARCHIVED") return "Archived";
  if (value === "COMPLETED" || value === "EXPIRED" || value === "CANCELLED") return "Completed";
  if (value === "READY_TO_APPLY") return "Ready to apply";
  if (value === "APPLYING") return "Applying";
  if (value === "BLOCKED") return "Blocked";
  return "Review";
}

function showToast(message: string) {
  toast.textContent = message;
  toast.classList.add("show");
  window.setTimeout(() => toast.classList.remove("show"), 2400);
}

async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (options.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail || "Request failed");
  }
  return response.json();
}

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  }[char]!));
}

async function authenticate() {
  const result = await api<{token: string; login: string}>("/api/auth/tailscale", { method: "POST" });
  token = result.token;
  connectLiveUpdates();
}

async function loadInbox(_tab = "all", directToCards = false) {
  current = null;
  const data = await api<{items: Array<Record<string, unknown>>}>("/api/inbox?tab=all");
  const reviewItems = data.items.filter(item => !terminalStatuses.has(String(item.status)));
  if (directToCards && reviewItems.length) {
    await openDecision(String(reviewItems[0].decision_id));
    return;
  }
  app.innerHTML = `<section class="shell">
    <header class="app-header" aria-label="Weekly Memory Wiki decision inbox"><div><div class="eyebrow">Weekly Memory Wiki</div><h1>Review cards</h1></div></header>
    <div class="inbox-list">${data.items.length ? data.items.map(item => {
      const status = statusLabel(item.status);
      const isReview = !terminalStatuses.has(String(item.status));
      const openButton = isReview
        ? `<button class="inbox-open" data-decision="${escapeHtml(item.decision_id)}" aria-label="Open ${escapeHtml(item.title)}">
          <div class="item-row"><span class="status-chip">${status}</span><span class="badge">${escapeHtml(item.priority)}</span></div>
          <div class="item-title">${escapeHtml(item.title)}</div>
          <div class="meta">${escapeHtml(item.card_count)} card${item.card_count === 1 ? "" : "s"} · ${new Date(String(item.created_at)).toLocaleString()}</div>
        </button>`
        : `<div class="inbox-open archived-item" aria-label="${escapeHtml(status)}: ${escapeHtml(item.title)}">
          <div class="item-row"><span class="status-chip">${status}</span><span class="badge">${escapeHtml(item.priority)}</span></div>
          <div class="item-title">${escapeHtml(item.title)}</div>
          <div class="meta">${escapeHtml(item.card_count)} card${item.card_count === 1 ? "" : "s"} · ${new Date(String(item.created_at)).toLocaleString()}</div>
        </div>`;
      return `
      <article class="inbox-item">
        ${openButton}
        <div class="item-actions">
          <button class="item-action view-action" data-view-decision="${escapeHtml(item.decision_id)}">View</button>
          ${item.can_edit ? `<button class="item-action edit-action" data-edit-decision="${escapeHtml(item.decision_id)}">Edit</button>` : ""}
          ${item.can_apply ? `<button class="item-action apply-action" data-apply-decision="${escapeHtml(item.decision_id)}">Apply</button>` : ""}
        </div>
      </article>`}).join("") : `<div class="empty">Nothing here.</div>`}</div>
  </section>`;
  app.querySelectorAll<HTMLButtonElement>("[data-decision]").forEach(button => button.onclick = () => openDecision(button.dataset.decision!));
  app.querySelectorAll<HTMLButtonElement>("[data-edit-decision]").forEach(button => button.onclick = event => {
    event.stopPropagation();
    openDecision(button.dataset.editDecision!, true);
  });
  app.querySelectorAll<HTMLButtonElement>("[data-view-decision]").forEach(button => button.onclick = event => {
    event.stopPropagation();
    openDecision(button.dataset.viewDecision!, false, true);
  });
  app.querySelectorAll<HTMLButtonElement>("[data-apply-decision]").forEach(button => button.onclick = event => {
    event.stopPropagation();
    const item = data.items.find(candidate => candidate.decision_id === button.dataset.applyDecision);
    if (item) applyDecision(item, button);
  });
}

async function openDecision(id: string, editing = false, viewing = false) {
  current = await api<Decision>(`/api/decisions/${id}`);
  cardIndex = 0;
  viewMode = viewing;
  editMode = editing && !viewing;
  renderDeck();
}

function renderDeck() {
  if (!current) return;
  const pending = current.cards.filter(card => !card.response);
  if (!pending.length && !editMode) {
    loadInbox("all", false);
    return;
  }
  const deck = editMode || viewMode ? current.cards : pending;
  if (!deck.length) {
    loadInbox("all", false);
    return;
  }
  const card = deck[Math.min(cardIndex, deck.length - 1)];
  const recommendation = card.options.find(option => option.is_recommended) || card.options[0];
  const completed = editMode || viewMode ? cardIndex : current.cards.length - pending.length;
  const decisionStatus = card.response ? String(card.response.outcome).replace(/_/g, " ") : "not decided";
  app.innerHTML = `<section class="shell">
    <header class="app-header review-topline" aria-label="Weekly Memory Wiki card review"><div><div class="eyebrow">Weekly Memory Wiki</div><h1>Review cards</h1></div><span class="progress">${completed + 1} of ${current.cards.length}</span></header>
    <div class="deck-header"><button class="back" aria-label="Back to inbox">← Inbox</button></div>
    <article class="card" tabindex="0" aria-label="Decision card: ${escapeHtml(card.title)}" aria-describedby="swipe-help">
      <div class="item-row"><span class="eyebrow">${escapeHtml(card.source_profile)}</span><span class="badge">${escapeHtml(card.priority)}</span></div>
      <h2>${escapeHtml(card.title)}</h2>
      <p>${escapeHtml(card.summary)}</p>
      <div class="recommendation"><span class="eyebrow">Hermes recommends</span><strong>${escapeHtml(recommendation.label)}</strong><span>${escapeHtml(recommendation.reason || recommendation.details)}</span></div>
      ${viewMode ? `<div class="decision-status"><span class="eyebrow">Decision status</span><strong>${escapeHtml(decisionStatus)}</strong>${card.response?.note ? `<span>${escapeHtml(card.response.note)}</span>` : ""}</div><div class="view-controls"><button class="secondary view-prev" ${cardIndex === 0 ? "disabled" : ""}>Previous</button><button class="primary view-next" ${cardIndex >= deck.length - 1 ? "disabled" : ""}>Next</button></div>` : ""}
    </article>
    <footer id="swipe-help" class="app-footer swipe-help" role="contentinfo">Swipe right to accept · left to reject · up to abstain · down for alternatives</footer>
  </section>`;
  app.querySelector<HTMLButtonElement>(".back")!.onclick = () => loadInbox("all", false);
  app.querySelectorAll<HTMLButtonElement>("[data-outcome]").forEach(button => {
    button.onclick = () => saveResponse(card, button.dataset.outcome as Outcome);
  });
  if (viewMode) {
    app.querySelector<HTMLButtonElement>(".view-prev")!.onclick = () => { cardIndex -= 1; renderDeck(); };
    app.querySelector<HTMLButtonElement>(".view-next")!.onclick = () => { cardIndex += 1; renderDeck(); };
  } else {
    bindSwipe(app.querySelector<HTMLElement>(".card")!, card);
  }
}

function bindSwipe(element: HTMLElement, card: Card) {
  let startX = 0, startY = 0, tracking = false, moved = false, pointerId: number | null = null;
  let suppressClickUntil = 0;
  element.onpointerdown = event => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    tracking = true;
    moved = false;
    pointerId = event.pointerId;
    startX = event.clientX;
    startY = event.clientY;
    element.classList.add("dragging");
    element.setPointerCapture(event.pointerId);
  };
  element.onpointermove = event => {
    if (!tracking || event.pointerId !== pointerId) return;
    const dx = event.clientX - startX, dy = event.clientY - startY;
    if (Math.hypot(dx, dy) > 8) moved = true;
    element.style.transition = "none";
    element.style.transform = `translate(${dx * .42}px, ${dy * .34}px) rotate(${dx * .035}deg)`;
    element.style.opacity = String(Math.max(.72, 1 - Math.hypot(dx, dy) / 700));
    event.preventDefault();
  };
  const finish = (event: PointerEvent, cancelled = false) => {
    if (!tracking || (pointerId !== null && event.pointerId !== pointerId)) return;
    tracking = false;
    const dx = event.clientX - startX, dy = event.clientY - startY;
    const action = !cancelled && moved ? gestureAction(dx, dy) : null;
    if (moved) suppressClickUntil = Date.now() + 350;
    try { element.releasePointerCapture(event.pointerId); } catch { /* already released */ }
    pointerId = null;
    element.classList.remove("dragging");
    element.style.transition = "";
    element.style.transform = "";
    element.style.opacity = "";
    if (action === "alternatives") showDetails(card, true);
    else if (action) saveResponse(card, action);
  };
  element.onpointerup = event => finish(event);
  element.onpointercancel = event => finish(event, true);
  element.onlostpointercapture = event => finish(event, true);
  element.onclick = event => {
    if (Date.now() < suppressClickUntil) {
      event.preventDefault();
      event.stopPropagation();
    }
  };
  element.onkeydown = event => {
    const actions: Record<string, () => void> = {
      ArrowRight: () => saveResponse(card, "recommended"),
      ArrowLeft: () => saveResponse(card, "rejected"),
      ArrowUp: () => saveResponse(card, "abstained"),
      ArrowDown: () => showDetails(card, true),
    };
    const action = actions[event.key];
    if (!action) return;
    event.preventDefault();
    action();
  };
  element.focus();
}

async function saveResponse(card: Card, outcome: Outcome, selected: string | null = null, note = "") {
  navigator.vibrate?.(12);
  current = await api<Decision>(`/api/cards/${card.card_id}/response`, {
    method: "PUT", body: JSON.stringify({ card_version: card.version, outcome, selected_option_id: selected, note })
  });
  if (editMode) {
    cardIndex += 1;
    if (cardIndex >= current.cards.length) {
      editMode = false;
      await loadInbox("all", false);
      return;
    }
  }
  renderDeck();
}

function showDetails(card: Card, focusAlternatives = false) {
  const dialog = document.createElement("dialog");
  dialog.className = "detail-modal";
  const alternatives = card.options.filter(option => !option.is_recommended);
  dialog.innerHTML = `<div class="modal-eyebrow">Alternative answers</div><h3>${escapeHtml(card.title)}</h3><p>${escapeHtml(card.details)}</p>
    ${card.evidence.length ? `<h4>Evidence</h4><ul>${card.evidence.map(item => `<li><strong>${escapeHtml(item.label)}:</strong> ${escapeHtml(item.value)}</li>`).join("")}</ul>` : ""}
    ${card.execution ? `<h4>Recommended Wiki change</h4><pre>${escapeHtml(card.execution.proposed_content)}</pre>` : ""}
    ${alternatives.length ? `<label for="alternative">Choose an alternative</label><select id="alternative"><option value="">Choose…</option>${alternatives.map(option => `<option value="${escapeHtml(option.option_id)}">${escapeHtml(option.label)}</option>`).join("")}</select><pre class="alternative-preview" hidden></pre>` : `<label for="custom-alternative">Write an alternative</label><textarea id="custom-alternative" class="custom-alternative" rows="3" placeholder="Type a different answer or instruction"></textarea>`}
    <div class="modal-actions"><button class="approve modal-accept">Accept</button>${alternatives.length ? `<button class="primary choose">Choose alternative</button>` : ""}<button class="secondary close">Close</button></div>`;
  document.body.append(dialog);
  dialog.querySelector<HTMLButtonElement>(".close")!.onclick = () => dialog.close();
  dialog.querySelector<HTMLButtonElement>(".modal-accept")!.onclick = () => {
    const custom = dialog.querySelector<HTMLTextAreaElement>("#custom-alternative")?.value.trim() || "";
    dialog.close();
    saveResponse(card, custom ? "alternative" : "recommended", null, custom);
  };
  dialog.querySelector<HTMLButtonElement>(".choose")?.addEventListener("click", () => {
    const selected = dialog.querySelector<HTMLSelectElement>("#alternative")!.value;
    if (!selected) return showToast("Choose an alternative first");
    dialog.close();
    saveResponse(card, "alternative", selected);
  });
  const select = dialog.querySelector<HTMLSelectElement>("#alternative");
  select?.addEventListener("change", () => {
    const option = alternatives.find(item => item.option_id === select.value);
    const preview = dialog.querySelector<HTMLElement>(".alternative-preview")!;
    const content = option?.execution?.proposed_content || option?.details || "";
    preview.textContent = content;
    preview.hidden = !content;
  });
  dialog.addEventListener("close", () => dialog.remove());
  dialog.showModal();
  if (focusAlternatives) select?.focus();
}

async function applyDecision(item: Record<string, unknown>, button?: HTMLButtonElement) {
  if (button) button.disabled = true;
  try {
    let result: Record<string, unknown> = item;
    if (item.status === "READY_TO_SUBMIT") {
      result = await api<Record<string, unknown>>(`/api/decisions/${item.decision_id}/submit`, {
        method: "POST", body: JSON.stringify({ expected_version: item.version })
      });
    }
    if (result.status === "READY_TO_APPLY") {
      await api(`/api/manifests/${result.manifest_id}/apply`, {
        method: "POST", body: JSON.stringify({ expected_version: result.decision_version })
      });
      showToast("Wiki apply queued");
    } else if (result.status === "APPLYING") {
      showToast("Wiki apply is already running");
    } else {
      showToast("Decisions submitted");
    }
    navigator.vibrate?.([18, 30, 18]);
    await loadInbox("all", false);
  } catch (error) {
    if (button) button.disabled = false;
    showToast(error instanceof Error ? error.message : "Submission failed");
  }
}

function connectLiveUpdates() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/api/ws?token=${encodeURIComponent(token)}`);
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.type === "decision_created") {
      showToast("A new decision arrived");
      if (!current) loadInbox("all");
    }
  };
  socket.onclose = () => {
    window.clearInterval(pollTimer);
    pollTimer = window.setInterval(() => { if (!current) loadInbox("all"); }, 20_000);
    window.setTimeout(connectLiveUpdates, 5000);
  };
}

authenticate().then(() => loadInbox()).catch(error => {
  app.innerHTML = `<section class="shell"><div class="error"><h1>Decision Inbox</h1><p>${escapeHtml(error.message)}</p></div></section>`;
});
