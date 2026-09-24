#include <dts/time_series_store.hpp>
#include <sqlite3.h>
#include <filesystem>
#include <iostream>
#include <unistd.h>
#include <sys/wait.h>
using namespace dts;
using namespace dts::storage;
namespace {
void check(bool x){if(!x)throw std::runtime_error("Research store assertion failed");}
template<class F>void rejects(F f){bool failed=false;try{f();}catch(const std::exception&){failed=true;}check(failed);}
constexpr std::int64_t start=1767571200;
HistorySpec spec(bool minute=false){HistorySpec s;s.contract.id=minute?124:123;s.contract.symbol=minute?"SYNTHETIC_MIN":"SYNTHETIC";s.contract.currency="USD";s.contract.exchange="SMART";s.contract.multiplier=1;s.bar_size=minute?"1 min":"1 day";return s;}
HistoryWindow window(bool minute=false){return {start,start+80*(minute?60:86400)};}
void populate(TimeSeriesStore& s,bool minute=false,double scale=1,bool empty=false,const std::string& end="complete"){
    auto model=spec(minute);auto did=s.historical_dataset(model);auto rid=s.queue_history(did,{window(minute)})[0];s.bind_history(rid,10);
    std::vector<BrokerEvent> events;
    if(!empty)for(int i=0;i<80;++i){const auto t=start+i*(minute?60:86400);const double p=scale*100*std::exp(.003*i+.01*(i%3));HistoricalBar b{minute?std::to_string(t):utc_text(t,"%Y%m%d"),p,p+1,p-1,p,std::string("123.5"),p,10};events.push_back(HistoricalBarEvent{10,b});}
    events.push_back(HistoricalEnd{10,end,0,"",""});s.record_history_events(events);
}
void raw_reject(sqlite3* db,const std::string& sql){check(sqlite3_exec(db,sql.c_str(),nullptr,nullptr,nullptr)!=SQLITE_OK);}
}
int main(int argc,char** argv){
    auto root=argc>2?std::filesystem::path(argv[2]):std::filesystem::temp_directory_path()/("replay-store-"+std::to_string(getpid()));
    if(argc>2&&std::string(argv[1])=="--seed") {try{TimeSeriesStore s({root,root/"backups"},"synthetic_test");populate(s);populate(s,true);s.close();return 0;}catch(const std::exception& e){std::cerr<<e.what();return 1;}}
    if(argc>2&&std::string(argv[1])=="--revise") {try{TimeSeriesStore s({root,root/"backups"},"synthetic_test");populate(s,false,2);s.close();return 0;}catch(const std::exception& e){std::cerr<<e.what();return 1;}}
    int cases=0;std::int64_t first=0,minute_id=0;std::string fingerprint;double original=0;
    try {
        {
            TimeSeriesStore store({root,root/"backups"},"test");populate(store);
            auto frozen=store.create_snapshot(1,window(),"Original");first=frozen.id;fingerprint=frozen.fingerprint;original=frozen.observations[0].bar.close;
            check(frozen.observations.size()==80&&frozen.uncovered_intervals.empty());++cases;
            const auto duplicate=store.create_snapshot(1,window(),"Another label");check(duplicate.id!=first&&duplicate.fingerprint==fingerprint);++cases;
            rejects([&]{store.create_snapshot(999,window(),"x");});rejects([&]{store.create_snapshot(1,window(),"\n");});check(!store.status().failed);++cases;
            populate(store,false,2);check(store.snapshot(first).observations[0].bar.close==original);check(store.snapshot(first).fingerprint==fingerprint);++cases;
            auto revised=store.create_snapshot(1,window(),"Revised");check(revised.observations[0].bar.close==2*original&&revised.fingerprint!=fingerprint);++cases;
            populate(store,false,1);auto reverted=store.create_snapshot(1,window(),"A B A");check(reverted.observations[0].version_id==frozen.observations[0].version_id&&reverted.observations[0].bar.close==original);++cases;
            populate(store,false,3,false,"failed");auto failed=store.create_snapshot(1,window(),"Failed response ignored");check(failed.observations[0].bar.close==original);++cases;
            populate(store,false,1,true);rejects([&]{store.create_snapshot(1,window(),"Empty now");});check(store.snapshot(first).observations.size()==80);check(!store.status().failed);++cases;
            auto page=store.snapshot_catalog(0,2);check(page.rows.size()==2&&page.has_more);check(store.snapshot_catalog(page.next_after_id,100).rows.size()==3);++cases;
            populate(store,true);minute_id=store.historical_dataset(spec(true));auto partial=store.create_snapshot(minute_id,{start,start+81*60},"Partial response coverage");check(partial.uncovered_intervals.size()==1);++cases;
            Experiment run;run.snapshot_id=first;run.name="Internal test only";run.engine_version=research::engine_version;run.config_json="{}";run.result_json="{\"test\":true}";auto id=store.save_experiment(run);
            run.parent_id=id;auto child=store.save_experiment(run);check(store.experiment(child).parent_id==id);check(store.experiment(id).result_json==run.result_json);++cases;
            run.snapshot_id=partial.id;rejects([&]{store.save_experiment(run);});check(!store.status().failed);++cases;
            store.close();
        }
        {
            TimeSeriesStore store({root,root/"backups"},"test");auto s=store.snapshot(first);check(s.fingerprint==fingerprint&&s.observations[0].bar.close==original);check(store.experiment_catalog().rows.size()==2);++cases;store.close();
        }
        {
            sqlite3* db=nullptr;check(sqlite3_open((root/"timeseries.sqlite3").c_str(),&db)==SQLITE_OK);
            for(const auto* query:{"UPDATE research_snapshots SET name='changed'","DELETE FROM research_snapshots","UPDATE research_snapshot_bars SET close=1","DELETE FROM research_snapshot_bars","UPDATE research_snapshot_gaps SET start_s=0","DELETE FROM research_snapshot_gaps","UPDATE research_experiments SET name='changed'","DELETE FROM research_experiments"})raw_reject(db,query);
            raw_reject(db,"INSERT INTO research_snapshot_gaps VALUES(1,999,0,1)");sqlite3_close(db);++cases;
        }
        // A committed snapshot survives an exit without destructors/backup callbacks.
        {
            const auto child=fork();check(child>=0);if(child==0){try{TimeSeriesStore store({root,root/"backups"},"test");store.create_snapshot(minute_id,window(true),"Abrupt exit");_exit(0);}catch(...){_exit(2);}}
            int status=0;waitpid(child,&status,0);check(WIFEXITED(status)&&WEXITSTATUS(status)==0);
            TimeSeriesStore store({root,root/"backups"},"test");check(store.snapshot_catalog().rows.size()==7);store.close();++cases;
        }
        std::cout<<cases<<" research storage cases passed\n";std::filesystem::remove_all(root);return 0;
    }catch(const std::exception& e){std::cerr<<e.what()<<" at case "<<cases<<'\n';return 1;}
}
