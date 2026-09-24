#include <dts/replay.hpp>
#include <cmath>
#include <functional>
#include <iostream>
#include <stdexcept>
using namespace dts::research;
namespace {
void check(bool b){if(!b)throw std::runtime_error("Replay assertion failed");}
void near(double x,double y){check(std::abs(x-y)<=1e-12*(1+std::abs(y)));}
template<class F> void rejects(F f){bool rejected=false;try{f();}catch(const std::exception&){rejected=true;}check(rejected);}
Snapshot sample(bool minute=false) {
    Snapshot s;s.dataset_id=1;s.request_cutoff=10;s.source="synthetic_test";s.spec.contract.id=123;s.spec.contract.symbol="SYNTHETIC";
    s.spec.contract.currency="USD";s.spec.contract.exchange="SMART";s.spec.contract.multiplier=1;s.spec.bar_size=minute?"1 min":"1 day";
    s.time_basis=minute?"UTC_epoch_seconds":"provider_session_date";s.adjustment_policy="provider_native_not_normalized";
    s.window={1767571200,1767571200+(minute?60:86400)*80};
    for(int i=0;i<80;++i){Observation r;r.coordinate_s=s.window.start+i*(minute?60:86400);r.version_id=i+1;r.request_id=1;r.bar.time=minute?std::to_string(r.coordinate_s):dts::utc_text(r.coordinate_s,"%Y%m%d");
        const auto price=100*std::exp(.003*i+.01*(i%3));r.bar.open=r.bar.close=price;r.bar.high=price+1;r.bar.low=price-1;s.observations.push_back(r);}
    return s;
}
}
int main(int argc,char** argv){
    if(argc==3&&std::string(argv[1])=="--hash"){std::cout<<sha256(argv[2]);return 0;}
    int cases=0;auto test=[&](const char* name,auto fn){try{fn();++cases;}catch(...){std::cerr<<"FAIL "<<name<<'\n';throw;}};
    try {
        test("SHA empty",[]{check(sha256("")=="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");});
        test("SHA abc",[]{check(sha256("abc")=="ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");});
        test("SHA million bytes",[]{check(sha256(std::string(1000000,'a'))=="cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0");});
        test("stable manifest",[]{auto s=sample();auto h=snapshot_fingerprint(s);s.name="different";s.id=999;s.created_ms=999;check(snapshot_fingerprint(s)==h);s.spec.price_type="BID";check(snapshot_fingerprint(s)!=h);});
        test("metadata provenance fingerprint",[]{auto s=sample();auto h=snapshot_fingerprint(s);s.observations[0].response_finished_ms=123;check(snapshot_fingerprint(s)!=h);});
        test("coverage fingerprint",[]{auto s=sample();auto h=snapshot_fingerprint(s);s.uncovered_intervals={{s.window.start,s.window.start+86400}};check(snapshot_fingerprint(s)!=h);check(!inspect(s).response_coverage_complete);});
        test("strict shape",[]{auto s=sample();s.observations[1].coordinate_s=s.observations[0].coordinate_s;rejects([&]{s.validate();});});
        test("invalid OHLC",[]{auto s=sample();s.observations[0].bar.low=1e10;rejects([&]{s.validate();});});
        test("bounds",[]{auto s=sample();rejects([&]{replay(s,{},81);});check(replay(s,{},0).points.empty());});
        test("config bounds",[]{rejects([]{ReplayConfig{{1},252}.validate();});rejects([]{ReplayConfig{{20,20},252}.validate();});rejects([]{ReplayConfig{{60,20},252}.validate();});rejects([]{ReplayConfig{{20},INFINITY}.validate();});});
        test("warmup exactly n returns",[]{auto s=sample();auto a=replay(s,{{20},252},20),b=replay(s,{{20},252},21);check(!a.points.back().rolling[0].annualized_volatility);check(b.points.back().rolling[0].annualized_volatility.has_value());});
        test("sample standard deviation",[]{auto s=sample();ReplayConfig c{{2},1};auto r=replay(s,c,3);const auto a=std::log(s.observations[1].bar.close/s.observations[0].bar.close),b=std::log(s.observations[2].bar.close/s.observations[1].bar.close);near(*r.points[2].rolling[0].mean_log_return,(a+b)/2);near(*r.points[2].rolling[0].annualized_volatility,std::abs(a-b)/std::sqrt(2.));});
        test("annualization sqrt",[]{auto s=sample();auto a=replay(s,{{20},1},80),b=replay(s,{{20},4},80);near(*b.points.back().rolling[0].annualized_volatility,2**a.points.back().rolling[0].annualized_volatility);});
        test("prefix invariant versus batch size",[]{auto s=sample();auto full=replay(s,{},80);for(std::size_t i=1;i<=80;++i){auto p=replay(s,{},i);check(p.points.back().log_return==full.points[i-1].log_return);check(p.points.back().rolling.back().annualized_volatility==full.points[i-1].rolling.back().annualized_volatility);}});
        test("future prices do not alter current results",[]{auto s=sample();auto before=replay(s,{},30);for(std::size_t i=30;i<80;++i){s.observations[i].bar.open*=2;s.observations[i].bar.close*=2;s.observations[i].bar.low*=2;s.observations[i].bar.high*=2;}auto after=replay(s,{},30);check(before.points.back().log_return==after.points.back().log_return);check(before.points.back().rolling[0].annualized_volatility==after.points.back().rolling[0].annualized_volatility);});
        test("daily not fake exchange time",[]{auto r=replay(sample(),{},1);check(!r.points[0].available_s);check(r.availability_policy=="completed_session_ordinal_no_exchange_timestamp");});
        test("minute releases at end",[]{auto s=sample(true);auto r=replay(s,{{2},98280},1);check(r.points[0].available_s==s.observations[0].coordinate_s+60);});
        test("minute discontinuity resets",[]{auto s=sample(true);s.observations.erase(s.observations.begin()+25);auto r=replay(s,{{2},1},30);check(!r.points[25].log_return);check(!r.points[26].rolling[0].annualized_volatility);check(r.points[27].rolling[0].annualized_volatility.has_value());check(inspect(s).discontinuities==1);});
        test("daily spans are disclosed not fabricated",[]{auto s=sample();s.observations.erase(s.observations.begin()+25);auto r=replay(s,{},30);check(r.points[25].log_return.has_value());check(r.points[25].status=="adjacent_observation_calendar_span_unverified");});
        test("nonpositive close resets",[]{auto s=sample();auto& b=s.observations[25].bar;b.open=b.high=b.low=b.close=0;auto r=replay(s,{{2},252},30);check(!r.points[25].log_return&&!r.points[26].log_return);check(r.points[28].rolling[0].annualized_volatility.has_value());check(inspect(s).nonpositive_closes==1);});
        test("constant prices yield zero sample vol",[]{auto s=sample();for(auto& r:s.observations){r.bar.open=r.bar.close=100;r.bar.low=99;r.bar.high=101;}auto r=replay(s,{},80);check(*r.points.back().rolling[0].annualized_volatility==0);});
        test("invalid source clock rejected",[]{auto s=sample();s.time_basis="exchange_timestamp";rejects([&]{s.validate();});});
        test("request cutoff enforced",[]{auto s=sample();s.observations[0].request_id=11;rejects([&]{s.validate();});});
        std::cout<<cases<<" replay numerical cases passed\n";
    }catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}
}
