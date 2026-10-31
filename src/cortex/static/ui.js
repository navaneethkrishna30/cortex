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
    const [width, color, label] = [
      [Math.min(value.length * 3, 30), "var(--red)", value ? `${12 - value.length > 0 ? 12 - value.length : 0} more characters needed` : "At least 12 characters. A few random words work well."],
      [65, "var(--orange)", "Fine. Longer is better: a stolen hash can be attacked offline."],
      [100, "var(--teal)", "Strong."],
    ][score];
    bar.style.width = `${width}%`;
    bar.style.background = color;
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
    await navigator.clipboard.writeText(document.getElementById(button.dataset.copy).textContent.trim());
    button.textContent = "Copied";
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
  dialog.querySelector("[data-yes]").onclick = () => { form.dataset.confirmed = "1"; form.requestSubmit(); };
  dialog.showModal();
}));

// Dialogs opened by a button; a form that came back with an error reopens its own
$$("[data-opens]").forEach((button) => button.addEventListener("click", () => document.getElementById(button.dataset.opens).showModal()));
$$("dialog[data-autoopen]").forEach((open) => open.showModal());
$$("dialog [data-close]").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));

// File filter
const filter = document.getElementById("filter");
if (filter) {
  filter.addEventListener("input", () => {
    const query = filter.value.toLowerCase();
    $$(".tree details").forEach((folder) => {
      const links = $$("a", folder);
      links.forEach((a) => { a.hidden = !a.textContent.toLowerCase().includes(query); });
      folder.open = query ? links.some((a) => !a.hidden) : folder.dataset.open === "1";
      folder.hidden = Boolean(query) && !folder.open;
    });
  });
}

// On a phone the list sits above the note, so fold it away once a note is open.
if (document.getElementById("editor") && matchMedia("(max-width: 820px)").matches) {
  $$(".tree details").forEach((folder) => { folder.open = false; folder.dataset.open = "0"; });
}

// Editor
const textarea = document.getElementById("editor");
if (textarea && window.EasyMDE) {
  const editor = new EasyMDE({
    element: textarea, autoDownloadFontAwesome: false, toolbar: false, status: false,
    spellChecker: false, forceSync: true, minHeight: "0",
    renderingConfig: { sanitizerFunction: (html) => new DOMParser().parseFromString(html, "text/html").body.innerHTML },
  });
  const form = textarea.form;
  const dirty = document.getElementById("dirty");
  const save = form.querySelector("[type=submit]");
  const initial = editor.value();
  let changed = false;
  if (save && form.dataset.new === undefined) save.disabled = true;
  editor.codemirror.on("change", () => {
    changed = editor.value() !== initial;
    dirty.hidden = !changed;
    if (save && form.dataset.new === undefined) save.disabled = !changed;
  });
  form.addEventListener("submit", () => { changed = false; });
  window.addEventListener("beforeunload", (event) => { if (changed) event.preventDefault(); });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "s") { event.preventDefault(); if (save && !save.disabled) form.requestSubmit(); }
  });
  const preview = document.getElementById("preview");
  preview.addEventListener("click", () => {
    editor.togglePreview();
    preview.textContent = editor.isPreviewActive() ? "Edit" : "Preview";
  });
}
