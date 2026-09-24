#pragma once
#include "time_series_store.hpp"
#include <dts/read_only_service.hpp>
#include <deque>

namespace dts::storage {
struct HistoryPlan {
    std::int64_t dataset_id=0;
    std::vector<std::int64_t> queued_ids;
    std::vector<HistoryWindow> missing;
};
// Called under the same application mutex as the broker. One native request
// at a time, >=15 seconds between dispatches across datasets (a conservative
// local research policy, not a guarantee concerning other TWS API clients).
class HistoricalManager {
    struct Work { std::int64_t id; HistorySpec spec; HistoryWindow window; };
    TimeSeriesStore& store_;
    std::deque<Work> queue_;
    std::optional<std::pair<std::int64_t,RequestId>> active_;
    Clock::time_point next_dispatch_{};
public:
    explicit HistoricalManager(TimeSeriesStore& s):store_(s) {
        const auto now=std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
        const auto wait=std::clamp<std::int64_t>(15000-(now-store_.recent_history_dispatch_ms()),0,15000);
        next_dispatch_=Clock::now()+std::chrono::milliseconds(wait);
    }
    HistoryPlan request(const HistorySpec& spec,HistoryWindow w,const std::string& policy,ConnectionState state) {
        spec.validate(w);
        if(policy!="saved"&&policy!="fetch_missing"&&policy!="refresh")throw std::invalid_argument("Invalid historical fetch policy");
        HistoryPlan out;out.dataset_id=store_.historical_dataset(spec);
        out.missing=store_.historical_gaps(out.dataset_id,w);
        if(policy=="saved")return out;
        auto gaps=store_.historical_gaps(out.dataset_id,w,true);
        if(policy=="refresh") {
            std::vector<HistoryWindow> pending;
            for(const auto& r:store_.historical_requests(out.dataset_id,w)) {
                const auto& st=std::get<std::string>(r.at("state"));
                if(st=="queued"||st=="pending")pending.push_back({std::get<std::int64_t>(r.at("start_s")),std::get<std::int64_t>(r.at("end_s"))});
            }
            gaps=uncovered(w,pending);
        }
        if(gaps.empty())return out;
        if(state!=ConnectionState::Ready)throw std::logic_error("Saved data may be read offline. Connect the intended TWS session before fetching uncovered/refreshed intervals");
        const auto now=std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch()).count();
        // Only closed windows. Daily UTC-coordinate end plus buffer must be past.
        if(w.end>(spec.bar_size=="1 day" ? now/86400*86400-2*86400 : now-60))
            throw std::invalid_argument("Historical window too recent: daily end at least two UTC dates ago; minute end at least one minute ago");
        auto chunks=historical_chunks(spec,gaps);
        if(queue_.size()+chunks.size()+(active_?1:0)>32)throw std::length_error("Historical queue full; wait or narrow the interval");
        out.queued_ids=store_.queue_history(out.dataset_id,chunks);
        for(std::size_t i=0;i<chunks.size();++i)queue_.push_back({out.queued_ids[i],spec,chunks[i]});
        return out;
    }
    void tick(ReadOnlyService& broker,Clock::time_point now=Clock::now()) {
        if((active_||!queue_.empty())&&broker.state()!=ConnectionState::Ready) {
            store_.interrupt_history();active_.reset();queue_.clear();return;
        }
        if(active_) {
            if(store_.history_state(active_->first)=="pending")return;
            active_.reset();
        }
        if(queue_.empty()||now<next_dispatch_)return;
        auto w=queue_.front();queue_.pop_front();next_dispatch_=now+std::chrono::seconds(15);
        try {
            const auto native=broker.request_history(w.spec,w.window);
            try {store_.bind_history(w.id,native);}
            catch(...) {broker.cancel_history(native);broker.disconnect();throw;}
            active_=std::make_pair(w.id,native);
        }catch(const std::exception&) {store_.finish_history(w.id,"failed",-1030);}
    }
    void cancel(ReadOnlyService& broker) {
        if(active_)broker.cancel_history(active_->second);
        store_.interrupt_history();active_.reset();queue_.clear();
    }
};
} // namespace dts::storage
