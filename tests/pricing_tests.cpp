#include <dts/pricing.hpp>
#include <algorithm>
#include <cmath>
#include <functional>
#include <iostream>
#include <limits>
#include <random>
#include <stdexcept>

using namespace dts::pricing;
namespace {
void require(bool ok) { if (!ok) throw std::runtime_error("assertion failed"); }
void close(double a, double b, double tol=1e-10) { require(std::abs(a-b) <= tol); }
template<class F> void rejects(F f) {
    bool failed=false; try { f(); } catch (const std::exception&) { failed=true; }
    require(failed);
}
int passed=0, failed=0;
void test(const char* name, const std::function<void()>& f) {
    try { f(); ++passed; std::cout << "PASS " << name << '\n'; }
    catch (const std::exception& e) { ++failed; std::cerr << "FAIL " << name << ": " << e.what() << '\n'; }
}
}
int main() {
    test("analytical call benchmark",[]{ close(black_scholes({}),10.450583572185565); });
    test("analytical put benchmark",[]{ Inputs x; x.right=Right::Put; close(black_scholes(x),5.573526022256971); });
    test("continuous dividend benchmark",[]{ Inputs x; x.dividend_yield=.02; close(black_scholes(x),9.227005508154036); });
    test("parity grid with negative rates and yields",[]{
        for(double s:{1.,50.,100.,200.}) for(double t:{.001,.5,5.}) for(double r:{-.1,0.,.2}) {
            Inputs x; x.spot=s; x.maturity=t; x.rate=r; x.dividend_yield=.03;
            const auto c=black_scholes(x); x.right=Right::Put;
            close(c-black_scholes(x),s*std::exp(-.03*t)-100*std::exp(-r*t),1e-10);
        }
    });
    test("price bounds and monotonicity",[]{
        Inputs x; double previous=0;
        for(double s:{0.,1.,50.,100.,200.,1000.}) {
            x.spot=s; const auto value=black_scholes(x);
            require(value>=previous && value<=s); previous=value;
            require(value+1e-10>=std::max(s-100*std::exp(-.05),0.));
        }
    });
    test("zero maturity has no random paths",[]{
        Inputs x; x.spot=120; x.maturity=0; const auto r=simulate(x,{});
        close(r.estimate.price,20); require(r.deterministic && r.paths_evaluated==0 && r.convergence.empty());
        require(r.estimate.standard_error==0 && r.estimate.ci_low==20 && r.independent_samples==0);
    });
    test("zero volatility discounted forward payoff",[]{
        Inputs x; x.volatility=0; x.dividend_yield=.02;
        close(simulate(x,{}).estimate.price,100*std::exp(-.02)-100*std::exp(-.05));
    });
    test("absorbing zero spot put",[]{
        Inputs x; x.spot=0; x.right=Right::Put;
        close(simulate(x,{}).estimate.price,100*std::exp(-.05));
    });
    test("nonfinite inputs rejected",[]{
        for(double n:{std::numeric_limits<double>::quiet_NaN(),std::numeric_limits<double>::infinity()}) {
            Inputs x; x.spot=n; rejects([&]{simulate(x,{});});
            x={}; x.volatility=n; rejects([&]{black_scholes(x);});
        }
    });
    test("domain bounds rejected",[]{
        Inputs x; x.spot=-1; rejects([&]{black_scholes(x);});
        x={}; x.strike=0; rejects([&]{simulate(x,{});});
        x={}; x.maturity=31; rejects([&]{simulate(x,{});});
        x={}; x.maturity=30; x.volatility=3; rejects([&]{simulate(x,{});});
        x={}; x.right=static_cast<Right>(99); rejects([&]{simulate(x,{});});
    });
    test("simulation bounds and enum rejected",[]{
        Config c; c.paths=999; rejects([&]{simulate({},c);});
        c.paths=max_paths+1; rejects([&]{simulate({},c);});
        c={}; c.method=static_cast<Method>(99); rejects([&]{simulate({},c);});
        rejects([]{simulate({}, {},std::chrono::milliseconds(0));});
    });
    test("antithetic odd path count rejected",[]{ Config c; c.paths=1001; c.method=Method::Antithetic; rejects([&]{simulate({},c);}); });
    test("64 bit seed round trip and overflow",[]{
        require(parse_seed("18446744073709551615")==std::numeric_limits<std::uint64_t>::max());
        require(parse_seed("0")==0); require(parse_seed("00042")==42);
        for(const auto* bad:{"", "-1", "+1", "1.5", "1e3", " 42", "18446744073709551616"}) rejects([&]{parse_seed(bad);});
    });
    test("plain seeded estimate agrees with benchmark",[]{
        auto r=simulate({},{}); require(std::abs(r.estimate.price-r.analytical)<6**r.estimate.standard_error);
        require(r.paths_evaluated==100000 && r.independent_samples==100000);
    });
    test("antithetic uncertainty uses half as many independent observations",[]{
        Config c; c.method=Method::Antithetic; auto r=simulate({},c);
        require(r.independent_samples==50000 && r.paths_evaluated==100000);
        close(*r.estimate.standard_error,std::sqrt(r.estimate.sample_variance/50000),1e-14);
        require(std::abs(r.estimate.price-r.analytical)<6**r.estimate.standard_error);
    });
    test("same binary repeatability",[]{
        const auto a=simulate({},{}), b=simulate({},{});
        require(a.estimate.price==b.estimate.price && a.estimate.standard_error==b.estimate.standard_error);
        require(a.convergence.size()==b.convergence.size());
        for(std::size_t i=0;i<a.convergence.size();++i) require(a.convergence[i].estimate.price==b.convergence[i].estimate.price);
    });
    test("different seed changes sample",[]{ Config c; c.seed=43; require(simulate({},c).estimate.price!=simulate({},{}).estimate.price); });
    test("convergence checkpoints are nested prefixes",[]{
        Config c; c.paths=102400; auto big=simulate({},c); c.paths=3200; auto small=simulate({},c);
        bool found=false; for(const auto& p:big.convergence) if(p.paths==3200) { found=true; require(p.estimate.price==small.estimate.price); }
        require(found && big.convergence.back().estimate.price==big.estimate.price && big.convergence.size()<30);
    });
    test("approximate standard error scales as inverse square root",[]{
        Config c; c.paths=20000; auto a=simulate({},c); c.paths=80000; auto b=simulate({},c);
        const auto ratio=*a.estimate.standard_error / *b.estimate.standard_error;
        require(ratio>1.8 && ratio<2.2);
    });
    test("antithetic variance reduction at equal payoff budget",[]{
        auto plain=simulate({},{}); Config c; c.method=Method::Antithetic;
        require(*simulate({},c).estimate.standard_error < *plain.estimate.standard_error);
    });
    test("unseen tail is not zero uncertainty",[]{
        Inputs x; x.strike=1e8; x.volatility=.001; auto r=simulate(x,{});
        require(!r.deterministic && r.estimate.price==0 && !r.estimate.ci_low && !r.estimate.standard_error);
        require(r.warnings.size()>=2 && r.positive_payoffs==0);
    });
    test("put and high-volatility warning",[]{
        Inputs x; x.right=Right::Put; x.volatility=1.5; auto r=simulate(x,{});
        require(!r.warnings.empty()); require(std::abs(r.estimate.price-r.analytical)<6**r.estimate.standard_error);
    });
    // Multiple fixed, independent seeds test estimator calibration. One run is
    // intentionally NOT required to fall in its nominal 95% interval.
    for (auto method:{Method::Plain,Method::Antithetic}) {
        test(method==Method::Plain?"plain multi-seed calibration":"antithetic multi-seed calibration",[method]{
            double sum=0,squares=0; int covered=0;
            for(std::uint64_t seed=0;seed<160;++seed) {
                Config c; c.paths=10000; c.seed=seed; c.method=method;
                auto r=simulate({},c); const auto z=(r.estimate.price-r.analytical)/ *r.estimate.standard_error;
                sum+=z; squares+=z*z; if(std::abs(z)<=1.959963984540054) ++covered;
            }
            require(std::abs(sum/160)<.3 && squares/160>.65 && squares/160<1.4);
            require(covered>=136 && covered<160);
        });
    }
    std::cout<<passed<<" cases passed; "<<failed<<" failed\n"; return failed?1:0;
}
