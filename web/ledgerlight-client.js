/*
 * Ledgerlight website client — drop into any of the 9 brand sites once they are
 * hosted on your own domains (the API only accepts requests from origins listed
 * in LL_ALLOWED_ORIGINS).
 *
 *   <script src="/ledgerlight-client.js"></script>
 *   <script>LL.init({ api: "https://api.ledgerlight.co.uk", brand: "sitesentinel" });</script>
 *
 * Then either call LL.* from your own code, or use data attributes:
 *   <form data-ll="signup">  email, password, name, org_name inputs  </form>
 *   <form data-ll="login">   email, password                          </form>
 *   <button data-ll="checkout" data-plan="team" data-cycle="annual">Buy</button>
 *   <button data-ll="export-events">Export CSV</button>
 *   <div data-ll="events"></div>   (renders the customer's feed as a table)
 */
(function () {
  const S = { api: "", brand: "ledgerlight", key: "ll_token" };
  const token = () => { try { return localStorage.getItem(S.key); } catch (e) { return null; } };
  const setToken = (t) => { try { t ? localStorage.setItem(S.key, t) : localStorage.removeItem(S.key); } catch (e) {} };

  async function call(path, opts = {}) {
    const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    if (token()) headers.Authorization = "Bearer " + token();
    const r = await fetch(S.api + path, Object.assign({}, opts, { headers }));
    if (r.status === 401) setToken(null);
    const type = r.headers.get("content-type") || "";
    const data = type.includes("json") ? await r.json() : await r.text();
    if (!r.ok) throw new Error((data && data.error) || ("Request failed: " + r.status));
    return data;
  }

  async function downloadCsv(path, filename) {
    const r = await fetch(S.api + path, { headers: { Authorization: "Bearer " + token() } });
    if (!r.ok) throw new Error("Export failed");
    const blob = await r.blob();
    const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: filename });
    document.body.appendChild(a); a.click(); a.remove();
  }

  const LL = {
    init(opts) { Object.assign(S, opts); wire(); },
    signedIn: () => !!token(),
    signOut() { setToken(null); },
    async signup(f) { const d = await call("/v1/auth/signup", { method: "POST", body: JSON.stringify(Object.assign({ brand: S.brand }, f)) }); setToken(d.token); return d; },
    async login(email, password) { const d = await call("/v1/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }); setToken(d.token); return d; },
    me: () => call("/v1/me"),
    plans: () => call("/v1/plans"),
    risks: () => call("/v1/risks?brand=" + encodeURIComponent(S.brand)),
    events: (f = {}) => call("/v1/events?" + new URLSearchParams(f)),
    company: (n) => call("/v1/companies/" + encodeURIComponent(n)),
    watchlists: () => call("/v1/watchlists"),
    watch: (wid, company_number, extra = {}) => call(`/v1/watchlists/${wid}/items`, { method: "POST", body: JSON.stringify(Object.assign({ company_number }, extra)) }),
    importWatchlist: (wid, csvText) => call(`/v1/watchlists/${wid}/import`, { method: "POST", body: csvText, headers: { "Content-Type": "text/csv" } }),
    rules: () => call("/v1/alert-rules"),
    addRule: (r) => call("/v1/alert-rules", { method: "POST", body: JSON.stringify(r) }),
    addChannel: (kind, target) => call("/v1/channels", { method: "POST", body: JSON.stringify({ kind, target }) }),
    exportEvents: (f = {}) => downloadCsv("/v1/exports/events.csv?" + new URLSearchParams(f), `${S.brand}-events.csv`),
    exportFields: () => downloadCsv("/v1/exports/fields.csv?brand=" + S.brand, `${S.brand}-fields.csv`),
    async checkout(plan, cycle = "monthly") {
      const here = location.href.split("#")[0];
      const d = await call("/v1/billing/checkout", { method: "POST", body: JSON.stringify({ plan, cycle, success_url: here + "#welcome", cancel_url: here + "#pricing" }) });
      location.href = d.url;
    },
    async billingPortal() { const d = await call("/v1/billing/portal?return_url=" + encodeURIComponent(location.href)); location.href = d.url; },
    objection: (kind, value, reason) => call("/v1/privacy/object", { method: "POST", body: JSON.stringify({ kind, value, reason }) }),
  };

  function formData(form) { return Object.fromEntries(new FormData(form).entries()); }
  function say(el, msg, bad) { let n = el.querySelector(".ll-msg"); if (!n) { n = document.createElement("p"); n.className = "ll-msg"; el.appendChild(n); } n.textContent = msg; n.style.color = bad ? "#B5432F" : "inherit"; }
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  async function renderEvents(el) {
    if (!token()) { el.innerHTML = "<p>Sign in to see your feed.</p>"; return; }
    try {
      const d = await LL.events({ limit: 50 });
      const cols = d.columns.slice(0, 8);
      el.innerHTML = `<p>${d.events.length} events · ${esc(d.level_name)} extraction</p><div style="overflow:auto"><table><thead><tr>${cols.map(c => `<th>${esc(c.label)}</th>`).join("")}</tr></thead><tbody>${d.events.map(e => `<tr>${cols.map(c => `<td>${esc(e[c.key])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
    } catch (err) { el.innerHTML = `<p>${esc(err.message)}</p>`; }
  }

  function wire() {
    document.querySelectorAll('form[data-ll="signup"]').forEach((f) => f.addEventListener("submit", async (e) => {
      e.preventDefault();
      try { await LL.signup(formData(f)); say(f, "Your 14-day trial has started."); document.dispatchEvent(new CustomEvent("ll:signedin")); }
      catch (err) { say(f, err.message, true); }
    }));
    document.querySelectorAll('form[data-ll="login"]').forEach((f) => f.addEventListener("submit", async (e) => {
      e.preventDefault();
      const v = formData(f);
      try { await LL.login(v.email, v.password); say(f, "Signed in."); document.dispatchEvent(new CustomEvent("ll:signedin")); }
      catch (err) { say(f, err.message, true); }
    }));
    document.querySelectorAll('[data-ll="checkout"]').forEach((b) => b.addEventListener("click", () => LL.checkout(b.dataset.plan, b.dataset.cycle || "monthly").catch((e) => alertBox(b, e.message))));
    document.querySelectorAll('[data-ll="export-events"]').forEach((b) => b.addEventListener("click", () => LL.exportEvents().catch((e) => alertBox(b, e.message))));
    const feeds = document.querySelectorAll('[data-ll="events"]');
    feeds.forEach(renderEvents);
    document.addEventListener("ll:signedin", () => feeds.forEach(renderEvents));
  }
  function alertBox(el, msg) { el.title = msg; el.setAttribute("aria-invalid", "true"); console.warn("[Ledgerlight]", msg); }

  window.LL = LL;
})();
