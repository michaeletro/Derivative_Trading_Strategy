#pragma once
#include "research_json.hpp"
#include <dts/sde.hpp>

namespace dts::sde_http {
using Json=crow::json::wvalue;using Read=crow::json::rvalue;
struct Request {pricing::sensitivity_http::Model model;sde::Config config;};
inline Request parse(const Read& j) {
    pricing::http::keys(j,{"schema_version","model","simulation"});
    if(pricing::http::number(j,"schema_version")!=1)throw std::invalid_argument("Unknown SDE request schema");
    Request r;r.model=pricing::sensitivity_http::parse_model(j["model"]);const auto& c=j["simulation"];
    pricing::http::keys(c,{"paths","seed","first_steps","levels"});
    r.config.paths=research_http::bounded_integer(c,"paths",1000,100000);
    r.config.first_steps=research_http::bounded_integer(c,"first_steps",1,1024);
    r.config.levels=research_http::bounded_integer(c,"levels",1,9);
    r.config.seed=pricing::parse_seed(pricing::http::text(c,"seed"));r.config.validate();return r;
}
inline Json canonical(const Request& r) {
    Json j;j["schema_version"]=1;j["model"]=pricing::sensitivity_http::model_json(r.model);
    j["simulation"]["paths"]=r.config.paths;j["simulation"]["seed"]=std::to_string(r.config.seed);
    j["simulation"]["first_steps"]=r.config.first_steps;j["simulation"]["levels"]=r.config.levels;return j;
}
inline Json stat(const sde::Statistic& s) {
    Json j;j["mean"]=s.mean;j["sample_variance"]=s.sample_variance;
    j["standard_error"]=research_http::optional(s.standard_error);j["ci_low"]=research_http::optional(s.ci_low);j["ci_high"]=research_http::optional(s.ci_high);return j;
}
inline Json run(const Request& req) {
    const auto r=sde::run(req.model.inputs,req.config);Json j;
    j["schema_version"]=1;j["kind"]="sde_convergence";j["engine_version"]=sde::engine_version;j["request"]=canonical(req);
    j["input_source"]="manual_simulation_not_market_data";j["measure"]="risk_neutral_Q";j["rng"]=pricing::rng_version;
    j["coupling"]="fine increments summed pairwise down dyadic grids; same Brownian realization within each path";
    j["units"]="state error in spot units; discounted payoff prices/bias per one payoff unit; time in years";
    j["analytical_price"]=r.analytical_price;j["exact_price"]=stat(r.exact_price);j["deterministic"]=r.deterministic;
    j["paths_processed"]=r.paths_processed;j["independent_paths"]=r.independent_paths;j["normal_draws"]=r.normal_draws;
    j["exact_positive_payoffs"]=r.exact_positive_payoffs;j["requested_work_units"]=req.config.work();
    j["warnings"]=r.warnings;std::vector<Json> levels,timings;
    for(const auto& level:r.levels) {
        Json l;l["steps"]=level.steps;l["step_years"]=level.step_years;l["scheme_updates_each"]=level.scheme_updates;
        Json t;t["steps"]=level.steps;
        for(unsigned k=0;k<2;++k){const auto& s=level.schemes[k];const char* name=k?"milstein":"euler";Json v;
            v["price"]=stat(s.price);v["absolute_terminal_error"]=stat(s.absolute_terminal_error);v["paired_payoff_bias"]=stat(s.paired_payoff_bias);
            v["rmse_terminal"]=s.root_mean_squared_terminal_error;v["nonpositive_steps"]=s.nonpositive_steps;v["paths_with_nonpositive"]=s.paths_with_nonpositive;
            v["positive_payoffs"]=s.positive_payoffs;v["price_minus_analytical"]=s.price.mean-r.analytical_price;
            v["bias_resolved_pointwise"]=s.paired_payoff_bias.ci_low&&(*s.paired_payoff_bias.ci_low>0||*s.paired_payoff_bias.ci_high<0);
            l[name]=std::move(v);t[name]=s.kernel_ms;
        }
        std::vector<Json> paths;
        for(const auto& path:level.sample_paths){std::vector<Json> points;for(const auto& p:path){Json v;v["time"]=p.time;v["exact"]=p.exact;v["euler"]=p.euler;v["milstein"]=p.milstein;points.push_back(std::move(v));}paths.emplace_back(std::move(points));}
        l["sample_paths"]=std::move(paths);levels.push_back(std::move(l));timings.push_back(std::move(t));
    }
    j["levels"]=std::move(levels);
    // Runtime/build identity deliberately excluded from numerical reproducibility.
    j["numerical_sha256"]=research::sha256(j.dump());
    j["timing"]["total_ms"]=r.runtime_ms;j["timing"]["brownian_generation_ms"]=r.brownian_generation_ms;j["timing"]["kernels_ms"]=std::move(timings);
    j["build"]=pricing::sensitivity_http::build_json();j["build"]["sde_source_sha256"]=pricing::build::sde_source_sha256;
    j["created_ms"]=std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count();return j;
}
} // namespace dts::sde_http
