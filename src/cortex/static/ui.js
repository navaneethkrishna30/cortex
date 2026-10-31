const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const EYE = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.6 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4M6.5 6.6A17 17 0 0 0 2 12s3.6 7 10 7a10 10 0 0 0 5.4-1.6M3 3l18 18M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>';

// Show / hide password
$$(".reveal").forEach((wrap) => {
  const input = wrap.querySelector("input");
  const button = document.createElement("button");
  button.type = "button";
  const show = (visible) => {
    input.type = visible ? "text" : "password";
    button.innerHTML = visible ? EYE_OFF : EYE;
    button.setAttribute("aria-label", visible ? "Hide password" : "Show password");
    button.setAttribute("aria-pressed", visible);
  };
  button.addEventListener("click", () => { show(input.type === "password"); input.focus(); });
  show(false);
  wrap.append(button);
});

// New password: strength and match, live
const password = document.querySelector("[data-new-password]");
const confirmation = document.querySelector("[data-confirm-password]");
if (password) {
  const bar = document.querySelector(".meter i");
  const strengthHint = document.getElementById("strength");
  const matchHint = document.getElementById("match");
  const submit = password.form.querySelector("[type=submit]");
  const update = () => {
    const value = password.value;
    const classes = [/[a-z]/, /[A-Z]/, /\d/, /[^\w]/].filter((re) => re.test(value)).length;
    const score = value.length < 12 ? 0 : value.length >= 20 || (value.length >= 14 && classes >= 3) ? 2 : 1;
    const label = [
      value ? `${12 - value.length > 0 ? 12 - value.length : 0} more characters needed` : "At least 12 characters. A few random words work well.",
      "Fine. Longer is better: a stolen hash can be attacked offline.",
      "Strong.",
    ][score];
    bar.dataset.level = value ? score : "empty";
    strengthHint.textContent = label;
    const matches = value && value === confirmation.value;
    matchHint.textContent = !confirmation.value ? "" : matches ? "Passwords match." : "Passwords don't match yet.";
    matchHint.className = `hint ${!confirmation.value ? "" : matches ? "good" : "bad"}`;
    submit.disabled = value.length < 12 || !matches;
  };
  password.addEventListener("input", update);
  confirmation.addEventListener("input", update);
  update();
}

// Copy buttons
$$("[data-copy]").forEach((button) => {
  const label = button.textContent;
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(document.getElementById(button.dataset.copy).textContent.trim());
      button.textContent = "Copied";
    } catch {
      button.textContent = "Couldn't copy";
    }
    setTimeout(() => { button.textContent = label; }, 1500);
  });
});

// Login code: K7QX-M2PD, formatted as you type
$$(".code-input").forEach((input) => input.addEventListener("input", () => {
  const raw = input.value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 8);
  input.value = raw.length > 4 ? `${raw.slice(0, 4)}-${raw.slice(4)}` : raw;
}));

// Authenticator code: digits only, submits itself once the form is complete
$$(".otp").forEach((input) => input.addEventListener("input", () => {
  input.value = input.value.replace(/\D/g, "").slice(0, 6);
  if (input.value.length === 6 && input.dataset.autosubmit !== undefined && input.form.checkValidity()) {
    input.form.requestSubmit();
  }
}));

// Confirm before something that can't be undone
const dialog = document.getElementById("confirm");
$$("form[data-confirm]").forEach((form) => form.addEventListener("submit", (event) => {
  if (form.dataset.confirmed) return;
  event.preventDefault();
  dialog.querySelector("h2").textContent = form.dataset.confirm;
  dialog.querySelector("p").textContent = form.dataset.detail || "";
  const yes = dialog.querySelector("[data-yes]");
  yes.textContent = form.dataset.yes || "Confirm";
  yes.className = `btn ${form.dataset.tone || "danger solid"}`;
  yes.onclick = () => { form.dataset.confirmed = "1"; form.requestSubmit(); };
  dialog.showModal();
}));

// Dialogs opened by a button; a form that came back with an error reopens its own
$$("[data-opens]").forEach((button) => button.addEventListener("click", () => {
  button.closest("details.menu")?.removeAttribute("open");
  document.getElementById(button.dataset.opens).showModal();
}));
// A click outside closes the note menu
document.addEventListener("click", (event) => {
  $$("details.menu[open]").forEach((menu) => { if (!menu.contains(event.target)) menu.open = false; });
});
$$("dialog[data-autoopen]").forEach((open) => open.showModal());
$$("dialog [data-close]").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));

