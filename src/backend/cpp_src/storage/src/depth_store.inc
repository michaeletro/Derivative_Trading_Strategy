// Included after the common store implementation; all operations share its lock,
// sole connection, sticky write-failure state, and verified backup lifecycle.
namespace dts::storage {
namespace {
constexpr const char* depth_session_columns =
 "cast(session_id AS TEXT) AS session_id,cast(run_id AS TEXT) AS run_id,source,"
 "native_id,contract_id,symbol,currency,contract_route,venue,requested_rows,smart_depth,"
 "started_ms,ended_ms,state,cast(last_sequence AS TEXT) AS last_sequence,"
 "cast(event_count AS TEXT) AS event_count";
std::string depth_price_repr(double x) {
    if (std::isnan(x)) return "nan";
    if (!std::isfinite(x)) return x<0 ? "-inf" : "inf";
    std::ostringstream s; s.imbue(std::locale::classic());
    s<<std::setprecision(std::numeric_limits<double>::max_digits10)<<x; return s.str();
}
bool depth_terminal(const std::string& kind) {
    return kind=="stop" || kind=="error" || kind=="gap" || kind=="interrupted";
}
}
std::int64_t TimeSeriesStore::begin_depth(const std::string& source, RequestId id, const DepthSpec& spec) {
    source_check(source); spec.validate();
    if(!id)throw std::invalid_argument("Missing native depth request ID");
    std::lock_guard<std::mutex> lock(impl_->mutex);
    return impl_->write([&] {
        Statement old(impl_->db,"SELECT session_id FROM depth_sessions WHERE run_id=?1 AND state='recording'");
        old.bind(1,impl_->run_id());if(old.step())throw std::logic_error("A depth session already records into this archive");
        Statement q(impl_->db,"INSERT INTO depth_sessions(run_id,source,native_id,contract_id,symbol,currency,contract_route,venue,requested_rows,smart_depth,started_ms,state) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,0,?10,'recording')");
        q.bind(1,impl_->run_id());q.bind(2,source);q.bind(3,std::to_string(id));
        q.bind(4,std::to_string(spec.contract.id));q.bind(5,spec.contract.symbol);
        q.bind(6,spec.contract.currency);q.bind(7,spec.contract.exchange);q.bind(8,spec.venue);
        q.bind(9,static_cast<std::int64_t>(spec.rows));q.bind(10,now_ms());q.step();
        const auto sid=static_cast<std::int64_t>(sqlite3_last_insert_rowid(impl_->db));
        depth_raw_begin(impl_->config.directory,impl_->run_id(),sid,source,id,spec,static_cast<int>(scalar(impl_->db,"PRAGMA user_version")));
        return sid;
    });
}
void TimeSeriesStore::record_depth_events(const std::string& source, const std::vector<BrokerEvent>& events) {
    source_check(source);
    if(std::none_of(events.begin(),events.end(),[](const auto& e){return std::holds_alternative<DepthEvent>(e);}))return;
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->write([&] {
        DepthRawBatch raw;
        for(const auto& value:events) {
            const auto* e=std::get_if<DepthEvent>(&value);if(!e)continue;
            if(!e->request_id || !e->sequence || e->sequence>static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
                throw std::invalid_argument("Invalid local depth sequence");
            for(const auto* text:{&e->kind,&e->origin,&e->size,&e->market_maker})small_text(*text,64);
            if(e->kind!="start" && e->kind!="update" && e->kind!="reset" && !depth_terminal(e->kind))
                throw std::invalid_argument("Unknown depth event kind");
            Statement session(impl_->db,"SELECT session_id,last_sequence,state FROM depth_sessions WHERE run_id=?1 AND source=?2 AND native_id=?3");
            session.bind(1,impl_->run_id());session.bind(2,source);session.bind(3,std::to_string(e->request_id));
            if(!session.step())throw std::logic_error("Depth event has no registered session");
            const auto sid=session.integer();
            if(session.text(2)!="recording" || static_cast<std::int64_t>(e->sequence)<=session.integer(1))
                throw std::logic_error("Out-of-order or post-terminal depth event");
            // Invalid numerical payloads are retained as explicit representations;
            // SQLite must never silently convert a NaN into an ordinary zero.
            Statement q(impl_->db,"INSERT INTO depth_events(session_id,local_sequence,kind,origin,received_unix_us,received_monotonic_ns,operation,side,position,price,price_repr,size,market_maker,smart_depth,code) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15)");
            q.bind(1,sid);q.bind(2,static_cast<std::int64_t>(e->sequence));q.bind(3,e->kind);q.bind(4,e->origin);
            q.bind(5,e->received.unix_us);q.bind(6,e->received.monotonic_ns);
            q.bind(7,static_cast<std::int64_t>(e->operation));q.bind(8,static_cast<std::int64_t>(e->side));q.bind(9,static_cast<std::int64_t>(e->position));
            const bool has_price=e->kind=="update" && e->operation!=2;
            q.bind(10,has_price&&std::isfinite(e->price)?std::optional<double>(e->price):std::nullopt);
            q.bind(11,has_price?depth_price_repr(e->price):"not_applicable");q.bind(12,e->size);q.bind(13,e->market_maker);
            q.bind(14,static_cast<std::int64_t>(e->smart_depth));q.bind(15,static_cast<std::int64_t>(e->code));q.step();
            Statement update(impl_->db,"UPDATE depth_sessions SET last_sequence=?1,event_count=event_count+1,state=?2,ended_ms=?3 WHERE session_id=?4");
            update.bind(1,static_cast<std::int64_t>(e->sequence));update.bind(2,depth_terminal(e->kind)?e->kind:"recording");
            update.bind(3,depth_terminal(e->kind)?std::optional<std::int64_t>(now_ms()):std::nullopt);update.bind(4,sid);update.step();
            raw.append(depth_raw_path(impl_->config.directory,impl_->run_id(),sid),*e);
        }
        // Durably stage raw metadata before the SQLite transaction commits.
        // A failed DB commit can leave a raw tail, explicitly excluded by its
        // authoritative committed sequence watermark.
        raw.flush();
        return 0;
    });
}
void TimeSeriesStore::end_depth_sessions(const std::string& source, const std::string& kind) {
    source_check(source);if(kind!="stop"&&kind!="interrupted")throw std::invalid_argument("Invalid depth end reason");
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->write([&] {
        const auto stamp=DepthStamp::now();
        DepthRawBatch raw;
        Statement sessions(impl_->db,"SELECT session_id,native_id,last_sequence FROM depth_sessions WHERE run_id=?1 AND source=?2 AND state='recording'");
        sessions.bind(1,impl_->run_id());sessions.bind(2,source);
        while(sessions.step()) {
            DepthEvent e;e.request_id=static_cast<RequestId>(std::stoull(sessions.text(1)));
            e.sequence=static_cast<std::uint64_t>(sessions.integer(2)+1);e.kind=kind;e.origin="recorder";e.received=stamp;
            raw.append(depth_raw_path(impl_->config.directory,impl_->run_id(),sessions.integer()),e);
        }
        raw.flush();
        Statement q(impl_->db,"INSERT INTO depth_events(session_id,local_sequence,kind,origin,received_unix_us,received_monotonic_ns,operation,side,position,price,price_repr,size,market_maker,smart_depth,code) SELECT session_id,last_sequence+1,?1,'recorder',?2,?3,-1,-1,-1,NULL,'not_applicable','','',0,0 FROM depth_sessions WHERE run_id=?4 AND source=?5 AND state='recording'");
        q.bind(1,kind);q.bind(2,stamp.unix_us);q.bind(3,stamp.monotonic_ns);q.bind(4,impl_->run_id());q.bind(5,source);q.step();
        Statement u(impl_->db,"UPDATE depth_sessions SET state=?1,last_sequence=last_sequence+1,event_count=event_count+1,ended_ms=?2 WHERE run_id=?3 AND source=?4 AND state='recording'");
        u.bind(1,kind);u.bind(2,now_ms());u.bind(3,impl_->run_id());u.bind(4,source);u.step();return 0;
    });
}
Row TimeSeriesStore::depth_session(std::int64_t id) const {
    if(id<=0)throw std::invalid_argument("Invalid depth session ID");
    std::lock_guard<std::mutex> lock(impl_->mutex);if(!impl_->db)throw std::runtime_error("Store closed");
    const auto text=std::string("SELECT ")+depth_session_columns+" FROM depth_sessions WHERE session_id=?1";
    Statement q(impl_->db,text.c_str());q.bind(1,id);if(!q.step())throw std::out_of_range("Unknown depth session");return q.row();
}
Row TimeSeriesStore::depth_recording(const std::string& source, RequestId request_id) const {
    source_check(source);
    std::lock_guard<std::mutex> lock(impl_->mutex);if(!impl_->db)throw std::runtime_error("Store closed");
    const auto text=std::string("SELECT ")+depth_session_columns+
        " FROM depth_sessions WHERE run_id=?1 AND source=?2"+
        (request_id?" AND native_id=?3":"")+" ORDER BY session_id DESC LIMIT 1";
    Statement q(impl_->db,text.c_str());q.bind(1,impl_->run_id());q.bind(2,source);
    if(request_id)q.bind(3,std::to_string(request_id));
    return q.step()?q.row():Row{};
}
Row TimeSeriesStore::depth_raw_metadata(std::int64_t id) const {
    if(id<=0)throw std::invalid_argument("Invalid depth session ID");
    std::lock_guard<std::mutex> lock(impl_->mutex);if(!impl_->db)throw std::runtime_error("Store closed");
    Statement q(impl_->db,"SELECT run_id,last_sequence FROM depth_sessions WHERE session_id=?1");
    q.bind(1,id);if(!q.step())throw std::out_of_range("Unknown depth session");
    const auto path=depth_raw_path(impl_->config.directory,q.integer(),id);
    auto committed=q.integer(1);
    // Recovery inserts a new DB-only marker at a sequence that an uncommitted
    // raw tail might already use. It must never promote that raw tail.
    Statement terminal(impl_->db,"SELECT origin FROM depth_events WHERE session_id=?1 AND local_sequence=?2");
    terminal.bind(1,id);terminal.bind(2,committed);
    if(terminal.step()&&terminal.text()=="recorder_recovery")--committed;
    struct stat st{};const int result=::lstat(path.c_str(),&st);
    if(result!=0&&errno!=ENOENT)throw std::runtime_error("Raw depth metadata status unavailable");
    const bool present=result==0;
    if(present&&(!S_ISREG(st.st_mode)||st.st_uid!=::geteuid()||(st.st_mode&0077)||st.st_nlink!=1))
        throw std::runtime_error("Invalid private depth metadata file");
    return {{"available",std::int64_t(present)},{"format",std::string("depth-raw-jsonl-v1")},
        {"path",present?Cell(path.string()):Cell(nullptr)},{"bytes",present?Cell(std::to_string(st.st_size)):Cell(nullptr)},
        {"committed_through_sequence",std::to_string(committed)},
        {"uncommitted_tail_possible",std::int64_t(1)},{"sqlite_backup_includes_raw_metadata",std::int64_t(0)}};
}
Page TimeSeriesStore::depth_sessions(std::int64_t after, int limit) const {
    page_check(after,limit);std::lock_guard<std::mutex> lock(impl_->mutex);if(!impl_->db)throw std::runtime_error("Store closed");
    const auto text=std::string("SELECT ")+depth_session_columns+" FROM depth_sessions WHERE session_id>?1 ORDER BY depth_sessions.session_id LIMIT ?2";
    Statement q(impl_->db,text.c_str());q.bind(1,after);q.bind(2,static_cast<std::int64_t>(limit+1));Page p;
    while(q.step()){if(p.rows.size()==static_cast<std::size_t>(limit)){p.has_more=true;break;}p.next_after_id=std::stoll(q.text());p.rows.push_back(q.row());}return p;
}
Page TimeSeriesStore::depth_events(std::int64_t sid, std::int64_t after, std::int64_t through, int limit) const {
    page_check(after,limit);if(sid<=0||through<0)throw std::invalid_argument("Invalid depth cursor");
    std::lock_guard<std::mutex> lock(impl_->mutex);if(!impl_->db)throw std::runtime_error("Store closed");
    Statement exists(impl_->db,"SELECT session_id FROM depth_sessions WHERE session_id=?1");exists.bind(1,sid);if(!exists.step())throw std::out_of_range("Unknown depth session");
    if(through==0){Statement last(impl_->db,"SELECT coalesce(max(event_id),0) FROM depth_events WHERE session_id=?1");last.bind(1,sid);last.step();through=last.integer();}
    Statement q(impl_->db,"SELECT cast(event_id AS TEXT) AS event_id,cast(local_sequence AS TEXT) AS sequence,kind,origin,cast(received_unix_us AS TEXT) AS received_unix_us,cast(received_monotonic_ns AS TEXT) AS received_monotonic_ns,operation,side,position,price,price_repr,size,market_maker,smart_depth,code FROM depth_events WHERE session_id=?1 AND event_id>?2 AND event_id<=?3 ORDER BY depth_events.event_id LIMIT ?4");
    q.bind(1,sid);q.bind(2,after);q.bind(3,through);q.bind(4,static_cast<std::int64_t>(limit+1));Page p;p.through_id=through;
    while(q.step()){if(p.rows.size()==static_cast<std::size_t>(limit)){p.has_more=true;break;}p.next_after_id=std::stoll(q.text());p.rows.push_back(q.row());}return p;
}
} // namespace dts::storage
