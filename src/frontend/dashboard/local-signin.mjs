// Only the one-use launch ticket reaches this module. The saved profile token
// stays in the local launcher. Session IDs are HttpOnly cookies, never JS values.
export function takeLaunchCode(location, history) {
  const fragment=location.hash;
  if (!fragment.startsWith('#local-signin=')) return null;
  // Erase even malformed/expired tickets before issuing a request. Fragments
  // are not sent in the HTTP request target; do not move them into a query.
  history.replaceState(null,'',`${location.pathname}${location.search}#overview`);
  const match=/^#local-signin=([a-f0-9]{64})$/.exec(fragment);
  if (!match) throw new Error('Invalid local sign-in link. Reopen the dashboard using your saved profile.');
  return match[1];
}
export function validSessionStatus(body) {
  return body && !Array.isArray(body) && body.schema_version===1 && typeof body.authenticated==='boolean';
}
export const reopenMessage='Open automatically with: python3 tools/open_dashboard.py --profile paper-tws. The local token is not embedded in this page.';
