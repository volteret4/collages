// Selector entre servicios del mismo host. La lista sale de CHARTS_SWITCH en el
// servidor (Nombre:puerto,…); solo se enseñan los que responden.
(async function () {
  let items;
  try {
    items = await (await fetch('/api/switch')).json();
  } catch (e) {
    return;
  }
  if (!Array.isArray(items) || items.length < 2) return;

  const urlOf = i => `${location.protocol}//${location.hostname}:${i.port}/`;
  // no-cors: la respuesta no se puede leer, pero la promesa solo falla si no hay servidor
  async function alive(i) {
    if (i.current) return true;
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 1500);
    try {
      await fetch(urlOf(i) + 'api/switch', {mode: 'no-cors', cache: 'no-store', signal: ctl.signal});
      return true;
    } catch (e) {
      return false;
    } finally {
      clearTimeout(timer);
    }
  }
  const up = await Promise.all(items.map(alive));
  const active = items.filter((_, n) => up[n]);
  if (active.length < 2) return;

  const style = document.createElement('style');
  style.textContent = `
#sb-switch{display:flex;flex-wrap:wrap;gap:4px;padding:8px 14px;border-bottom:1px solid var(--border);flex-shrink:0}
#sb-switch>*{flex:1;text-align:center;padding:4px 8px;border:1px solid var(--border);border-radius:3px;font-size:.72rem;white-space:nowrap;text-decoration:none;color:var(--text-muted)}
#sb-switch a:hover{color:var(--text)}
#sb-switch [aria-current]{background:var(--surface-2);color:var(--text);font-weight:700}`;
  document.head.appendChild(style);

  const nav = document.createElement('nav');
  nav.id = 'sb-switch';
  for (const i of active) {
    const el = document.createElement(i.current ? 'span' : 'a');
    el.textContent = i.label;
    if (i.current) el.setAttribute('aria-current', 'page');
    else el.href = urlOf(i);
    nav.appendChild(el);
  }
  document.getElementById('sb-head').after(nav);
})();
