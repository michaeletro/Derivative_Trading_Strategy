#pragma once
#include "pricing.hpp"

namespace dts::hedging {
inline constexpr char engine_version[] = "self-financing-gbm-hedge-1";

// One SHORT European payoff unit. No dividends, deposits, margin, stock-borrow
// fees or asymmetric funding. All stock trades are fractional at the modeled
// spot plus explicit costs; these are not broker fills.
struct Inputs {
    pricing::Inputs option; // volatility is the HEDGING-model volatility
    double path_drift = 0.05;
    double path_volatility = 0.20;
    double cost_bps = 0;
    double fixed_cost = 0; // per NONZERO stock trade, including entry/liquidation
    void validate() const;
};
struct Config {
    std::uint64_t paths = 5000, seed = 42;
    unsigned first_steps = 8, levels = 6;
    unsigned finest_steps() const;
    std::uint64_t work() const;
    void validate() const;
};
struct LedgerRow {
    double time=0, spot=0, cash_previous=0, shares_previous=0;
    double financing=0, cash_before=0, shares_after=0, shares_traded=0;
    double trade_notional=0, cost=0, cash_after_trade=0, settlement=0;
    double cash_after=0, hedge_value=0, liability_value=0, surplus=0;
    double cost_sum=0, costs_at_time=0, balance_residual=0;
    std::optional<double> model_delta;
};
// The accounting primitive is usable/testable without any stochastic model.
// settle() pays the liability once and liquidates the stock; reuse is rejected.
class CashLedger {
    double cash_, shares_=0, time_=0, maturity_, rate_, bps_, fixed_;
    bool started_=false, settled_=false;
    double costs_=0, costs_at_time_=0, turnover_=0, min_cash_;
    double max_residual_=0;
    std::uint64_t trades_=0;
    LedgerRow apply(double time,double spot,double target,double payment);
public:
    CashLedger(double premium,double maturity,double rate,double bps,double fixed);
    LedgerRow rebalance(double time,double spot,double target);
    LedgerRow settle(double time,double spot,double payoff);
    double cash() const noexcept {return cash_;}
    double shares() const noexcept {return shares_;}
    double costs() const noexcept {return costs_;}
    double costs_at_time() const noexcept {return costs_at_time_;}
    double turnover() const noexcept {return turnover_;}
    double min_cash() const noexcept {return min_cash_;}
    double max_residual() const noexcept {return max_residual_;}
    std::uint64_t trades() const noexcept {return trades_;}
};
enum class Policy { Unhedged, InitialDelta, Periodic };
struct PathResult {
    double terminal_error=0, nominal_costs=0, terminal_costs=0;
    double turnover=0, minimum_cash=0, max_balance_residual=0;
    std::uint64_t trades=0;
    std::vector<LedgerRow> ledger;
};
// Exact policy accounting on a supplied POSITIVE fine path. Decisions inspect
// only the current sampled spot. Future nodes cannot change earlier decisions.
PathResult on_path(const Inputs&,const std::vector<double>& fine_path,
                   Policy,unsigned intervals,bool keep_ledger=true);
struct Distribution {
    pricing::Estimate mean;
    double standard_deviation=0, rmse=0, q05=0, q50=0, q95=0;
    double minimum=0, maximum=0, fraction_deficit=0;
    std::vector<double> bin_edges;
    std::vector<std::uint64_t> bin_counts;
};
struct PolicyResult {
    Policy policy=Policy::Unhedged;
    unsigned intervals=1;
    Distribution error;
    pricing::Estimate paired_minus_initial;
    double mean_cost=0, mean_terminal_cost=0, mean_turnover=0, mean_trades=0;
    double minimum_cash=0, max_balance_residual=0;
    std::vector<std::vector<LedgerRow>> sample_ledgers;
};
struct Result {
    double premium=0, path_volatility_reference_premium=0;
    std::uint64_t paths_processed=0, normal_draws=0;
    bool deterministic=false;
    std::vector<PolicyResult> policies;
    std::vector<std::string> warnings;
    double runtime_ms=0;
};
Result run(const Inputs&,const Config&,
           std::chrono::milliseconds budget=std::chrono::milliseconds(5000));
} // namespace dts::hedging
