#include <dts/sde.hpp>
#include <cmath>
#include <functional>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
using namespace dts;
namespace {
int count=0;
void require(bool b,const char* text="assertion failed"){if(!b)throw std::runtime_error(text);}
void near(double a,double b,double tol=1e-10){require(std::abs(a-b)<=tol,"numeric comparison failed");}
void rejects(const std::function<void()>& fn){bool failed=false;try{fn();}catch(const std::exception&){failed=true;}require(failed,"expected rejection");}
void test(const char* name,const std::function<void()>& fn){fn();++count;std::cout<<"PASS "<<name<<'\n';}
}
int main(){try{
 pricing::Inputs x;sde::Config c;c.paths=2000;c.first_steps=4;c.levels=5;
 test("dyadic grid and work accounting",[&]{require(c.steps()==std::vector<unsigned>({4,8,16,32,64}));require(c.work()==2000*(64+2*124));});
 test("reject invalid grids and workloads",[&]{for(auto first:{0u,3u,2048u}){auto q=c;q.first_steps=first;rejects([&]{q.validate();});}auto q=c;q.levels=10;rejects([&]{q.validate();});q=c;q.paths=999;rejects([&]{q.validate();});q=c;q.paths=100001;rejects([&]{q.validate();});q=c;q.first_steps=1024;q.levels=2;q.paths=100000;rejects([&]{q.validate();});});
 test("one step formulas independently checked",[&]{auto p=sde::coupled_path(x,{.3},{1},true);near(p.exact_terminal,100*std::exp(.03+.06));near(p.levels[0].euler_terminal,111);near(p.levels[0].milstein_terminal,100*(1+.05+.06+.02*(.09-1)));require(p.levels[0].trace.size()==2);});
 test("coarse increments are sums of fine increments",[&]{std::vector<double> d{.1,-.2,.4,-.1};auto p=sde::coupled_path(x,d,{1,2,4},true);auto q=sde::coupled_path(x,{.2},{1},true);near(p.levels[0].euler_terminal,q.levels[0].euler_terminal);near(p.levels[0].milstein_terminal,q.levels[0].milstein_terminal);near(p.exact_terminal,q.exact_terminal);for(const auto& l:p.levels)near(l.trace.back().exact,p.exact_terminal);});
 test("uncoupled or malformed paths rejected",[&]{rejects([&]{sde::coupled_path(x,{1,2,3},{1});});rejects([&]{sde::coupled_path(x,{1,2,3,4},{1,4});});rejects([&]{sde::coupled_path(x,{std::numeric_limits<double>::infinity()},{1});});});
 test("zero maturity is deterministic",[&]{auto a=x;a.maturity=0;auto r=sde::run(a,c);require(r.normal_draws==0&&r.independent_paths==0&&r.paths_processed==1);near(r.exact_price.mean,0);near(*r.exact_price.standard_error,0);for(auto& l:r.levels)for(auto& v:l.schemes)near(v.absolute_terminal_error.mean,0);rejects([&]{sde::coupled_path(a,{.1},{1});});});
 test("zero volatility retains drift discretization bias",[&]{auto a=x;a.volatility=0;auto r=sde::run(a,c);require(r.deterministic);near(r.exact_price.mean,std::exp(-.05)*(100*std::exp(.05)-100));require(r.levels[0].schemes[0].absolute_terminal_error.mean>0);near(r.levels[0].schemes[0].price.mean,r.levels[0].schemes[1].price.mean);near(*r.levels[0].schemes[0].paired_payoff_bias.standard_error,0);});
 test("zero spot put and call",[&]{auto a=x;a.spot=0;a.right=pricing::Right::Put;auto r=sde::run(a,c);near(r.exact_price.mean,100*std::exp(-.05));near(r.levels[0].schemes[0].absolute_terminal_error.mean,0);});
 test("same seed reproduces non-timing results",[&]{auto a=sde::run(x,c),b=sde::run(x,c);require(a.exact_price.mean==b.exact_price.mean);for(std::size_t j=0;j<a.levels.size();++j)for(int k=0;k<2;++k)require(a.levels[j].schemes[k].paired_payoff_bias.mean==b.levels[j].schemes[k].paired_payoff_bias.mean);});
 test("analytical baseline and sample counts",[&]{auto r=sde::run(x,c);near(r.analytical_price,10.4505835721856,1e-10);require(r.normal_draws==c.paths*c.steps().back());require(r.independent_paths==c.paths);require(r.levels[0].sample_paths.size()==2);near(*r.exact_price.standard_error,std::sqrt(r.exact_price.sample_variance/c.paths));});
 test("coupling gives low variance payoff differences",[&]{auto r=sde::run(x,c);for(auto& l:r.levels)for(auto& v:l.schemes)require(v.paired_payoff_bias.sample_variance<v.price.sample_variance);});
 test("typical refinement improves mean terminal error",[&]{auto r=sde::run(x,c);for(int k=0;k<2;++k)require(r.levels.back().schemes[k].absolute_terminal_error.mean<r.levels.front().schemes[k].absolute_terminal_error.mean);require(r.levels.back().schemes[1].absolute_terminal_error.mean<r.levels.back().schemes[0].absolute_terminal_error.mean);});
 test("nonpositive schemes are counted and never clamped",[&]{auto a=x;a.volatility=3;auto q=c;q.first_steps=1;q.levels=1;auto r=sde::run(a,q);require(r.levels[0].schemes[0].paths_with_nonpositive>0);auto p=sde::coupled_path(a,{-1},{1},true);require(p.levels[0].euler_terminal<0&&p.levels[0].trace.back().euler<0);require(p.exact_terminal>0);});
 test("rare tail zero observed variance is not certainty",[&]{auto a=x;a.strike=1e8;auto r=sde::run(a,c);near(r.exact_price.mean,0);require(!r.exact_price.standard_error);require(!r.levels[0].schemes[0].paired_payoff_bias.standard_error);});
 test("put pricing and signed difference identity",[&]{auto a=x;a.right=pricing::Right::Put;auto r=sde::run(a,c);for(auto& l:r.levels)for(auto& v:l.schemes)near(v.price.mean-r.exact_price.mean,v.paired_payoff_bias.mean,1e-11);});
 test("invalid model and budget fail without partial results",[&]{auto a=x;a.spot=std::numeric_limits<double>::quiet_NaN();rejects([&]{sde::run(a,c);});rejects([&]{sde::run(x,c,std::chrono::milliseconds(0));});auto q=c;q.paths=100000;rejects([&]{sde::run(x,q,std::chrono::milliseconds(1));});});
 test("multi-seed exact MC calibration not single-run interval assertion",[&]{unsigned covered=0;auto q=c;q.first_steps=2;q.levels=2;q.paths=1000;for(unsigned i=0;i<32;++i){q.seed=i+97;auto r=sde::run(x,q);if(std::abs(r.exact_price.mean-r.analytical_price)<3*(*r.exact_price.standard_error))++covered;}require(covered>=27);});
 test("sample preview does not change simulated terminal identities",[&]{std::vector<double>d{.2,-.1,.3,-.2};auto p=sde::coupled_path(x,d,{2,4},true),q=sde::coupled_path(x,d,{2,4},false);for(std::size_t i=0;i<p.levels.size();++i){require(p.levels[i].euler_terminal==q.levels[i].euler_terminal);require(p.levels[i].milstein_terminal==q.levels[i].milstein_terminal);}});
 std::cout<<count<<" SDE cases passed\n";return 0;
}catch(const std::exception& e){std::cerr<<"FAIL after "<<count<<" cases: "<<e.what()<<'\n';return 1;}}
