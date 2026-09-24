#include <algorithm>
#include <dts/time_series_store.hpp>
#include <dts/recording_broker.hpp>
#include <dts/mock_broker.hpp>
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