// Explorer: remember which folders are open, and filter across the whole tree
const folders = $$(".tree-list details[data-folder]");
const liveFiles = $$(".tree-list details[data-folder] .tree-file");
const OPEN_KEY = "cortex-opened-folders";
const readOpen = () => { try { return JSON.parse(localStorage.getItem(OPEN_KEY)); } catch { return null; } };
const remembered = readOpen();
if (remembered) folders.forEach((folder) => { folder.open = remembered.includes(folder.dataset.folder) || folder.dataset.keep === "1"; });
const filter = document.getElementById("filter");
let filtering = false;
folders.forEach((folder) => folder.addEventListener("toggle", () => {
  if (filtering) return;
  // A folder held open for the current note isn't one you chose to open.
  const open = folders.filter((f) => f.open && f.dataset.keep !== "1").map((f) => f.dataset.folder);
  try { localStorage.setItem(OPEN_KEY, JSON.stringify(open)); } catch {}
}));
if (filter) {
  let before = null;
  filter.addEventListener("input", () => {
    const query = filter.value.trim().toLowerCase();
    filtering = true;
    if (query && !before) before = new Map(folders.map((f) => [f, f.open]));
    liveFiles.forEach((file) => { file.parentElement.hidden = Boolean(query) && !file.title.toLowerCase().includes(query); });
    $$(".tree-empty").forEach((row) => { row.hidden = Boolean(query); });
    folders.forEach((folder) => {
      const matches = $$(".tree-file", folder).some((file) => !file.parentElement.hidden);
      folder.parentElement.hidden = Boolean(query) && !matches;
      if (query) folder.open = matches;
      else if (before) folder.open = before.get(folder);
    });
    document.getElementById("tree-none").hidden = !query || liveFiles.some((file) => !file.parentElement.hidden);
    if (!query) before = null;
    // Toggle events fire after this handler; only then is saving safe again.
    setTimeout(() => { filtering = false; });
  });
  document.getElementById("collapse-all").addEventListener("click", () => {
    $$(".tree-list details").forEach((folder) => { folder.open = false; });
    // Clearing a filter must not reopen what was just collapsed.
    if (before) before = new Map(folders.map((f) => [f, false]));
  });
  document.getElementById("filter-clear").addEventListener("click", () => {
    filter.value = "";
    filter.dispatchEvent(new Event("input"));
    filter.focus();
  });
}

// Editor
const textarea = document.getElementById("editor");
if (textarea) {
  const form = textarea.form;
  const dirty = document.getElementById("dirty");
  const save = document.getElementById("save");
  // After a save the page is the note itself, so a reload must not land on the old address.
  if (form.elements.path?.type === "hidden") history.replaceState(null, "", `/ui/files?path=${encodeURIComponent(form.elements.path.value)}`);
  const initial = textarea.value;
  // A form that came back with an error holds a draft the saved note doesn't.
  let changed = form.querySelector(".alert.bad") !== null && form.dataset.new === undefined;
  const refresh = () => {
    dirty.hidden = !changed;
    if (save && form.dataset.new === undefined) save.disabled = !changed;
  };
  refresh();
  textarea.addEventListener("input", () => {
    changed = textarea.value !== initial;
    refresh();
  });
  // A submit held back by the confirm dialog hasn't saved anything yet.
  form.addEventListener("submit", (event) => { if (!event.defaultPrevented) changed = false; });
  window.addEventListener("beforeunload", (event) => { if (changed) event.preventDefault(); });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "s") { event.preventDefault(); if (save && !save.disabled) form.requestSubmit(); }
  });
  // Read shows the last save, rendered by the server; Edit shows the source.
  const readButton = document.getElementById("view-read");
  const editButton = document.getElementById("view-edit");
  if (readButton) {
    const view = document.getElementById("note-view");
    const edit = document.getElementById("note-edit");
    const show = (editing) => {
      view.hidden = editing;
      edit.hidden = !editing;
      readButton.setAttribute("aria-pressed", !editing);
      editButton.setAttribute("aria-pressed", editing);
      if (editing) textarea.focus();
    };
    readButton.addEventListener("click", () => show(false));
    editButton.addEventListener("click", () => show(true));
    if (!edit.hidden) show(true);
  }
}
