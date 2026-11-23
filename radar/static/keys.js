// Radar de Empleo CO: atajos de teclado y cambio de estado sin recargar.
// Mejora progresiva: sin este archivo, los formularios funcionan igual (POST + redirección).
(() => {
  "use strict";

  const KEY_STATUS = { a: "aprobada", d: "descartada", e: "enviada" };
  const LABEL = {
    encontrada: "Encontrada", aprobada: "Aprobada", enviada: "Enviada", entrevista: "Entrevista",
    oferta: "Oferta", rechazo: "Rechazo", descartada: "Descartada",
  };
  const undoStack = []; // [{card, previous}]
  let current = null;

  const cards = () => [...document.querySelectorAll("article.job")];
  const titleOf = (card) => card.querySelector("h2")?.textContent.trim() ?? "la oferta";
  const live = () => document.getElementById("live");

  function announce(msg) {
    const el = live();
    if (!el) return;
    el.textContent = "";
    // Un pequeño retraso hace que los lectores de pantalla repitan mensajes iguales
    setTimeout(() => { el.textContent = msg; }, 30);
    el.classList.add("is-visible");
    clearTimeout(announce.timer);
    announce.timer = setTimeout(() => el.classList.remove("is-visible"), 5000);
  }

  function focusCard(card) {
    if (!card) return;
    current?.classList.remove("is-current");
    current = card;
    card.classList.add("is-current");
    card.focus({ preventScroll: true });
    const smooth = matchMedia("(prefers-reduced-motion: no-preference)").matches;
    card.scrollIntoView({ block: "nearest", behavior: smooth ? "smooth" : "auto" });
  }

  function move(step) {
    const list = cards();
    if (!list.length) return;
    const active = document.activeElement?.closest?.("article.job") ?? current;
    const i = list.indexOf(active);
    const next = i < 0 ? (step > 0 ? 0 : list.length - 1) : Math.min(list.length - 1, Math.max(0, i + step));
    focusCard(list[next]);
  }

  function activeCard() {
    return document.activeElement?.closest?.("article.job") ?? current;
  }

  function renderStatus(card, status) {
    card.dataset.status = status;
    card.classList.toggle("has-status", status !== "encontrada");
    card.classList.toggle("is-discarded", status === "descartada");
    card.querySelectorAll("form.status button[name=status]").forEach((btn) => {
      if (btn.classList.contains("undo")) {
        btn.hidden = status === "encontrada";
        return;
      }
      const on = btn.value === status;
      btn.disabled = on;
      if (on) btn.setAttribute("aria-pressed", "true");
      else btn.removeAttribute("aria-pressed");
    });
  }

  function renderCounts(counts) {
    if (!counts) return;
    for (const [status, n] of Object.entries(counts.by_status ?? {})) {
      document.querySelectorAll(`[data-count="${status}"]`).forEach((el) => { el.textContent = n; });
    }
    document.querySelectorAll('[data-count="follow_ups"]').forEach((el) => {
      el.textContent = counts.follow_ups;
      if (el.tagName === "DD") el.classList.toggle("kpi-amber", counts.follow_ups > 0);
    });
  }

  async function post(form, data) {
    const resp = await fetch(form.action, {
      method: "POST",
      body: new URLSearchParams(data),
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || `Error ${resp.status}`);
    return body;
  }

  async function setStatus(card, status, { remember = true } = {}) {
    const form = card?.querySelector("form.status");
    if (!form) return;
    const previous = card.dataset.status;
    if (previous === status) {
      announce(`Ya está como ${LABEL[status].toLowerCase()}.`);
      return;
    }
    card.setAttribute("aria-busy", "true");
    try {
      const res = await post(form, { status, back: form.elements.back?.value ?? "/" });
      renderStatus(card, res.job.status);
      renderCounts(res.counts);
      if (remember) undoStack.push({ card, previous: res.previous });
      let msg = `«${titleOf(card)}»: ${LABEL[res.job.status]}.`;
      if (res.follow_up) msg += ` Seguimiento el ${res.follow_up}.`;
      if (remember) msg += " Pulsa u para deshacer.";
      announce(msg);
    } catch (err) {
      announce(`No se pudo cambiar el estado: ${err.message}`);
    } finally {
      card.removeAttribute("aria-busy");
    }
  }

  function undo() {
    const last = undoStack.pop();
    if (last) {
      focusCard(last.card);
      setStatus(last.card, last.previous, { remember: false });
      return;
    }
    const card = activeCard();
    if (card && card.dataset.status !== "encontrada") setStatus(card, "encontrada", { remember: false });
    else announce("No hay nada que deshacer.");
  }

  function openOffer() {
    const link = activeCard()?.querySelector("a.open");
    if (link && /^https?:$/.test(link.protocol)) window.open(link.href, "_blank", "noopener,noreferrer");
  }

  function toggleHelp() {
    const dlg = document.getElementById("keys-help");
    if (!dlg) return;
    if (dlg.open) dlg.close();
    else dlg.showModal();
  }

  function isTyping(target) {
    return target instanceof HTMLElement &&
      (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
  }

  document.addEventListener("keydown", (ev) => {
    if (ev.ctrlKey || ev.metaKey || ev.altKey || ev.defaultPrevented || isTyping(ev.target)) return;
    const dlg = document.getElementById("keys-help");
    if (dlg?.open && ev.key !== "?") return; // Esc y Tab los maneja el diálogo
    const key = ev.key;
    let handled = true;
    if (key === "j") move(1);
    else if (key === "k") move(-1);
    else if (key in KEY_STATUS) {
      const card = activeCard();
      if (card) setStatus(card, KEY_STATUS[key]);
      else announce("Elige primero una oferta con j o k.");
    } else if (key === "u") undo();
    else if (key === "o") openOffer();
    else if (key === "?") toggleHelp();
    else handled = false;
    if (handled) ev.preventDefault();
  });

  // Los botones de estado y las notas también van por fetch (sin recargar ni perder el scroll)
  document.addEventListener("submit", async (ev) => {
    const form = ev.target;
    const card = form.closest("article.job");
    if (!card) return;
    if (form.matches("form.status")) {
      const btn = ev.submitter;
      if (!btn || btn.name !== "status") return;
      ev.preventDefault();
      current = card;
      card.classList.add("is-current");
      await setStatus(card, btn.value);
    } else if (form.action.endsWith("/notes")) {
      ev.preventDefault();
      try {
        await post(form, { notes: form.elements.notes.value, back: form.elements.back?.value ?? "/" });
        announce(`Notas guardadas: «${titleOf(card)}».`);
      } catch (err) {
        announce(`No se pudieron guardar las notas: ${err.message}`);
      }
    }
  });

  // Seguir la tarjeta en la que está el foco (clic, Tab) para que los atajos actúen sobre ella
  document.addEventListener("focusin", (ev) => {
    const card = ev.target.closest?.("article.job");
    if (card && card !== current) {
      current?.classList.remove("is-current");
      current = card;
      card.classList.add("is-current");
    }
  });

  document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("show-keys");
    if (btn) {
      btn.hidden = false;
      btn.addEventListener("click", toggleHelp);
    }
    document.documentElement.classList.add("js-keys");
  });
})();
