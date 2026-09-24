#include <dts/time_series_store.hpp>
#include <sqlite3.h>
#include <dts/history_schema.hpp>
#include <dts/research_schema.hpp>
#include <dts/numerical_schema.hpp>
#include <dts/hedging_schema.hpp>
#include <dts/depth_schema.hpp>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <fcntl.h>
#include <iomanip>
#include <limits>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

namespace dts::storage {
namespace {
namespace fs = std::filesystem;
constexpr int application_id = 1146377044; // DTST: never adopt an unrelated database.
std::int64_t now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}
std::string environment(const char* key) { const auto* s = std::getenv(key); return s ? s : ""; }
void check(int rc, sqlite3* db) {
    if (rc != SQLITE_OK && rc != SQLITE_DONE && rc != SQLITE_ROW)
        throw std::runtime_error("SQLite operation failed (code " + std::to_string(db ? sqlite3_extended_errcode(db) : rc) + ")");
}
void sql(sqlite3* db, const char* s) { check(sqlite3_exec(db, s, nullptr, nullptr, nullptr), db); }
class Statement {
    sqlite3* db_;
public:
    sqlite3_stmt* p = nullptr;
    Statement(sqlite3* db, const char* text) : db_(db) { check(sqlite3_prepare_v2(db, text, -1, &p, nullptr), db); }
    ~Statement() { sqlite3_finalize(p); }
    Statement(const Statement&) = delete;
    Statement& operator=(const Statement&) = delete;
    void bind(int i, std::int64_t v) { check(sqlite3_bind_int64(p, i, v), db_); }
    void bind(int i, const std::string& v) { check(sqlite3_bind_text(p, i, v.c_str(), static_cast<int>(v.size()), SQLITE_TRANSIENT), db_); }
    void bind(int i, double v) { check(sqlite3_bind_double(p, i, v), db_); }
    void bind(int i, const std::optional<double>& v) { if(v) bind(i,*v); else check(sqlite3_bind_null(p,i),db_); }
    void bind(int i, const std::optional<std::int64_t>& v) { if(v) bind(i,*v); else check(sqlite3_bind_null(p,i),db_); }
    bool step() { const int rc=sqlite3_step(p); check(rc,db_); return rc==SQLITE_ROW; }
    std::int64_t integer(int i=0) const { return sqlite3_column_int64(p,i); }
    std::string text(int i=0) const {
        const auto* s=sqlite3_column_text(p,i);
        return s ? std::string(reinterpret_cast<const char*>(s),sqlite3_column_bytes(p,i)) : "";
    }
    Row row() const {
        Row out;
        for(int i=0;i<sqlite3_column_count(p);++i) {
            Cell v=nullptr;
            switch(sqlite3_column_type(p,i)) {
                case SQLITE_INTEGER: v=integer(i); break;
                case SQLITE_FLOAT: v=sqlite3_column_double(p,i); break;
                case SQLITE_TEXT: v=text(i); break;
                case SQLITE_NULL: break;
                default: throw std::runtime_error("Unexpected binary value in history");
            }
            out[sqlite3_column_name(p,i)]=std::move(v);
        }
        return out;
    }
};
std::int64_t scalar(sqlite3* db,const char* s) { Statement q(db,s); q.step(); return q.integer(); }
std::string scalar_text(sqlite3* db,const char* s) { Statement q(db,s); q.step(); return q.text(); }
class Transaction {
    sqlite3* db_; bool done_=false;
public:
    explicit Transaction(sqlite3* db):db_(db){sql(db_,"BEGIN IMMEDIATE");}
    ~Transaction(){if(!done_) sqlite3_exec(db_,"ROLLBACK",nullptr,nullptr,nullptr);}
    void commit(){sql(db_,"COMMIT");done_=true;}
};
void sync_path(const fs::path& p,bool directory=false) {
    const int fd=::open(p.c_str(),O_RDONLY|O_CLOEXEC|(directory?O_DIRECTORY:0)|O_NOFOLLOW);
    if(fd<0) throw std::runtime_error("Cannot open storage path for synchronization");
    const int rc=::fsync(fd); ::close(fd);
    if(rc!=0) throw std::runtime_error("Storage fsync failed");
}
void private_directory(const fs::path& p) {
    if(!p.is_absolute()) throw std::invalid_argument("Storage paths must be absolute (use $HOME, not a literal ~)");
    const bool created=fs::create_directories(p);
    if(fs::is_symlink(p)||!fs::is_directory(p)) throw std::invalid_argument("Storage directory must be a real local directory");
    if(created) fs::permissions(p,fs::perms::owner_all,fs::perm_options::replace);
    struct stat st{};
    if(::stat(p.c_str(),&st)!=0||st.st_uid!=::geteuid()||(st.st_mode&0077)!=0)
        throw std::invalid_argument("Storage directories must be owned by this user with mode 0700");
}
void small_text(const std::string& s,std::size_t max=256) {
    if(s.size()>max||s.find('\0')!=std::string::npos) throw std::invalid_argument("Invalid storage metadata");
}
void source_check(const std::string& source) {
    if(source!="ibkr_tws"&&source!="mock") throw std::invalid_argument("Unknown broker recording source");
}
std::string feed(MarketDataType t) {
    switch(t) {
        case MarketDataType::Realtime:return "realtime";
        case MarketDataType::Delayed:return "delayed";
        case MarketDataType::Frozen:return "frozen";
        case MarketDataType::DelayedFrozen:return "delayed_frozen";
        case MarketDataType::Simulation:return "simulation";
    }
    throw std::invalid_argument("Invalid quote feed type");
}
void page_check(std::int64_t after,int limit) {
    if(after<0||limit<1||limit>1000) throw std::invalid_argument("Invalid history cursor or limit (1..1000)");
}
std::string encode(const std::string& s) { return std::to_string(s.size())+":"+s; }
std::string bar_key(const Bar& b) {
    // Exact canonical content, not a lossy hash. Preserve revisions, deduplicate
    // repeated identical source rows. Unknown NULLs remain distinct from zeros.
    std::ostringstream s; s.imbue(std::locale::classic());
    s<<encode(b.source_row_id)<<encode(b.time_text);
    for(const auto& v:{b.open,b.high,b.low,b.close}) {
        if(v) {if(!std::isfinite(*v)) throw std::invalid_argument("Nonfinite historical bar");s<<'V'<<std::hexfloat<<*v<<';';}
        else s<<"N;";
    }
    s<<(b.volume?"V"+std::to_string(*b.volume):"N");return s.str();
}
}
Config Config::from_environment() {
    Config c; const auto explicit_dir=environment("DTS_DATA_DIR");
    if(!explicit_dir.empty()) c.directory=explicit_dir;
    else {
        const auto xdg=environment("XDG_DATA_HOME"),home=environment("HOME");
        if(!xdg.empty()&&fs::path(xdg).is_absolute()) c.directory=fs::path(xdg)/"derivative-lab";
        else if(!home.empty()&&fs::path(home).is_absolute()) c.directory=fs::path(home)/".local/share/derivative-lab";
        else throw std::invalid_argument("Set absolute DTS_DATA_DIR; HOME is unavailable");
    }
    c.directory=c.directory.lexically_normal();
    const auto backup=environment("DTS_BACKUP_DIR");c.backup_directory=backup.empty()?c.directory/"backups":fs::path(backup);
    const auto seconds=environment("DTS_BACKUP_TIMEOUT_SECONDS");
    if(!seconds.empty()) {
        std::size_t used=0;const auto v=std::stol(seconds,&used);
        if(used!=seconds.size()||v<1||v>3600)throw std::invalid_argument("Backup timeout must be 1..3600 seconds");
        c.backup_timeout_ms=static_cast<int>(v*1000);
    }
    const auto assets=environment("DB_PATH").empty()?"quant_data.db":environment("DB_PATH");
    if(fs::weakly_canonical(assets)==fs::weakly_canonical(c.directory/"timeseries.sqlite3"))
        throw std::invalid_argument("DB_PATH is the read-only asset source, not the recording database");
    return c;
}
struct TimeSeriesStore::Impl {
    Config config;
    fs::path path;
    sqlite3* db=nullptr;
    int lock_fd=-1;
    mutable std::mutex mutex;
    Status info;
    std::map<std::pair<std::string,ContractId>,std::int64_t> contract_series;
    explicit Impl(Config c,const std::string& mode):config(std::move(c)),path(config.directory/"timeseries.sqlite3") {
        if(config.backup_directory.empty()) config.backup_directory=config.directory/"backups";
        if(config.backup_timeout_ms<1)throw std::invalid_argument("Invalid backup timeout");
        private_directory(config.directory);private_directory(config.backup_directory);
        lock_fd=::open((config.directory/"recorder.lock").c_str(),O_CREAT|O_RDWR|O_CLOEXEC|O_NOFOLLOW,0600);
        if(lock_fd<0)throw std::runtime_error("Cannot open recorder lock");
        if(::flock(lock_fd,LOCK_EX|LOCK_NB)!=0) {::close(lock_fd);lock_fd=-1;throw std::runtime_error("Recording database already in use; stop that instance or choose a separate DTS_DATA_DIR");}
        try {
            if(fs::exists(path)) {
                struct stat st{};
                if(::lstat(path.c_str(),&st)!=0||!S_ISREG(st.st_mode)||st.st_uid!=::geteuid()||(st.st_mode&0077)!=0)
                    throw std::runtime_error("Existing recording file must be regular, user-owned and mode 0600");
            } else {
                const int fd=::open(path.c_str(),O_CREAT|O_EXCL|O_WRONLY|O_CLOEXEC|O_NOFOLLOW,0600);
                if(fd<0)throw std::runtime_error("Cannot create recording database");
                ::close(fd);
            }
            check(sqlite3_open_v2(path.c_str(),&db,SQLITE_OPEN_READWRITE|SQLITE_OPEN_FULLMUTEX,nullptr),db);
            sqlite3_extended_result_codes(db,1);sqlite3_busy_timeout(db,1000);
            const auto appid=scalar(db,"PRAGMA application_id"),version=scalar(db,"PRAGMA user_version");
            const bool empty=scalar(db,"SELECT count(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")==0;
            if(!((appid==0&&version==0&&empty)||(appid==application_id&&(version==1||version==2||version==3||version==4||version==5||version==6))))
                throw std::runtime_error("Unknown recording schema: existing file was not adopted or reset");
            sql(db,"PRAGMA trusted_schema=OFF; PRAGMA foreign_keys=ON;");
            // Single connection in exclusive mode: no cross-process WAL writers
            // or checkpoint races, including on older system SQLite builds.
            sql(db,"PRAGMA locking_mode=EXCLUSIVE");
            if(scalar_text(db,"PRAGMA journal_mode=WAL")!="wal")throw std::runtime_error("WAL mode unavailable on this filesystem");
            sql(db,"PRAGMA synchronous=FULL; PRAGMA wal_autocheckpoint=1000;");
            if(empty) {
                Transaction t(db);
                sql(db,R"SQL(
CREATE TABLE runs(run_id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT NOT NULL, started_ms INTEGER NOT NULL, stopped_ms INTEGER, state TEXT NOT NULL);
CREATE TABLE series(series_id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, kind TEXT NOT NULL, series_key TEXT NOT NULL,
 symbol TEXT NOT NULL, contract_id INTEGER, exchange TEXT, currency TEXT, security_type TEXT, multiplier REAL,
 expiry TEXT, strike REAL, option_right TEXT, exercise_style TEXT, row_count INTEGER NOT NULL DEFAULT 0,
 first_observed_ms INTEGER, last_observed_ms INTEGER, UNIQUE(source,kind,series_key));
CREATE TABLE quote_observations(observation_id INTEGER PRIMARY KEY AUTOINCREMENT, series_id INTEGER NOT NULL REFERENCES series, run_id INTEGER NOT NULL REFERENCES runs,
 observed_ms INTEGER NOT NULL, source_time_ms INTEGER, feed TEXT NOT NULL, bid REAL, ask REAL, bid_age_ms INTEGER, ask_age_ms INTEGER,
 mid_at_capture REAL, quality TEXT NOT NULL);
CREATE INDEX quotes_series_cursor ON quote_observations(series_id,observation_id);
CREATE INDEX quotes_series_time ON quote_observations(series_id,observed_ms,observation_id);
CREATE TABLE bar_observations(observation_id INTEGER PRIMARY KEY AUTOINCREMENT, series_id INTEGER NOT NULL REFERENCES series, run_id INTEGER NOT NULL REFERENCES runs,
 observed_ms INTEGER NOT NULL, source_row_id TEXT NOT NULL, source_time_text TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL, volume INTEGER,
 content_key TEXT NOT NULL, UNIQUE(series_id,content_key));
CREATE INDEX bars_series_cursor ON bar_observations(series_id,observation_id);
CREATE INDEX bars_series_time ON bar_observations(series_id,observed_ms,observation_id);
CREATE TABLE requests(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES runs, observed_ms INTEGER NOT NULL,
 source TEXT NOT NULL, kind TEXT NOT NULL, external_id TEXT NOT NULL, subject TEXT NOT NULL);
CREATE TABLE asset_reads(read_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES runs, observed_ms INTEGER NOT NULL,
 dataset TEXT NOT NULL, ticker TEXT NOT NULL, start_filter TEXT NOT NULL, end_filter TEXT NOT NULL, requested_limit INTEGER NOT NULL, returned_count INTEGER NOT NULL);
CREATE TABLE asset_read_rows(read_id INTEGER NOT NULL REFERENCES asset_reads, ordinal INTEGER NOT NULL, observation_id INTEGER NOT NULL REFERENCES bar_observations,
 PRIMARY KEY(read_id,ordinal));
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
PRAGMA application_id=1146377044;
PRAGMA user_version=1;
)SQL");t.commit();
            }
            if(scalar_text(db,"PRAGMA quick_check")!="ok")throw std::runtime_error("Recording database integrity check failed; restore separately, never overwrite automatically");
            if (version<6) {
                // Refuse an upgrade unless an existing v1 archive can be backed up.
                // New empty databases need no pre-migration backup.
                if(!empty) { info.run_id="migration"; backup_locked(); }
                Transaction migration(db);
                if(version<2) sql(db,history_schema);
                if(version<3) sql(db,research_schema);
                if(version<4) sql(db,numerical_schema);
                if(version<5) sql(db,hedging_schema);
                sql(db,depth_schema);
                {Statement check_fk(db,"PRAGMA foreign_key_check");if(check_fk.step())throw std::runtime_error("Migration foreign-key check failed");}
                migration.commit();
            }
            Transaction t(db);
            // Record a terminal boundary after a crash; this is not an IBKR event
            // and does not claim to know the number of in-flight observations lost.
            sql(db,R"SQL(INSERT INTO depth_events(session_id,local_sequence,kind,origin,received_unix_us,received_monotonic_ns,operation,side,position,price,price_repr,size,market_maker,smart_depth,code)
 SELECT session_id,last_sequence+1,'interrupted','recorder_recovery',strftime('%s','now')*1000000,0,-1,-1,-1,NULL,'not_applicable','','',0,-1031
 FROM depth_sessions WHERE state='recording';
 UPDATE depth_sessions SET state='interrupted',last_sequence=last_sequence+1,event_count=event_count+1,ended_ms=strftime('%s','now')*1000 WHERE state='recording';)SQL");
            sql(db,"UPDATE history_requests SET state='interrupted',finished_ms=strftime('%s','now')*1000 WHERE state IN ('queued','pending')");
            sql(db,"UPDATE runs SET state='interrupted' WHERE state='running'");
            Statement run(db,"INSERT INTO runs(mode,started_ms,state) VALUES(?1,?2,'running')");
            run.bind(1,mode);run.bind(2,now_ms());run.step();info.run_id=std::to_string(sqlite3_last_insert_rowid(db));
            t.commit();
            info.open=true;info.database=path.string();info.backup_directory=config.backup_directory.string();
            Statement last(db,"SELECT value FROM metadata WHERE key='last_backup'");if(last.step())info.last_backup=last.text();
            info.last_commit_ms=now_ms();sync_path(config.directory,true);
        } catch (...) {emergency_close();throw;}
    }
    ~Impl(){emergency_close();}
    void emergency_close() noexcept {
        if(db){sqlite3_close_v2(db);db=nullptr;}
        if(lock_fd>=0){::flock(lock_fd,LOCK_UN);::close(lock_fd);lock_fd=-1;}
        info.open=false;
    }
    void healthy()const{if(!db||info.failed)throw std::runtime_error("Recording unavailable; restart after resolving the storage error");}
    template<class F> auto write(F fn) {
        healthy();
        try {Transaction t(db);auto result=fn();t.commit();info.last_commit_ms=now_ms();return result;}
        catch(...){info.failed=true;info.last_error="Recording transaction failed; data acquisition must stop. Committed history retained.";throw;}
    }
    std::int64_t run_id()const{return std::stoll(info.run_id);}
    std::int64_t series(const std::string& source,const std::string& kind,const std::string& key,const std::string& symbol) {
        Statement insert(db,"INSERT OR IGNORE INTO series(source,kind,series_key,symbol) VALUES(?1,?2,?3,?4)");
        insert.bind(1,source);insert.bind(2,kind);insert.bind(3,key);insert.bind(4,symbol);insert.step();
        Statement find(db,"SELECT series_id FROM series WHERE source=?1 AND kind=?2 AND series_key=?3");
        find.bind(1,source);find.bind(2,kind);find.bind(3,key);find.step();return find.integer();
    }
    std::int64_t instrument(const std::string& source,const Contract& c) {
        c.validate();for(const auto* s:{&c.symbol,&c.exchange,&c.currency})small_text(*s,64);
        // Keep metadata variants distinct: a changed multiplier/route must not
        // retroactively relabel previously recorded observations.
        std::ostringstream key;key.imbue(std::locale::classic());
        key<<c.id<<encode(c.exchange)<<encode(c.currency)<<encode(c.symbol)<<std::hexfloat<<c.multiplier;
        if(c.option)key<<encode(c.option->expiry)<<c.option->strike<<static_cast<int>(c.option->right)<<static_cast<int>(c.option->exercise_style);
        const auto id=series(source,"quote",key.str(),c.symbol);
        Statement q(db,"UPDATE series SET contract_id=?1,exchange=?2,currency=?3,security_type=?4,multiplier=?5,expiry=?6,strike=?7,option_right=?8,exercise_style=?9 WHERE series_id=?10");
        q.bind(1,c.id);q.bind(2,c.exchange);q.bind(3,c.currency);q.bind(4,std::string(c.option?"OPT":"STK"));q.bind(5,c.multiplier);
        q.bind(6,c.option?c.option->expiry:"");q.bind(7,c.option?std::optional<double>(c.option->strike):std::nullopt);
        q.bind(8,std::string(c.option?(c.option->right==OptionRight::Call?"C":"P"):""));
        q.bind(9,std::string(!c.option||c.option->exercise_style==ExerciseStyle::Unknown?"unknown":c.option->exercise_style==ExerciseStyle::European?"european":"american"));
        q.bind(10,id);q.step();return id;
    }
    void bump(std::int64_t id,std::int64_t stamp) {
        Statement q(db,"UPDATE series SET row_count=row_count+1,first_observed_ms=CASE WHEN first_observed_ms IS NULL THEN ?1 ELSE min(first_observed_ms,?1) END,last_observed_ms=CASE WHEN last_observed_ms IS NULL THEN ?1 ELSE max(last_observed_ms,?1) END WHERE series_id=?2");
        q.bind(1,stamp);q.bind(2,id);q.step();
    }
    void note(const std::string& source,const std::string& kind,RequestId external,const std::string& subject) {
        Statement q(db,"INSERT INTO requests(run_id,observed_ms,source,kind,external_id,subject) VALUES(?1,?2,?3,?4,?5,?6)");
        q.bind(1,run_id());q.bind(2,now_ms());q.bind(3,source);q.bind(4,kind);q.bind(5,std::to_string(external));q.bind(6,subject);q.step();
    }
    std::string backup_locked() {
        if(!db)throw std::runtime_error("Recording store closed");
        sqlite3* destination=nullptr;sqlite3_backup* handle=nullptr;fs::path temp;
        try {
            private_directory(config.backup_directory);
            std::string pattern=(config.backup_directory/("dts-"+std::to_string(now_ms())+"-run"+info.run_id+"-XXXXXX.sqlite.partial")).string();
            std::vector<char> name(pattern.begin(),pattern.end());name.push_back('\0');
            const int fd=::mkstemps(name.data(),15); // suffix: .sqlite.partial
            if(fd<0)throw std::runtime_error("Cannot create backup temporary file");
            ::close(fd);temp=name.data();
            check(sqlite3_open_v2(temp.c_str(),&destination,SQLITE_OPEN_READWRITE|SQLITE_OPEN_FULLMUTEX,nullptr),destination);
            sqlite3_busy_timeout(destination,250);sql(destination,"PRAGMA synchronous=FULL");
            handle=sqlite3_backup_init(destination,"main",db,"main");if(!handle)throw std::runtime_error("Cannot start SQLite backup");
            const auto deadline=Clock::now()+std::chrono::milliseconds(config.backup_timeout_ms);
            int rc=SQLITE_OK;
            do {
                rc=sqlite3_backup_step(handle,128);
                if(rc==SQLITE_BUSY||rc==SQLITE_LOCKED)sqlite3_sleep(10);
                else if(rc!=SQLITE_OK&&rc!=SQLITE_DONE)check(rc,destination);
                if(Clock::now()>deadline)throw std::runtime_error("Backup deadline exceeded; no complete backup published");
            } while(rc!=SQLITE_DONE);
            const int finish=sqlite3_backup_finish(handle);handle=nullptr;check(finish,destination);
            // A self-contained rollback-mode snapshot; do not copy a live DB/WAL.
            sql(destination,"PRAGMA journal_mode=DELETE");
            if(scalar_text(destination,"PRAGMA quick_check")!="ok")throw std::runtime_error("Backup integrity check failed");
            check(sqlite3_close(destination),destination);destination=nullptr;
            sync_path(temp);
            const fs::path final=temp.string().substr(0,temp.string().size()-8); // remove .partial
            fs::rename(temp,final);temp.clear();sync_path(config.backup_directory,true);
            info.last_backup=final.string();
            Statement last(db,"INSERT INTO metadata(key,value) VALUES('last_backup',?1) ON CONFLICT(key) DO UPDATE SET value=excluded.value");
            last.bind(1,info.last_backup);last.step();info.last_commit_ms=now_ms();return info.last_backup;
        } catch(...) {
            if(handle)sqlite3_backup_finish(handle);
            if(destination)sqlite3_close_v2(destination);
            if(!temp.empty()) {std::error_code ec;fs::remove(temp,ec);fs::remove(temp.string()+"-journal",ec);fs::remove(temp.string()+"-wal",ec);fs::remove(temp.string()+"-shm",ec);}
            info.last_error="Backup failed; existing committed database retained. Check free space, paths and permissions.";
            throw;
        }
    }
};
TimeSeriesStore::TimeSeriesStore(Config c,std::string mode):impl_(std::make_unique<Impl>(std::move(c),mode)){}
TimeSeriesStore::~TimeSeriesStore()=default;
void TimeSeriesStore::require_healthy()const{std::lock_guard<std::mutex> l(impl_->mutex);impl_->healthy();}
void TimeSeriesStore::request(const std::string& source,const std::string& kind,RequestId id,const std::string& subject) {
    source_check(source);small_text(kind,64);small_text(subject);
    std::lock_guard<std::mutex> l(impl_->mutex);impl_->write([&]{impl_->note(source,kind,id,subject);return 0;});
}
void TimeSeriesStore::register_contract(const std::string& source,const Contract& c) {
    source_check(source);std::lock_guard<std::mutex> l(impl_->mutex);
    const auto id=impl_->write([&]{return impl_->instrument(source,c);});
    impl_->contract_series[{source,c.id}]=id;
}
void TimeSeriesStore::record_events(const std::string& source,const std::vector<BrokerEvent>& events) {
    if(events.empty())return;
    source_check(source);std::lock_guard<std::mutex> l(impl_->mutex);
    // Do not write position/account payloads. Already-registered subscription
    // metadata remains authoritative if an unrelated contract search overlaps.
    const bool useful=std::any_of(events.begin(),events.end(),[](const auto& e){return std::holds_alternative<Quote>(e)||std::holds_alternative<ConnectionEvent>(e)||std::holds_alternative<BrokerError>(e);});
    if(!useful)return;
    impl_->write([&]{
        const auto stamp=now_ms();const auto steady=Clock::now();
        for(const auto& e:events) {
            if(const auto* quote=std::get_if<Quote>(&e)) {
                const auto it=impl_->contract_series.find({source,quote->contract_id});
                if(it==impl_->contract_series.end())throw std::runtime_error("Quote arrived without subscription metadata");
                Statement q(impl_->db,"INSERT INTO quote_observations(series_id,run_id,observed_ms,source_time_ms,feed,bid,ask,bid_age_ms,ask_age_ms,mid_at_capture,quality) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)");
                q.bind(1,it->second);q.bind(2,impl_->run_id());q.bind(3,stamp);
                std::optional<std::int64_t> exchange;
                if(quote->exchange_time)exchange=std::chrono::duration_cast<std::chrono::milliseconds>(quote->exchange_time->time_since_epoch()).count();
                q.bind(4,exchange);q.bind(5,feed(quote->data_type));
                for(const auto& item:{std::pair<int,const std::optional<QuoteSide>*>(6,&quote->bid),{7,&quote->ask}}) {
                    std::optional<double> price;std::optional<std::int64_t> age;
                    if(*item.second) {
                        const auto& side=**item.second;
                        if(!std::isfinite(side.price))throw std::runtime_error("Nonfinite normalized quote");
                        price=side.price;age=std::chrono::duration_cast<std::chrono::milliseconds>(steady-side.received_at).count();
                    }
                    q.bind(item.first,price);q.bind(item.first+2,age);
                }
                const auto mid=quote->mid(steady,std::chrono::seconds(5));q.bind(10,mid);
                std::string quality=mid?"indicative_at_capture":!quote->bid||!quote->ask?"missing_side":quote->bid->price>quote->ask->price?"crossed":"invalid_or_stale_at_capture";
                q.bind(11,quality);q.step();impl_->bump(it->second,stamp);
            } else if(const auto* c=std::get_if<ConnectionEvent>(&e))impl_->note(source,"connection_state",0,std::to_string(static_cast<int>(c->state)));
            else if(const auto* b=std::get_if<BrokerError>(&e))impl_->note(source,"broker_error_code",b->request_id,std::to_string(b->code));
        }
        return 0;
    });
}
RecordedRead TimeSeriesStore::record_asset_read(const AssetRead& read,const std::vector<Bar>& bars) {
    small_text(read.dataset,4096);small_text(read.ticker,64);small_text(read.start,32);small_text(read.end,32);
    if(bars.size()>5000||read.limit<1||read.limit>5000)throw std::invalid_argument("Asset response too large");
    std::lock_guard<std::mutex> l(impl_->mutex);
    return impl_->write([&]{
        const auto stamp=now_ms();Statement r(impl_->db,"INSERT INTO asset_reads(run_id,observed_ms,dataset,ticker,start_filter,end_filter,requested_limit,returned_count) VALUES(?1,?2,?3,?4,?5,?6,?7,?8)");
        r.bind(1,impl_->run_id());r.bind(2,stamp);r.bind(3,read.dataset);r.bind(4,read.ticker);r.bind(5,read.start);r.bind(6,read.end);
        r.bind(7,static_cast<std::int64_t>(read.limit));r.bind(8,static_cast<std::int64_t>(bars.size()));r.step();
        RecordedRead result{sqlite3_last_insert_rowid(impl_->db),0};std::int64_t ordinal=0;
        for(const auto& b:bars) {
            small_text(b.symbol,64);small_text(b.source_row_id,64);small_text(b.time_text,128);
            const auto key=bar_key(b);
            const auto series=impl_->series("legacy_asset_db","bar",encode(read.dataset)+encode(b.symbol),b.symbol);
            Statement q(impl_->db,"INSERT OR IGNORE INTO bar_observations(series_id,run_id,observed_ms,source_row_id,source_time_text,open,high,low,close,volume,content_key) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)");
            q.bind(1,series);q.bind(2,impl_->run_id());q.bind(3,stamp);q.bind(4,b.source_row_id);q.bind(5,b.time_text);
            q.bind(6,b.open);q.bind(7,b.high);q.bind(8,b.low);q.bind(9,b.close);q.bind(10,b.volume);q.bind(11,key);q.step();
            if(sqlite3_changes(impl_->db)>0){++result.inserted;impl_->bump(series,stamp);}
            Statement find(impl_->db,"SELECT observation_id FROM bar_observations WHERE series_id=?1 AND content_key=?2");
            find.bind(1,series);find.bind(2,key);find.step();
            Statement member(impl_->db,"INSERT INTO asset_read_rows VALUES(?1,?2,?3)");member.bind(1,result.id);member.bind(2,ordinal++);member.bind(3,find.integer());member.step();
        }
        return result;
    });
}
Status TimeSeriesStore::status()const {
    std::lock_guard<std::mutex> l(impl_->mutex);Status out=impl_->info;if(!impl_->db)return out;
    out.quotes=scalar(impl_->db,"SELECT coalesce(sum(row_count),0) FROM series WHERE kind='quote'");
    out.bars=scalar(impl_->db,"SELECT coalesce(sum(row_count),0) FROM series WHERE kind='bar'");
    out.interrupted_runs=scalar(impl_->db,"SELECT count(*) FROM runs WHERE state='interrupted'");return out;
}
Page TimeSeriesStore::catalog(std::int64_t after,int limit)const {
    page_check(after,limit);std::lock_guard<std::mutex> l(impl_->mutex);if(!impl_->db)throw std::runtime_error("Store closed");
    Statement q(impl_->db,"SELECT cast(series_id AS TEXT) AS series_id,source,kind,symbol,cast(contract_id AS TEXT) AS contract_id,exchange,currency,security_type,multiplier,expiry,strike,option_right,exercise_style,cast(row_count AS TEXT) AS row_count,first_observed_ms,last_observed_ms FROM series WHERE series_id>?1 ORDER BY series_id LIMIT ?2");
    q.bind(1,after);q.bind(2,static_cast<std::int64_t>(limit+1));Page p;
    while(q.step()){if(p.rows.size()==static_cast<std::size_t>(limit)){p.has_more=true;break;}p.next_after_id=std::stoll(q.text());p.rows.push_back(q.row());}
    return p;
}
Page TimeSeriesStore::history(std::int64_t series_id,std::int64_t after,std::int64_t through,int limit,std::int64_t from,std::int64_t to)const {
    page_check(after,limit);if(series_id<=0||through<0||from<0||to<from||to>253402300799999LL)throw std::invalid_argument("Invalid history selection");
    std::lock_guard<std::mutex> l(impl_->mutex);if(!impl_->db)throw std::runtime_error("Store closed");
    Statement kind(impl_->db,"SELECT kind FROM series WHERE series_id=?1");kind.bind(1,series_id);
    if(!kind.step())throw std::out_of_range("Unknown recorded series");
    const bool quote=kind.text()=="quote";const std::string table=quote?"quote_observations":"bar_observations";
    if(through==0){Statement last(impl_->db,("SELECT coalesce(max(observation_id),0) FROM "+table+" WHERE series_id=?1").c_str());last.bind(1,series_id);last.step();through=last.integer();}
    const std::string cols=quote?"source_time_ms,feed,bid,ask,bid_age_ms,ask_age_ms,mid_at_capture,quality":"source_row_id,source_time_text,open,high,low,close,volume";
    const auto query="SELECT cast(observation_id AS TEXT) AS observation_id,cast(run_id AS TEXT) AS run_id,observed_ms,"+cols+" FROM "+table+" WHERE series_id=?1 AND observation_id>?2 AND observation_id<=?3 AND observed_ms>=?4 AND observed_ms<=?5 ORDER BY observation_id LIMIT ?6";
    Statement q(impl_->db,query.c_str());q.bind(1,series_id);q.bind(2,after);q.bind(3,through);q.bind(4,from);q.bind(5,to);q.bind(6,static_cast<std::int64_t>(limit+1));
    Page p;p.through_id=through;
    while(q.step()){if(p.rows.size()==static_cast<std::size_t>(limit)){p.has_more=true;break;}p.next_after_id=std::stoll(q.text());p.rows.push_back(q.row());}
    return p;
}
std::string TimeSeriesStore::backup(){std::lock_guard<std::mutex> l(impl_->mutex);return impl_->backup_locked();}
void TimeSeriesStore::close(bool acquisition_clean) {
    if(impl_->db && !impl_->info.failed) {
        try { for(const auto* source:{"ibkr_tws","mock"}) end_depth_sessions(source, acquisition_clean?"stop":"interrupted"); }
        catch(...) { acquisition_clean=false; }
    }
    std::lock_guard<std::mutex> l(impl_->mutex);if(!impl_->db)return;
    std::string error;
    try {
        sql(impl_->db,"UPDATE history_requests SET state='interrupted',finished_ms=strftime('%s','now')*1000 WHERE state IN ('queued','pending')");
        Statement run(impl_->db,"UPDATE runs SET state=?1,stopped_ms=?2 WHERE run_id=?3");
        run.bind(1,std::string(acquisition_clean&&!impl_->info.failed?"clean":"recording_incomplete"));run.bind(2,now_ms());run.bind(3,impl_->run_id());run.step();
        impl_->backup_locked();
        check(sqlite3_wal_checkpoint_v2(impl_->db,nullptr,SQLITE_CHECKPOINT_TRUNCATE,nullptr,nullptr),impl_->db);
        if(impl_->info.failed)error="Recording failed during this session; backup contains committed history only";
    } catch(const std::exception& e){error=std::string("Storage shutdown incomplete: ")+e.what();}
    const int rc=sqlite3_close(impl_->db);
    if(rc==SQLITE_OK)impl_->db=nullptr;else error="SQLite close failed";
    impl_->emergency_close();
    if(!error.empty())throw std::runtime_error(error);
}
} // namespace dts::storage

#include "history_store.inc"

#include "research_store.inc"

#include "numerical_store.inc"

#include "depth_store.inc"
