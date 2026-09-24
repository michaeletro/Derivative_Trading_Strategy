#pragma once
#include "history_json.hpp"
#include "greeks_json.hpp"
#include <dts/replay.hpp>

namespace dts::research_http {
using Json=crow::json::wvalue;
using Read=crow::json::rvalue;
using history_http::fields;
using history_http::id;
inline int bounded_integer(const Read& j,const char* key,int low,int high) {
    if(!j.has(key)||j[key].t()!=crow::json::type::Number)throw std::invalid_argument("Expected integer research field");
    const double d=j[key].d();if(!std::isfinite(d)||d!=std::floor(d)||d<low||d>high)throw std::invalid_argument("Research integer outside permitted range");
    return static_cast<int>(d);
}
inline std::string name(const Read& j) {
    if(!j.has("name")||j["name"].t()!=crow::json::type::String)throw std::invalid_argument("Supply a research name");
    const std::string s=j["name"].s();
    if(s.empty()||s.size()>80||s.find_first_not_of(' ')==std::string::npos||std::any_of(s.begin(),s.end(),[](unsigned char c){return c<32||c==127;}))
        throw std::invalid_argument("Name must contain 1..80 printable bytes");
    return s;
}
inline research::ReplayConfig config(const Read& j) {
    pricing::http::keys(j,{"windows","annualization_factor"});research::ReplayConfig c;c.windows.clear();
    if(j["windows"].t()!=crow::json::type::List||j["windows"].size()>4)throw std::invalid_argument("Expected 1..4 rolling windows");
    for(const auto& v:j["windows"]) {
        if(v.t()!=crow::json::type::Number||!std::isfinite(v.d())||std::floor(v.d())!=v.d()||v.d()<2||v.d()>250)
            throw std::invalid_argument("Window must be an integer 2..250");
        c.windows.push_back(static_cast<int>(v.d()));
    }
    c.annualization_factor=pricing::http::number(j,"annualization_factor");c.validate();return c;
}
inline research::ReplayConfig request_config(const Read& j) {
    if(!j.has("config"))throw std::invalid_argument("Supply a research configuration");
    return config(j["config"]);
}
inline Json config_json(const research::ReplayConfig& c) {
    Json j;std::vector<Json> windows;for(int w:c.windows)windows.emplace_back(w);
    j["windows"]=std::move(windows);j["annualization_factor"]=c.annualization_factor;return j;
}
inline Json optional(const std::optional<double>& v){return v?Json(*v):Json(nullptr);}
inline Json snapshot_json(const research::Snapshot& s,bool full=false) {
    const auto q=research::inspect(s);Json j;
    j["schema_version"]=1;j["kind"]="frozen_historical_snapshot";j["retrospective_only"]=true;j["immutable"]=true;
    j["snapshot_id"]=std::to_string(s.id);j["dataset_id"]=std::to_string(s.dataset_id);j["name"]=s.name;j["fingerprint"]=s.fingerprint;
    j["created_ms"]=s.created_ms;j["start_s"]=s.window.start;j["end_s"]=s.window.end;j["request_cutoff"]=std::to_string(s.request_cutoff);
    j["source"]=s.source;j["symbol"]=s.spec.contract.symbol;j["contract_id"]=std::to_string(s.spec.contract.id);j["exchange"]=s.spec.contract.exchange;
    j["bar_size"]=s.spec.bar_size;j["price_type"]=s.spec.price_type;j["use_rth"]=s.spec.use_rth;j["currency"]=s.spec.contract.currency;
    j["time_basis"]=s.time_basis;j["adjustment_policy"]=s.adjustment_policy;j["bar_count"]=q.bars;
    j["quality"]["nonpositive_closes"]=q.nonpositive_closes;j["quality"]["discontinuities"]=q.discontinuities;
    j["quality"]["large_return_candidates"]=q.large_return_candidates;j["quality"]["response_coverage_complete"]=q.response_coverage_complete;
    j["quality"]["complete_market_history"]=false;j["quality"]["calendar_verified"]=false;
    std::vector<Json> warnings;for(const auto& w:q.warnings)warnings.emplace_back(w);j["quality"]["warnings"]=std::move(warnings);
    std::vector<Json> gaps;for(auto w:s.uncovered_intervals){Json g;g["start_s"]=w.start;g["end_s"]=w.end;gaps.push_back(std::move(g));}j["uncovered_intervals"]=std::move(gaps);
    if(full){std::vector<Json> bars;std::size_t ordinal=0;
        for(const auto& b:s.observations){Json r;r["ordinal"]=++ordinal;r["version_id"]=std::to_string(b.version_id);r["request_id"]=std::to_string(b.request_id);
            r["coordinate_s"]=b.coordinate_s;r["source_time"]=b.bar.time;r["open"]=b.bar.open;r["high"]=b.bar.high;r["low"]=b.bar.low;r["close"]=b.bar.close;
            r["volume"]=b.bar.volume?Json(*b.bar.volume):Json(nullptr);r["wap"]=optional(b.bar.wap);r["bar_count"]=b.bar.count?Json(*b.bar.count):Json(nullptr);
            r["observed_ms"]=b.observed_ms;r["response_finished_ms"]=b.response_finished_ms;bars.push_back(std::move(r));}j["bars"]=std::move(bars);}
    return j;
}
inline Json run(const research::Snapshot& s,const research::ReplayConfig& c,std::size_t count) {
    const auto result=research::replay(s,c,count);Json j;
    j["kind"]="retrospective_return_diagnostics";j["schema_version"]=1;j["engine_version"]=research::engine_version;j["retrospective_only"]=true;
    j["snapshot_id"]=std::to_string(s.id);j["snapshot_fingerprint"]=s.fingerprint;j["config"]=config_json(c);
    j["processed"]=result.processed;j["total"]=result.total;j["complete"]=result.processed==result.total;
    j["availability_policy"]=result.availability_policy;j["return_policy"]=result.return_policy;
    j["units"]="log price returns; annualized sample volatility in decimal units, NOT implied volatility or total returns";
    std::vector<Json> points;
    for(const auto& p:result.points){Json r;r["ordinal"]=p.ordinal;r["segment"]=p.segment;r["coordinate_s"]=p.coordinate_s;
        r["available_s"]=p.available_s?Json(*p.available_s):Json(nullptr);r["return_span_seconds"]=p.return_span_seconds?Json(*p.return_span_seconds):Json(nullptr);
        r["close"]=p.close;r["log_return"]=optional(p.log_return);r["status"]=p.status;std::vector<Json> windows;
        for(const auto& w:p.rolling){Json x;x["window"]=w.window;x["observations"]=w.observations;x["mean_log_return"]=optional(w.mean_log_return);x["annualized_volatility"]=optional(w.annualized_volatility);windows.push_back(std::move(x));}
        r["rolling"]=std::move(windows);points.push_back(std::move(r));}
    j["points"]=std::move(points);
    // Canonical numerical payload is separately hashed without timestamps/build identity.
    j["numerical_sha256"]=research::sha256(j.dump());
    j["build"]=pricing::sensitivity_http::build_json();j["build"]["research_source_sha256"]=pricing::build::research_source_sha256;
    return j;
}
// This vendored Crow parser tags JSON decimals as Floating_point. Its generic
// rvalue->wvalue conversion then prints only float precision. Promote parsed
// decimals explicitly so archived doubles round-trip without silent truncation.
inline Json precise_json(const Read& r) {
    using Type=crow::json::type;
    switch(r.t()) {
        case Type::Null: return Json(nullptr);
        case Type::True: return Json(true);
        case Type::False: return Json(false);
        case Type::String: return Json(std::string(r.s()));
        case Type::Number:
            if(r.nt()==crow::json::num_type::Signed_integer)return Json(r.i());
            if(r.nt()==crow::json::num_type::Unsigned_integer)return Json(r.u());
            return Json(r.d());
        case Type::List: {std::vector<Json> values;for(const auto& v:r)values.push_back(precise_json(v));return Json(std::move(values));}
        case Type::Object: {Json out(Json::object{});for(const auto& key:r.keys())out[key]=precise_json(r[key]);return out;}
        default: throw std::runtime_error("Unsupported archived JSON type");
    }
}
inline research::ReplayConfig verified_experiment_config(const storage::Experiment& e) {
    const auto saved=crow::json::load(e.result_json),settings=crow::json::load(e.config_json);
    try {
        if(!saved||!settings||!saved.has("config")||!saved.has("snapshot_id")||!saved.has("engine_version")||
           std::string(saved["engine_version"].s())!=e.engine_version||id(saved,"snapshot_id")!=e.snapshot_id)
            throw std::runtime_error("Saved experiment metadata does not match its result");
        const auto c=config(settings);
        if(research::config_key(c)!=research::config_key(config(saved["config"])))
            throw std::runtime_error("Saved experiment configuration does not match its result");
        return c;
    } catch(const std::exception&) {throw std::runtime_error("Saved experiment binding validation failed; no automatic repair");}
}
inline Json experiment_json(const storage::Experiment& e) {
    (void)verified_experiment_config(e);
    auto saved=crow::json::load(e.result_json);auto config_saved=crow::json::load(e.config_json);
    if(!saved||!config_saved)throw std::runtime_error("Saved research JSON failed validation");
    Json j;j["experiment_id"]=std::to_string(e.id);j["snapshot_id"]=std::to_string(e.snapshot_id);j["parent_id"]=e.parent_id?Json(std::to_string(e.parent_id)):Json(nullptr);
    j["name"]=e.name;j["created_ms"]=e.created_ms;j["engine_version"]=e.engine_version;j["config"]=precise_json(config_saved);
    j["result"]=precise_json(saved);j["result_sha256"]=e.result_sha256;j["immutable"]=true;j["retrospective_only"]=true;return j;
}
inline Json save(storage::TimeSeriesStore& store,std::int64_t snapshot_id,const std::string& label,const research::ReplayConfig& c,std::int64_t parent=0) {
    const auto s=store.snapshot(snapshot_id);storage::Experiment e;e.snapshot_id=snapshot_id;e.parent_id=parent;e.name=label;e.engine_version=research::engine_version;
    e.config_json=config_json(c).dump();auto result=run(s,c,s.observations.size());result["snapshot"]=snapshot_json(s);e.result_json=result.dump();
    const auto id=store.save_experiment(e);return experiment_json(store.experiment(id));
}
inline Json catalog(storage::TimeSeriesStore& store,const Read& j,bool snapshots) {
    fields(j,{"after_id","limit"});std::int64_t after=0;
    if(j.has("after_id")){if(j["after_id"].t()==crow::json::type::String&&std::string(j["after_id"].s())=="0")after=0;else after=id(j,"after_id");}
    const auto limit=j.has("limit")?bounded_integer(j,"limit",1,100):100;
    const auto page=snapshots?store.snapshot_catalog(after,limit):store.experiment_catalog(after,limit);
    Json out;out["rows"]=history_http::rows(page.rows);out["has_more"]=page.has_more;out["next_after_id"]=std::to_string(page.next_after_id);out["limit"]=limit;return out;
}
} // namespace dts::research_http
