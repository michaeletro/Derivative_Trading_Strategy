#include <dts/hedging.hpp>
#include "simulation_detail.hpp"
#include <algorithm>
#include <cmath>
#include <limits>
#include <numeric>
#include <stdexcept>

namespace dts::hedging {
namespace {
void finite(double x) {if(!std::isfinite(x))throw std::overflow_error("Nonfinite hedge accounting; no partial result");}
void bound(double x,double low,double high,const char* message) {
    if(!std::isfinite(x)||x<low||x>high)throw std::invalid_argument(message);
}
bool power_two(unsigned n){return n&&!(n&(n-1));}
double delta_at(const pricing::Inputs& x,double spot,double tau) {
    // q=0 and sigma,tau>0 are validated by the hedge experiment. No terminal
    // delta is evaluated. Use erfc for both tails, avoiding 1-N(d1) cancellation.
    const double d=(std::log(spot)-std::log(x.strike)+(x.rate+0.5*x.volatility*x.volatility)*tau)/(x.volatility*std::sqrt(tau));
    return x.right==pricing::Right::Call?0.5*std::erfc(-d/std::sqrt(2.0)):-0.5*std::erfc(d/std::sqrt(2.0));
}
double payoff(const pricing::Inputs& x,double spot) {
    return std::max(0.0,x.right==pricing::Right::Call?spot-x.strike:x.strike-spot);
}
pricing::Estimate estimate(const pricing::detail::Moments& m,bool deterministic) {
    auto e=m.estimate();if(deterministic){e.standard_error=0;e.ci_low=e.price;e.ci_high=e.price;}return e;
}
Distribution distribution(std::vector<double> values,bool deterministic) {
    Distribution d;pricing::detail::Moments m;long double squares=0;std::uint64_t deficits=0;
    for(double x:values){m.add(x);squares+=static_cast<long double>(x)*x;deficits+=x<0;}
    d.mean=estimate(m,deterministic);d.standard_deviation=std::sqrt(d.mean.sample_variance);
    d.rmse=std::sqrt(static_cast<double>(squares/values.size()));finite(d.rmse);
    std::sort(values.begin(),values.end());d.minimum=values.front();d.maximum=values.back();
    const auto quantile=[&](double p){const double index=p*(values.size()-1);const auto k=static_cast<std::size_t>(index);return values[k]+(index-k)*(values[std::min(k+1,values.size()-1)]-values[k]);};
    d.q05=quantile(.05);d.q50=quantile(.5);d.q95=quantile(.95);d.fraction_deficit=double(deficits)/values.size();
    const unsigned bins=d.minimum==d.maximum?1:40;d.bin_counts.assign(bins,0);
    for(unsigned k=0;k<=bins;++k)d.bin_edges.push_back(d.minimum+(d.maximum-d.minimum)*double(k)/bins);
    for(double x:values){const auto b=bins==1?0:std::min<unsigned>(bins-1,static_cast<unsigned>((x-d.minimum)/(d.maximum-d.minimum)*bins));++d.bin_counts[b];}
    return d;
}
PathResult evaluate(const Inputs& x,const std::vector<double>& path,Policy policy,unsigned n,bool keep,double premium) {
    CashLedger account(premium,x.option.maturity,x.option.rate,x.cost_bps,x.fixed_cost);
    PathResult out;const auto stride=(path.size()-1)/n;
    for(unsigned k=0;k<=n;++k) {
        const double time=k==n?x.option.maturity:x.option.maturity*double(k)/n;
        const double spot=path[k*stride];LedgerRow row;
        if(k==n){row=account.settle(time,spot,payoff(x.option,spot));row.liability_value=row.settlement;}
        else {
            const double d=delta_at(x.option,spot,x.option.maturity-time);
            const double target=policy==Policy::Unhedged?0:(policy==Policy::InitialDelta&&k>0?account.shares():d);
            row=account.rebalance(time,spot,target);row.model_delta=d;
            if(keep){auto mark=x.option;mark.spot=spot;mark.maturity=x.option.maturity-time;row.liability_value=pricing::black_scholes(mark);}
        }
        if(keep){row.surplus=row.hedge_value-row.liability_value;out.ledger.push_back(row);}
    }
    out.terminal_error=account.cash();out.nominal_costs=account.costs();out.terminal_costs=account.costs_at_time();
    out.turnover=account.turnover();out.trades=account.trades();out.minimum_cash=account.min_cash();out.max_balance_residual=account.max_residual();return out;
}
} // namespace
void Inputs::validate() const {
    option.validate();
    if(option.spot<=0||option.strike<=0||option.maturity<=0||option.volatility<=0||option.dividend_yield!=0)
        throw std::invalid_argument("Hedging requires positive spot/strike/maturity/hedge volatility and zero dividends");
    bound(option.maturity,1e-6,10,"Hedging maturity must be 0.000001..10 years");
    bound(option.volatility,1e-6,3,"Hedge volatility must be 0.000001..3");
    bound(path_drift,-2,2,"Path drift must be -2..2 per year");
    bound(path_volatility,0,3,"Path volatility must be 0..3");
    if(std::abs(path_drift)*option.maturity>5||path_volatility*std::sqrt(option.maturity)>3)
        throw std::invalid_argument("Excessive total path drift or volatility");
    bound(cost_bps,0,100,"Stock transaction cost must be 0..100 basis points");
    bound(fixed_cost,0,1000,"Fixed cost must be 0..1000 per nonzero stock trade");
}
unsigned Config::finest_steps() const {
    if(!power_two(first_steps)||first_steps>1024||levels<1||levels>8)
        throw std::invalid_argument("Require power-of-two first_steps <=1024 and 1..8 levels");
    const unsigned n=first_steps<<(levels-1);if(n>1024)throw std::invalid_argument("Finest hedge grid exceeds 1024");return n;
}
std::uint64_t Config::work() const {
    const auto finest=finest_steps();std::uint64_t sum=0;
    for(unsigned k=0;k<levels;++k)sum+=first_steps<<k;
    return paths*(finest+sum+4);
}
void Config::validate() const {
    if(paths<1000||paths>50000)throw std::invalid_argument("Hedge paths must be 1000..50000");
    if(work()>12000000)throw std::invalid_argument("Hedge workload exceeds 12 million units; reduce paths or grid");
}
CashLedger::CashLedger(double premium,double maturity,double rate,double bps,double fixed)
    :cash_(premium),maturity_(maturity),rate_(rate),bps_(bps),fixed_(fixed),min_cash_(premium) {
    bound(premium,0,1e15,"Invalid initial premium");bound(maturity,1e-6,10,"Invalid ledger maturity");
    bound(rate,-1,1,"Invalid ledger rate");bound(bps,0,100,"Invalid ledger bps");bound(fixed,0,1000,"Invalid ledger fixed fee");
}
LedgerRow CashLedger::apply(double t,double spot,double target,double payment) {
    if(settled_)throw std::logic_error("Hedge is already settled");
    bound(spot,std::numeric_limits<double>::min(),1e30,"Positive finite stock price required");finite(target);bound(payment,0,1e30,"Invalid settlement");
    LedgerRow row;row.time=t;row.spot=spot;row.cash_previous=cash_;row.shares_previous=shares_;
    const double growth=std::exp(rate_*(t-time_));row.financing=cash_*std::expm1(rate_*(t-time_));row.cash_before=cash_+row.financing;
    row.shares_after=target;row.shares_traded=target-shares_;row.trade_notional=row.shares_traded*spot;
    row.cost=row.shares_traded==0?0:std::abs(row.trade_notional)*bps_/10000+fixed_;
    row.cash_after_trade=row.cash_before-row.trade_notional-row.cost;row.settlement=payment;row.cash_after=row.cash_after_trade-payment;
    row.hedge_value=row.cash_after_trade+target*spot;
    row.cost_sum=costs_+row.cost;row.costs_at_time=costs_at_time_*growth+row.cost;
    const double before=row.cash_before+shares_*spot;
    row.balance_residual=(row.cash_after+target*spot)-(before-row.cost-payment);
    for(double v:{row.financing,row.cash_before,row.trade_notional,row.cost,row.cash_after_trade,row.cash_after,row.hedge_value,row.cost_sum,row.costs_at_time,row.balance_residual})finite(v);
    const double scale=std::max({1.0,std::abs(before),std::abs(row.cash_after),std::abs(target*spot),payment});
    if(std::abs(row.balance_residual)>1e-10*scale)throw std::runtime_error("Self-financing reconciliation failed");
    cash_=row.cash_after;shares_=target;time_=t;costs_=row.cost_sum;costs_at_time_=row.costs_at_time;
    turnover_+=std::abs(row.trade_notional);finite(turnover_);trades_+=row.shares_traded!=0;
    min_cash_=std::min({min_cash_,row.cash_before,cash_});max_residual_=std::max(max_residual_,std::abs(row.balance_residual));started_=true;return row;
}
LedgerRow CashLedger::rebalance(double time,double spot,double target) {
    if(!std::isfinite(time)||time<0||time>=maturity_||(!started_&&time!=0)||(started_&&time<=time_))
        throw std::invalid_argument("Rebalances start at zero, increase strictly, and precede expiry");
    return apply(time,spot,target,0);
}
LedgerRow CashLedger::settle(double time,double spot,double payoff_value) {
    if(!started_||time!=maturity_)throw std::invalid_argument("Initialize hedge then settle exactly at expiry");
    auto row=apply(time,spot,0,payoff_value);settled_=true;return row;
}
PathResult on_path(const Inputs& x,const std::vector<double>& path,Policy policy,unsigned n,bool keep) {
    x.validate();if(policy!=Policy::Unhedged&&policy!=Policy::InitialDelta&&policy!=Policy::Periodic)throw std::invalid_argument("Unknown hedge policy");
    if(path.size()<2||path.size()>1025||n==0||n>1024||(path.size()-1)%n||path.front()!=x.option.spot)
        throw std::invalid_argument("Path/grid incompatible with initial spot");
    for(double v:path)bound(v,std::numeric_limits<double>::min(),1e8,"Path states must be positive finite values");
    return evaluate(x,path,policy,n,keep,pricing::black_scholes(x.option));
}
Result run(const Inputs& x,const Config& c,std::chrono::milliseconds budget) {
    x.validate();c.validate();if(budget.count()<=0)throw std::invalid_argument("Positive runtime budget required");
    const auto begin=std::chrono::steady_clock::now();Result r;r.premium=pricing::black_scholes(x.option);
    auto reference=x.option;reference.volatility=x.path_volatility;r.path_volatility_reference_premium=pricing::black_scholes(reference);
    r.deterministic=x.path_volatility==0;r.paths_processed=r.deterministic?1:c.paths;
    PolicyResult unhedged,initial;initial.policy=Policy::InitialDelta;r.policies={unhedged,initial};
    for(unsigned k=0;k<c.levels;++k){PolicyResult p;p.policy=Policy::Periodic;p.intervals=c.first_steps<<k;r.policies.push_back(p);}
    const auto count=r.policies.size();std::vector<std::vector<double>> errors(count);
    std::vector<pricing::detail::Moments> paired(count),costs(count),terminal_costs(count),turnover(count),trades(count);
    for(auto& e:errors)e.reserve(r.paths_processed);
    pricing::detail::Normals normals(c.seed);const auto finest=c.finest_steps();std::vector<double> path(finest+1);
    const double dt=x.option.maturity/finest,drift=(x.path_drift-0.5*x.path_volatility*x.path_volatility)*dt,scale=x.path_volatility*std::sqrt(dt);
    for(std::uint64_t i=0;i<r.paths_processed;++i) {
        if((i%16)==0&&std::chrono::steady_clock::now()-begin>budget)throw std::runtime_error("Hedge runtime budget exceeded; reduce work; no partial result");
        path[0]=x.option.spot;
        for(unsigned k=1;k<=finest;++k){const double z=r.deterministic?0:normals.next();r.normal_draws+=!r.deterministic;
            path[k]=path[k-1]*std::exp(drift+scale*z);bound(path[k],std::numeric_limits<double>::min(),1e8,"Exact GBM path overflow/underflow");}
        std::vector<PathResult> results;results.reserve(count);
        for(auto& p:r.policies)results.push_back(evaluate(x,path,p.policy,p.intervals,i<2,r.premium));
        for(std::size_t k=0;k<count;++k){auto& p=r.policies[k];const auto& a=results[k];errors[k].push_back(a.terminal_error);
            paired[k].add(a.terminal_error-results[1].terminal_error);costs[k].add(a.nominal_costs);terminal_costs[k].add(a.terminal_costs);
            turnover[k].add(a.turnover);trades[k].add(double(a.trades));p.minimum_cash=i?std::min(p.minimum_cash,a.minimum_cash):a.minimum_cash;
            p.max_balance_residual=std::max(p.max_balance_residual,a.max_balance_residual);if(i<2)p.sample_ledgers.push_back(a.ledger);
        }
    }
    for(std::size_t k=0;k<count;++k){auto& p=r.policies[k];p.error=distribution(std::move(errors[k]),r.deterministic);
        p.paired_minus_initial=estimate(paired[k],r.deterministic||k==1||(p.policy==Policy::Periodic&&p.intervals==1));
        p.mean_cost=costs[k].mean;p.mean_terminal_cost=terminal_costs[k].mean;p.mean_turnover=turnover[k].mean;p.mean_trades=trades[k].mean;}
    r.warnings={"Synthetic short-option replication, not broker fills, account P&L or a trading signal.",
        "Fractional stock and unlimited symmetric funding; no dividends, margin, stock-borrow fees, impact or option-trade costs.",
        "Initial premium uses hedge-model volatility. Changing it changes both funding and the hedge policy.",
        "Quantiles and deficit fractions are sample diagnostics under assumed path dynamics, not real-world risk forecasts.",
        "Same-path coupling applies within a run. Changing finest-grid size changes RNG consumption across runs."};
    if(x.path_drift!=x.option.rate)r.warnings.push_back("Path drift differs from r; scenarios are not risk-neutral GBM under this rate.");
    if(x.path_volatility!=x.option.volatility)r.warnings.push_back("Path and hedge volatilities differ: a deliberate model mismatch.");
    if(x.cost_bps||x.fixed_cost)r.warnings.push_back("Entry, every nonzero rebalance, and liquidation incur costs; more frequent hedging need not improve net error.");
    if(r.deterministic)r.warnings.push_back("Zero path volatility: one deterministic path, no random draws; requested sample count is not used.");
    for(const auto& p:r.policies)if(!p.error.mean.standard_error) {r.warnings.push_back("Zero observed stochastic variation: mean SE/CI withheld, not evidence of certainty.");break;}
    r.runtime_ms=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-begin).count();return r;
}
} // namespace dts::hedging
