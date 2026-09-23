#include <dts/depth.hpp>
#include <dts/tws_state.hpp>
#include <dts/recording_broker.hpp>
#include <dts/read_only_service.hpp>
#include <filesystem>
#include <functional>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <unistd.h>

namespace {
int tests=0;
void check(bool v){if(!v)throw std::runtime_error("assertion failed");}
template<class F> void rejects(F fn){bool rejected=false;try{fn();}catch(const std::exception&){rejected=true;}check(rejected);}
void test(const char* name,const std::function<void()>& fn){fn();++tests;std::cout<<"PASS "<<name<<'\n';}
dts::Contract contract(){dts::Contract c;c.id=9001;c.symbol="SYNTHETIC";c.exchange="TESTEX";c.currency="USD";c.multiplier=1;return c;}
dts::DepthSpec spec(){return {contract(),"TESTEX",3};}
dts::DepthEvent event(std::uint64_t seq,const std::string& kind="update",int side=0,int pos=0,int op=0,double price=101,std::string size="10") {
    dts::DepthEvent e;e.request_id=42;e.sequence=seq;e.kind=kind;e.side=side;e.position=pos;e.operation=op;e.price=price;e.size=std::move(size);
    e.received={1700000000000000LL+static_cast<std::int64_t>(seq)*100000,1000000000LL+static_cast<std::int64_t>(seq)*100000000};return e;
}
std::vector<dts::DepthEvent> fixture() {
    auto a=std::vector<dts::DepthEvent>{event(1,"start"),event(2,"update",0,0,0,101,"12.5"),event(3,"update",1,0,0,99,"20"),
        event(4,"update",0,1,0,102,"30"),event(5,"update",1,1,0,98,"40"),
        event(6,"update",0,0,1,100.12345678912345,"14.25"),event(7,"update",1,1,2),
        event(8,"reset"),event(9,"update",1,0,0,100,"50"),event(10,"update",0,0,0,101,"20"),
        event(11,"update",0,2,1,102,"5"),event(12,"update",0,0,1,102,"5"),
        event(13,"reset"),event(14,"update",1,0,0,100,"50"),event(15,"update",0,0,0,101,"20"),event(16,"stop")};
    for(auto& e:a)e.origin="synthetic_fixture";
    return a;
}
std::string state_json(const dts::DepthBook& b) {
    std::ostringstream out;out<<std::setprecision(17)<<"{\"sequence\":\""<<b.sequence()<<"\",\"epoch\":\""<<b.epoch()<<"\",\"quality\":\""<<b.quality()<<"\",\"active\":"<<(b.active()?"true":"false")<<",\"structural_valid\":"<<(b.structural_valid()?"true":"false");
    auto side=[&](const char* name,const auto& levels){out<<",\""<<name<<"\":[";bool first=true;for(const auto& r:levels){if(!first)out<<',';first=false;out<<"{\"price\":"<<r.price<<",\"size\":\""<<r.size<<"\",\"market_maker\":\"\"}";}out<<']';};
    side("bids",b.bids());side("asks",b.asks());out<<'}';return out.str();
}
std::filesystem::path temp(const char* tag){auto p=std::filesystem::temp_directory_path()/(std::string("dts-depth-")+std::to_string(getpid())+"-"+tag);std::filesystem::remove_all(p);return p;}
class Fake final: public dts::IBroker {
public:
    dts::TwsState s;
    void connect()override{s.start(dts::Clock::now());s.ready();}
    void disconnect()noexcept override{s.disconnect();}
    dts::ConnectionState state()const noexcept override{return s.state();}
    dts::RequestId resolve(const dts::ContractQuery& q)override{auto id=s.resolve(q,dts::Clock::now());s.contract(id,contract());s.contract_end(id);return id;}
    dts::RequestId subscribe(const dts::Contract& c)override{return s.subscribe(c,dts::Clock::now());}
    bool unsubscribe(dts::RequestId id)override{return s.unsubscribe(id);}
    void request_positions(dts::RequestId)override{}
    dts::RequestId subscribe_depth(const dts::DepthSpec& d)override{return s.depth(d,dts::Clock::now());}
    bool unsubscribe_depth(dts::RequestId id)override{return s.cancel_depth(id);}
    std::vector<dts::BrokerEvent> poll()override{return s.poll();}
};
void seed(const std::filesystem::path& p,bool crash) {
    dts::storage::TimeSeriesStore store({p,p.parent_path()/"backups"},"none");store.begin_depth("mock",42,spec());dts::DepthBook book(3);
    auto events=fixture();if(crash)events.pop_back();std::vector<dts::BrokerEvent> batch;std::vector<std::string> states;
    for(const auto& e:events){batch.emplace_back(e);book.apply(e);states.push_back(state_json(book));}
    store.record_depth_events("mock",batch);
    if(crash)std::_Exit(0);
    store.close();std::cout<<'[';for(std::size_t i=0;i<states.size();++i){if(i)std::cout<<',';std::cout<<states[i];}std::cout<<"]\n";
}
}
int main(int argc,char** argv) {
    try {
        if(argc>=2){seed(argv[1],argc==3);return 0;}
        test("direct scope",[]{auto s=spec();s.validate();s.venue="SMART";rejects([&]{s.validate();});s=spec();s.rows=11;rejects([&]{s.validate();});s=spec();s.contract.currency="EUR";rejects([&]{s.validate();});});
        test("decimal size",[]{check(dts::depth_size("12.5")==12.5);for(const auto* s:{"nan","-1","1e99"," 1","1x",""})rejects([&]{dts::depth_size(s);});});
        test("rows bounded",[]{rejects([]{dts::DepthBook b(0);});rejects([]{dts::DepthBook b(11);});});
        test("initial state",[]{dts::DepthBook b(3);check(!b.active()&&b.quality()=="not_started");b.apply(event(1,"start"));check(b.active()&&b.quality()=="one_sided_or_building");});
        test("insert update delete",[]{dts::DepthBook b(3);auto f=fixture();for(int i=0;i<7;++i)b.apply(f[i]);check(b.asks().size()==2&&b.bids().size()==1);check(b.asks()[0].price==100.12345678912345&&b.asks()[0].size=="14.25");});
        test("reset boundary",[]{dts::DepthBook b;for(auto e:fixture()){b.apply(e);if(e.sequence==8)check(b.asks().empty()&&b.epoch()==2);}check(!b.active());});
        test("duplicate local sequence",[]{dts::DepthBook b;b.apply(event(1,"start"));b.apply(event(2));b.apply(event(2));check(!b.structural_valid()&&b.asks().empty());});
        test("sequence gap latches until reset",[]{dts::DepthBook b;b.apply(event(1,"start"));b.apply(event(3));b.apply(event(4));check(!b.structural_valid());b.apply(event(5,"reset"));check(b.structural_valid()&&b.asks().empty());});
        test("unexpected start",[]{dts::DepthBook b;b.apply(event(1,"start"));b.apply(event(2,"start"));check(b.quality()=="unexpected_start");});
        test("terminal cannot reset",[]{dts::DepthBook b;b.apply(event(1,"start"));b.apply(event(2,"stop"));b.apply(event(3,"reset"));check(!b.active()&&b.quality()=="unexpected_reset");});
        test("invalid row latches",[]{dts::DepthBook b;b.apply(event(1,"start"));b.apply(event(2,"update",0,2,1));check(b.quality()=="missing_row_position");b.apply(event(3));check(b.asks().empty());});
        test("invalid prices",[]{for(double p:{0.0,-1.0,std::numeric_limits<double>::quiet_NaN(),1e99}){dts::DepthBook b;b.apply(event(1,"start"));b.apply(event(2,"update",0,0,0,p));check(b.quality()=="invalid_depth_price");}});
        test("crossed and locked",[]{dts::DepthBook b;b.apply(event(1,"start"));b.apply(event(2,"update",0,0,0,100));b.apply(event(3,"update",1,0,0,101));check(b.quality()=="crossed");b.apply(event(4,"update",1,0,1,100));check(b.quality()=="locked");});
        test("unordered rows and zero sizes",[]{dts::DepthBook b;b.apply(event(1,"start"));b.apply(event(2,"update",0,0,0,101));b.apply(event(3,"update",1,0,0,99));b.apply(event(4,"update",0,1,0,100));check(b.quality()=="unordered_rows");b.apply(event(5,"update",0,1,1,102,"0"));check(b.quality()=="zero_size_row");});
        test("insert displaces tail",[]{dts::DepthBook b(1);b.apply(event(1,"start"));b.apply(event(2));b.apply(event(3,"update",0,0,0,102));check(b.asks().size()==1&&b.asks()[0].price==102);});
        test("smart depth refused",[]{dts::DepthBook b;b.apply(event(1,"start"));auto e=event(2);e.smart_depth=true;b.apply(e);check(!b.structural_valid());});
        test("state cancellation",[]{dts::TwsState s;s.start(dts::Clock::now());s.ready();auto id=s.depth(spec(),dts::Clock::now());check(s.cancel_depth(id));auto events=s.poll();int n=0;for(auto& v:events)if(auto* e=std::get_if<dts::DepthEvent>(&v)){check(e->sequence==static_cast<unsigned>(++n));}check(n==2);});
        test("provider reset and halt",[]{dts::TwsState s;s.start(dts::Clock::now());s.ready();auto id=s.depth(spec(),dts::Clock::now());s.poll();s.error(id,317,"reset");auto a=s.poll();check(std::get<dts::DepthEvent>(a.front()).kind=="reset");s.error(id,316,"halt");a=s.poll();check(std::get<dts::DepthEvent>(a.front()).kind=="error");check(s.depth_cancellations().size()==1);});
        test("overflow boundary",[]{dts::TwsState s;s.start(dts::Clock::now());s.ready();auto id=s.depth(spec(),dts::Clock::now());s.poll();s.fail(-1010,"overflow");auto a=s.poll();const auto& e=std::get<dts::DepthEvent>(a.front());check(e.request_id==id&&e.kind=="gap");});
        test("store pages and replay",[]{auto p=temp("store");{dts::storage::TimeSeriesStore st({p,{}} ,"none");auto id=st.begin_depth("mock",42,spec());std::vector<dts::BrokerEvent> a;for(auto e:fixture())a.emplace_back(e);st.record_depth_events("mock",a);auto page=st.depth_events(id,0,0,3);check(page.rows.size()==3&&page.has_more);auto next=st.depth_events(id,page.next_after_id,page.through_id,100);check(next.rows.size()==13&&!next.has_more);check(std::get<std::string>(st.depth_session(id).at("state"))=="stop");st.close();}{dts::storage::TimeSeriesStore st({p,{}} ,"none");check(st.depth_events(1).rows.size()==16);st.close();}std::filesystem::remove_all(p);});
        test("nonfinite event retained",[]{auto p=temp("invalid");{dts::storage::TimeSeriesStore st({p,{}} ,"none");st.begin_depth("mock",42,spec());auto bad=event(2);bad.price=std::numeric_limits<double>::quiet_NaN();st.record_depth_events("mock",{event(1,"start"),bad});auto a=st.depth_events(1);check(std::holds_alternative<std::nullptr_t>(a.rows[1].at("price")));check(std::get<std::string>(a.rows[1].at("price_repr"))=="nan");st.close();}std::filesystem::remove_all(p);});
        test("decorator and service",[]{auto p=temp("service");{dts::storage::TimeSeriesStore st({p,{}} ,"none");auto inner=std::make_unique<Fake>();auto* raw=inner.get();dts::ReadOnlyService service(std::make_unique<dts::storage::RecordingBroker>(std::move(inner),st,"mock"));service.connect();service.poll();dts::ContractQuery q;q.symbol="SYNTHETIC";service.resolve(q);service.poll();auto id=service.subscribe_depth(9001,"TESTEX",3);auto e=event(0);e.request_id=id;raw->s.depth_update(e);service.poll();check(service.depth()->book.asks().size()==1);check(st.depth_events(1).rows.size()==2);service.unsubscribe_depth(id);check(st.depth_events(1).rows.size()==3);service.disconnect();st.close();}std::filesystem::remove_all(p);});
        std::cout<<tests<<" depth cases passed\n";return 0;
    } catch(const std::exception& e){std::cerr<<"FAIL after "<<tests<<": "<<e.what()<<'\n';return 1;}
}
