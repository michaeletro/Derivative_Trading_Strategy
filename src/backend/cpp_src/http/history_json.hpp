#pragma once
#include "storage_json.hpp"
#include <dts/historical_manager.hpp>
#include <set>
namespace dts::history_http {
using Json=crow::json::wvalue;
inline Json rows(const std::vector<storage::Row>& values) {
    std::vector<Json> out;
    for(const auto& row:values){Json j;for(const auto& kv:row)std::visit([&](const auto& v){j[kv.first]=v;},kv.second);out.push_back(std::move(j));}
    return Json(std::move(out));
}
inline std::int64_t id(const crow::json::rvalue& j,const char* key) {
    if(!j.has(key)||j[key].t()!=crow::json::type::String)throw std::invalid_argument("Dataset IDs must be decimal strings");
    const std::string s=j[key].s();
    if(s.empty()||s.size()>18||s.find_first_not_of("0123456789")!=std::string::npos)throw std::invalid_argument("Invalid dataset ID");
    const auto v=std::stoll(s);if(v<=0)throw std::invalid_argument("Dataset ID must be positive");return v;
}
inline std::int64_t integer(const crow::json::rvalue& j,const char* key) {
    if(!j.has(key)||j[key].t()!=crow::json::type::Number)throw std::invalid_argument("Expected integer numeric field");
    const auto v=j[key].d();if(!std::isfinite(v)||std::floor(v)!=v||v<0||v>4102444800LL)throw std::invalid_argument("Invalid history timestamp/contract ID");return static_cast<std::int64_t>(v);
}
inline void fields(const crow::json::rvalue& j,std::set<std::string> allowed) {
    for(const auto& key:j.keys())if(!allowed.count(key))throw std::invalid_argument("Unknown historical request field");
}
inline HistoryWindow window(const crow::json::rvalue& j) {return {integer(j,"start_s"),integer(j,"end_s")};}
inline Json view(storage::TimeSeriesStore& store,std::int64_t id,HistoryWindow w) {
    const auto spec=store.historical_spec(id);spec.validate(w);
    Json j;j["dataset_id"]=std::to_string(id);j["symbol"]=spec.contract.symbol;j["contract_id"]=spec.contract.id;
    j["bar_size"]=spec.bar_size;j["price_type"]=spec.price_type;j["use_rth"]=spec.use_rth;j["currency"]=spec.contract.currency;
    j["start_s"]=w.start;j["end_s"]=w.end;
    j["time_basis"]=spec.bar_size=="1 day"?"provider_session_date":"UTC_epoch_seconds";
    j["adjustment_policy"]="provider_native_not_normalized";
    j["bars"]=rows(store.historical_bars(id,w));j["requests"]=rows(store.historical_requests(id,w));
    std::vector<Json> gaps;for(auto g:store.historical_gaps(id,w)){Json row;row["start_s"]=g.start;row["end_s"]=g.end;gaps.push_back(std::move(row));}
    j["response_coverage_complete"]=gaps.empty();j["uncovered_intervals"]=std::move(gaps);
    j["complete_market_history"]=false;j["recorded_not_live"]=true;j["recent_request_limit"]=200;
    j["revision_policy"]="latest_completed_response_per_coordinate";
    return j;
}
} // namespace dts::history_http
