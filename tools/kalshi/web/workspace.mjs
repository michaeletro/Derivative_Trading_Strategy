// Navigation only: switching views preserves mounted producers and data caches.
export const WORKSPACE_TABS = Object.freeze(['market', 'strategies', 'portfolio', 'models', 'history', 'research', 'health', 'trade']);
const STORAGE_KEY = 'dts.kalshi.workspace.tab.v1';
const valid = value => typeof value === 'string' && WORKSPACE_TABS.includes(value);

export function mountWorkspaceTabs(doc = document, win = window) {
  const nav = doc?.getElementById?.('workspace-tabs');
  if (!nav || typeof nav.querySelectorAll !== 'function') return null;
  const buttons = [...nav.querySelectorAll('[role="tab"]')];
  const records = new Map();
  for (const button of buttons) {
    const key = button.getAttribute('data-workspace-tab');
    const panel = doc.getElementById(button.getAttribute('aria-controls'));
    if (!valid(key) || !panel || records.has(key) || panel.id !== `workspace-${key}`) return null;
    records.set(key, {button, panel});
  }
  if (records.size !== WORKSPACE_TABS.length) return null;
  let selected = null, stopped = false;
  const removers = [];
  const listen = (node, name, handler) => {
    node?.addEventListener?.(name, handler);
    removers.push(() => node?.removeEventListener?.(name, handler));
  };
  function fromHash() {
    const hash = win.location?.hash || '';
    if (!/^#[A-Za-z][A-Za-z0-9_.:-]{0,127}$/.test(hash)) return null;
    const id = hash.slice(1);
    if (valid(id)) return {key: id, target: null};
    let target = doc.getElementById(id), ancestor = target;
    while (ancestor) {
      const key = ancestor.getAttribute?.('data-workspace-panel');
      if (valid(key)) return {key, target};
      ancestor = ancestor.parentElement;
    }
    return null;
  }
  function signal(name, detail) {
    const EventType = win.CustomEvent;
    if (typeof EventType === 'function') doc.dispatchEvent?.(new EventType(name, {detail}));
  }
  function activate(key, {focus = false, updateHash = false, target = null, scroll = true} = {}) {
    if (stopped || !valid(key)) return false;
    const previous = selected;
    if (key !== previous) signal('workspace-will-change', {previous, tab: key});
    selected = key;
    for (const [name, {button, panel}] of records) {
      const active = name === key;
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
      panel.hidden = !active;
    }
    nav.setAttribute('data-active-tab', key);
    try { win.sessionStorage?.setItem(STORAGE_KEY, key); } catch {}
    if (updateHash) {
      try { win.history?.replaceState(null, '', `#${key}`); } catch {}
    }
    if (focus) records.get(key).button.focus?.();
    if (target) {
      // A legacy deep link can point into an optional disclosure.
      let ancestor = target.parentElement;
      while (ancestor && ancestor !== records.get(key).panel) {
        if (ancestor.tagName === 'DETAILS') ancestor.open = true;
        ancestor = ancestor.parentElement;
      }
      const reveal = () => { if (!stopped && selected === key) target.scrollIntoView?.({block: 'start'}); };
      if (typeof win.requestAnimationFrame === 'function') win.requestAnimationFrame(reveal);
      else reveal();
    } else if (scroll && previous !== null && key !== previous) {
      records.get(key).panel.scrollIntoView?.({block: 'start'});
    }
    if (key !== previous) signal('workspace-tab-change', {previous, tab: key});
    return true;
  }
  for (const [key, {button}] of records) {
    listen(button, 'click', () => activate(key, {updateHash: true}));
    listen(button, 'keydown', event => {
      if (event.ctrlKey || event.metaKey || event.altKey || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const index = WORKSPACE_TABS.indexOf(key);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? WORKSPACE_TABS.length - 1
        : (index + (event.key === 'ArrowRight' ? 1 : -1) + WORKSPACE_TABS.length) % WORKSPACE_TABS.length;
      activate(WORKSPACE_TABS[next], {focus: true, updateHash: true});
    });
  }
  const hashChange = () => { const value = fromHash(); if (value) activate(value.key, {target: value.target}); };
  listen(win, 'hashchange', hashChange);
  let remembered = null;
  try { remembered = win.sessionStorage?.getItem(STORAGE_KEY); } catch {}
  const initial = fromHash();
  activate(initial?.key || (valid(remembered) ? remembered : 'market'), {target: initial?.target});
  function dispose() { if (stopped) return; stopped = true; for (const remove of removers) remove(); }
  listen(win, 'pagehide', dispose);
  return {activate, getActiveTab: () => selected, dispose};
}
