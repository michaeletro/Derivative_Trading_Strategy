#include <dts/greeks.hpp>
#include "simulation_detail.hpp"
#include <array>
#include <cmath>
#include <stdexcept>

namespace dts::pricing {
namespace {
using Clock = std::chrono::steady_clock;
long double cdf(long double x) { return 0.5L * std::erfc(-x / std::sqrt(2.0L)); }
void bound(double x, double lo, double hi, const char* message) {
    if (!std::isfinite(x) || x < lo || x > hi) throw std::invalid_argument(message);
}
double payoff(const Inputs& x, double z) {
    const double v = x.volatility * std::sqrt(x.maturity);
    const double s = std::exp(std::log(x.spot) - x.dividend_yield*x.maturity - v*v/2 + v*z);
    const double k = x.strike * std::exp(-x.rate*x.maturity);
    return std::max(x.right == Right::Call ? s-k : k-s, 0.0);
}
std::array<Inputs,4> bumped(const Inputs& x, const SensitivityConfig& c) {
    std::array<Inputs,4> b{x,x,x,x};
    b[0].spot *= 1+c.relative_spot_bump; b[1].spot *= 1-c.relative_spot_bump;
    b[2].volatility += c.volatility_bump; b[3].volatility -= c.volatility_bump;
    for (const auto& input : b) input.validate(); // Never silently clamp a bump.
    if (b[1].spot <= 0 || b[3].volatility <= 0)
        throw std::invalid_argument("Central differences require strictly positive lower spot and volatility bumps");
    return b;
}
} // namespace

Greeks analytical_greeks(const Inputs& x) {
    x.validate();
    Greeks out; out.price = black_scholes(x);
    // A deliberate domain policy, NOT a claim that all boundary derivatives are
    // undefined. At expiry/zero volatility payoff kinks need one-sided conventions.
    if (x.spot == 0 || x.maturity == 0 || x.volatility == 0) {
        out.reason = "Interior-only Greeks require positive spot, maturity and volatility; boundary price remains available";
        return out;
    }
    const long double S=x.spot, K=x.strike, T=x.maturity, v=x.volatility;
    const long double r=x.rate, q=x.dividend_yield, root=std::sqrt(T);
    const long double Dq=std::exp(-q*T), Dr=std::exp(-r*T);
    const long double d1=(std::log(S)-std::log(K)+(r-q+v*v/2)*T)/(v*root);
    const long double d2=d1-v*root, sign=x.right==Right::Call ? 1 : -1;
    const long double phi=std::exp(-d1*d1/2)/std::sqrt(6.283185307179586476925286766559L);
    const long double n1=cdf(sign*d1), n2=cdf(sign*d2);
    const std::array<double,5> values{
        static_cast<double>(sign*Dq*n1),
        static_cast<double>(Dq*phi/(S*v*root)),
        static_cast<double>(S*Dq*phi*root),
        static_cast<double>(-S*Dq*phi*v/(2*root)-sign*r*K*Dr*n2+sign*q*S*Dq*n1),
        static_cast<double>(sign*K*T*Dr*n2)};
    for (double value : values) if (!std::isfinite(value)) {
        out.reason="Sensitivity exceeds representable numerical range"; return out;
    }
    out.delta=values[0]; out.gamma=values[1]; out.vega=values[2]; out.theta=values[3]; out.rho=values[4];
    out.status="available";
    return out;
}

void SensitivityConfig::validate() const {
    sampling.validate();
    if (estimator!=SensitivityMethod::Pathwise && estimator!=SensitivityMethod::CentralCRN)
        throw std::invalid_argument("Unknown sensitivity estimator");
    bound(relative_spot_bump,1e-6,0.1,"Relative spot bump must be between 1e-6 and 0.1");
    bound(volatility_bump,1e-6,0.1,"Absolute decimal volatility bump must be between 1e-6 and 0.1");
}
SensitivityResult simulate_greeks(const Inputs& x, const SensitivityConfig& c,
                                std::chrono::milliseconds budget) {
    x.validate(); c.validate();
    if (budget.count()<=0) throw std::invalid_argument("Runtime budget must be positive");
    const auto start=Clock::now();
    SensitivityResult out;
    const auto a=analytical_greeks(x);
    if (a.status!="available") { out.reason=a.reason; return out; }
    const bool crn=c.estimator==SensitivityMethod::CentralCRN;
    std::array<Inputs,4> b{x,x,x,x};
    if (crn) b=bumped(x,c);
    const double hs=x.spot*c.relative_spot_bump, hv=c.volatility_bump;
    out.delta_target=crn ? (black_scholes(b[0])-black_scholes(b[1]))/(2*hs) : *a.delta;
    out.vega_target=crn ? (black_scholes(b[2])-black_scholes(b[3]))/(2*hv) : *a.vega;
    out.delta_fd_bias=out.delta_target-*a.delta; out.vega_fd_bias=out.vega_target-*a.vega;
    detail::Normals normals(c.sampling.seed);
    detail::Moments delta,vega;
    const bool paired=c.sampling.method==Method::Antithetic;
    const std::uint64_t factor=paired?2:1, count=c.sampling.paths/factor;
    const double root=std::sqrt(x.maturity), total=x.volatility*root;
    const double log_s=std::log(x.spot)-x.dividend_yield*x.maturity-total*total/2;
    const double k=x.strike*std::exp(-x.rate*x.maturity);
    const double sign=x.right==Right::Call?1:-1;
    const auto observation=[&](double z) -> std::array<double,2> {
        const double terminal=std::exp(log_s+total*z);
        const bool active=sign*(terminal-k)>0;
        if (active) ++out.active_base_paths;
        if (crn) return {(payoff(b[0],z)-payoff(b[1],z))/(2*hs),
                        (payoff(b[2],z)-payoff(b[3],z))/(2*hv)};
        return {active?sign*terminal/x.spot:0,
                active?sign*terminal*(root*z-x.volatility*x.maturity):0};
    };
    for (std::uint64_t i=0;i<count;++i) {
        if ((i&1023U)==0 && Clock::now()-start>budget)
            throw std::runtime_error("Sensitivity runtime budget exceeded; no partial result accepted");
        const double z=normals.next(); auto obs=observation(z);
        if (paired) {
            const auto other=observation(-z);
            for (std::size_t j=0;j<2;++j) obs[j]=obs[j]/2+other[j]/2;
        }
        delta.add(obs[0]); vega.add(obs[1]);
    }
    out.available=true; out.delta=delta.estimate(); out.vega=vega.estimate();
    out.terminal_draws=c.sampling.paths; out.independent_samples=count;
    out.bumped_payoff_evaluations=crn?4*c.sampling.paths:0;
    if (!out.delta.standard_error || !out.vega.standard_error)
        out.warnings.emplace_back("No observed variation in at least one estimator: its sampling interval is withheld, not evidence of certainty.");
    if (out.active_base_paths<30 || out.terminal_draws-out.active_base_paths<30)
        out.warnings.emplace_back("Fewer than 30 paths on one side of the strike: finite-sample uncertainty diagnostics may be unreliable.");
    if (total>1) out.warnings.emplace_back("High total volatility: normal-approximation sampling intervals may be unreliable.");
    if (crn) out.warnings.emplace_back("CRN intervals measure sampling error about the finite-difference target. Finite-bump bias is reported separately; it is not included in the interval.");
    out.runtime_ms=std::chrono::duration<double,std::milli>(Clock::now()-start).count();
    return out;
}

Scenario reprice_scenario(const Inputs& base, const Shock& shock) {
    base.validate(); Scenario out; out.shock=shock;
    try {
        bound(shock.relative_spot,-0.9,1,"Relative spot shock must be between -0.9 and 1");
        bound(shock.volatility,-1,1,"Decimal volatility shock must be between -1 and 1");
        bound(shock.elapsed_years,0,base.maturity,"Time roll must be between zero and remaining maturity");
        Inputs x=base; x.spot*=1+shock.relative_spot;
        x.volatility+=shock.volatility; x.maturity-=shock.elapsed_years;
        x.validate();
        out.shocked_price=black_scholes(x);
        const auto g=analytical_greeks(base);
        out.full_change=*out.shocked_price-g.price; out.valid=true;
        if (g.status=="available") {
            const double ds=base.spot*shock.relative_spot;
            const double approx=*g.delta*ds+0.5*(*g.gamma)*ds*ds+(*g.vega)*shock.volatility+(*g.theta)*shock.elapsed_years;
            const double residual=*out.full_change-approx;
            if (std::isfinite(approx) && std::isfinite(residual)) {
                out.approximation=approx; out.residual=residual;
            } else out.reason="Greek approximation exceeds numerical range; full repricing is available";
        } else out.reason=g.reason;
    } catch (const std::invalid_argument& e) { out.reason=e.what(); }
    return out;
}
std::vector<Scenario> scenario_grid(const Inputs& x, double ss, double vs, double elapsed) {
    x.validate(); bound(ss,0,0.5,"Spot grid half-width must be between zero and 0.5");
    bound(vs,0,0.5,"Volatility grid half-width must be between zero and 0.5");
    bound(elapsed,0,x.maturity,"Grid time roll exceeds maturity");
    std::vector<Scenario> out; out.reserve(231);
    for (int j=-5;j<=5;++j) for (int i=-10;i<=10;++i)
        out.push_back(reprice_scenario(x,{ss*i/10.0,vs*j/5.0,elapsed}));
    return out;
}
std::vector<CurvePoint> spot_curve(const Inputs& x, double span) {
    x.validate(); bound(span,0,0.5,"Spot curve half-width must be between zero and 0.5");
    std::vector<CurvePoint> out; out.reserve(41);
    for (int i=-20;i<=20;++i) {
        Inputs b=x; b.spot*=1+span*i/20.0;
        if (b.spot>1e8) { Greeks g; g.reason="Spot outside model domain"; out.push_back({b.spot,g,false}); }
        else out.push_back({b.spot,analytical_greeks(b)});
    }
    return out;
}
} // namespace dts::pricing
