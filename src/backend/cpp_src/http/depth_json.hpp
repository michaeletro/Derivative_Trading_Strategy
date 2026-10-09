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
inline Json metadata_fields() {
    Json j;
    j["session"]=std::vector<std::string>{"session_id","run_id","source","native_id","contract_id","symbol","currency","contract_route","venue","requested_rows","smart_depth","started_ms","ended_ms","state","last_sequence","event_count"};
    j["event"]=std::vector<std::string>{"event_id","sequence","kind","origin","received_unix_us","received_monotonic_ns","operation","side","position","price","price_repr","size","market_maker","smart_depth","code"};
    j["receipt_time_basis"]="local callback receipt; not exchange time";
    j["sequence_basis"]="local subscription sequence; not exchange sequence";
    j["count_basis"]="all committed depth events, including start/reset/stop/error/gap markers";
    j["raw_sidecar"]=std::vector<std::string>{"callback_format","raw_payload_hex","raw_price_repr","size_hex","market_maker_hex","received_unix_us","received_monotonic_ns","sequence","request_id","kind","origin","operation","side","position","smart_depth","code"};
    j["raw_scope"]="Depth callbacks received by this adapter; SDK-decoded protobuf reserialized with unknown/presence fields retained. Not wire-identical bytes or a packet capture or all TWS messages.";
    j["raw_commit_rule"]="Accept raw records only through raw_metadata.committed_through_sequence, excluding DB-only recovery markers; a crash can leave an uncommitted tail.";
    j["raw_backup"]="Private depth-raw directory must be backed up separately from SQLite.";

    j["unavailable"]=std::vector<std::string>{"exchange_timestamp","exchange_sequence","individual_order_id","complete_exchange_book","hidden_liquidity"};
    return j;
}
inline Json raw_metadata(storage::TimeSeriesStore& store, std::int64_t session_id) {
    const auto row=store.depth_raw_metadata(session_id);Json j;
    for(const auto& [key,value]:row)std::visit([&](const auto& x){j[key]=x;},value);
    j["available"]=std::get<std::int64_t>(row.at("available"))!=0;
    j["uncommitted_tail_possible"]=true;j["sqlite_backup_includes_raw_metadata"]=false;
    j["recovery_markers_may_be_sqlite_only"]=true;return j;
}
inline Json recording(storage::TimeSeriesStore* store, const std::string& source, RequestId request_id) {
    Json j;j["available"]=store!=nullptr;j["healthy"]=false;j["active"]=false;
    j["session_id"]=nullptr;j["state"]="unavailable";j["committed_event_count"]=nullptr;j["committed_sequence"]=nullptr;
    j["session"]=nullptr;j["last_commit_ms"]=nullptr;j["last_error"]="";
    j["raw_metadata"]=nullptr;
    j["count_includes_lifecycle_events"]=true;j["last_commit_scope"]="all recorder streams";
    j["journal_mode"]="wal";j["synchronous"]="full";
    if(!store)return j;
    const auto status=store->status();
    j["healthy"]=status.open&&!status.failed;j["last_commit_ms"]=status.last_commit_ms;j["last_error"]=status.last_error;
    j["run_id"]=status.run_id;j["state"]="idle";
    if(!status.open|| (source!="mock" && source!="ibkr_tws"))return j;
    const auto row=store->depth_recording(source,request_id);
    if(row.empty())return j;
    Json session;for(const auto& [key,value]:row)std::visit([&](const auto& x){session[key]=x;},value);
    j["session"]=std::move(session);
    for(const auto* key:{"session_id","state","source","venue","requested_rows"})
        std::visit([&](const auto& x){j[key]=x;},row.at(key));
    j["committed_event_count"]=std::get<std::string>(row.at("event_count"));
    j["committed_sequence"]=std::get<std::string>(row.at("last_sequence"));
    j["active"]=std::get<std::string>(row.at("state"))=="recording";
    try { j["raw_metadata"]=raw_metadata(*store,std::stoll(std::get<std::string>(row.at("session_id")))); }
    catch(const std::exception&) {
        // A damaged sidecar must not hide the actual sticky recorder failure.
        j["raw_metadata"]["available"]=false;
        j["raw_metadata"]["error"]="Raw metadata status unavailable; inspect recorder health";
    }
    return j;
}
inline Json current(const ReadOnlyService& service, storage::TimeSeriesStore* store=nullptr, const std::string& source="") {
    Json j=conventions();j["available"]=bool(service.depth());
    j["recording"]=recording(store,source,service.depth()?service.depth()->request_id:0);
    j["metadata_fields"]=metadata_fields();
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
    j["time_filter_basis"]="local_event_id";j["complete_exchange_book"]=false;j["exchange_timestamp"]=nullptr;j["metadata_fields"]=metadata_fields();j["raw_metadata"]=raw_metadata(store,sid);return j;
}
}
