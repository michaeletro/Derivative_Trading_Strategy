#pragma once
#include <crow_all.h>
#include <dts/time_series_store.hpp>
#include <limits>

namespace dts::storage::http {
using Json=crow::json::wvalue;
inline Json status(const Status& s) {
    Json j;j["open"]=s.open;j["failed"]=s.failed;j["database"]=s.database;
    j["backup_directory"]=s.backup_directory;j["run_id"]=s.run_id;
    j["quote_count"]=std::to_string(s.quotes);j["bar_count"]=std::to_string(s.bars);
    j["interrupted_runs"]=std::to_string(s.interrupted_runs);j["last_commit_ms"]=s.last_commit_ms;
    j["last_backup"]=s.last_backup;j["last_error"]=s.last_error;
    j["journal_mode"]="wal";j["synchronous"]="full";j["auto_backup_on_shutdown"]=true;
    j["single_recorder"]=true;j["automatic_deletion"]=false;return j;
}
inline Json page(const Page& p) {
    Json j;std::vector<Json> rows;
    for(const auto& row:p.rows){Json r;for(const auto& [k,v]:row)std::visit([&](const auto& x){r[k]=x;},v);rows.push_back(std::move(r));}
    j["rows"]=std::move(rows);j["through_id"]=std::to_string(p.through_id);
    j["next_after_id"]=std::to_string(p.next_after_id);j["has_more"]=p.has_more;
    j["recorded_not_live"]=true;j["time_filter_basis"]="local_observation_time_ms";
    j["complete_market_history"]=false;return j;
}
inline std::int64_t parameter(const crow::request& req,const char* key,std::int64_t fallback=0) {
    const auto* raw=req.url_params.get(key);if(!raw)return fallback;
    const std::string value=raw;
    if(value.empty()||value.size()>19||value.find_first_not_of("0123456789")!=std::string::npos)
        throw std::invalid_argument("History IDs and times must be unsigned decimal strings");
    std::size_t n=0;const auto v=std::stoll(value,&n);
    if(n!=value.size())throw std::invalid_argument("Invalid history parameter");
    return v;
}
inline int limit(const crow::request& req){const auto v=parameter(req,"limit",100);if(v<1||v>1000)throw std::invalid_argument("History limit must be 1..1000");return static_cast<int>(v);}
} // namespace dts::storage::http
