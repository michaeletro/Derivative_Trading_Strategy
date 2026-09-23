#pragma once
#include "research_json.hpp"
#include <dts/read_only_service.hpp>
namespace dts::depth_http {
using Json=crow::json::wvalue;
inline Json conventions() {
    Json j; j["schema_version"]=1;j["kind"]="displayed_depth";
    j["complete_exchange_book"]=false;j["individual_orders"]=false;
    j["sequence_basis"]="local_subscription_sequence";
    j["time_basis"]="local_callback_receipt";j["exchange_timestamp"]=nullptr;
    j["source_timestamp_available"]=false;j["direct_depth_only"]=true;return j;
}
inline Json current(const ReadOnlyService& service) {
    Json j=conventions();j["available"]=bool(service.depth());
    if(!service.depth()){j["book"]=nullptr;return j;}
    const auto& view=*service.depth();const auto& b=view.book;
    j["request_id"]=std::to_string(view.request_id);j["contract_id"]=std::to_string(view.spec.contract.id);
    j["venue"]=view.spec.venue;j["requested_rows"]=view.spec.rows;
    j["active"]=b.active();j["structural_valid"]=b.structural_valid();j["quality"]=b.quality();
    j["sequence"]=std::to_string(b.sequence());j["epoch"]=std::to_string(b.epoch());
    j["last_receipt_unix_us"]=std::to_string(b.received().unix_us);
    // Last EVENT age is not proof that both sides or all levels are fresh.
    const auto now=DepthStamp::now();
    j["last_event_age_ms"]=b.received().monotonic_ns>0?Json((now.monotonic_ns-b.received().monotonic_ns)/1000000):Json(nullptr);
    const auto rows=[](const auto& values){std::vector<Json> out;
        for(const auto& v:values){Json r;r["price"]=v.price;r["size"]=v.size;r["market_maker"]=v.market_maker;out.push_back(std::move(r));}return Json(std::move(out));};
    j["book"]["bids"]=rows(b.bids());j["book"]["asks"]=rows(b.asks());return j;
}
inline std::int64_t cursor(const crow::json::rvalue& j,const char* key) {
    if(!j.has(key))return 0;
    if(j[key].t()==crow::json::type::String && std::string(j[key].s())=="0")return 0;
    return history_http::id(j,key);
}
inline Json sessions(storage::TimeSeriesStore& store,const crow::json::rvalue& req) {
    history_http::fields(req,{"after_id","limit"});
    auto j=storage::http::page(store.depth_sessions(cursor(req,"after_id"),req.has("limit")?research_http::bounded_integer(req,"limit",1,1000):100));
    j["schema_version"]=1;j["time_filter_basis"]="local_session_id";return j;
}
inline Json events(storage::TimeSeriesStore& store,const crow::json::rvalue& req) {
    history_http::fields(req,{"session_id","after_id","through_id","limit"});const auto sid=history_http::id(req,"session_id");
    auto j=storage::http::page(store.depth_events(sid,cursor(req,"after_id"),cursor(req,"through_id"),req.has("limit")?research_http::bounded_integer(req,"limit",1,1000):1000));
    Json meta;for(const auto& [k,v]:store.depth_session(sid))std::visit([&](const auto& x){meta[k]=x;},v);
    j["schema_version"]=1;j["session"]=std::move(meta);
    j["time_filter_basis"]="local_event_id";j["complete_exchange_book"]=false;j["exchange_timestamp"]=nullptr;return j;
}
}
