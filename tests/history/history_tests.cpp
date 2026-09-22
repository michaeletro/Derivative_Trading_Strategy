#include <dts/historical_manager.hpp>
#include <dts/recording_broker.hpp>
#include <dts/tws_state.hpp>
#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <unistd.h>

namespace {
void check(bool yes,const char* message){if(!yes)throw std::runtime_error(message);}
template<class F>void rejects(F fn){bool failed=false;try{fn();}catch(const std::exception&){failed=true;}check(failed,"Expected rejection");}
dts::HistorySpec spec(){dts::HistorySpec s;s.contract.id=123;s.contract.symbol="SYNTHETIC";s.contract.currency="USD";s.contract.exchange="SMART";s.contract.multiplier=1;return s;}
dts::HistoricalBar bar(const std::string& date="20260105",double close=101){return {date,100,110,90,close,std::string("123.5"),100.5,10};}
constexpr std::int64_t start=1767571200; // 2026-01-05 date coordinate
const dts::HistoryWindow window{start,start+5*86400};
class Peer final:public dts::IBroker {
public:
    dts::ConnectionState status=dts::ConnectionState::Disconnected;
    dts::RequestId next=100,last=0;int sent=0;
    std::vector<dts::BrokerEvent> events;
    void connect()override{status=dts::ConnectionState::Ready;}
    void disconnect()noexcept override{status=dts::ConnectionState::Disconnected;}
    dts::ConnectionState state()const noexcept override{return status;}
    dts::RequestId subscribe(const dts::Contract&)override{return next++;}
    bool unsubscribe(dts::RequestId)override{return true;}
    void request_positions(dts::RequestId)override{}
    dts::RequestId request_history(const dts::HistorySpec&,dts::HistoryWindow)override{++sent;last=next++;return last;}
    void cancel_history(dts::RequestId id)override{events.push_back(dts::HistoricalEnd{id,"interrupted",-1022,"",""});}
    std::vector<dts::BrokerEvent> poll()override{std::vector<dts::BrokerEvent> out;out.swap(events);return out;}
};
}
int main(int argc,char** argv){
    const auto root=argc==2?std::filesystem::path(argv[1]):std::filesystem::temp_directory_path()/("dts-history-"+std::to_string(getpid()));
    try {
        int cases=0;
        {
            auto s=spec();s.validate(window);auto bad=s;bad.contract.currency="EUR";rejects([&]{bad.validate();});
            rejects([&]{s.validate({start,start+367LL*86400});});
            rejects([&]{s.validate({start+1,start+86400});});++cases;
            auto minute=s;minute.bar_size="1 min";minute.validate({start,start+86400});rejects([&]{minute.validate(window);});++cases;
            check(dts::bar_coordinate(s,bar())==start,"daily date normalization");
            auto invalid=bar("20260230");rejects([&]{dts::bar_coordinate(s,invalid);});
            invalid=bar();invalid.low=200;rejects([&]{invalid.validate();});++cases;
            auto gaps=dts::uncovered({0,100},{{50,80},{10,20},{15,60}});
            check(gaps.size()==2&&gaps[0].end==10&&gaps[1].start==80,"union gaps");++cases;
            check(dts::historical_chunks(s,{{start,start+65LL*86400}}).size()==3,"daily chunks");++cases;
        }
        {
            dts::TwsState t;const auto now=dts::Clock::now();t.start(now);t.ready();t.poll();
            const auto id=t.history(spec(),window,now);rejects([&]{t.history(spec(),window,now);});
            t.historical_bar(id+1,bar());check(t.poll().empty(),"wrong request id ignored");++cases;
            t.historical_bar(id,bar());t.historical_end(id,"start","end");auto e=t.poll();
            check(e.size()==2&&std::holds_alternative<dts::HistoricalEnd>(e.back()),"bar plus completion");++cases;
            auto id2=t.history(spec(),window,now+std::chrono::seconds(1));t.expire(now+std::chrono::seconds(62));e=t.poll();
            check(std::get<dts::HistoricalEnd>(e[0]).status=="failed"&&t.history_cancellations()[0]==id2,"historical deadline/cancel");++cases;
            auto id3=t.history(spec(),window,now+std::chrono::seconds(64));t.error(id3,162,"permission or pacing");
            e=t.poll();check(std::get<dts::HistoricalEnd>(e[0]).status=="failed","generic 162 not empty success");++cases;
        }
        std::int64_t dataset=0;
        {
            dts::storage::TimeSeriesStore store({root,root/"backups"},"test");
            dataset=store.historical_dataset(spec());check(store.historical_gaps(dataset,window).size()==1,"new window uncovered");++cases;
            const auto id=store.queue_history(dataset,{window}).at(0);store.bind_history(id,1);
            store.record_history_events({dts::HistoricalBarEvent{1,bar()}});
            check(store.historical_bars(dataset,window).empty()&&!store.historical_gaps(dataset,window).empty(),"pending cannot grant data/coverage");++cases;
            store.record_history_events({dts::HistoricalEnd{1,"complete",0,"a","b"}});
            check(store.historical_bars(dataset,window).size()==1&&store.historical_gaps(dataset,window).empty(),"complete publishes saved bars");++cases;
            auto id2=store.queue_history(dataset,{window}).at(0);store.bind_history(id2,2);
            store.record_history_events({dts::HistoricalBarEvent{2,bar("20260105",102)},dts::HistoricalEnd{2,"failed",162,"",""}});
            check(std::get<double>(store.historical_bars(dataset,window)[0].at("close"))==101,"failed refresh preserves completed version");++cases;
            auto id3=store.queue_history(dataset,{window}).at(0);store.bind_history(id3,3);
            store.record_history_events({dts::HistoricalBarEvent{3,bar("20260105",102)},dts::HistoricalEnd{3,"complete",0,"",""}});
            check(std::get<double>(store.historical_bars(dataset,window)[0].at("close"))==102,"successful refresh revises");++cases;
            auto id4=store.queue_history(dataset,{window}).at(0);store.bind_history(id4,4);
            store.record_history_events({dts::HistoricalBarEvent{4,bar()},dts::HistoricalEnd{4,"complete",0,"",""}});
            auto view=store.historical_bars(dataset,window);
            check(std::get<double>(view[0].at("close"))==101&&std::get<std::int64_t>(view[0].at("revisions"))==2,"A B A correction uses request ordering not version id");++cases;
            auto id5=store.queue_history(dataset,{window}).at(0);store.bind_history(id5,5);
            store.record_history_events({dts::HistoricalEnd{5,"complete",0,"",""}});
            check(store.history_state(id5)=="empty"&&store.historical_bars(dataset,window).empty(),"empty refresh omits previously returned bar; keeps revisions");++cases;
            store.queue_history(dataset,{{start+5*86400,start+6*86400}});
            store.close();
        }
        {
            dts::storage::TimeSeriesStore store({root,root/"backups"},"test");
            check(store.historical_gaps(dataset,window).empty(),"coverage survives restart");++cases;
            auto rows=store.historical_requests(dataset,{start,start+6*86400});
            check(std::get<std::string>(rows.front().at("state"))=="interrupted","unfinished requests marked interrupted");++cases;
            auto peer=std::make_unique<Peer>();auto* raw=peer.get();
            dts::ReadOnlyService service(std::make_unique<dts::storage::RecordingBroker>(std::move(peer),store,"mock"));
            dts::storage::HistoricalManager manager(store);
            auto cached=manager.request(spec(),window,"fetch_missing",dts::ConnectionState::Disconnected);
            check(cached.queued_ids.empty(),"cache reuse while disconnected");++cases;
            rejects([&]{manager.request(spec(),window,"refresh",dts::ConnectionState::Disconnected);});++cases;
            service.connect();auto work=manager.request(spec(),window,"refresh",service.state());
            auto repeat=manager.request(spec(),window,"refresh",service.state());check(work.queued_ids.size()==1&&repeat.queued_ids.empty(),"duplicate pending refresh suppressed");++cases;
            const auto now=dts::Clock::now()+std::chrono::seconds(20);manager.tick(service,now);check(raw->sent==1,"one dispatched request");
            raw->events={dts::HistoricalBarEvent{raw->last,bar()},dts::HistoricalEnd{raw->last,"complete",0,"",""}};
            service.poll();manager.tick(service,now);check(store.historical_bars(dataset,window).size()==1,"decorator durably records callback before publish");++cases;
            auto later=manager.request(spec(),{start+7*86400,start+8*86400},"fetch_missing",service.state());
            manager.tick(service,now+std::chrono::seconds(1));check(raw->sent==1,"pacing delays next dispatch");++cases;
            manager.cancel(service);check(store.history_state(later.queued_ids[0])=="interrupted","explicit cancellation persisted");++cases;
            service.disconnect();store.close();
        }
        std::cout<<cases<<" historical cases passed\n";
        if(argc!=2)std::filesystem::remove_all(root);
        return 0;
    }catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}
}
