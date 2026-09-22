#pragma once
#include <crow_all.h>
#include <dts/pricing.hpp>
#include <pricing_build.hpp>
#include <algorithm>
#include <cmath>
#include <chrono>
#include <initializer_list>

namespace dts::pricing::http {
using Json = crow::json::wvalue;
using Read = crow::json::rvalue;
struct Request { Inputs inputs; Config config; std::string currency; };

inline void keys(const Read& j, std::initializer_list<const char*> allowed) {
    if (!j || j.t() != crow::json::type::Object || j.size() != allowed.size())
        throw std::invalid_argument("Pricing request has missing or unexpected fields");
    for (const auto* k : allowed) if (!j.has(k))
        throw std::invalid_argument("Pricing request has missing or unexpected fields");
}
inline std::string text(const Read& j, const char* key) {
    if (!j.has(key) || j[key].t() != crow::json::type::String)
        throw std::invalid_argument("Expected pricing string field");
    const std::string out = j[key].s();
    if (out.size() > 32) throw std::invalid_argument("Pricing string too long");
    return out;
}
inline double number(const Read& j, const char* key) {
    if (!j.has(key) || j[key].t() != crow::json::type::Number || !std::isfinite(j[key].d()))
        throw std::invalid_argument("Expected finite pricing number");
    return j[key].d();
}
inline Request parse(const Read& j) {
    keys(j, {"schema_version", "exercise_style", "right", "spot", "strike", "maturity_years", "rate", "dividend_yield", "volatility", "paths", "seed", "method", "currency"});
    if (number(j,"schema_version") != 1 || text(j,"exercise_style") != "european")
        throw std::invalid_argument("Only schema 1 and explicitly European exercise are supported");
    Request r;
    const auto right = text(j,"right"), method = text(j,"method");
    if (right != "call" && right != "put") throw std::invalid_argument("Right must be call or put");
    if (method != "plain" && method != "antithetic") throw std::invalid_argument("Method must be plain or antithetic");
    r.inputs.right = right == "call" ? Right::Call : Right::Put;
    r.inputs.spot = number(j,"spot"); r.inputs.strike = number(j,"strike");
    r.inputs.maturity = number(j,"maturity_years"); r.inputs.rate = number(j,"rate");
    r.inputs.dividend_yield = number(j,"dividend_yield"); r.inputs.volatility = number(j,"volatility");
    const auto paths = number(j,"paths");
    if (paths < min_paths || paths > max_paths || std::floor(paths) != paths)
        throw std::invalid_argument("Paths must be an integer between 1000 and 2000000");
    r.config.paths = static_cast<std::uint64_t>(paths);
    r.config.seed = parse_seed(text(j,"seed"));
    r.config.method = method == "plain" ? Method::Plain : Method::Antithetic;
    r.currency = text(j,"currency");
    if (r.currency.size() != 3 || r.currency.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZ") != std::string::npos)
        throw std::invalid_argument("Currency label must contain three uppercase letters (no FX conversion)");
    r.inputs.validate(); r.config.validate(); return r;
}
inline Json canonical_request(const Request& r) {
    Json j;
    j["schema_version"] = 1; j["exercise_style"] = "european";
    j["right"] = r.inputs.right == Right::Call ? "call" : "put";
    j["spot"] = r.inputs.spot; j["strike"] = r.inputs.strike;
    j["maturity_years"] = r.inputs.maturity; j["rate"] = r.inputs.rate;
    j["dividend_yield"] = r.inputs.dividend_yield; j["volatility"] = r.inputs.volatility;
    j["paths"] = r.config.paths; j["seed"] = std::to_string(r.config.seed);
    j["method"] = r.config.method == Method::Plain ? "plain" : "antithetic";
    j["currency"] = r.currency; return j;
}
inline Json estimate(const Estimate& e) {
    Json j;
    const auto optional = [](std::optional<double> v) { return v ? Json(*v) : Json(nullptr); };
    j["price"] = e.price; j["sample_variance"] = e.sample_variance;
    j["standard_error"] = optional(e.standard_error);
    j["ci_low"] = optional(e.ci_low); j["ci_high"] = optional(e.ci_high);
    return j;
}
inline Json record(const Request& request, const Result& r) {
    Json j;
    j["kind"] = "derivative_lab.pricing_experiment"; j["schema_version"] = 1;
    j["created_at_unix_ms"] = std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
    j["request"] = canonical_request(request);
    Json model;
    model["engine_version"] = engine_version; model["rng"] = rng_version;
    model["measure"] = "risk_neutral_Q"; model["scheme"] = "exact_terminal_gbm";
    model["input_source"] = "manual_scenario_not_broker_data";
    model["price_units"] = "one_payoff_unit_in_currency_label";
    model["assumptions"] = std::vector<std::string>{"European exercise", "constant volatility", "constant continuously compounded rate and dividend yield", "no discrete dividends", "no transaction costs or borrow model"};
    j["model"] = std::move(model);
    Json b;
    b["git_revision"] = build::revision; b["dirty_worktree"] = build::dirty;
    b["pricing_source_sha256"] = build::source_sha256; b["compiler"] = build::compiler;
    b["platform"] = build::platform; b["configuration"] = build::configuration;
    b["reproducibility"] = "Same inputs, seed and binary reproduce numerical results; cross-build rounding can differ. Runtime and timestamp are not deterministic.";
    j["build"] = std::move(b);
    Json result;
    result["analytical_price"] = r.analytical; result["monte_carlo"] = estimate(r.estimate);
    result["difference_mc_minus_analytical"] = r.estimate.price - r.analytical;
    result["paths_evaluated"] = r.paths_evaluated;
    result["independent_samples"] = r.independent_samples; result["positive_payoffs"] = r.positive_payoffs;
    result["deterministic"] = r.deterministic; result["runtime_ms"] = r.runtime_ms;
    result["confidence_level"] = 0.95;
    result["interval_scope"] = "Pointwise normal approximation for Monte Carlo sampling error only, not model risk or a simultaneous convergence band";
    result["warnings"] = r.warnings;
    std::vector<Json> points;
    for (const auto& p : r.convergence) {
        Json point = estimate(p.estimate);
        point["paths"] = p.paths; point["independent_samples"] = p.independent_samples;
        points.push_back(std::move(point));
    }
    result["convergence"] = std::move(points); j["result"] = std::move(result);
    return j;
}
} // namespace dts::pricing::http
