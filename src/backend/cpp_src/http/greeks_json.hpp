#pragma once
#include "pricing_json.hpp"
#include <dts/greeks.hpp>

namespace dts::pricing::sensitivity_http {
using http::Json; using http::Read; using http::keys; using http::number; using http::text;
struct Model { Inputs inputs; std::string currency; };
inline Model parse_model(const Read& j) {
    keys(j,{"exercise_style","right","spot","strike","maturity_years","rate","dividend_yield","volatility","currency"});
    if (text(j,"exercise_style")!="european") throw std::invalid_argument("Only explicitly European exercise is supported");
    const auto right=text(j,"right");
    if (right!="call" && right!="put") throw std::invalid_argument("Right must be call or put");
    Model m;
    m.inputs={number(j,"spot"),number(j,"strike"),number(j,"maturity_years"),number(j,"rate"),
        number(j,"dividend_yield"),number(j,"volatility"),right=="call"?Right::Call:Right::Put};
    m.currency=text(j,"currency");
    if (m.currency.size()!=3 || m.currency.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZ")!=std::string::npos)
        throw std::invalid_argument("Currency must be a three-letter uppercase label");
    m.inputs.validate(); return m;
}
inline Json model_json(const Model& m) {
    Json j; const auto& x=m.inputs;
    j["exercise_style"]="european"; j["right"]=x.right==Right::Call?"call":"put";
    j["spot"]=x.spot; j["strike"]=x.strike; j["maturity_years"]=x.maturity;
    j["rate"]=x.rate; j["dividend_yield"]=x.dividend_yield; j["volatility"]=x.volatility; j["currency"]=m.currency;
    return j;
}
inline Json optional(std::optional<double> v) { return v ? Json(*v) : Json(nullptr); }
inline Json greek_json(const Greeks& g) {
    Json j; j["price"]=g.price; j["status"]=g.status; j["reason"]=g.reason;
    j["delta"]=optional(g.delta); j["gamma"]=optional(g.gamma); j["vega"]=optional(g.vega);
    j["theta"]=optional(g.theta); j["rho"]=optional(g.rho); return j;
}
inline Json build_json() {
    Json j;
    j["application_version"]=build::application_version; j["git_revision"]=build::revision;
    j["dirty_worktree"]=build::dirty; j["source_sha256"]=build::source_sha256;
    j["compiler"]=build::compiler; j["configuration"]=build::configuration;
    j["pricing_engine"]=engine_version; j["sensitivity_engine"]=sensitivity_version;
    j["metadata_scope"]="configure_time; source archives may have unavailable Git revision"; return j;
}
inline Json envelope(const char* kind, Json request) {
    Json j; j["kind"]=kind; j["schema_version"]=1; j["engine_version"]=sensitivity_version;
    j["input_source"]="manual_scenario_not_broker_data"; j["build"]=build_json(); j["request"]=std::move(request);
    j["created_at_unix_ms"]=std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
    j["units"]="per payoff unit; raw vega/rho per unit decimal input; theta=-dV/dtau per year";
    return j;
}
struct GreekRequest { Model model; SensitivityConfig config; };
inline GreekRequest parse_greeks(const Read& j) {
    keys(j,{"schema_version","model","simulation"});
    if (number(j,"schema_version")!=1) throw std::invalid_argument("Unknown sensitivity schema");
    GreekRequest r; r.model=parse_model(j["model"]); const auto& s=j["simulation"];
    keys(s,{"draws","seed","pairing","estimator","relative_spot_bump","volatility_bump"});
    const double n=number(s,"draws");
    if (n<min_paths || n>max_paths || std::floor(n)!=n) throw std::invalid_argument("Draws must be 1000..2000000 integer");
    r.config.sampling.paths=static_cast<std::uint64_t>(n);
    r.config.sampling.seed=parse_seed(text(s,"seed"));
    const auto pairing=text(s,"pairing"), estimator=text(s,"estimator");
    if (pairing!="plain" && pairing!="antithetic") throw std::invalid_argument("Unknown pairing");
    if (estimator!="pathwise" && estimator!="central_crn") throw std::invalid_argument("Unknown Greek estimator");
    r.config.sampling.method=pairing=="plain"?Method::Plain:Method::Antithetic;
    r.config.estimator=estimator=="pathwise"?SensitivityMethod::Pathwise:SensitivityMethod::CentralCRN;
    r.config.relative_spot_bump=number(s,"relative_spot_bump"); r.config.volatility_bump=number(s,"volatility_bump");
    r.config.validate(); return r;
}
inline Json canonical(const GreekRequest& r) {
    Json j,s; j["schema_version"]=1; j["model"]=model_json(r.model);
    s["draws"]=r.config.sampling.paths; s["seed"]=std::to_string(r.config.sampling.seed);
    s["pairing"]=r.config.sampling.method==Method::Plain?"plain":"antithetic";
    s["estimator"]=r.config.estimator==SensitivityMethod::Pathwise?"pathwise":"central_crn";
    s["relative_spot_bump"]=r.config.relative_spot_bump; s["volatility_bump"]=r.config.volatility_bump;
    j["simulation"]=std::move(s); return j;
}
inline Json estimate_json(const Estimate& e, double target, double exact) {
    Json j; j["value"]=e.price; j["standard_error"]=optional(e.standard_error);
    j["ci_low"]=optional(e.ci_low); j["ci_high"]=optional(e.ci_high); j["sample_variance"]=e.sample_variance;
    j["sampling_target"]=target; j["analytical_derivative"]=exact; j["finite_bump_bias"]=target-exact;
    return j;
}
inline Json run(const GreekRequest& r) {
    const auto a=analytical_greeks(r.model.inputs);
    const auto mc=simulate_greeks(r.model.inputs,r.config);
    Json j=envelope("derivative_lab.greeks_experiment",canonical(r)); j["analytical"]=greek_json(a);
    Json m; m["status"]=mc.available?"available":"unavailable"; m["reason"]=mc.reason;
    m["delta"]=mc.available?estimate_json(mc.delta,mc.delta_target,*a.delta):Json(nullptr);
    m["vega"]=mc.available?estimate_json(mc.vega,mc.vega_target,*a.vega):Json(nullptr);
    m["terminal_draws"]=mc.terminal_draws; m["independent_samples"]=mc.independent_samples;
    m["bumped_payoff_evaluations"]=mc.bumped_payoff_evaluations; m["active_base_paths"]=mc.active_base_paths;
    m["runtime_ms"]=mc.runtime_ms; m["warnings"]=mc.warnings; m["rng"]=rng_version;
    m["interval_scope"]="Pointwise normal sampling interval about the estimator target; excludes finite-bump bias, model risk and parameter uncertainty";
    j["simulation"]=std::move(m); return j;
}
struct ScenarioRequest { Model model; Shock shock; double spot_span=0.2, volatility_span=0.05; };
inline ScenarioRequest parse_scenarios(const Read& j) {
    keys(j,{"schema_version","model","shock","grid"});
    if (number(j,"schema_version")!=1) throw std::invalid_argument("Unknown scenario schema");
    ScenarioRequest r; r.model=parse_model(j["model"]);
    keys(j["shock"],{"relative_spot","volatility","elapsed_years"});
    keys(j["grid"],{"spot_span","volatility_span"});
    r.shock={number(j["shock"],"relative_spot"),number(j["shock"],"volatility"),number(j["shock"],"elapsed_years")};
    r.spot_span=number(j["grid"],"spot_span");r.volatility_span=number(j["grid"],"volatility_span");
    // Reject an invalid selected scenario; grid cells outside the domain remain explicit nulls.
    const auto selected=reprice_scenario(r.model.inputs,r.shock);
    if (!selected.valid) throw std::invalid_argument(selected.reason);
    if (r.spot_span<0 || r.spot_span>0.5 || r.volatility_span<0 || r.volatility_span>0.5)
        throw std::invalid_argument("Grid half-widths must be between zero and 0.5");
    return r;
}
inline Json scenario_json(const Scenario& s) {
    Json j; j["relative_spot"]=s.shock.relative_spot; j["volatility"]=s.shock.volatility;
    j["elapsed_years"]=s.shock.elapsed_years; j["valid"]=s.valid; j["reason"]=s.reason;
    j["shocked_price"]=optional(s.shocked_price); j["full_change"]=optional(s.full_change);
    j["approximation"]=optional(s.approximation); j["residual"]=optional(s.residual); return j;
}
inline Json canonical(const ScenarioRequest& r) {
    Json j,s,g; j["schema_version"]=1; j["model"]=model_json(r.model);
    s["relative_spot"]=r.shock.relative_spot; s["volatility"]=r.shock.volatility;s["elapsed_years"]=r.shock.elapsed_years;
    g["spot_span"]=r.spot_span;g["volatility_span"]=r.volatility_span;
    j["shock"]=std::move(s);j["grid"]=std::move(g);return j;
}
inline Json run(const ScenarioRequest& r) {
    Json j=envelope("derivative_lab.scenario_experiment",canonical(r));
    j["base"]=greek_json(analytical_greeks(r.model.inputs));
    j["selected"]=scenario_json(reprice_scenario(r.model.inputs,r.shock));
    j["approximation_scope"]="Delta*dS + 0.5*Gamma*dS^2 + Vega*dSigma + Theta*dt; omits cross and higher-order terms. Not account P&L.";
    std::vector<Json> grid,curves;
    for (const auto& cell:scenario_grid(r.model.inputs,r.spot_span,r.volatility_span,r.shock.elapsed_years))
        grid.push_back(scenario_json(cell));
    for (double factor : {1.0,0.5,0.1}) {
        Inputs x=r.model.inputs; x.maturity*=factor;
        Json curve; curve["maturity_years"]=x.maturity; std::vector<Json> points;
        for (const auto& p:spot_curve(x,r.spot_span)) {
            Json point=greek_json(p.greeks); point["spot"]=p.spot;
            point["valid"]=p.valid;
            if (!p.valid) point["price"]=nullptr;
            points.push_back(std::move(point));
        }
        curve["points"]=std::move(points);curves.push_back(std::move(curve));
    }
    j["grid"]=std::move(grid); j["spot_curves"]=std::move(curves);
    std::vector<Json> residuals;
    for (int i=-20;i<=20;++i) residuals.push_back(scenario_json(reprice_scenario(
        r.model.inputs,{r.spot_span*i/20.0,r.shock.volatility,r.shock.elapsed_years})));
    j["residual_curve"]=std::move(residuals);
    j["grid_shape"]=std::vector<int>{11,21};
    return j;
}
} // namespace dts::pricing::sensitivity_http
