// Included by time_series_store.cpp: share its sole connection, mutex and WAL
// ownership. Never open a second writer/checkpointer on this archive.
namespace dts::storage {
namespace {
HistorySpec history_spec_locked(sqlite3* db,std::int64_t id) {
    Statement q(db,"SELECT contract_id,symbol,exchange,currency,bar_size,price_type,use_rth FROM history_datasets WHERE dataset_id=?1");
    q.bind(1,id);if(!q.step())throw std::out_of_range("Unknown historical dataset");
    HistorySpec s;s.contract.id=q.integer(0);s.contract.symbol=q.text(1);s.contract.exchange=q.text(2);
    s.contract.currency=q.text(3);s.contract.multiplier=1;s.bar_size=q.text(4);s.price_type=q.text(5);s.use_rth=q.integer(6)!=0;
    return s;
}
std::string historical_key(const HistoricalBar& b) {
    std::ostringstream out;out.imbue(std::locale::classic());out<<std::hexfloat;
    for(double v:{b.open,b.high,b.low,b.close})out<<v<<';';
    out<<encode(b.volume.value_or("NULL"))<<';';if(b.wap)out<<*b.wap;else out<<"NULL";
    out<<';';if(b.count)out<<*b.count;else out<<"NULL";return out.str();
}
}
std::int64_t TimeSeriesStore::historical_dataset(const HistorySpec& s) {
    s.validate();std::lock_guard<std::mutex> lock(impl_->mutex);
    const auto key=std::to_string(s.contract.id)+encode(s.contract.exchange)+encode(s.contract.currency)+encode(s.contract.symbol)+encode(s.bar_size)+encode(s.price_type)+(s.use_rth?"R":"A");
    return impl_->write([&]{
        Statement q(impl_->db,"INSERT OR IGNORE INTO history_datasets(source,identity,contract_id,symbol,exchange,currency,bar_size,price_type,use_rth,time_basis,adjustment_policy) VALUES('ibkr_tws_historical',?1,?2,?3,?4,?5,?6,?7,?8,?9,'provider_native_not_normalized')");
        q.bind(1,key);q.bind(2,s.contract.id);q.bind(3,s.contract.symbol);q.bind(4,s.contract.exchange);q.bind(5,s.contract.currency);
        q.bind(6,s.bar_size);q.bind(7,s.price_type);q.bind(8,std::int64_t(s.use_rth));
        q.bind(9,std::string(s.bar_size=="1 day"?"provider_session_date":"UTC_epoch_seconds"));q.step();
        Statement f(impl_->db,"SELECT dataset_id FROM history_datasets WHERE identity=?1");f.bind(1,key);f.step();return f.integer();
    });
}
HistorySpec TimeSeriesStore::historical_spec(std::int64_t id)const {
    std::lock_guard<std::mutex> lock(impl_->mutex);impl_->healthy();return history_spec_locked(impl_->db,id);
}
std::vector<Row> TimeSeriesStore::historical_catalog()const {
    std::lock_guard<std::mutex> lock(impl_->mutex);impl_->healthy();
    Statement q(impl_->db,"SELECT cast(dataset_id AS TEXT) AS dataset_id,source,contract_id,symbol,exchange,currency,bar_size,price_type,use_rth,time_basis,adjustment_policy FROM history_datasets ORDER BY dataset_id DESC LIMIT 200");
    std::vector<Row> out;while(q.step())out.push_back(q.row());return out;
}
std::vector<HistoryWindow> TimeSeriesStore::historical_gaps(std::int64_t id,HistoryWindow w,bool pending)const {
    std::lock_guard<std::mutex> lock(impl_->mutex);impl_->healthy();history_spec_locked(impl_->db,id).validate(w);
    Statement q(impl_->db,"SELECT start_s,end_s FROM history_requests WHERE dataset_id=?1 AND end_s>?2 AND start_s<?3 AND (state IN ('complete','empty') OR (?4=1 AND state IN ('queued','pending'))) ORDER BY start_s");
    q.bind(1,id);q.bind(2,w.start);q.bind(3,w.end);q.bind(4,std::int64_t(pending));std::vector<HistoryWindow> covered;
    while(q.step())covered.push_back({q.integer(0),q.integer(1)});
    return uncovered(w,covered);
}
std::vector<std::int64_t> TimeSeriesStore::queue_history(std::int64_t id,const std::vector<HistoryWindow>& windows) {
    const auto spec=historical_spec(id);for(auto w:windows)spec.validate(w);
    if(windows.size()>32)throw std::length_error("Historical queue limit is 32 chunks");
    std::lock_guard<std::mutex> lock(impl_->mutex);impl_->healthy();
    if(scalar(impl_->db,"SELECT count(*) FROM history_requests WHERE state IN ('queued','pending')")+windows.size()>32)
        throw std::length_error("Historical request queue full");
    return impl_->write([&]{std::vector<std::int64_t> ids;
        for(auto w:windows){Statement q(impl_->db,"INSERT INTO history_requests(dataset_id,run_id,start_s,end_s,state,created_ms) VALUES(?1,?2,?3,?4,'queued',?5)");
            q.bind(1,id);q.bind(2,impl_->run_id());q.bind(3,w.start);q.bind(4,w.end);q.bind(5,now_ms());q.step();ids.push_back(sqlite3_last_insert_rowid(impl_->db));}
        return ids;
    });
}
void TimeSeriesStore::bind_history(std::int64_t id,RequestId native) {
    if(!native||native>2147483647)throw std::invalid_argument("Invalid native historical ID");
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->write([&]{Statement q(impl_->db,"UPDATE history_requests SET state='pending',native_id=?1 WHERE request_id=?2 AND run_id=?3 AND state='queued'");
        q.bind(1,static_cast<std::int64_t>(native));q.bind(2,id);q.bind(3,impl_->run_id());q.step();
        if(sqlite3_changes(impl_->db)!=1)throw std::runtime_error("Historical request binding failed");
        Statement stamp(impl_->db,"INSERT INTO metadata(key,value) VALUES('history_last_dispatch_ms',?1) ON CONFLICT(key) DO UPDATE SET value=excluded.value");stamp.bind(1,std::to_string(now_ms()));stamp.step();return 0;});
}
std::int64_t TimeSeriesStore::recent_history_dispatch_ms()const {
    std::lock_guard<std::mutex> lock(impl_->mutex);impl_->healthy();
    Statement q(impl_->db,"SELECT value FROM metadata WHERE key='history_last_dispatch_ms'");return q.step()?std::stoll(q.text()):0;
}
void TimeSeriesStore::finish_history(std::int64_t id,const std::string& state,int code) {
    if(state!="failed"&&state!="interrupted"&&state!="unavailable")throw std::invalid_argument("Invalid terminal history state");
    std::lock_guard<std::mutex> lock(impl_->mutex);impl_->write([&]{
        Statement q(impl_->db,"UPDATE history_requests SET state=?1,code=?2,finished_ms=?3 WHERE request_id=?4 AND state IN ('queued','pending')");
        q.bind(1,state);q.bind(2,std::int64_t(code));q.bind(3,now_ms());q.bind(4,id);q.step();return 0;});
}
std::string TimeSeriesStore::history_state(std::int64_t id)const {
    std::lock_guard<std::mutex> lock(impl_->mutex);impl_->healthy();Statement q(impl_->db,"SELECT state FROM history_requests WHERE request_id=?1");
    q.bind(1,id);if(!q.step())throw std::out_of_range("Unknown historical request");return q.text();
}
void TimeSeriesStore::interrupt_history() {
    std::lock_guard<std::mutex> lock(impl_->mutex);impl_->write([&]{
        Statement q(impl_->db,"UPDATE history_requests SET state='interrupted',finished_ms=?1 WHERE state IN ('queued','pending')");q.bind(1,now_ms());q.step();return 0;});
}
void TimeSeriesStore::record_history_events(const std::vector<BrokerEvent>& events) {
    bool relevant=false;for(const auto& e:events)if(std::holds_alternative<HistoricalBarEvent>(e)||std::holds_alternative<HistoricalEnd>(e)||std::holds_alternative<ConnectionEvent>(e))relevant=true;
    if(!relevant)return;
    std::lock_guard<std::mutex> lock(impl_->mutex);impl_->write([&]{
        for(const auto& e:events){
            if(const auto* c=std::get_if<ConnectionEvent>(&e)) {
                if(c->state==ConnectionState::Failed||c->state==ConnectionState::Disconnected)
                    sql(impl_->db,"UPDATE history_requests SET state='interrupted',finished_ms=strftime('%s','now')*1000 WHERE state IN ('queued','pending')");
                continue;
            }
            const auto* bar=std::get_if<HistoricalBarEvent>(&e);const auto* end=std::get_if<HistoricalEnd>(&e);
            if(!bar&&!end)continue;
            const auto native=bar?bar->request_id:end->request_id;
            Statement request(impl_->db,"SELECT request_id,dataset_id,start_s,end_s FROM history_requests WHERE run_id=?1 AND native_id=?2 AND state='pending'");
            request.bind(1,impl_->run_id());request.bind(2,static_cast<std::int64_t>(native));if(!request.step())continue;
            const auto rid=request.integer(0),did=request.integer(1);
            if(bar){const auto spec=history_spec_locked(impl_->db,did);const auto coordinate=bar_coordinate(spec,bar->bar);
                if(coordinate<request.integer(2)||coordinate>=request.integer(3))continue;
                const auto& b=bar->bar;const auto key=historical_key(b);
                Statement v(impl_->db,"INSERT OR IGNORE INTO history_versions(dataset_id,coordinate_s,source_time,open,high,low,close,volume,wap,bar_count,content_key,observed_ms) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12)");
                v.bind(1,did);v.bind(2,coordinate);v.bind(3,b.time);v.bind(4,b.open);v.bind(5,b.high);v.bind(6,b.low);v.bind(7,b.close);
                if(b.volume)v.bind(8,*b.volume);
                v.bind(9,b.wap);
                if(b.count)v.bind(10,std::int64_t(*b.count));
                v.bind(11,key);v.bind(12,now_ms());v.step();
                Statement f(impl_->db,"SELECT version_id FROM history_versions WHERE dataset_id=?1 AND coordinate_s=?2 AND content_key=?3");f.bind(1,did);f.bind(2,coordinate);f.bind(3,key);f.step();
                Statement m(impl_->db,"INSERT INTO history_membership VALUES(?1,?2,?3) ON CONFLICT(request_id,coordinate_s) DO UPDATE SET version_id=excluded.version_id");
                m.bind(1,rid);m.bind(2,coordinate);m.bind(3,f.integer());m.step();
            }else{
                std::string state=end->status;
                if(state=="complete"){
                    Statement count(impl_->db,"SELECT count(*) FROM history_membership WHERE request_id=?1");count.bind(1,rid);count.step();if(count.integer()==0)state="empty";
                }else if(state!="failed"&&state!="unavailable"&&state!="interrupted")throw std::runtime_error("Unknown historical completion status");
                Statement done(impl_->db,"UPDATE history_requests SET state=?1,code=?2,finished_ms=?3,provider_start=?4,provider_end=?5 WHERE request_id=?6");
                done.bind(1,state);done.bind(2,std::int64_t(end->code));done.bind(3,now_ms());done.bind(4,end->provider_start.substr(0,64));done.bind(5,end->provider_end.substr(0,64));done.bind(6,rid);done.step();
            }
        }return 0;
    });
}
std::vector<Row> TimeSeriesStore::historical_requests(std::int64_t id,HistoryWindow w)const {
    std::lock_guard<std::mutex> lock(impl_->mutex);impl_->healthy();history_spec_locked(impl_->db,id).validate(w);
    Statement q(impl_->db,"SELECT cast(r.request_id AS TEXT) AS request_id,start_s,end_s,state,created_ms,finished_ms,code,provider_start,provider_end,(SELECT count(*) FROM history_membership m WHERE m.request_id=r.request_id) AS received_rows FROM history_requests r WHERE dataset_id=?1 AND end_s>?2 AND start_s<?3 ORDER BY r.request_id DESC LIMIT 200");
    q.bind(1,id);q.bind(2,w.start);q.bind(3,w.end);std::vector<Row> out;while(q.step())out.push_back(q.row());return out;
}
std::vector<Row> TimeSeriesStore::historical_bars(std::int64_t id,HistoryWindow w)const {
    std::lock_guard<std::mutex> lock(impl_->mutex);impl_->healthy();history_spec_locked(impl_->db,id).validate(w);
    // Latest COMPLETED RESPONSE covering a coordinate wins, not max version ID.
    // A->B->A revisions and empty refreshed responses therefore behave correctly.
    // Failed/partial responses never grant cache coverage or replace valid history.
    Statement q(impl_->db,R"SQL(SELECT cast(v.version_id AS TEXT) AS version_id,cast(r.request_id AS TEXT) AS request_id,
 v.coordinate_s,v.source_time,v.open,v.high,v.low,v.close,v.volume,v.wap,v.bar_count,v.observed_ms,r.finished_ms AS received_ms,
 (SELECT count(*) FROM history_versions z WHERE z.dataset_id=v.dataset_id AND z.coordinate_s=v.coordinate_s) AS revisions
 FROM history_versions v JOIN history_membership m ON v.version_id=m.version_id
 JOIN history_requests r ON r.request_id=m.request_id
 WHERE v.dataset_id=?1 AND v.coordinate_s>=?2 AND v.coordinate_s<?3
 AND r.state IN ('complete','empty') AND r.request_id=(SELECT max(q.request_id) FROM history_requests q
 WHERE q.dataset_id=?1 AND q.start_s<=v.coordinate_s AND q.end_s>v.coordinate_s AND q.state IN ('complete','empty'))
 ORDER BY v.coordinate_s LIMIT 2001)SQL");
    q.bind(1,id);q.bind(2,w.start);q.bind(3,w.end);std::vector<Row> out;while(q.step())out.push_back(q.row());
    if(out.size()>2000)throw std::length_error("Historical result limit exceeded; narrow the range");
    return out;
}
} // namespace dts::storage
