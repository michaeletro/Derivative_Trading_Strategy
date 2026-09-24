#include <dts/greeks.hpp>
#include <cmath>
#include <functional>
#include <iostream>
#include <limits>
#include <stdexcept>

using namespace dts::pricing;
#define CHECK(x) do { if (!(x)) throw std::runtime_error(#x); } while(false)
void near(double a,double b,double tol) { CHECK(std::isfinite(a)); CHECK(std::abs(a-b)<=tol); }
void rejects(std::function<void()> fn) { bool caught=false;try{fn();}catch(const std::invalid_argument&){caught=true;}CHECK(caught); }
int main() {
    int cases=0;
    const auto test=[&](const char* name,auto fn){fn();++cases;std::cout<<"PASS "<<name<<'\n';};
    try {
        Inputs x;
        test("reference and unit conversions",[&]{
            const auto g=analytical_greeks(x);
            near(g.price,10.450583572185565,1e-12);near(*g.delta,.6368306511756191,1e-14);
            near(*g.gamma,.018762017345846895,1e-14);near(*g.vega*.01,.375240346916938,1e-13);
            near(*g.theta/365,-.01757267820941972,1e-14);near(*g.rho*.01,.5323248154537634,1e-13);
        });
        test("put-call derivative parity",[&]{
            auto b=x;b.rate=-.01;b.dividend_yield=.03;auto c=analytical_greeks(b);b.right=Right::Put;auto p=analytical_greeks(b);
            near(*c.delta-*p.delta,std::exp(-b.dividend_yield*b.maturity),1e-14);
            near(*c.gamma,*p.gamma,1e-15);near(*c.vega,*p.vega,1e-13);
            near(*c.rho-*p.rho,b.strike*b.maturity*std::exp(-b.rate*b.maturity),1e-12);
        });
        test("PDE identity and gamma-vega identity",[&]{
            for (auto right:{Right::Call,Right::Put}) for(double spot:{60.,100.,140.}){
                auto b=x;b.right=right;b.spot=spot;b.dividend_yield=.03;auto g=analytical_greeks(b);
                near(*g.theta+(b.rate-b.dividend_yield)*b.spot*(*g.delta)+.5*b.volatility*b.volatility*b.spot*b.spot*(*g.gamma)-b.rate*g.price,0,1e-12);
                near(*g.vega,b.spot*b.spot*b.volatility*b.maturity*(*g.gamma),1e-12);
            }
        });
        test("all analytical Greeks versus central finite differences",[&]{
            for(auto right:{Right::Call,Right::Put}){
                auto b=x;b.right=right;b.dividend_yield=.02;auto g=analytical_greeks(b);
                const auto fd=[&](double Inputs::*field,double h){auto u=b,d=b;u.*field+=h;d.*field-=h;return (black_scholes(u)-black_scholes(d))/(2*h);};
                near(*g.delta,fd(&Inputs::spot,1e-3),1e-8);near(*g.vega,fd(&Inputs::volatility,1e-5),1e-7);
                near(*g.rho,fd(&Inputs::rate,1e-5),1e-6);near(*g.theta,-fd(&Inputs::maturity,1e-5),1e-7);
                auto u=b,d=b;u.spot+=.01;d.spot-=.01;near(*g.gamma,(black_scholes(u)-2*g.price+black_scholes(d))/.0001,1e-8);
            }
        });
        test("boundary prices retained, no fabricated Greeks",[&]{
            for(auto field:{&Inputs::spot,&Inputs::volatility,&Inputs::maturity}){
                auto b=x;b.*field=0;auto g=analytical_greeks(b);CHECK(g.status=="unavailable");CHECK(!g.delta&&!g.theta&&!g.gamma&&!g.vega&&!g.rho);
                near(g.price,black_scholes(b),0);auto mc=simulate_greeks(b,{});CHECK(!mc.available&&mc.terminal_draws==0);
            }
        });
        test("input rejection",[&]{auto b=x;b.rate=std::numeric_limits<double>::infinity();rejects([&]{analytical_greeks(b);});});
        test("same seed repeatability",[&]{auto a=simulate_greeks(x,{}),b=simulate_greeks(x,{});near(a.delta.price,b.delta.price,0);near(a.vega.price,b.vega.price,0);});
        test("antithetic independent accounting",[&]{SensitivityConfig c;c.sampling.method=Method::Antithetic;auto r=simulate_greeks(x,c);CHECK(r.terminal_draws==100000&&r.independent_samples==50000);near(*r.delta.standard_error,std::sqrt(r.delta.sample_variance/50000),1e-16);CHECK(r.bumped_payoff_evaluations==0);});
        test("CRN difference matches coupled price runs",[&]{
            SensitivityConfig c;c.estimator=SensitivityMethod::CentralCRN;auto r=simulate_greeks(x,c);auto u=x,d=x;u.spot*=1.001;d.spot*=.999;
            near(r.delta.price,(simulate(u,c.sampling).estimate.price-simulate(d,c.sampling).estimate.price)/.2,1e-10);
            CHECK(r.bumped_payoff_evaluations==400000);near(r.delta_target,(black_scholes(u)-black_scholes(d))/.2,1e-12);
        });
        test("finite-bump bias is separate",[&]{SensitivityConfig c;c.estimator=SensitivityMethod::CentralCRN;c.relative_spot_bump=.1;auto r=simulate_greeks(x,c);CHECK(std::abs(r.delta_fd_bias)>1e-4);});
        test("invalid lower volatility bump rejects not clamps",[&]{SensitivityConfig c;c.estimator=SensitivityMethod::CentralCRN;c.volatility_bump=.1;auto b=x;b.volatility=.01;rejects([&]{simulate_greeks(b,c);});});
        test("invalid configs",[&]{SensitivityConfig c;c.sampling.paths=999;rejects([&]{simulate_greeks(x,c);});c={};c.volatility_bump=0;rejects([&]{simulate_greeks(x,c);});rejects([&]{simulate_greeks(x,{},std::chrono::milliseconds(0));});});
        test("rare tails withhold sampling interval",[&]{auto b=x;b.strike=1e8;b.volatility=.001;auto r=simulate_greeks(b,{});CHECK(r.available&&!r.delta.standard_error&&!r.vega.standard_error);CHECK(!r.warnings.empty());});
        test("multi-seed calibration pathwise and CRN, plain and antithetic",[&]{
            for(auto method:{SensitivityMethod::Pathwise,SensitivityMethod::CentralCRN}) for(auto pairing:{Method::Plain,Method::Antithetic}){
                double mean[2]={},squares[2]={};
                for(int seed=0;seed<64;++seed){SensitivityConfig c;c.estimator=method;c.sampling.method=pairing;c.sampling.paths=4000;c.sampling.seed=seed;auto r=simulate_greeks(x,c);
                    const double zs[]={(r.delta.price-r.delta_target)/ *r.delta.standard_error,(r.vega.price-r.vega_target)/ *r.vega.standard_error};
                    for(int i=0;i<2;++i){mean[i]+=zs[i]/64;squares[i]+=zs[i]*zs[i]/64;}}
                for(int i=0;i<2;++i){CHECK(std::abs(mean[i])<.6);CHECK(squares[i]>.35&&squares[i]<1.9);}
            }
        });
        test("zero scenario and approximation",[&]{auto r=reprice_scenario(x,{});CHECK(r.valid);near(*r.full_change,0,0);near(*r.residual,0,0);});
        test("full repricing and sign of elapsed time",[&]{auto r=reprice_scenario(x,{.1,.01,.01});auto b=x;b.spot=110;b.volatility=.21;b.maturity=.99;near(*r.shocked_price,black_scholes(b),1e-12);auto t=reprice_scenario(x,{0,0,1e-5});near(*t.full_change/1e-5,*analytical_greeks(x).theta,5e-5);});
        test("small shock approximation residual",[&]{auto r=reprice_scenario(x,{1e-4,1e-5,1e-5});CHECK(std::abs(*r.residual)<1e-5);});
        test("expiry scenario payoff and zero-vol scenario",[&]{auto r=reprice_scenario(x,{.1,0,1});near(*r.shocked_price,10,1e-12);auto z=reprice_scenario(x,{0,-.2,0});CHECK(z.valid);});
        test("invalid shocks return explicit nulls",[&]{for(auto s:{Shock{0,-.3,0},Shock{0,0,2},Shock{-1,0,0}}){auto r=reprice_scenario(x,s);CHECK(!r.valid&&!r.full_change&&!r.approximation&&!r.shocked_price);}});
        test("boundary base full repricing but no approximation",[&]{auto b=x;b.volatility=0;auto r=reprice_scenario(b,{.1,.01,0});CHECK(r.valid&&r.full_change&&!r.approximation);});
        test("grid count, center and invalid cells",[&]{auto grid=scenario_grid(x,.2,.05,0);CHECK(grid.size()==231);near(*grid[115].full_change,0,0);auto b=x;b.volatility=.02;int invalid=0;for(const auto& s:scenario_grid(b,.2,.05,0))invalid+=!s.valid;CHECK(invalid>0);});
        test("curve size and analytical center",[&]{auto curve=spot_curve(x,.2);CHECK(curve.size()==41);near(curve[20].spot,100,0);near(*curve[20].greeks.delta,*analytical_greeks(x).delta,0);});
        test("grid/curve bounds",[&]{rejects([&]{scenario_grid(x,.6,.1,0);});rejects([&]{spot_curve(x,-1);});});
        std::cout<<"PASS "<<cases<<" sensitivity/scenario cases\n";
    } catch(const std::exception& e){std::cerr<<"FAIL after "<<cases<<" cases: "<<e.what()<<'\n';return 1;}
}
