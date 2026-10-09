#define CROW_USE_BOOST 1
#include <boost/asio.hpp>
namespace asio = boost::asio;
#include "crow_all.h"
#include "tick_jobs.hpp"
#include <dts/tws_state.hpp>
#include <iostream>

namespace {
using namespace dts;
using namespace dts::tick_jobs;
void check(bool value,const char* label){if(!value)throw std::runtime_error(label);}
template<class F>void rejects(F fn){bool failed=false;try{fn();}catch(const std::exception&){failed=true;}check(failed,"Expected rejection");}
constexpr std::int64_t start=1767706200; // Synthetic January 2026 fixture; never live evidence.
TickSpec spec(){TickSpec s;s.contract.id=123;s.contract.symbol="SYNTHETIC";s.contract.exchange="SMART";s.contract.currency="USD";s.contract.multiplier=1;return s;}
HistoricalTick row(std::int64_t t=start){HistoricalTick r;r.time=t;r.price=100;r.size="1.25";r.exchange="TEST";r.unreported=true;return r;}
Read parsed(Json j){return crow::json::load(j.dump());}
class Peer final:public IBroker {
public:
    ConnectionState status=ConnectionState::Ready;
    RequestId last=0;std::int64_t requested=0;int sent=0,cancelled=0;
    std::vector<BrokerEvent> events;
    void connect()override{status=ConnectionState::Ready;}
    void disconnect()noexcept override{status=ConnectionState::Disconnected;}
    ConnectionState state()const noexcept override{return status;}
    RequestId subscribe(const Contract&)override{return 0;}
    bool unsubscribe(RequestId)override{return false;}
    void request_positions(RequestId)override{}
    RequestId request_ticks(const TickSpec&,std::int64_t s)override{requested=s;++sent;return ++last;}
    void cancel_ticks(RequestId)override{++cancelled;}
    std::vector<BrokerEvent> poll()override{std::vector<BrokerEvent> out;out.swap(events);return out;}
    void reply(std::vector<HistoricalTick> rows,const std::string& status="complete",int code=0){events.emplace_back(std::in_place_type<HistoricalTickPage>,HistoricalTickPage{last,std::move(rows),status,code,{}});}
};
}
int main(int argc,char** argv){
    const bool fixture=argc==2;
    const auto root=fixture?fs::path(argv[1]):fs::temp_directory_path()/("dts-ticks-"+std::to_string(getpid()));
    try{
        private_dir(root,true);
        auto peer=std::make_unique<Peer>();auto* p=peer.get();ReadOnlyService service(std::move(peer));
        const auto future=Clock::now()+std::chrono::seconds(100);
        auto s=spec();s.validate({start,start+1});
        rejects([&]{s.validate({start,start});});rejects([&]{s.validate({start,start+32*86400});});
        auto bad=s;bad.type="DEPTH";rejects([&]{bad.validate();});bad=s;bad.contract.id=0;rejects([&]{bad.validate();});
        Manager m((root/"timeseries.sqlite3").string());
        auto j=parsed(m.start(s,{start,start+10},ConnectionState::Ready));const auto id=field(j,"download_id");
        rejects([&]{m.start(s,{start,start+10},ConnectionState::Ready);});
        m.tick(service,future);check(p->requested==start&&p->sent==1,"first request starts exactly at selected second");
        // All 1,005 equal ticks in the last second must survive pagination.
        p->reply(std::vector<HistoricalTick>(1005,row()));service.poll();m.tick(service,future);
        j=parsed(m.status(id));check(j["tick_count"].i()==1005&&j["next_s"].i()==start+1,"duplicates preserved and second boundary advanced");
        m.tick(service,future+std::chrono::seconds(14));check(p->sent==1,"provider pacing retained");
        m.tick(service,future+std::chrono::seconds(15));check(p->requested==start+1&&p->sent==2,"next request avoids duplicated boundary");
        p->reply({row(start+1),row(start+9),row(start+10)});service.poll();m.tick(service,future+std::chrono::seconds(16));
        j=parsed(m.status(id));check(field(j,"state")=="complete"&&j["tick_count"].i()==1007,"exclusive end filter");
        auto v=parsed(m.view(id,0,1000));check(v["ticks"].size()==1000&&v["has_more"].b(),"first replay page");
        v=parsed(m.view(id,1000,1000));check(v["ticks"].size()==7&&!v["has_more"].b()&&v["ticks"][0]["ordinal"].i()==1001,"second replay page across files");
        check(v["ticks"][0]["size"].s()=="1.25"&&v["ticks"][0]["unreported"].b(),"decimal precision and flags retained");
        if(fixture){std::cout<<id<<'\n';return 0;}
        rejects([&]{m.resume(id,ConnectionState::Ready);});rejects([&]{m.view("../outside",0,1);});rejects([&]{m.view(id,0,1001);});
        const auto paused=field(parsed(m.start(s,{start,start+20},ConnectionState::Ready)),"download_id");
        m.tick(service,future+std::chrono::seconds(31));p->reply({row(start+3)});service.poll();m.tick(service,future+std::chrono::seconds(32));
        j=parsed(m.cancel(paused,service));check(field(j,"state")=="cancelled"&&j["next_s"].i()==start+4,"cancel preserves committed progress");
        m.resume(paused,ConnectionState::Ready);m.tick(service,future+std::chrono::seconds(46));check(p->requested==start+4,"resume from durable cursor");
        p->reply({},"failed",162);service.poll();m.tick(service,future+std::chrono::seconds(47));j=parsed(m.status(paused));
        check(field(j,"state")=="failed"&&j["next_s"].i()==start+4&&j["code"].i()==162,"failed request is not empty success");
        m.resume(paused,ConnectionState::Ready);m.tick(service,future+std::chrono::seconds(62));m.shutdown(service);
        check(p->cancelled==1&&field(parsed(m.status(paused)),"state")=="interrupted","shutdown cancels local request explicitly");
        const auto empty=field(parsed(m.start(s,{start,start+20},ConnectionState::Ready)),"download_id");
        m.tick(service,future+std::chrono::seconds(80));p->reply({});service.poll();m.tick(service,future+std::chrono::seconds(81));
        j=parsed(m.status(empty));check(field(j,"state")=="complete"&&j["empty_pages"].i()==1&&!j["complete_market_history"].b(),"empty response records uncertainty");
        auto quotes=s;quotes.type="BID_ASK";auto quote=row(start-1);quote.bid=99;quote.ask=101;quote.bid_size="1.25";quote.ask_size="2.5";
        const auto quote_id=field(parsed(m.start(quotes,{start,start+10},ConnectionState::Ready)),"download_id");
        m.tick(service,future+std::chrono::seconds(96));
        auto inside=quote;inside.time=start;auto outside=quote;outside.time=start+10;
        p->reply({quote,inside,outside});service.poll();m.tick(service,future+std::chrono::seconds(97));
        auto clipped=parsed(m.view(quote_id,0,1000));check(clipped["ticks"].size()==1&&clipped["ticks"][0]["time_s"].i()==start,"provider quote seed clipped to exact start");
        check(m.next_dispatch()==future+std::chrono::seconds(126),"best quotes use conservative doubled pacing");
        const auto interrupted=field(parsed(m.start(s,{start,start+10},ConnectionState::Ready)),"download_id");
        Manager restarted((root/"timeseries.sqlite3").string());
        check(!restarted.busy()&&field(parsed(restarted.status(interrupted)),"state")=="interrupted","restart does not automatically contact broker");
        check(parsed(restarted.view(id,1000,1000))["ticks"].size()==7,"replay survives restart");
        auto manifest=read_json(root/"historical-ticks"/id/"state.json",1000000);
        auto file=root/"historical-ticks"/id/field(manifest["pages"][0],"file");
        {std::ofstream corrupt(file,std::ios::app);corrupt<<' ';}
        rejects([&]{restarted.view(id,0,1);});
        TwsState state;state.start(future);state.ready();state.poll();
        auto request=state.ticks(s,start,future);rejects([&]{state.ticks(s,start,future);});
        state.historical_ticks(request+1,"TRADES",{row()},true);check(state.poll().empty(),"late foreign callback ignored");
        state.historical_ticks(request,"TRADES",{row()},false);check(state.poll().empty(),"incomplete page not released");
        state.historical_ticks(request,"TRADES",{row()},true);auto events=state.poll();
        check(std::get<HistoricalTickPage>(events[0]).ticks.size()==2,"multi-callback page preserves duplicates");
        request=state.ticks(s,start,future+std::chrono::seconds(1));state.historical_ticks(request,"TRADES",{row(start-1)},true);
        check(std::get<HistoricalTickPage>(state.poll()[0]).status=="failed","unordered or early ticks rejected");
        request=state.ticks(s,start,future+std::chrono::seconds(2));state.cancel_ticks(request);state.historical_ticks(request,"TRADES",{row()},true);
        check(state.poll().size()==1,"cancel ignores late callback");
        state.ticks(s,start,future+std::chrono::seconds(3));state.expire(future+std::chrono::seconds(64));
        check(std::get<HistoricalTickPage>(state.poll()[0]).code==-1043,"tick timeout");
        request=state.ticks(quotes,start,future+std::chrono::seconds(65));state.historical_ticks(request,"BID_ASK",{quote,inside},true);
        check(std::get<HistoricalTickPage>(state.poll()[0]).ticks.size()==2,"real-provider preceding-second quote seed accepted");
        request=state.ticks(quotes,start,future+std::chrono::seconds(66));quote.time=start-2;state.historical_ticks(request,"BID_ASK",{quote},true);
        check(std::get<HistoricalTickPage>(state.poll()[0]).status=="failed","older quote outside allowed seed remains invalid");
        fs::remove_all(root);std::cout<<"Tick paging, boundaries, persistence, cancellation, restart, checksum and failure checks passed\n";return 0;
    }catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}
}
