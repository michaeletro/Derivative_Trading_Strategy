const positive=/^[1-9][0-9]{0,18}$/;
export function historyURL(series,after='0',through='0') {
  if(!positive.test(series)||!(/^(0|[1-9][0-9]{0,18})$/).test(after)||!(/^(0|[1-9][0-9]{0,18})$/).test(through))throw new Error('Invalid recorded-series cursor');
  const query=new URLSearchParams({series_id:series,after_id:after,through_id:through,limit:'100'});
  return `/api/storage/history?${query}`;
}
export function validatePage(page) {
  if(!page||!Array.isArray(page.rows)||page.rows.length>1000||typeof page.has_more!=='boolean'||page.recorded_not_live!==true)throw new Error('Invalid recorded-history response');
  for(const key of ['next_after_id','through_id'])if(!(/^(0|[1-9][0-9]{0,18})$/).test(page[key]))throw new Error('Invalid history cursor');
  return page;
}
export function columns(kind) {
  return kind==='quote' ? ['observation_id','observed_ms','bid','ask','mid_at_capture','feed','quality'] : ['observation_id','source_time_text','observed_ms','open','high','low','close','volume'];
}
