#pragma once
#include "orderbook_jobs.hpp"
#include <dts/read_only_service.hpp>

namespace dts::tick_jobs {
// Reuse the existing private, fsynced file helpers.
using orderbook_jobs::Json;
using orderbook_jobs::Read;
using orderbook_jobs::job_id;
using orderbook_jobs::private_dir;
using orderbook_jobs::read_json;
using orderbook_jobs::state_write;
using orderbook_jobs::now_ms;
using orderbook_jobs::field;
using orderbook_jobs::write_file;
using orderbook_jobs::read_file;
namespace fs = std::filesystem;
inline Json tick_json(const HistoricalTick& r,const std::string& type) {
    Json j;j["time_s"]=r.time;
    if(type=="TRADES"){
        j["price"]=r.price;j["size"]=r.size;j["exchange"]=r.exchange;j["conditions"]=r.conditions;
        j["past_limit"]=r.past_limit;j["unreported"]=r.unreported;
    }else{
        j["bid"]=r.bid;j["ask"]=r.ask;j["bid_size"]=r.bid_size;j["ask_size"]=r.ask_size;
        j["bid_past_low"]=r.bid_past_low;j["ask_past_high"]=r.ask_past_high;
    }
    return j;
}
// A sidecar archive preserves the existing SQLite schema. Completed pages are
// durable before their cursor advances. One owner: the existing dashboard lock.
class Manager {
    fs::path root_;
    std::string active_;
    TickSpec spec_;
    RequestId request_=0;
    Clock::time_point next_=Clock::now()+std::chrono::seconds(15);
    static constexpr std::size_t meta_cap=1000000;
    fs::path directory(const std::string& id) const {
        if(!job_id(id))throw std::invalid_argument("Invalid tick download ID");
        auto p=root_/id;private_dir(p,false);return p;
    }
    Read read(const std::string& id) const {return read_json(directory(id)/"state.json",meta_cap);}
    void terminal(const std::string& state,int code=0,const std::string& error="") {
        auto dir=directory(active_);Json j(read(active_));j["state"]=state;j["code"]=code;j["updated_ms"]=now_ms();
        j["error"]=error;state_write(dir,j);active_.clear();request_=0;
    }
    static TickSpec spec_from(const Read& r) {
        TickSpec s;s.contract.id=r["contract_id"].i();s.contract.symbol=field(r,"symbol");
        s.contract.currency="USD";s.contract.exchange=field(r,"exchange");s.contract.multiplier=1;
        s.type=field(r,"tick_type");s.use_rth=r["use_rth"].b();s.validate();return s;
    }
public:
    explicit Manager(const std::string& database):root_(fs::path(database).parent_path()/"historical-ticks") {
        private_dir(root_,true);
        for(const auto& e:fs::directory_iterator(root_))if(job_id(e.path().filename().string())) {
            try {
            auto m=read(e.path().filename().string());
            if(field(m,"state")=="downloading"){
                Json j(m);j["state"]="interrupted";j["code"]=-1044;state_write(e.path(),j);
            }
            }catch(const std::exception&){} // Preserve invalid/orphaned files; report in catalog.
        }
    }
    bool busy() const {return !active_.empty();}
    Clock::time_point next_dispatch() const {return next_;}
    Json catalog() const {
        std::vector<std::pair<std::int64_t,std::string>> ids;int invalid=0;
        for(const auto& e:fs::directory_iterator(root_))if(job_id(e.path().filename().string())) {
            try{const auto id=e.path().filename().string();auto m=read(id);ids.emplace_back(m["created_ms"].i(),id);}
            catch(const std::exception&){++invalid;}
        }
        std::sort(ids.rbegin(),ids.rend());std::vector<Json> rows;
        for(std::size_t n=0;n<std::min<std::size_t>(100,ids.size());++n)rows.push_back(status(ids[n].second));
        Json out;out["downloads"]=std::move(rows);out["catalog_limit"]=100;out["invalid_records"]=invalid;return out;
    }
    Json status(const std::string& id) const {
        auto r=read(id);Json j(r);j["pages"]=static_cast<std::uint64_t>(r["pages"].size());
        j["active"]=id==active_;return j;
    }
    Json start(const TickSpec& spec,HistoryWindow w,ConnectionState state) {
        spec.validate(w);
        const auto now=now_ms()/1000;
        if(w.end>now-60)throw std::invalid_argument("End the period at least one minute in the past");
        if(busy())throw std::logic_error("A tick download is already active; stop it first");
        if(state!=ConnectionState::Ready)throw std::logic_error("Connect the intended paper TWS session first");
        std::size_t count=0;for(const auto& e:fs::directory_iterator(root_))if(job_id(e.path().filename().string()))++count;
        if(count>=1000)throw std::length_error("Tick archive catalog is full; preserve/archive files before adding more");
        const auto id=local_auth::random_secret().substr(0,32);auto dir=root_/id;private_dir(dir,true);
        Json j;j["schema_version"]=1;j["download_id"]=id;j["source"]="ibkr_tws_historical_ticks";
        j["symbol"]=spec.contract.symbol;j["contract_id"]=spec.contract.id;j["exchange"]=spec.contract.exchange;j["currency"]="USD";
        j["tick_type"]=spec.type;j["use_rth"]=spec.use_rth;j["start_s"]=w.start;j["end_s"]=w.end;j["next_s"]=w.start;
        j["created_ms"]=now_ms();j["updated_ms"]=now_ms();j["state"]="downloading";j["code"]=0;j["tick_count"]=0;
        j["pages"]=std::vector<Json>{};j["empty_pages"]=0;j["complete_market_history"]=false;
        j["timestamp_precision"]="one_second";j["level"]="trades_or_top_of_book_not_depth";
        state_write(dir,j);spec_=spec;active_=id;return status(id);
    }
    Json resume(const std::string& id,ConnectionState state) {
        if(busy())throw std::logic_error("A tick download is already active");
        if(state!=ConnectionState::Ready)throw std::logic_error("Connect the intended paper TWS session first");
        auto r=read(id);const auto st=field(r,"state");
        if(st!="cancelled" && st!="failed" && st!="interrupted")throw std::logic_error("Choose a new period starting at the displayed next timestamp");
        spec_=spec_from(r);spec_.validate({r["start_s"].i(),r["end_s"].i()});
        if(r["next_s"].i()<r["start_s"].i()||r["next_s"].i()>=r["end_s"].i())throw std::invalid_argument("Invalid saved tick cursor");
        Json j(r);j["state"]="downloading";j["code"]=0;j["updated_ms"]=now_ms();state_write(directory(id),j);
        active_=id;return status(id);
    }
    Json cancel(const std::string& id,ReadOnlyService& broker) {
        if(active_==id){if(request_)broker.cancel_ticks(request_);terminal("cancelled");}
        return status(id);
    }
    void shutdown(ReadOnlyService& broker) {
        if(busy()){if(request_)broker.cancel_ticks(request_);terminal("interrupted",-1044);}
    }
    void tick(ReadOnlyService& broker,Clock::time_point now=Clock::now()) {
        for(const auto& page:broker.take_tick_pages()) {
            if(!busy()||page.request_id!=request_)continue;
            request_=0;
            if(page.status!="complete"){terminal("failed",page.code,page.error);continue;}
            auto m=read(active_);const auto start=m["next_s"].i(),end=m["end_s"].i();
            std::vector<Json> rows;
            for(const auto& r:page.ticks)if(r.time>=start && r.time<end)rows.push_back(tick_json(r,spec_.type));
            // done=true closes the entire last second, even when >1000 ticks.
            // An empty response only advances to the next UTC date; it does not
            // assert that the interval contained no trading.
            const auto next=page.ticks.empty()?std::min(end,(start/86400+1)*86400):std::min(end,page.ticks.back().time+1);
            if(next<=start){terminal("failed",-1045);continue;}
            Json file;file["requested_start_s"]=start;file["next_s"]=next;
            file["provider_count"]=static_cast<std::uint64_t>(page.ticks.size());file["ticks"]=std::move(rows);
            file["excluded_before_start"]=std::count_if(page.ticks.begin(),page.ticks.end(),[&](const auto& r){return r.time<start;});
            file["excluded_at_or_after_end"]=std::count_if(page.ticks.begin(),page.ticks.end(),[&](const auto& r){return r.time>=end;});
            const auto raw=file.dump();const auto filename="page-"+local_auth::random_secret().substr(0,32)+".json";
            write_file(directory(active_)/filename,raw);
            Json entry;entry["file"]=filename;entry["sha256"]=research::sha256(raw);
            entry["count"]=static_cast<std::uint64_t>(file["ticks"].size());
            std::vector<Json> pages;for(const auto& p:m["pages"])pages.emplace_back(p);pages.push_back(std::move(entry));
            Json updated(m);updated["pages"]=std::move(pages);updated["next_s"]=next;
            updated["tick_count"]=m["tick_count"].i()+static_cast<std::int64_t>(file["ticks"].size());
            updated["empty_pages"]=m["empty_pages"].i()+(page.ticks.empty()?1:0);updated["updated_ms"]=now_ms();
            state_write(directory(active_),updated);
            if(next==end)terminal("complete");
            else if(updated["pages"].size()>=1000 || m["tick_count"].i()+static_cast<std::int64_t>(file["ticks"].size())>=1000000)terminal("limited",-1046);
        }
        if(!busy())return;
        if(broker.state()!=ConnectionState::Ready){terminal("interrupted",-1044);return;}
        if(request_||now<next_)return;
        next_=now+std::chrono::seconds(spec_.type=="BID_ASK"?30:15);
        try{request_=broker.request_ticks(spec_,read(active_)["next_s"].i());}
        catch(const std::exception&){terminal("failed",-1047);}
    }
    Json view(const std::string& id,std::int64_t after,int limit) const {
        // The final full provider page may cross the one-million stopping point.
        if(after<0||after>1000000+static_cast<std::int64_t>(max_tick_page)||limit<1||limit>1000)throw std::invalid_argument("Invalid tick page cursor or limit");
        auto m=read(id);std::vector<Json> rows;std::int64_t ordinal=0;
        for(const auto& p:m["pages"]){
            const auto count=p["count"].i();
            if(ordinal+count<=after){ordinal+=count;continue;}
            const auto filename=field(p,"file");
            if(filename.size()!=42||filename.substr(0,5)!="page-"||filename.substr(37)!=".json"||!job_id(filename.substr(5,32)))throw std::runtime_error("Invalid tick page path");
            auto raw=read_file(directory(id)/filename,16000000);
            if(research::sha256(raw)!=field(p,"sha256"))throw std::runtime_error("Saved tick page checksum mismatch");
            auto page=crow::json::load(raw);if(!page||page["ticks"].size()!=static_cast<std::size_t>(count))throw std::runtime_error("Invalid saved tick page");
            for(const auto& t:page["ticks"]){++ordinal;if(ordinal>after && rows.size()<static_cast<std::size_t>(limit)){Json row(t);row["ordinal"]=ordinal;rows.push_back(std::move(row));}}
            if(rows.size()>=static_cast<std::size_t>(limit))break;
        }
        const auto next=after+static_cast<std::int64_t>(rows.size());
        Json out;out["download"]=status(id);out["ticks"]=std::move(rows);out["next_after"]=next;
        out["has_more"]=next<m["tick_count"].i();return out;
    }
};
} // namespace dts::tick_jobs
