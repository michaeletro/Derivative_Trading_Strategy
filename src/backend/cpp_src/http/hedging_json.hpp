#pragma once
#include "research_json.hpp"
#include <dts/hedging.hpp>

namespace dts::hedging_http {
using Json=crow::json::wvalue;using Read=crow::json::rvalue;
struct Request {pricing::sensitivity_http::Model model;hedging::Inputs inputs;hedging::Config config;};
inline Request parse(const Read& j) {
    pricing::http::keys(j,{"schema_version","model","dynamics","costs","simulation"});
    if(pricing::http::number(j,"schema_version")!=1)throw std::invalid_argument("Unknown hedge request schema");
    Request r;r.model=pricing::sensitivity_http::parse_model(j["model"]);r.inputs.option=r.model.inputs;
    const auto& d=j["dynamics"];pricing::http::keys(d,{"drift","volatility"});
    r.inputs.path_drift=pricing::http::number(d,"drift");r.inputs.path_volatility=pricing::http::number(d,"volatility");
    const auto& costs=j["costs"];pricing::http::keys(costs,{"bps","fixed_per_trade"});
    r.inputs.cost_bps=pricing::http::number(costs,"bps");r.inputs.fixed_cost=pricing::http::number(costs,"fixed_per_trade");
    const auto& c=j["simulation"];pricing::http::keys(c,{"paths","seed","first_steps","levels"});
    r.config.paths=research_http::bounded_integer(c,"paths",1000,50000);r.config.seed=pricing::parse_seed(pricing::http::text(c,"seed"));
    r.config.first_steps=research_http::bounded_integer(c,"first_steps",1,1024);r.config.levels=research_http::bounded_integer(c,"levels",1,8);
    r.inputs.validate();r.config.validate();return r;
}
inline Json canonical(const Request& r) {
    Json j;j["schema_version"]=1;j["model"]=pricing::sensitivity_http::model_json(r.model);
    j["dynamics"]["drift"]=r.inputs.path_drift;j["dynamics"]["volatility"]=r.inputs.path_volatility;
    j["costs"]["bps"]=r.inputs.cost_bps;j["costs"]["fixed_per_trade"]=r.inputs.fixed_cost;
    j["simulation"]["paths"]=r.config.paths;j["simulation"]["seed"]=std::to_string(r.config.seed);
    j["simulation"]["first_steps"]=r.config.first_steps;j["simulation"]["levels"]=r.config.levels;return j;
}
inline Json stat(const pricing::Estimate& e) {
    Json j;j["mean"]=e.price;j["sample_variance"]=e.sample_variance;j["standard_error"]=research_http::optional(e.standard_error);
    j["ci_low"]=research_http::optional(e.ci_low);j["ci_high"]=research_http::optional(e.ci_high);return j;
}
inline Json row(const hedging::LedgerRow& r) {
    Json j;
    j["time"]=r.time;j["spot"]=r.spot;j["cash_previous"]=r.cash_previous;j["shares_previous"]=r.shares_previous;
    j["financing"]=r.financing;j["cash_before"]=r.cash_before;j["shares_after"]=r.shares_after;j["shares_traded"]=r.shares_traded;
    j["trade_notional"]=r.trade_notional;j["cost"]=r.cost;j["cash_after_trade"]=r.cash_after_trade;j["settlement"]=r.settlement;
    j["cash_after"]=r.cash_after;j["hedge_value"]=r.hedge_value;j["liability_value"]=r.liability_value;j["surplus"]=r.surplus;
    j["cost_sum"]=r.cost_sum;j["costs_at_time"]=r.costs_at_time;j["balance_residual"]=r.balance_residual;
    j["model_delta"]=research_http::optional(r.model_delta);return j;
}
inline Json run(const Request& req) {
    const auto r=hedging::run(req.inputs,req.config);Json j;
    j["schema_version"]=1;j["kind"]="hedging_replication";j["engine_version"]=hedging::engine_version;j["request"]=canonical(req);
    j["input_source"]="synthetic_exact_gbm_not_broker_data";j["initial_premium"]=r.premium;
    j["path_volatility_bsm_reference"]=r.path_volatility_reference_premium;
    j["funding_convention"]="hedge_model_bsm_premium; no external cash; same constant r for lending and borrowing";
    j["units"]="one short European payoff unit; terminal surplus after cash settlement and stock liquidation; no dividends";
    j["dynamics_scope"]=req.inputs.path_drift==req.inputs.option.rate?"risk_neutral_GBM_under_supplied_rate":"assumed_GBM_scenarios_not_calibrated_probabilities";
    j["deterministic"]=r.deterministic;j["paths_processed"]=r.paths_processed;j["independent_paths"]=r.deterministic?0:r.paths_processed;
    j["normal_draws"]=r.normal_draws;j["rng"]=pricing::rng_version;j["work_units"]=req.config.work();j["warnings"]=r.warnings;
    std::vector<Json> policies;
    for(const auto& p:r.policies) {
        Json out;out["policy"]=p.policy==hedging::Policy::Unhedged?"unhedged":p.policy==hedging::Policy::InitialDelta?"initial_delta":"periodic";
        out["intervals"]=p.intervals;out["error"]=stat(p.error.mean);
        out["error"]["standard_deviation"]=p.error.standard_deviation;out["error"]["rmse"]=p.error.rmse;
        out["error"]["q05"]=p.error.q05;out["error"]["q50"]=p.error.q50;out["error"]["q95"]=p.error.q95;
        out["error"]["minimum"]=p.error.minimum;out["error"]["maximum"]=p.error.maximum;out["error"]["fraction_deficit"]=p.error.fraction_deficit;
        out["histogram"]["edges"]=p.error.bin_edges;out["histogram"]["counts"]=p.error.bin_counts;
        out["paired_minus_initial"]=stat(p.paired_minus_initial);out["mean_cost"]=p.mean_cost;out["mean_terminal_cost"]=p.mean_terminal_cost;
        out["mean_turnover"]=p.mean_turnover;out["mean_trades"]=p.mean_trades;out["minimum_cash"]=p.minimum_cash;out["max_balance_residual"]=p.max_balance_residual;
        std::vector<Json> ledgers;for(const auto& path:p.sample_ledgers){std::vector<Json> rows;for(const auto& r0:path)rows.push_back(row(r0));ledgers.emplace_back(std::move(rows));}
        out["sample_ledgers"]=std::move(ledgers);policies.push_back(std::move(out));
    }
    j["policies"]=std::move(policies);j["numerical_sha256"]=research::sha256(j.dump());
    j["timing"]["runtime_ms"]=r.runtime_ms;j["build"]=pricing::sensitivity_http::build_json();
    j["build"]["hedging_source_sha256"]=pricing::build::hedging_source_sha256;
    j["created_ms"]=std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count();return j;
}
} // namespace dts::hedging_http
