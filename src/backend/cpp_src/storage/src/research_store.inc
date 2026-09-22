// Uses the existing sole SQLite connection/lock. No competing checkpoint writer.
namespace dts::storage {
namespace {
void research_name(const std::string& name) {
    if(name.empty()||name.size()>80||std::all_of(name.begin(),name.end(),[](unsigned char c){return c==' ';})||
       std::any_of(name.begin(),name.end(),[](unsigned char c){return c<32||c==127;}))
        throw std::invalid_argument("Research names must contain 1..80 printable bytes");
}
research::Observation observation(const Statement& q, int offset=0) {
    research::Observation r;
    r.version_id=q.integer(offset);r.request_id=q.integer(offset+1);r.coordinate_s=q.integer(offset+2);r.bar.time=q.text(offset+3);
    r.bar.open=sqlite3_column_double(q.p,offset+4);r.bar.high=sqlite3_column_double(q.p,offset+5);
    r.bar.low=sqlite3_column_double(q.p,offset+6);r.bar.close=sqlite3_column_double(q.p,offset+7);
    if(sqlite3_column_type(q.p,offset+8)!=SQLITE_NULL)r.bar.volume=q.text(offset+8);
    if(sqlite3_column_type(q.p,offset+9)!=SQLITE_NULL)r.bar.wap=sqlite3_column_double(q.p,offset+9);
    if(sqlite3_column_type(q.p,offset+10)!=SQLITE_NULL)r.bar.count=static_cast<int>(q.integer(offset+10));
    r.observed_ms=q.integer(offset+11);r.response_finished_ms=q.integer(offset+12);return r;
}
research::Snapshot snapshot_locked(sqlite3* db,std::int64_t id) {
    Statement q(db,"SELECT snapshot_id,dataset_id,name,created_ms,start_s,end_s,request_cutoff,bar_count,fingerprint,source,contract_id,symbol,exchange,currency,bar_size,price_type,use_rth,time_basis,adjustment_policy FROM research_snapshots WHERE snapshot_id=?1 AND sealed=1");
    q.bind(1,id);if(!q.step())throw std::out_of_range("Unknown research snapshot");
    research::Snapshot s;s.id=q.integer(0);s.dataset_id=q.integer(1);s.name=q.text(2);s.created_ms=q.integer(3);
    s.window={q.integer(4),q.integer(5)};s.request_cutoff=q.integer(6);const auto expected=q.integer(7);s.fingerprint=q.text(8);s.source=q.text(9);
    s.spec.contract.id=q.integer(10);s.spec.contract.symbol=q.text(11);s.spec.contract.exchange=q.text(12);s.spec.contract.currency=q.text(13);s.spec.contract.multiplier=1;
    s.spec.bar_size=q.text(14);s.spec.price_type=q.text(15);s.spec.use_rth=q.integer(16)!=0;s.time_basis=q.text(17);s.adjustment_policy=q.text(18);
    Statement bars(db,"SELECT version_id,request_id,coordinate_s,source_time,open,high,low,close,volume,wap,bar_count,observed_ms,response_finished_ms FROM research_snapshot_bars WHERE snapshot_id=?1 ORDER BY ordinal LIMIT 2001");
    bars.bind(1,id);while(bars.step())s.observations.push_back(observation(bars));
    Statement gaps(db,"SELECT start_s,end_s FROM research_snapshot_gaps WHERE snapshot_id=?1 ORDER BY ordinal LIMIT 2001");
    gaps.bind(1,id);while(gaps.step())s.uncovered_intervals.push_back({gaps.integer(0),gaps.integer(1)});
    if(expected!=static_cast<std::int64_t>(s.observations.size())||research::snapshot_fingerprint(s)!=s.fingerprint)
        throw std::runtime_error("Snapshot integrity check failed; no automatic repair performed");
    return s;
}
Experiment experiment_locked(sqlite3* db,std::int64_t id) {
    Statement q(db,"SELECT experiment_id,snapshot_id,coalesce(parent_id,0),name,created_ms,engine_version,config_json,result_json,result_sha256 FROM research_experiments WHERE experiment_id=?1");
    q.bind(1,id);if(!q.step())throw std::out_of_range("Unknown saved experiment");
    Experiment e;e.id=q.integer(0);e.snapshot_id=q.integer(1);e.parent_id=q.integer(2);e.name=q.text(3);e.created_ms=q.integer(4);e.engine_version=q.text(5);
    e.config_json=q.text(6);e.result_json=q.text(7);e.result_sha256=q.text(8);
    if(research::sha256(e.result_json)!=e.result_sha256)throw std::runtime_error("Experiment result integrity check failed");
    return e;
}
}
research::Snapshot TimeSeriesStore::create_snapshot(std::int64_t dataset_id,HistoryWindow w,const std::string& name) {
    research_name(name);std::lock_guard<std::mutex> lock(impl_->mutex);impl_->healthy();
    research::Snapshot s;s.dataset_id=dataset_id;s.name=name;s.window=w;s.spec=history_spec_locked(impl_->db,dataset_id);s.spec.validate(w);s.created_ms=now_ms();
    Statement meta(impl_->db,"SELECT source,time_basis,adjustment_policy FROM history_datasets WHERE dataset_id=?1");meta.bind(1,dataset_id);meta.step();
    s.source=meta.text(0);s.time_basis=meta.text(1);s.adjustment_policy=meta.text(2);
    s.request_cutoff=scalar(impl_->db,"SELECT coalesce(max(request_id),0) FROM history_requests");
    // The recorder's mutex/exclusive connection makes cutoff, rows and coverage
    // one coherent selection. Pending refreshes do not displace completed data.
    Statement q(impl_->db,R"SQL(SELECT v.version_id,r.request_id,v.coordinate_s,v.source_time,v.open,v.high,v.low,v.close,v.volume,v.wap,v.bar_count,v.observed_ms,r.finished_ms
 FROM history_versions v JOIN history_membership m ON v.version_id=m.version_id JOIN history_requests r ON r.request_id=m.request_id
 WHERE v.dataset_id=?1 AND v.coordinate_s>=?2 AND v.coordinate_s<?3 AND r.state IN ('complete','empty')
 AND r.request_id=(SELECT max(c.request_id) FROM history_requests c WHERE c.dataset_id=?1 AND c.request_id<=?4 AND c.start_s<=v.coordinate_s AND c.end_s>v.coordinate_s AND c.state IN ('complete','empty'))
 ORDER BY v.coordinate_s LIMIT 2001)SQL");
    q.bind(1,dataset_id);q.bind(2,w.start);q.bind(3,w.end);q.bind(4,s.request_cutoff);
    while(q.step())s.observations.push_back(observation(q));
    if(s.observations.empty())throw std::invalid_argument("No completed saved bars in this window; no snapshot created");
    if(s.observations.size()>research::maximum_bars)throw std::invalid_argument("Snapshot limit is 2000 bars; narrow the window");
    Statement coverage(impl_->db,"SELECT start_s,end_s FROM history_requests WHERE dataset_id=?1 AND request_id<=?4 AND start_s<?3 AND end_s>?2 AND state IN ('complete','empty') ORDER BY start_s LIMIT 20001");
    coverage.bind(1,dataset_id);coverage.bind(2,w.start);coverage.bind(3,w.end);coverage.bind(4,s.request_cutoff);
    std::vector<HistoryWindow> covered;while(coverage.step())covered.push_back({coverage.integer(0),coverage.integer(1)});
    if(covered.size()>20000)throw std::length_error("Too many response revisions for a bounded snapshot; narrow the range");
    s.uncovered_intervals=uncovered(w,covered);s.fingerprint=research::snapshot_fingerprint(s);
    return impl_->write([&]{
        Statement h(impl_->db,"INSERT INTO research_snapshots(dataset_id,name,created_ms,start_s,end_s,request_cutoff,bar_count,fingerprint,source,contract_id,symbol,exchange,currency,bar_size,price_type,use_rth,time_basis,adjustment_policy) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18)");
        h.bind(1,dataset_id);h.bind(2,name);h.bind(3,s.created_ms);h.bind(4,w.start);h.bind(5,w.end);h.bind(6,s.request_cutoff);h.bind(7,static_cast<std::int64_t>(s.observations.size()));h.bind(8,s.fingerprint);
        h.bind(9,s.source);h.bind(10,s.spec.contract.id);h.bind(11,s.spec.contract.symbol);h.bind(12,s.spec.contract.exchange);h.bind(13,s.spec.contract.currency);h.bind(14,s.spec.bar_size);h.bind(15,s.spec.price_type);h.bind(16,std::int64_t(s.spec.use_rth));h.bind(17,s.time_basis);h.bind(18,s.adjustment_policy);h.step();s.id=sqlite3_last_insert_rowid(impl_->db);
        std::int64_t ordinal=0;
        for(const auto& b:s.observations) {
            Statement row(impl_->db,"INSERT INTO research_snapshot_bars VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15)");
            row.bind(1,s.id);row.bind(2,++ordinal);row.bind(3,b.version_id);row.bind(4,b.request_id);row.bind(5,b.coordinate_s);row.bind(6,b.bar.time);
            row.bind(7,b.bar.open);row.bind(8,b.bar.high);row.bind(9,b.bar.low);row.bind(10,b.bar.close);if(b.bar.volume)row.bind(11,*b.bar.volume);
            row.bind(12,b.bar.wap);if(b.bar.count)row.bind(13,std::int64_t(*b.bar.count));row.bind(14,b.observed_ms);row.bind(15,b.response_finished_ms);row.step();
        }
        ordinal=0;for(auto g:s.uncovered_intervals){Statement gap(impl_->db,"INSERT INTO research_snapshot_gaps VALUES(?1,?2,?3,?4)");gap.bind(1,s.id);gap.bind(2,++ordinal);gap.bind(3,g.start);gap.bind(4,g.end);gap.step();}
        Statement seal(impl_->db,"UPDATE research_snapshots SET sealed=1 WHERE snapshot_id=?1");seal.bind(1,s.id);seal.step();return s;
    });
}
research::Snapshot TimeSeriesStore::snapshot(std::int64_t id) const {
    std::lock_guard<std::mutex> lock(impl_->mutex);impl_->healthy();return snapshot_locked(impl_->db,id);
}
Page TimeSeriesStore::snapshot_catalog(std::int64_t after,int limit) const {
    page_check(after,limit);std::lock_guard<std::mutex> lock(impl_->mutex);impl_->healthy();
    Statement q(impl_->db,"SELECT cast(snapshot_id AS TEXT) AS snapshot_id,name,symbol,bar_size,price_type,created_ms,bar_count,fingerprint FROM research_snapshots WHERE snapshot_id>?1 AND sealed=1 ORDER BY snapshot_id LIMIT ?2");
    q.bind(1,after);q.bind(2,std::int64_t(limit+1));Page p;
    while(q.step()){if(p.rows.size()==std::size_t(limit)){p.has_more=true;break;}p.next_after_id=std::stoll(q.text());p.rows.push_back(q.row());}return p;
}
std::int64_t TimeSeriesStore::save_experiment(const Experiment& e) {
    research_name(e.name);small_text(e.engine_version,80);small_text(e.config_json,4096);small_text(e.result_json,4*1024*1024);
    if(e.engine_version!=research::engine_version||e.result_json.empty()||e.config_json.empty()||e.parent_id<0)throw std::invalid_argument("Invalid research experiment record");
    std::lock_guard<std::mutex> lock(impl_->mutex);impl_->healthy();snapshot_locked(impl_->db,e.snapshot_id);
    if(e.parent_id){const auto parent=experiment_locked(impl_->db,e.parent_id);if(parent.snapshot_id!=e.snapshot_id)throw std::invalid_argument("A rerun must use its parent's frozen snapshot");}
    const auto digest=research::sha256(e.result_json);
    return impl_->write([&]{Statement q(impl_->db,"INSERT INTO research_experiments(snapshot_id,parent_id,name,created_ms,engine_version,config_json,result_json,result_sha256) VALUES(?1,?2,?3,?4,?5,?6,?7,?8)");
        q.bind(1,e.snapshot_id);if(e.parent_id)q.bind(2,e.parent_id);q.bind(3,e.name);q.bind(4,now_ms());q.bind(5,e.engine_version);q.bind(6,e.config_json);q.bind(7,e.result_json);q.bind(8,digest);q.step();return static_cast<std::int64_t>(sqlite3_last_insert_rowid(impl_->db));});
}
Experiment TimeSeriesStore::experiment(std::int64_t id) const {
    std::lock_guard<std::mutex> lock(impl_->mutex);impl_->healthy();return experiment_locked(impl_->db,id);
}
Page TimeSeriesStore::experiment_catalog(std::int64_t after,int limit) const {
    page_check(after,limit);std::lock_guard<std::mutex> lock(impl_->mutex);impl_->healthy();
    Statement q(impl_->db,"SELECT cast(experiment_id AS TEXT) AS experiment_id,cast(snapshot_id AS TEXT) AS snapshot_id,cast(parent_id AS TEXT) AS parent_id,name,created_ms,engine_version,result_sha256 FROM research_experiments WHERE experiment_id>?1 ORDER BY experiment_id LIMIT ?2");
    q.bind(1,after);q.bind(2,std::int64_t(limit+1));Page p;
    while(q.step()){if(p.rows.size()==std::size_t(limit)){p.has_more=true;break;}p.next_after_id=std::stoll(q.text());p.rows.push_back(q.row());}return p;
}
} // namespace dts::storage
