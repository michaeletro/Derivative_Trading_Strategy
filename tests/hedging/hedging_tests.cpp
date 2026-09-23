#include <dts/hedging.hpp>
#include <dts/greeks.hpp>
#include <algorithm>
#include <cmath>
#include <functional>
#include <iostream>
#include <limits>
#include <numeric>
#include <stdexcept>
using namespace dts::hedging;
namespace {
int count=0;
void ck(bool v){if(!v)throw std::runtime_error("Assertion failed");}
void near(double a,double b,double tol=1e-9){ck(std::abs(a-b)<=tol*std::max({1.0,std::abs(a),std::abs(b)}));}
void rejects(const std::function<void()>& f){bool bad=false;try{f();}catch(const std::exception&){bad=true;}ck(bad);}
void test(const char* name,const std::function<void()>& f){f();++count;std::cout<<"PASS "<<name<<'\n';}
}
int main(){try{
 Inputs x;Config c;c.paths=2000;c.first_steps=4;c.levels=5;
 test("entry and nonzero costs reconcile",[]{CashLedger l(10,1,.05,10,.2);auto a=l.rebalance(0,100,.6);near(a.cost,.26);near(a.cash_after,-50.26);near(a.hedge_value,9.74);near(a.balance_residual,0);});
 test("negative cash compounds at the SAME rate",[]{CashLedger l(10,1,.05,0,0);l.rebalance(0,100,.6);auto a=l.rebalance(.5,110,.7);near(a.financing,-50*std::expm1(.025));near(a.cash_after,-50*std::exp(.025)-11);ck(a.financing<0);});
 test("sale releases cash and no external capital appears",[]{CashLedger l(10,1,0,0,0);l.rebalance(0,100,.6);auto a=l.rebalance(.5,120,.3);near(a.cash_after,-14);near(a.hedge_value,22);near(a.trade_notional,-36);});
 test("liquidation charged and payoff settled exactly once",[]{CashLedger l(10,1,0,10,.2);l.rebalance(0,100,.6);auto a=l.settle(1,110,10);near(a.cost,.266);near(l.cash(),5.474);near(l.shares(),0);near(a.balance_residual,0);rejects([&]{l.settle(1,110,10);});});
 test("unchanged holdings have no fixed trading fee",[]{CashLedger l(10,1,0,10,.2);l.rebalance(0,100,.6);auto a=l.rebalance(.5,110,.6);near(a.cost,0);ck(l.trades()==1);l.settle(1,110,10);ck(l.trades()==2);});
 test("time ordering and invalid ledger values rejected",[]{CashLedger l(10,1,0,0,0);rejects([&]{l.rebalance(.1,100,.5);});rejects([&]{l.settle(1,100,0);});l.rebalance(0,100,.5);rejects([&]{l.rebalance(0,100,.6);});rejects([&]{l.rebalance(1,100,.6);});rejects([&]{l.rebalance(.5,0,.5);});rejects([&]{l.settle(.9,100,0);});rejects([&]{l.settle(1,100,-1);});});
 test("cost future value reconciles to explicit sums",[]{CashLedger l(10,1,.05,10,.2);auto a=l.rebalance(0,100,.6);auto b=l.rebalance(.5,110,.7);auto z=l.settle(1,90,0);near(z.costs_at_time,a.cost*std::exp(.05)+b.cost*std::exp(.025)+z.cost);});
 test("negative rate lending and borrowing",[]{CashLedger l(10,1,-.05,0,0);l.rebalance(0,100,0);auto a=l.settle(1,100,0);near(a.cash_after,10*std::exp(-.05));});
 test("unhedged retains premium until settlement",[&]{auto a=on_path(x,{100,90,110},Policy::Unhedged,2);near(a.terminal_error,dts::pricing::black_scholes(x.option)*std::exp(.05)-10);ck(a.trades==0);near(a.nominal_costs,0);});
 test("initial delta matches independent Greeks",[&]{auto a=on_path(x,{100,90,110},Policy::InitialDelta,2);const auto delta=*dts::pricing::analytical_greeks(x.option).delta;near(a.ledger[0].shares_after,delta);near(a.ledger[1].shares_after,delta);near(a.terminal_error,(dts::pricing::black_scholes(x.option)-100*delta)*std::exp(.05)+110*delta-10);});
 test("put hedge permits negative stock holdings",[&]{auto p=x;p.option.right=dts::pricing::Right::Put;auto a=on_path(p,{100,80,90},Policy::Periodic,2);ck(a.ledger[0].shares_after<0);ck(a.ledger.back().shares_after==0);near(a.ledger.back().settlement,10);});
 test("changing valid future prices cannot change prior decisions",[&]{auto a=on_path(x,{100,101,99,102,95},Policy::Periodic,4),b=on_path(x,{100,101,99,80,120},Policy::Periodic,4);for(int k=0;k<=2;++k){ck(a.ledger[k].cash_after==b.ledger[k].cash_after);ck(a.ledger[k].shares_after==b.ledger[k].shares_after);}});
 test("path and hedge grid validation",[&]{rejects([&]{on_path(x,{100,-1},Policy::Periodic,1);});rejects([&]{on_path(x,{100,101,99,100},Policy::Periodic,2);});rejects([&]{on_path(x,{99,100},Policy::Periodic,1);});});
 test("zero dividends positive hedge model boundaries",[&]{for(auto key:{0,1,2,3}){auto a=x;if(key==0)a.option.dividend_yield=.01;if(key==1)a.option.volatility=0;if(key==2)a.option.maturity=0;if(key==3)a.path_volatility=-1;rejects([&]{a.validate();});}});
 test("work and path bounds",[&]{near(c.work(),2000*(64+124+4));for(auto n:{0u,3u,2048u}){auto a=c;a.first_steps=n;rejects([&]{a.validate();});}auto a=c;a.paths=999;rejects([&]{a.validate();});a=c;a.paths=50001;rejects([&]{a.validate();});a=c;a.first_steps=1024;a.paths=50000;a.levels=1;rejects([&]{a.validate();});});
 const auto r=run(x,c);
 test("counts premiums and selected-policy ledgers",[&]{near(r.premium,10.450583572185565);ck(r.paths_processed==2000&&r.normal_draws==128000&&r.policies.size()==7);for(const auto& p:r.policies){ck(p.sample_ledgers.size()==2);ck(p.sample_ledgers[0].size()==p.intervals+1);near(p.sample_ledgers[0].back().surplus,p.sample_ledgers[0].back().cash_after);}});
 test("all policies observe identical common-time prices",[&]{for(const auto& p:r.policies){const auto& fine=r.policies.back().sample_ledgers[0];for(std::size_t k=0;k<p.sample_ledgers[0].size();++k)ck(p.sample_ledgers[0][k].spot==fine[k*64/p.intervals].spot);}});
 test("standard errors RMSE and histogram mass",[&]{for(const auto& p:r.policies){const auto& d=p.error;near(*d.mean.standard_error,std::sqrt(d.mean.sample_variance/2000));near(d.rmse*d.rmse,d.mean.price*d.mean.price+d.mean.sample_variance*1999/2000);ck(d.q05<=d.q50&&d.q50<=d.q95);ck(std::accumulate(d.bin_counts.begin(),d.bin_counts.end(),std::uint64_t{0})==2000);near(p.max_balance_residual,0,1e-8);}});
 test("paired-policy mean identity",[&]{for(const auto& p:r.policies)near(p.paired_minus_initial.price,p.error.mean.price-r.policies[1].error.mean.price);near(*r.policies[1].paired_minus_initial.standard_error,0);});
 test("matched-model aggregate refinement reduces dispersion",[&]{ck(r.policies.back().error.standard_deviation<r.policies[2].error.standard_deviation);ck(r.policies.back().error.rmse<r.policies[1].error.rmse);});
 test("same seed reproduces numerical results",[&]{const auto b=run(x,c);for(std::size_t k=0;k<r.policies.size();++k){ck(r.policies[k].error.mean.price==b.policies[k].error.mean.price);ck(r.policies[k].error.q05==b.policies[k].error.q05);}});
 test("costs reduce each path by their financed terminal value",[&]{auto a=x;a.cost_bps=5;a.fixed_cost=.01;const auto z=run(a,c);for(std::size_t k=0;k<r.policies.size();++k){near(r.policies[k].error.mean.price-z.policies[k].error.mean.price,z.policies[k].mean_terminal_cost);for(unsigned p=0;p<2;++p)near(r.policies[k].sample_ledgers[p].back().cash_after-z.policies[k].sample_ledgers[p].back().cash_after,z.policies[k].sample_ledgers[p].back().costs_at_time);}});
 test("one-interval periodic equals initial delta exactly",[&]{auto a=c;a.first_steps=1;a.levels=1;const auto z=run(x,a);ck(z.policies[1].error.mean.price==z.policies[2].error.mean.price);near(*z.policies[2].paired_minus_initial.standard_error,0);});
 test("deterministic path uses no random samples",[&]{auto a=x;a.path_volatility=0;const auto z=run(a,c);ck(z.deterministic&&z.paths_processed==1&&z.normal_draws==0);for(const auto& p:z.policies){near(*p.error.mean.standard_error,0);ck(p.sample_ledgers.size()==1);}});
 test("mismatched hedge volatility changes premium not stock paths",[&]{auto a=x;a.option.volatility=.3;const auto z=run(a,c);ck(z.premium!=r.premium);for(unsigned k=0;k<=64;++k)ck(z.policies.back().sample_ledgers[0][k].spot==r.policies.back().sample_ledgers[0][k].spot);});
 test("ledger capture does not affect decisions",[&]{auto a=on_path(x,{100,90,110},Policy::Periodic,2,true),b=on_path(x,{100,90,110},Policy::Periodic,2,false);ck(a.terminal_error==b.terminal_error);ck(b.ledger.empty());});
 test("runtime budget failure publishes no partial result",[&]{rejects([&]{run(x,c,std::chrono::milliseconds(0));});auto a=c;a.paths=50000;a.levels=1;rejects([&]{run(x,a,std::chrono::milliseconds(1));});});
 test("multi-seed matched Q mean calibration",[&]{unsigned good=0;auto a=c;a.paths=1000;a.first_steps=4;a.levels=3;for(unsigned i=0;i<24;++i){a.seed=100+i;const auto z=run(x,a);const auto e=z.policies.back().error.mean;good+=std::abs(e.price)<3*(*e.standard_error);}ck(good>=20);});
 std::cout<<count<<" hedge cases passed\n";return 0;
}catch(const std::exception& e){std::cerr<<"FAIL after "<<count<<": "<<e.what()<<'\n';return 1;}}
