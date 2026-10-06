// Routes select existing DOM workspaces only. This module never calls an API.
export const routes = Object.freeze({
  orderbook: {workspace:'live', title:'Live collection', description:'Inspect broker-delivered depth and explicitly record your research sample.', section:'orderbook', orderbookTab:'live'},
  recordings: {workspace:'recordings', title:'Recordings & replay', description:'Reopen saved depth, inspect original events and select recordings for research.', section:'orderbook', orderbookTab:'recordings'},
  quality: {workspace:'quality', title:'Data quality', description:'Check collection health, timing and saved measurement eligibility before modeling.', section:'orderbook', orderbookTab:'quality'},
  research: {workspace:'research', title:'Research experiments', description:'Prepare measurements and run reproducible analyses on explicitly selected recordings.', section:'orderbook', orderbookTab:'recordings'},
  results: {workspace:'results', title:'Results & exports', description:'Reopen saved research, inspect its evidence and download verified outputs.', section:'orderbook', orderbookTab:'results'},
  instruments: {workspace:'instruments', title:'Instruments & quotes', description:'Resolve a contract explicitly, inspect quotes, or select historical data.', section:'market-workspace'},
  positions: {workspace:'positions', title:'Position snapshot', description:'Inspect a requested broker snapshot. Holdings are not a continuously reconciled stream.', section:'positions'},
  history: {workspace:'history', title:'Historical prices & volumes', description:'Request daily or intraday bars and reopen saved datasets.', section:'history'},
  ticks: {workspace:'ticks', title:'Historical ticks', description:'Download and replay trades or best quotes. These are not historical depth ladders.', section:'ticks'},
  storage: {workspace:'storage', title:'Local data & backups', description:'Inspect durable observations and create a backup when the broker is disconnected.', section:'storage'},
  replay: {workspace:'replay', title:'Historical bar replay', description:'Freeze saved bars and inspect retrospective return and volatility diagnostics.', section:'replay'},
  variation: {workspace:'variation', title:'Variation research pilot', description:'Explore existing RV/BPV diagnostics from saved observations.', section:'variation'},
  pricing: {workspace:'pricing', title:'Pricing lab', description:'Run manual European-option model experiments independently of market data.', section:'pricing'},
  sensitivities: {workspace:'sensitivities', title:'Greeks & scenarios', description:'Validate model sensitivities and reprice manual scenarios.', section:'sensitivities'},
  sde: {workspace:'sde', title:'SDE discretization', description:'Compare numerical approximations using explicitly simulated paths.', section:'sde'},
  hedging: {workspace:'hedging', title:'Delta hedging', description:'Study synthetic replication experiments and their assumptions.', section:'hedging'},
  'lab-catalog': {workspace:'lab-catalog', title:'Saved numerical experiments', description:'Inspect, compare and rerun immutable numerical experiments.', section:'lab-catalog'},
  roadmap: {workspace:'roadmap', title:'Help & study roadmap', description:'Understand the numerical laboratories and their research boundaries.', section:'roadmap'},
});
const aliases=Object.freeze({overview:'orderbook',live:'orderbook'});
export function resolveRoute(fragment) {
  if(typeof fragment!=='string'||fragment.startsWith('#local-signin='))return null;
  const key=fragment.replace(/^#/,'')||'orderbook';
  const route=Object.hasOwn(aliases,key)?aliases[key]:Object.hasOwn(routes,key)?key:'orderbook';
  return {route,...routes[route]};
}

export function mountNavigation(win=window,doc=document) {
  const $=id=>doc.getElementById(id), sidebar=$('workspace-sidebar'), toggle=$('workspace-menu');
  let current=null;
  function closeMenu(returnFocus=false) {
    sidebar.classList.remove('is-open');toggle.setAttribute('aria-expanded','false');
    if(returnFocus)toggle.focus();
  }
  function render({focus=false}={}) {
    const next=resolveRoute(win.location.hash);if(!next)return;
    current=next;doc.body.dataset.workspace=next.workspace;
    for(const section of doc.querySelectorAll('[data-workspace-section]'))section.hidden=section.id!==next.section;
    $('workspace-title').textContent=next.title;$('workspace-description').textContent=next.description;
    doc.title=`${next.title} · Derivative Lab`;
    for(const link of doc.querySelectorAll('[data-workspace-link]')) {
      const selected=link.getAttribute('href')===`#${next.route}`;
      link.classList.toggle('active',selected);
      if(selected){link.setAttribute('aria-current','page');const group=link.closest('details');if(group)group.open=true;}
      else link.removeAttribute('aria-current');
    }
    closeMenu();
    win.dispatchEvent(new CustomEvent('dts:workspacechange',{detail:next}));
    if(focus){$('workspace-title').focus({preventScroll:true});win.scrollTo({top:0,behavior:'instant'});}
  }
  function navigate(route) {
    if(typeof route!=='string'||!Object.hasOwn(routes,route))return false;
    if(win.location.hash===`#${route}`)render({focus:true});else win.location.hash=route;
    return true;
  }
  toggle.addEventListener('click',()=>{
    const open=!sidebar.classList.contains('is-open');sidebar.classList.toggle('is-open',open);toggle.setAttribute('aria-expanded',String(open));
    if(open)sidebar.querySelector('[aria-current="page"]')?.focus();
  });
  $('workspace-menu-close').addEventListener('click',()=>closeMenu(true));
  doc.addEventListener('keydown',event=>{if(event.key==='Escape'&&sidebar.classList.contains('is-open')){event.preventDefault();closeMenu(true);}});
  doc.addEventListener('click',event=>{
    if(event.target.closest?.('.skip')){event.preventDefault();$('workspace-title').focus();return;}
    const link=event.target.closest?.('[data-workspace-link]');
    if(link&&!event.ctrlKey&&!event.metaKey&&!event.shiftKey&&!event.altKey){event.preventDefault();navigate(link.getAttribute('href').slice(1));}
  });
  win.addEventListener('hashchange',()=>render({focus:true}));
  win.addEventListener('popstate',()=>render({focus:true}));
  win.addEventListener('dts:navigate',event=>navigate(event.detail?.route));
  render();
  return {refresh:()=>render(),navigate,current:()=>current};
}
