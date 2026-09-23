#pragma once
#include "sde_json.hpp"
#include "hedging_json.hpp"

namespace dts::experiments_http {
using Json=crow::json::wvalue;using Read=crow::json::rvalue;
inline bool kind_valid(const std::string& k){return k=="return_volatility"||k=="option_pricing"||k=="greek_validation"||k=="sde_convergence"||k=="hedging_replication";}
inline std::pair<std::string,std::int64_t> reference(const Read& j) {
    if(!j.has("reference")||j["reference"].t()!=crow::json::type::String)throw std::invalid_argument("Supply a typed experiment reference");
    const std::string ref=j["reference"].s();const auto pos=ref.find(':');
    if(pos==std::string::npos||!kind_valid(ref.substr(0,pos)))throw std::invalid_argument("Unknown typed experiment reference");
    const auto number=ref.substr(pos+1);if(number.empty()||number.size()>18||number.front()=='0'||number.find_first_not_of("0123456789")!=std::string::npos)
        throw std::invalid_argument("Invalid experiment identifier");
    return {ref.substr(0,pos),std::stoll(number)};
}
inline std::string engine(const std::string& kind) {
    if(kind=="hedging_replication")return hedging::engine_version;
    if(kind=="sde_convergence")return sde::engine_version;
    if(kind=="option_pricing")return pricing::engine_version;
    if(kind=="greek_validation")return pricing::sensitivity_version;
    if(kind=="return_volatility")return research::engine_version;
    throw std::invalid_argument("Unsupported experiment kind");
}
inline Json canonical(const std::string& kind,const Read& req) {
    if(kind=="hedging_replication")return hedging_http::canonical(hedging_http::parse(req));
    if(kind=="sde_convergence")return sde_http::canonical(sde_http::parse(req));
    if(kind=="option_pricing")return pricing::http::canonical_request(pricing::http::parse(req));
    if(kind=="greek_validation")return pricing::sensitivity_http::canonical(pricing::sensitivity_http::parse_greeks(req));
    throw std::invalid_argument("Unsupported numerical experiment kind");
}
inline Json numerical_json(const storage::NumericalExperiment& e) {
    const auto saved=crow::json::load(e.result_json),configuration=crow::json::load(e.config_json);
    if(!saved||!configuration||!saved.has("request"))throw std::runtime_error("Invalid archived numerical JSON");
    // Binding must be checked, not just a result digest. The stored kind is not
    // allowed to reinterpret a different result family or configuration.
    try {
        const std::string expected=e.kind=="hedging_replication"?"hedging_replication":e.kind=="sde_convergence"?"sde_convergence":e.kind=="option_pricing"?"derivative_lab.pricing_experiment":"derivative_lab.greeks_experiment";
        if(!saved.has("kind")||std::string(saved["kind"].s())!=expected||canonical(e.kind,configuration).dump()!=canonical(e.kind,saved["request"]).dump())
            throw std::runtime_error("Archived binding mismatch");
    }catch(const std::exception&){throw std::runtime_error("Numerical experiment binding validation failed");}
    Json out;out["reference"]=e.kind+":"+std::to_string(e.id);out["kind"]=e.kind;out["immutable"]=true;out["name"]=e.name;
    out["parent_reference"]=e.parent_id?Json(e.kind+":"+std::to_string(e.parent_id)):Json(nullptr);out["created_ms"]=e.created_ms;out["engine_version"]=e.engine_version;
    out["configuration"]=research_http::precise_json(configuration);out["result"]=research_http::precise_json(saved);
    out["result_sha256"]=e.result_sha256;out["record_sha256"]=e.record_sha256;return out;
}
inline Json replay_json(const storage::Experiment& e) {
    auto out=research_http::experiment_json(e);out["kind"]="return_volatility";out["reference"]="return_volatility:"+std::to_string(e.id);
    out["parent_reference"]=e.parent_id?Json("return_volatility:"+std::to_string(e.parent_id)):Json(nullptr);return out;
}
inline Json view(storage::TimeSeriesStore& store,const Read& j) {
    research_http::fields(j,{"reference"});const auto ref=reference(j);
    if(ref.first=="return_volatility")return replay_json(store.experiment(ref.second));
    const auto e=store.numerical_experiment(ref.second);if(e.kind!=ref.first)throw std::out_of_range("Experiment kind does not match its identifier");return numerical_json(e);
}
inline Json compute(storage::TimeSeriesStore& store,const std::string& kind,const std::string& label,const Read& req,std::int64_t parent=0) {
    if(kind=="return_volatility") {
        pricing::http::keys(req,{"snapshot_id","config"});
        const auto saved=research_http::save(store,research_http::id(req,"snapshot_id"),label,research_http::request_config(req),parent);
        const auto parsed=crow::json::load(saved.dump());return replay_json(store.experiment(research_http::id(parsed,"experiment_id")));
    }
    storage::NumericalExperiment e;e.kind=kind;e.name=label;e.parent_id=parent;e.engine_version=engine(kind);
    e.config_json=canonical(kind,req).dump();Json result;
    if(kind=="hedging_replication")result=hedging_http::run(hedging_http::parse(req));
    else if(kind=="sde_convergence")result=sde_http::run(sde_http::parse(req));
    else if(kind=="option_pricing"){const auto r=pricing::http::parse(req);result=pricing::http::record(r,pricing::simulate(r.inputs,r.config));}
    else result=pricing::sensitivity_http::run(pricing::sensitivity_http::parse_greeks(req));
    e.result_json=result.dump();return numerical_json(store.numerical_experiment(store.save_numerical_experiment(e)));
}
inline Json rerun(storage::TimeSeriesStore& store,const Read& j) {
    research_http::fields(j,{"reference","name"});const auto ref=reference(j);const auto label=research_http::name(j);
    if(ref.first=="return_volatility") {
        const auto e=store.experiment(ref.second);if(e.engine_version!=research::engine_version)throw std::logic_error("Saved replay engine incompatible");
        auto out=research_http::save(store,e.snapshot_id,label,research_http::verified_experiment_config(e),e.id);
        auto parsed=crow::json::load(out.dump());return replay_json(store.experiment(research_http::id(parsed,"experiment_id")));
    }
    const auto e=store.numerical_experiment(ref.second);if(e.kind!=ref.first)throw std::out_of_range("Experiment kind mismatch");
    (void)numerical_json(e);if(e.engine_version!=engine(e.kind))throw std::logic_error("Saved numerical engine incompatible");
    const auto req=crow::json::load(e.config_json);return compute(store,e.kind,label,req,e.id);
}
inline Json catalog(storage::TimeSeriesStore& store,const Read& j) {
    research_http::fields(j,{"kind","after_reference","limit"});
    const auto k=j.has("kind")?pricing::http::text(j,"kind"):"";
    std::string after;
    if(j.has("after_reference")) {if(j["after_reference"].t()!=crow::json::type::String)throw std::invalid_argument("Invalid catalog cursor");after=j["after_reference"].s();if(after.size()>50)throw std::invalid_argument("Catalog cursor too long");}
    const int limit=j.has("limit")?research_http::bounded_integer(j,"limit",1,100):100;
    const auto p=store.typed_experiment_catalog(k,after,limit);Json out;out["rows"]=history_http::rows(p.rows);out["has_more"]=p.has_more;
    out["next_reference"]=p.next_reference;out["limit"]=limit;out["ordering"]="kind_then_local_id";return out;
}
} // namespace dts::experiments_http
