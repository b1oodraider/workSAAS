function copyText(id, btn) {
  const text = document.getElementById(id).innerText;
  navigator.clipboard.writeText(text).then(() => {
    const old = btn.textContent;
    btn.textContent = "Скопировано";
    setTimeout(() => (btn.textContent = old), 1500);
  });
}

// Job page: poll status and reload when something changes.
document.addEventListener("DOMContentLoaded", () => {
  const el = document.querySelector("[data-job-poll]");
  if (!el) return;
  const url = el.dataset.jobPoll;
  const status = el.dataset.jobStatus;
  const finished = Number(el.dataset.jobFinished || 0);
  const tick = async () => {
    try {
      const r = await fetch(url, { headers: { Accept: "application/json" } });
      if (r.ok) {
        const d = await r.json();
        const c = d.children || {};
        const hasChildren = Object.keys(c).length > 0;
        const nowFinished = (c.done || 0) + (c.failed || 0);
        if (d.status === "done" && d.result_url && !hasChildren) {
          window.location = d.result_url;
          return;
        }
        if (d.status !== status || nowFinished !== finished) {
          window.location.reload();
          return;
        }
      }
    } catch (e) { /* network hiccup: try again */ }
    setTimeout(tick, 2000);
  };
  setTimeout(tick, 2000);
});
