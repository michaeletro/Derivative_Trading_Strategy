#include <algorithm>
#include <dts/time_series_store.hpp>
#include <dts/recording_broker.hpp>
#include <dts/mock_broker.hpp>
#include <dts/backfill_schema.hpp>
#include <dts/depth_schema.hpp>
#include <sqlite3.h>
#include <unistd.h>
#include <sys/wait.h>
#include <csignal>
#include <iostream>
#include <thread>
#include <atomic>
#include <fstream>

using namespace dts;
using namespace dts::storage;
namespace fs=std::filesystem;
#define CHECK(x) do {if(!(x))throw std::runtime_error("check failed: " #x);}while(false)
struct Temp {
    fs::path path;
    Temp(){char name[]="/tmp/dts-storage-XXXXXX";auto p=mkdtemp(name);if(!p)throw std::runtime_error("mkdtemp");path=p;}
    ~Temp(){std::error_code ec;fs::remove_all(path,ec);}
    Config config()const{return {path/"data",path/"backups",3000};}
};
template<class F>void rejects(F f){bool failed=false;try{f();}catch(const std::exception&){failed=true;}CHECK(failed);}
Contract contract(){return {9001,"SYNTHETIC","TEST","USD",SecurityType::Equity,1.0,std::nullopt};}
Quote quote(){Quote q;q.contract_id=9001;q.bid=QuoteSide{99.0,Clock::now()};q.ask=QuoteSide{101.0,Clock::now()};q.data_type=MarketDataType::Simulation;return q;}
std::int64_t scalar(const fs::path& path,const char* query){
    sqlite3* db=nullptr;CHECK(sqlite3_open_v2(path.c_str(),&db,SQLITE_OPEN_READONLY,nullptr)==SQLITE_OK);
    sqlite3_stmt* s=nullptr;CHECK(sqlite3_prepare_v2(db,query,-1,&s,nullptr)==SQLITE_OK);CHECK(sqlite3_step(s)==SQLITE_ROW);
    auto n=sqlite3_column_int64(s,0);sqlite3_finalize(s);CHECK(sqlite3_close(db)==SQLITE_OK);return n;
}
void execute(const fs::path& path,const char* query){sqlite3* db=nullptr;CHECK(sqlite3_open(path.c_str(),&db)==SQLITE_OK);CHECK(sqlite3_exec(db,query,nullptr,nullptr,nullptr)==SQLITE_OK);CHECK(sqlite3_close(db)==SQLITE_OK);}
std::string dump_query(const fs::path& path,const std::string& query){
    sqlite3* db=nullptr;CHECK(sqlite3_open_v2(path.c_str(),&db,SQLITE_OPEN_READONLY,nullptr)==SQLITE_OK);
    sqlite3_stmt* q=nullptr;CHECK(sqlite3_prepare_v2(db,query.c_str(),-1,&q,nullptr)==SQLITE_OK);std::string out;
    int rc;while((rc=sqlite3_step(q))==SQLITE_ROW){for(int i=0;i<sqlite3_column_count(q);++i){
        const auto* text=sqlite3_column_text(q,i);const auto n=sqlite3_column_bytes(q,i);
        out+=std::to_string(sqlite3_column_type(q,i))+":"+std::to_string(n)+":";
        if(text)out.append(reinterpret_cast<const char*>(text),n);
        out+=';';
    }out+='\n';}CHECK(rc==SQLITE_DONE);sqlite3_finalize(q);CHECK(sqlite3_close(db)==SQLITE_OK);return out;
}
void legacy_depth6(const fs::path& db){
    const std::string schema=depth_schema;const auto end=schema.find("CREATE TABLE depth_events");CHECK(end!=std::string::npos);
    const auto text=std::string("PRAGMA foreign_keys=OFF; PRAGMA legacy_alter_table=ON; BEGIN IMMEDIATE; ALTER TABLE depth_sessions RENAME TO legacy_depth_sessions;")+
        schema.substr(0,end)+"INSERT INTO depth_sessions SELECT * FROM legacy_depth_sessions; DROP TABLE legacy_depth_sessions; PRAGMA user_version=6; COMMIT; PRAGMA legacy_alter_table=OFF; PRAGMA foreign_keys=ON;";
    execute(db,text.c_str());
}
void seed_depth(TimeSeriesStore& store){
    DepthSpec spec{contract(),"TEST",10};store.begin_depth("mock",42,spec);
    DepthEvent start;start.request_id=42;start.sequence=1;start.kind="start";start.received={1700000000000000LL,1000000000};
    auto update=start;update.sequence=2;update.kind="update";update.operation=0;update.side=0;update.position=0;update.price=101.125;update.size="123.000";update.market_maker="TEST";update.callback_format="synthetic_fixture";
    auto stop=start;stop.sequence=3;stop.kind="stop";store.record_depth_events("mock",{start,update,stop});
}
void sql_rejected(const fs::path& path,const char* query){
    sqlite3* db=nullptr;CHECK(sqlite3_open(path.c_str(),&db)==SQLITE_OK);const int rc=sqlite3_exec(db,query,nullptr,nullptr,nullptr);
    CHECK(sqlite3_close(db)==SQLITE_OK);CHECK(rc!=SQLITE_OK);
}
int main(){int passed=0;auto test=[&](const char* name,auto fn){try{fn();++passed;std::cout<<"PASS "<<name<<'\n';}catch(const std::exception& e){std::cerr<<"FAIL "<<name<<": "<<e.what()<<'\n';throw;}};
try{
 test("quote commit, repeated prices, cursor paging, mode changes and durable restart",[]{
    Temp t;std::int64_t id=0;std::string backup;
    {TimeSeriesStore s(t.config(),"mock");s.register_contract("mock",contract());
     s.record_events("mock",{quote(),quote()});auto q=quote();q.ask.reset();q.data_type=MarketDataType::Delayed;s.record_events("mock",{q});
     CHECK(s.status().quotes==3);id=std::stoll(std::get<std::string>(s.catalog().rows[0].at("series_id")));
     auto p=s.history(id,0,0,2);CHECK(p.rows.size()==2&&p.has_more&&p.through_id==3);
     s.record_events("mock",{quote()});auto next=s.history(id,p.next_after_id,p.through_id,2);CHECK(next.rows.size()==1&&!next.has_more);
     CHECK(std::holds_alternative<std::nullptr_t>(next.rows[0].at("ask")));CHECK(std::get<std::string>(next.rows[0].at("feed"))=="delayed");
     CHECK(s.history(id,0,0,100).rows.size()==4);s.close();backup=s.status().last_backup;CHECK(fs::exists(backup));}
    CHECK(scalar(backup,"SELECT count(*) FROM quote_observations")==4);
    CHECK(scalar(backup,"SELECT count(*) FROM runs WHERE state='clean'")==1);
    {TimeSeriesStore s(t.config(),"none");CHECK(s.status().quotes==4);CHECK(s.history(id).rows.size()==4);s.close();}
    CHECK(scalar(t.config().directory/"timeseries.sqlite3","SELECT count(*) FROM runs")==2);
 });
 test("atomic rollback and sticky recording failure",[]{
    Temp t;TimeSeriesStore s(t.config(),"mock");s.register_contract("mock",contract());
    auto bad=quote();bad.contract_id=42;
    rejects([&]{s.record_events("mock",{quote(),bad});});CHECK(s.status().failed&&s.status().quotes==0);
    rejects([&]{s.record_events("mock",{quote()});});rejects([&]{s.close();});
    CHECK(scalar(t.config().directory/"timeseries.sqlite3","SELECT count(*) FROM quote_observations")==0);
 });
 test("bar read deduplication, revisions, NULL preservation and request memberships",[]{
    Temp t;TimeSeriesStore s(t.config(),"none");AssetRead read{"synthetic-source","TEST","","",100};
    Bar b{"TEST","1","2026-01-01",1.0,3.0,0.5,2.0,100};
    CHECK(s.record_asset_read(read,{b}).inserted==1);CHECK(s.record_asset_read(read,{b}).inserted==0);
    b.close=2.5;CHECK(s.record_asset_read(read,{b}).inserted==1);b.close.reset();b.volume.reset();CHECK(s.record_asset_read(read,{b}).inserted==1);
    CHECK(s.record_asset_read(read,{}).inserted==0);CHECK(s.status().bars==3);s.close();
    const auto db=t.config().directory/"timeseries.sqlite3";
    CHECK(scalar(db,"SELECT count(*) FROM asset_reads")==5);CHECK(scalar(db,"SELECT count(*) FROM asset_read_rows")==4);
    CHECK(scalar(db,"SELECT count(*) FROM bar_observations WHERE close IS NULL AND volume IS NULL")==1);
 });
 test("metadata variants and source namespaces are distinct",[]{
    Temp t;TimeSeriesStore s(t.config(),"none");auto c=contract();s.register_contract("mock",c);s.record_events("mock",{quote()});
    c.multiplier=100;s.register_contract("mock",c);s.record_events("mock",{quote()});s.register_contract("ibkr_tws",c);s.record_events("ibkr_tws",{quote()});
    CHECK(s.catalog().rows.size()==3);CHECK(s.status().quotes==3);s.close();
 });
 test("decorator records backend updates once, independent of reads",[]{
    Temp t;TimeSeriesStore store(t.config(),"mock");auto mock=std::make_unique<MockBroker>();auto* raw=mock.get();
    {RecordingBroker b(std::move(mock),store,"mock");b.connect();auto id=b.subscribe(contract());raw->publish(quote());
     CHECK(b.poll().size()>=1);CHECK(store.status().quotes==1);(void)b.poll();(void)store.status();CHECK(store.status().quotes==1);
     raw->publish(quote());b.request_positions(9);CHECK(b.unsubscribe(id));
     auto events=b.poll();CHECK(std::any_of(events.begin(),events.end(),[](const auto& x){return std::holds_alternative<PositionsComplete>(x);}));
     CHECK(store.status().quotes==2);b.disconnect();}
    store.close();
 });
 test("single writer lock and exclusive SQLite connection",[]{
    Temp t;TimeSeriesStore store(t.config(),"none");rejects([&]{TimeSeriesStore duplicate(t.config(),"none");});
    sqlite3* db=nullptr;CHECK(sqlite3_open_v2(store.status().database.c_str(),&db,SQLITE_OPEN_READONLY,nullptr)==SQLITE_OK);
    CHECK(sqlite3_exec(db,"SELECT * FROM series",nullptr,nullptr,nullptr)==SQLITE_BUSY);sqlite3_close(db);store.close();
 });
 test("query validation and empty history",[]{
    Temp t;TimeSeriesStore s(t.config(),"mock");s.register_contract("mock",contract());CHECK(s.history(1).rows.empty());
    rejects([&]{s.history(0);});rejects([&]{s.history(1,-1);});rejects([&]{s.history(1,0,0,1001);});rejects([&]{s.catalog(0,0);});rejects([&]{s.history(99);});s.close();
 });
 test("backup refusal retains database and older backup",[]{
    Temp t;TimeSeriesStore s(t.config(),"mock");s.register_contract("mock",contract());s.record_events("mock",{quote()});const auto old=s.backup();
    fs::rename(t.config().backup_directory,t.path/"saved-backups");std::ofstream(t.config().backup_directory)<<"blocked";
    rejects([&]{s.close();});CHECK(fs::exists(t.path/"saved-backups"/fs::path(old).filename()));
    CHECK(scalar(t.config().directory/"timeseries.sqlite3","SELECT count(*) FROM quote_observations")==1);
 });
 test("wrong schema never adopted",[]{
    Temp t;fs::create_directories(t.config().directory);fs::permissions(t.config().directory,fs::perms::owner_all);
    const auto path=t.config().directory/"timeseries.sqlite3";execute(path,"CREATE TABLE existing(value); INSERT INTO existing VALUES(7)");
    fs::permissions(path,fs::perms::owner_read|fs::perms::owner_write);rejects([&]{TimeSeriesStore s(t.config(),"none");});CHECK(scalar(path,"SELECT value FROM existing")==7);
 });
 test("future schema never downgraded",[]{
    Temp t;{TimeSeriesStore s(t.config(),"none");s.close();}const auto db=t.config().directory/"timeseries.sqlite3";
    execute(db,"PRAGMA user_version=99");rejects([&]{TimeSeriesStore s(t.config(),"none");});CHECK(scalar(db,"PRAGMA user_version")==99);
 });
 test("known schema 7 upgrades to 8 retaining history and backups",[]{
    Temp t;{TimeSeriesStore s(t.config(),"mock");s.register_contract("mock",contract());s.record_events("mock",{quote()});s.close();}
    const auto db=t.config().directory/"timeseries.sqlite3";
    CHECK(scalar(db,"PRAGMA user_version")==8);
    legacy_depth6(db);
    execute(db,backfill_schema);
    execute(db,R"SQL(
INSERT INTO history_datasets VALUES(1,'mock','schema7-fixture',9001,'SYNTHETIC','TEST','USD','1 min','TRADES',1,'UTC','unknown');
INSERT INTO history_backfills VALUES(1,1,100,200,1,'complete',1234);
INSERT INTO history_backfills VALUES(2,1,200,300,1,'running',1235);
INSERT INTO history_backfill_windows VALUES(1,1,100,200,'complete',NULL,1,7,0);
INSERT INTO history_backfill_windows VALUES(2,2,200,300,'pending',NULL,1,0,0);
)SQL");
    std::string backup;
    {TimeSeriesStore s(t.config(),"none");CHECK(s.status().quotes==1);s.close();backup=s.status().last_backup;}
    for(const fs::path& p:{db,fs::path(backup)}) {
        CHECK(scalar(p,"PRAGMA user_version")==8);
        CHECK(scalar(p,"SELECT count(*) FROM quote_observations")==1);
        CHECK(scalar(p,"SELECT count(*) FROM history_backfills")==2);
        CHECK(scalar(p,"SELECT count(*) FROM history_backfills WHERE backfill_id=1 AND state='complete'")==1);
        CHECK(scalar(p,"SELECT received_rows FROM history_backfill_windows WHERE window_id=1")==7);
        CHECK(scalar(p,"SELECT count(*) FROM history_backfills WHERE backfill_id=2 AND state='interrupted'")==1);
        CHECK(scalar(p,"SELECT count(*) FROM history_backfill_windows WHERE window_id=2 AND state='interrupted'")==1);
    }
    {TimeSeriesStore s(t.config(),"none");CHECK(s.status().quotes==1);s.close();}
    CHECK(scalar(db,"PRAGMA user_version")==8);
 });
 test("schema 7 number without recognized extension is refused before run mutation",[]{
    Temp t;{TimeSeriesStore s(t.config(),"none");s.close();}
    const auto db=t.config().directory/"timeseries.sqlite3";
    legacy_depth6(db);execute(db,"PRAGMA user_version=7");
    rejects([&]{TimeSeriesStore s(t.config(),"none");});
    CHECK(scalar(db,"SELECT count(*) FROM runs")==1);
    execute(db,backfill_schema);
    execute(db,"DROP INDEX one_running_backfill");
    rejects([&]{TimeSeriesStore s(t.config(),"none");});
    CHECK(scalar(db,"SELECT count(*) FROM runs")==1);
    CHECK(scalar(db,"PRAGMA user_version")==7);
 });
 test("schema 6 and 7 migrations preserve depth IDs payloads unrelated data and metadata",[]{
    for(const int version:{6,7}){
        Temp t;std::string raw_before;
        {TimeSeriesStore s(t.config(),"mock");s.register_contract("mock",contract());s.record_events("mock",{quote()});seed_depth(s);s.close();}
        const auto db=t.config().directory/"timeseries.sqlite3";legacy_depth6(db);
        if(version==7)execute(db,backfill_schema);
        execute(db,"CREATE TABLE preserved_extra(key TEXT PRIMARY KEY,value BLOB); INSERT INTO preserved_extra VALUES('archive-note',x'001122ff'); UPDATE sqlite_sequence SET seq=500 WHERE name='depth_sessions';");
        const auto sessions=dump_query(db,"SELECT * FROM depth_sessions ORDER BY session_id");
        const auto events=dump_query(db,"SELECT * FROM depth_events ORDER BY event_id");
        const auto quotes=dump_query(db,"SELECT * FROM quote_observations ORDER BY observation_id");
        const auto extra=dump_query(db,"SELECT * FROM preserved_extra");
        const auto definitions=dump_query(db,"SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND tbl_name!='depth_sessions' ORDER BY type,name");
        const auto raw=t.config().directory/"depth-raw/run-1/session-1.jsonl";
        {std::ifstream f(raw);raw_before.assign(std::istreambuf_iterator<char>(f),std::istreambuf_iterator<char>());}
        {
            TimeSeriesStore s(t.config(),"none");
            CHECK(s.depth_session(1).at("requested_rows")==Cell(std::int64_t(10)));
            auto wide=DepthSpec{contract(),"TEST",50};const auto id=s.begin_depth("mock",99,wide);CHECK(id==501);
            DepthEvent start;start.request_id=99;start.sequence=1;start.kind="start";start.received=DepthStamp::now();
            s.record_depth_events("mock",{start});s.close();
        }
        CHECK(scalar(db,"PRAGMA user_version")==8);
        CHECK(dump_query(db,"SELECT * FROM depth_sessions WHERE session_id=1")==sessions);
        CHECK(dump_query(db,"SELECT * FROM depth_events WHERE session_id=1 ORDER BY event_id")==events);
        CHECK(dump_query(db,"SELECT * FROM quote_observations ORDER BY observation_id")==quotes);
        CHECK(dump_query(db,"SELECT * FROM preserved_extra")==extra);
        CHECK(dump_query(db,"SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND tbl_name!='depth_sessions' ORDER BY type,name")==definitions);
        CHECK(scalar(db,"SELECT count(*) FROM pragma_foreign_key_check")==0);
        CHECK(scalar(db,"SELECT requested_rows FROM depth_sessions WHERE session_id=501")==50);
        CHECK(scalar(db,"SELECT count(*) FROM sqlite_master WHERE type='trigger' AND tbl_name='depth_events'")==2);
        sql_rejected(db,"UPDATE depth_sessions SET requested_rows=51 WHERE session_id=501");
        sql_rejected(db,"UPDATE depth_events SET size='lost' WHERE session_id=1");
        sql_rejected(db,"DELETE FROM depth_events WHERE session_id=1");
        {std::ifstream f(raw);const std::string after{std::istreambuf_iterator<char>(f),std::istreambuf_iterator<char>()};CHECK(after==raw_before);}
        bool old_backup=false;
        for(const auto& f:fs::directory_iterator(t.config().backup_directory))
            if(f.path().filename().string().find("runmigration8")!=std::string::npos){
                CHECK(scalar(f.path(),"PRAGMA user_version")==version);
                CHECK(dump_query(f.path(),"SELECT * FROM depth_sessions ORDER BY session_id")==sessions);
                CHECK(dump_query(f.path(),"SELECT * FROM depth_events ORDER BY event_id")==events);
                old_backup=true;
            }
        CHECK(old_backup);
        {TimeSeriesStore s(t.config(),"none");CHECK(s.depth_sessions().rows.size()==2);s.close();}
    }
 });
 test("empty legacy session table preserves its prior allocation watermark",[]{
    Temp t;{TimeSeriesStore s(t.config(),"none");s.close();}
    const auto db=t.config().directory/"timeseries.sqlite3";legacy_depth6(db);
    execute(db,"DELETE FROM sqlite_sequence WHERE name='depth_sessions'; INSERT INTO sqlite_sequence(name,seq) VALUES('depth_sessions',700);");
    {TimeSeriesStore s(t.config(),"none");CHECK(s.begin_depth("mock",42,{contract(),"TEST",50})==701);s.close();}
    CHECK(scalar(db,"SELECT seq FROM sqlite_sequence WHERE name='depth_sessions'")==701);
 });
 test("migration refuses missing backups before changing schema or observations",[]{
    Temp t;{TimeSeriesStore s(t.config(),"mock");seed_depth(s);s.close();}
    const auto db=t.config().directory/"timeseries.sqlite3";legacy_depth6(db);
    const auto before=dump_query(db,"SELECT * FROM depth_events ORDER BY event_id");
    fs::rename(t.config().backup_directory,t.path/"saved-backups");std::ofstream(t.config().backup_directory)<<"blocked";
    rejects([&]{TimeSeriesStore s(t.config(),"none");});
    CHECK(scalar(db,"PRAGMA user_version")==6);CHECK(scalar(db,"SELECT count(*) FROM runs")==1);
    CHECK(dump_query(db,"SELECT * FROM depth_events ORDER BY event_id")==before);
 });
 test("migration refuses foreign-key corruption and malformed depth definitions",[]{
    Temp t;{TimeSeriesStore s(t.config(),"mock");seed_depth(s);s.close();}
    const auto db=t.config().directory/"timeseries.sqlite3";legacy_depth6(db);
    execute(db,"UPDATE depth_sessions SET run_id=999 WHERE session_id=1");
    rejects([&]{TimeSeriesStore s(t.config(),"none");});CHECK(scalar(db,"PRAGMA user_version")==6);
    execute(db,"UPDATE depth_sessions SET run_id=1 WHERE session_id=1; DROP TRIGGER depth_events_no_delete");
    rejects([&]{TimeSeriesStore s(t.config(),"none");});CHECK(scalar(db,"SELECT count(*) FROM runs")==1);
 });
 test("schema 8 refuses partial backfill and forged version labels",[]{
    Temp t;{TimeSeriesStore s(t.config(),"none");s.close();}
    const auto db=t.config().directory/"timeseries.sqlite3";
    execute(db,"CREATE TABLE history_backfills(fake TEXT)");
    rejects([&]{TimeSeriesStore s(t.config(),"none");});CHECK(scalar(db,"SELECT count(*) FROM runs")==1);
    execute(db,"DROP TABLE history_backfills");legacy_depth6(db);execute(db,"PRAGMA user_version=8");
    rejects([&]{TimeSeriesStore s(t.config(),"none");});CHECK(scalar(db,"SELECT count(*) FROM runs")==1);
 });
 test("SQLite write error fails closed without losing prior committed rows",[]{
    Temp t;{TimeSeriesStore s(t.config(),"mock");s.register_contract("mock",contract());s.record_events("mock",{quote()});s.close();}
    const auto db=t.config().directory/"timeseries.sqlite3";
    execute(db,"CREATE TRIGGER injected_write_error BEFORE INSERT ON quote_observations BEGIN SELECT RAISE(ABORT,'test injection'); END;");
    {TimeSeriesStore s(t.config(),"mock");s.register_contract("mock",contract());rejects([&]{s.record_events("mock",{quote()});});CHECK(s.status().failed&&s.status().quotes==1);rejects([&]{s.close();});}
    CHECK(scalar(db,"SELECT count(*) FROM quote_observations")==1);
 });
 test("process crash preserves committed data and marks the old run interrupted",[]{
    Temp t;const auto pid=fork();CHECK(pid>=0);
    if(pid==0){try{TimeSeriesStore s(t.config(),"mock");s.register_contract("mock",contract());s.record_events("mock",{quote()});_exit(0);}catch(...){_exit(9);}}
    int status=0;waitpid(pid,&status,0);CHECK(WIFEXITED(status)&&WEXITSTATUS(status)==0);
    TimeSeriesStore s(t.config(),"none");CHECK(s.status().quotes==1&&s.status().interrupted_runs==1);s.close();
 });
 test("concurrent serialized recording and reads",[]{
    Temp t;TimeSeriesStore s(t.config(),"mock");s.register_contract("mock",contract());std::atomic<bool> failed{false};
    auto producer=[&]{try{for(int i=0;i<50;++i)s.record_events("mock",{quote()});}catch(...){failed=true;}};
    std::thread a(producer),b(producer);for(int i=0;i<50;++i){(void)s.status();(void)s.history(1);}
    a.join();b.join();CHECK(!failed&&s.status().quotes==100);s.close();
 });
 std::cout<<passed<<" storage cases passed\n";return 0;
}catch(...){return 1;}}
