#include <dts/sde.hpp>
#include "simulation_detail.hpp"
#include <algorithm>
#include <cmath>
#include <limits>
#include <numeric>

namespace dts::sde {
namespace {
using Clock = std::chrono::steady_clock;
bool power_two(unsigned n) { return n && !(n & (n-1)); }
double elapsed(Clock::time_point t) { return std::chrono::duration<double,std::milli>(Clock::now()-t).count(); }
void finite(double v) { if(!std::isfinite(v)) throw std::overflow_error("Nonfinite SDE calculation; no partial result accepted"); }
Statistic summary(const pricing::detail::Moments& m, bool deterministic) {
    const auto e=m.estimate(); Statistic s{e.price,e.sample_variance,e.standard_error,e.ci_low,e.ci_high};
    if(deterministic) {s.sample_variance=0;s.standard_error=0;s.ci_low=s.ci_high=s.mean;}
    finite(s.mean);finite(s.sample_variance);
    if(s.standard_error)finite(*s.standard_error);
    if(s.ci_low)finite(*s.ci_low);
    if(s.ci_high)finite(*s.ci_high);
    return s;
}
double payoff(const pricing::Inputs& x,double s) {
    const double v=std::exp(-x.rate*x.maturity)*std::max(0.0,x.right==pricing::Right::Call?s-x.strike:x.strike-s);
    finite(v);return v;
}
struct Accumulator {
    pricing::detail::Moments price,absolute_error,squared_error,bias;
    std::uint64_t nonpositive=0,affected=0,hits=0;
    double kernel_ms=0;
};
}
void Config::validate() const {
    if(paths<1000||paths>100000)throw std::invalid_argument("SDE paths must be 1000..100000 independent paths");
    if(!power_two(first_steps)||first_steps>1024||levels<1||levels>9||
       (std::uint64_t(first_steps)<<(levels-1))>2048)
        throw std::invalid_argument("Use 1..9 dyadic levels, power-of-two first steps, and at most 2048 finest steps");
    if(work()>maximum_work)throw std::invalid_argument("SDE work cap exceeded (50 million normal draws plus scheme updates); reduce paths or steps");
}
std::vector<unsigned> Config::steps() const {
    if(!power_two(first_steps)||levels<1||levels>9||first_steps>1024||
       (std::uint64_t(first_steps)<<(levels-1))>2048)throw std::invalid_argument("Invalid SDE grid");
    std::vector<unsigned> out;for(unsigned j=0;j<levels;++j)out.push_back(first_steps<<j);return out;
}
std::uint64_t Config::work() const {
    const auto grid=steps();const std::uint64_t unit=grid.back()+2*std::accumulate(grid.begin(),grid.end(),std::uint64_t(0));
    if(paths>maximum_work/unit)return maximum_work+1;
    return paths*unit;
}
CoupledPath coupled_path(const pricing::Inputs& x,const std::vector<double>& fine,const std::vector<unsigned>& grid,bool save_trace) {
    x.validate();
    if(fine.empty()||fine.size()>2048||!power_two(static_cast<unsigned>(fine.size()))||grid.empty()||grid.size()>9)
        throw std::invalid_argument("Invalid fine Brownian grid");
    for(std::size_t i=0;i<grid.size();++i)if(!power_two(grid[i])||grid[i]>fine.size()||(i&&grid[i]!=2*grid[i-1]))
        throw std::invalid_argument("SDE levels must be consecutive dyadic refinements");
    if(grid.back()!=fine.size())throw std::invalid_argument("Finest requested grid must match supplied increments");
    for(double dw:fine){finite(dw);if(x.maturity==0&&dw!=0)throw std::invalid_argument("Zero maturity needs zero increments");}
    std::vector<std::vector<double>> increments(grid.size());increments.back()=fine;
    for(std::size_t level=grid.size()-1;level>0;--level) {
        auto& coarse=increments[level-1];const auto& finer=increments[level];coarse.resize(grid[level-1]);
        for(std::size_t k=0;k<coarse.size();++k){coarse[k]=finer[2*k]+finer[2*k+1];finite(coarse[k]);}
    }
    const double a=x.rate-x.dividend_yield,variance=x.volatility*x.volatility;
    const double wt=std::accumulate(fine.begin(),fine.end(),0.0);
    CoupledPath out;out.exact_terminal=x.spot==0?0:x.spot*std::exp((a-variance/2)*x.maturity+x.volatility*wt);finite(out.exact_terminal);
    for(std::size_t level=0;level<grid.size();++level) {
        PathLevel p;p.steps=grid[level];const double h=x.maturity/p.steps;
        if(save_trace){p.trace.resize(p.steps+1);p.trace[0]={0,x.spot,x.spot,x.spot};double w=0;
            for(unsigned k=0;k<p.steps;++k){w+=increments[level][k];const double t=(k+1)*h;
                const double exact=x.spot==0?0:x.spot*std::exp((a-variance/2)*t+x.volatility*w);finite(exact);p.trace[k+1].time=t;p.trace[k+1].exact=exact;}}
        for(unsigned scheme=0;scheme<2;++scheme) {
            double s=x.spot;const auto start=Clock::now();
            for(unsigned k=0;k<p.steps;++k) {
                const double dw=increments[level][k];
                // No positivity clipping. Clipping would be a different scheme.
                s*=1+a*h+x.volatility*dw+(scheme?0.5*variance*(dw*dw-h):0.0);finite(s);
                if(s<=0)++p.nonpositive_steps[scheme];
                if(save_trace){if(scheme)p.trace[k+1].milstein=s;else p.trace[k+1].euler=s;}
            }
            p.kernel_ms[scheme]=elapsed(start);
            if(scheme)p.milstein_terminal=s;else p.euler_terminal=s;
        }
        out.levels.push_back(std::move(p));
    }
    return out;
}
Result run(const pricing::Inputs& x,const Config& cfg,std::chrono::milliseconds budget) {
    x.validate();cfg.validate();if(budget.count()<=0)throw std::invalid_argument("Positive SDE runtime budget required");
    const auto started=Clock::now(),deadline=started+budget;const auto grid=cfg.steps();
    Result r;r.analytical_price=pricing::black_scholes(x);
    r.deterministic=x.spot==0||x.volatility==0||x.maturity==0;
    r.independent_paths=r.deterministic?0:cfg.paths;
    const std::uint64_t count=r.deterministic?1:cfg.paths;
    std::vector<std::array<Accumulator,2>> accumulators(grid.size());
    r.levels.resize(grid.size());
    for(std::size_t j=0;j<grid.size();++j){r.levels[j].steps=grid[j];r.levels[j].step_years=x.maturity/grid[j];r.levels[j].scheme_updates=count*grid[j];}
    pricing::detail::Normals normals(cfg.seed);pricing::detail::Moments exact;
    std::vector<double> increments(grid.back(),0);const double root_h=std::sqrt(x.maturity/grid.back());
    for(std::uint64_t i=0;i<count;++i) {
        if(i%32==0&&Clock::now()>deadline)throw std::runtime_error("SDE runtime budget exceeded; reduce workload; no partial result saved");
        if(!r.deterministic){const auto t=Clock::now();for(auto& dw:increments)dw=root_h*normals.next();r.brownian_generation_ms+=elapsed(t);}
        const auto p=coupled_path(x,increments,grid,i<2);
        const double reference=payoff(x,p.exact_terminal);exact.add(reference);if(reference>0)++r.exact_positive_payoffs;
        for(std::size_t level=0;level<grid.size();++level){const auto& path=p.levels[level];
            if(i<2)r.levels[level].sample_paths.push_back(path.trace);
            for(unsigned scheme=0;scheme<2;++scheme){auto& a=accumulators[level][scheme];
                const double terminal=scheme?path.milstein_terminal:path.euler_terminal;
                const double value=payoff(x,terminal),error=terminal-p.exact_terminal;
                a.price.add(value);a.absolute_error.add(std::abs(error));a.squared_error.add(error*error);a.bias.add(value-reference);
                a.nonpositive+=path.nonpositive_steps[scheme];if(path.nonpositive_steps[scheme])++a.affected;if(value>0)++a.hits;
                a.kernel_ms+=path.kernel_ms[scheme];
            }
        }
    }
    r.paths_processed=count;r.normal_draws=r.deterministic?0:count*grid.back();r.exact_price=summary(exact,r.deterministic);
    for(std::size_t j=0;j<grid.size();++j)for(unsigned k=0;k<2;++k){const auto& a=accumulators[j][k];auto& s=r.levels[j].schemes[k];
        s.price=summary(a.price,r.deterministic);s.absolute_terminal_error=summary(a.absolute_error,r.deterministic);
        // Structural equality when spot=0, T=0 or deterministic drift=0 allows
        // zero uncertainty. Other zero-variation stochastic samples remain null.
        s.paired_payoff_bias=summary(a.bias,r.deterministic);
        s.root_mean_squared_terminal_error=std::sqrt(a.squared_error.mean);finite(s.root_mean_squared_terminal_error);
        s.kernel_ms=a.kernel_ms;s.nonpositive_steps=a.nonpositive;s.paths_with_nonpositive=a.affected;s.positive_payoffs=a.hits;
        if(a.affected)r.warnings.push_back(std::string(k?"Milstein":"Euler")+" at "+std::to_string(grid[j])+" steps produced nonpositive states; these were not clipped or discarded.");
        if(!r.deterministic&&(!s.paired_payoff_bias.standard_error||a.hits<30))
            r.warnings.push_back(std::string(k?"Milstein":"Euler")+" at "+std::to_string(grid[j])+" steps: zero observed variation or few payoff hits; sampling uncertainty may be unresolved.");
    }
    if(r.deterministic)r.warnings.push_back("Deterministic model limit: one path evaluated, no random draws; discrete drift error can still remain.");
    else if(!r.exact_price.standard_error||r.exact_positive_payoffs<30)r.warnings.push_back("Exact payoff sampling has low observed variation or few tail hits; a zero estimate is not certainty.");
    r.warnings.push_back("Intervals are pointwise sampling intervals, not model-risk bounds or simultaneous convergence bands. Levels are correlated through shared Brownian increments.");
    r.warnings.push_back("Timings include instrumentation: per-scheme kernels exclude shared RNG, exact reference, aggregation and statistics; this is not an equal-total-cost benchmark.");
    r.runtime_ms=elapsed(started);if(Clock::now()>deadline)throw std::runtime_error("SDE runtime budget exceeded; no partial result saved");
    return r;
}
} // namespace dts::sde
