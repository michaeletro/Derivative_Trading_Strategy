namespace dts::storage {
namespace {
bool numerical_kind(const std::string& k){return k=="option_pricing"||k=="greek_validation"||k=="sde_convergence"||k=="hedging_replication";}
std::string numerical_digest(const NumericalExperiment& e) {
    std::string out="typed-experiment-1\n";
    // Length-delimited fields avoid ambiguous concatenation. IDs are local,
    // and parent/name/time bindings are included in the record integrity hash.
    for(const auto& s:{e.kind,e.name,e.engine_version,e.config_json,e.result_json,std::to_string(e.parent_id),std::to_string(e.created_ms)})
        out+=std::to_string(s.size())+":"+s;
    return research::sha256(out);
}
NumericalExperiment numerical_locked(sqlite3* db,std::int64_t id) {
    Statement q(db,"SELECT experiment_id,kind,coalesce(parent_id,0),name,created_ms,engine_version,config_json,result_json,result_sha256,record_sha256 FROM numerical_experiments WHERE experiment_id=?1");
    q.bind(1,id);if(!q.step())throw std::out_of_range("Unknown numerical experiment");
    NumericalExperiment e;e.id=q.integer(0);e.kind=q.text(1);e.parent_id=q.integer(2);e.name=q.text(3);e.created_ms=q.integer(4);
    e.engine_version=q.text(5);e.config_json=q.text(6);e.result_json=q.text(7);e.result_sha256=q.text(8);e.record_sha256=q.text(9);
    if(!numerical_kind(e.kind)||research::sha256(e.result_json)!=e.result_sha256||numerical_digest(e)!=e.record_sha256)
        throw std::runtime_error("Numerical experiment integrity failed; no automatic repair");
    return e;
}
}
std::int64_t TimeSeriesStore::save_numerical_experiment(const NumericalExperiment& input) {
    NumericalExperiment e=input;
    research_name(e.name);small_text(e.engine_version,80);small_text(e.config_json,8192);small_text(e.result_json,4*1024*1024);
    if(!numerical_kind(e.kind)||e.parent_id<0||e.engine_version.empty()||e.config_json.empty()||e.result_json.empty())
        throw std::invalid_argument("Invalid typed numerical experiment");
    std::lock_guard<std::mutex> lock(impl_->mutex);impl_->healthy();
    if(e.parent_id){const auto parent=numerical_locked(impl_->db,e.parent_id);
        if(parent.kind!=e.kind||parent.config_json!=e.config_json)
            throw std::invalid_argument("A rerun must use the parent's type and canonical configuration");}
    e.created_ms=now_ms();e.result_sha256=research::sha256(e.result_json);e.record_sha256=numerical_digest(e);
    return impl_->write([&]{Statement q(impl_->db,"INSERT INTO numerical_experiments(kind,parent_id,name,created_ms,engine_version,config_json,result_json,result_sha256,record_sha256) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)");
        q.bind(1,e.kind);if(e.parent_id)q.bind(2,e.parent_id);q.bind(3,e.name);q.bind(4,e.created_ms);q.bind(5,e.engine_version);
        q.bind(6,e.config_json);q.bind(7,e.result_json);q.bind(8,e.result_sha256);q.bind(9,e.record_sha256);q.step();
        return static_cast<std::int64_t>(sqlite3_last_insert_rowid(impl_->db));});
}
NumericalExperiment TimeSeriesStore::numerical_experiment(std::int64_t id) const {
    if(id<=0)throw std::invalid_argument("Positive experiment identifier required");
    std::lock_guard<std::mutex> lock(impl_->mutex);impl_->healthy();return numerical_locked(impl_->db,id);
}
TypedPage TimeSeriesStore::typed_experiment_catalog(const std::string& kind,const std::string& after,int limit) const {
    if(!kind.empty()&&kind!="return_volatility"&&!numerical_kind(kind))throw std::invalid_argument("Unknown experiment kind");
    if(limit<1||limit>100)throw std::invalid_argument("Catalog limit must be 1..100");
    std::string after_kind;std::int64_t after_id=0;
    if(!after.empty()) {
        const auto colon=after.find(':');if(colon==std::string::npos)throw std::invalid_argument("Invalid typed catalog cursor");
        after_kind=after.substr(0,colon);const auto text=after.substr(colon+1);
        if((!numerical_kind(after_kind)&&after_kind!="return_volatility")||text.empty()||text.size()>18||text.front()=='0'||text.find_first_not_of("0123456789")!=std::string::npos)
            throw std::invalid_argument("Invalid typed catalog cursor");
        after_id=std::stoll(text);
    }
    std::lock_guard<std::mutex> lock(impl_->mutex);impl_->healthy();
    Statement q(impl_->db,"SELECT kind,reference,name,created_ms,engine_version,parent_reference,result_sha256 FROM typed_experiment_catalog WHERE (?1='' OR kind=?1) AND (kind>?2 OR (kind=?2 AND experiment_id>?3)) ORDER BY kind,experiment_id LIMIT ?4");
    q.bind(1,kind);q.bind(2,after_kind);q.bind(3,after_id);q.bind(4,std::int64_t(limit+1));TypedPage p;
    while(q.step()){if(p.rows.size()==std::size_t(limit)){p.has_more=true;break;}p.next_reference=q.text(1);p.rows.push_back(q.row());}return p;
}
} // namespace dts::storage
