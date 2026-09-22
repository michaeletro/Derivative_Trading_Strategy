#pragma once
#include "time_series_store.hpp"
#include <memory>
#include <utility>

namespace dts::storage {
// Decorates the native/mock boundary, not the browser poll. Every delivered
// normalized quote is committed before the application can publish that batch.
class RecordingBroker final : public IBroker {
    std::unique_ptr<IBroker> inner_;
    TimeSeriesStore& store_;
    std::string source_;
    bool active_ = false;
    std::vector<BrokerEvent> pending_;
public:
    RecordingBroker(std::unique_ptr<IBroker> broker, TimeSeriesStore& store, std::string source)
        : inner_(std::move(broker)), store_(store), source_(std::move(source)) {
        if (!inner_) throw std::invalid_argument("Recording broker requires an adapter");
    }
    ~RecordingBroker() override { disconnect(); }
    void connect() override {
        pending_.clear();store_.request(source_, "connect_requested"); active_ = true;
        inner_->connect();
    }
    ConnectionState state() const noexcept override { return inner_->state(); }
    void disconnect() noexcept override {
        if (active_) {
            // A final drain has a finite cutoff. Bytes still in the network/SDK
            // after this point are not claimed to have been recorded.
            try { store_.record_events(source_, inner_->poll()); }
            catch (...) { /* Store retains a sticky failure; shutdown reports it. */ }
            inner_->disconnect();
            try { store_.request(source_, "disconnected"); } catch (...) {}
            active_ = false;pending_.clear();
        } else inner_->disconnect();
    }
    RequestId resolve(const ContractQuery& query) override {
        query.validate(); store_.request(source_, "resolve_requested", 0, query.symbol);
        const auto id = inner_->resolve(query);
        store_.request(source_, "resolve_accepted", id, query.symbol); return id;
    }
    RequestId subscribe(const Contract& contract) override {
        store_.register_contract(source_, contract);
        store_.request(source_, "subscribe_requested", 0, std::to_string(contract.id));
        const auto id = inner_->subscribe(contract);
        store_.request(source_, "subscribe_accepted", id, std::to_string(contract.id)); return id;
    }
    bool unsubscribe(RequestId id) override {
        store_.require_healthy();
        // Preserve events already delivered at cancellation time, even though
        // they need not be shown as current after the subscription disappears.
        auto drained=inner_->poll();store_.record_events(source_,drained);
        if(pending_.size()+drained.size()>4096) {
            store_.request(source_,"recorder_delivery_overflow");disconnect();
            throw std::overflow_error("Recorded event delivery queue overflow");
        }
        pending_.insert(pending_.end(),std::make_move_iterator(drained.begin()),std::make_move_iterator(drained.end()));
        const bool cancelled = inner_->unsubscribe(id);
        store_.request(source_, cancelled ? "unsubscribed" : "unsubscribe_unknown", id);
        return cancelled;
    }
    void request_positions(RequestId id) override {
        store_.require_healthy(); inner_->request_positions(id);
        // Deliberately do not persist account identifiers or position contents.
    }
    std::vector<BrokerEvent> poll() override {
        store_.require_healthy();
        auto events = inner_->poll(); store_.record_events(source_, events);
        std::vector<BrokerEvent> out;out.swap(pending_);
        out.insert(out.end(),std::make_move_iterator(events.begin()),std::make_move_iterator(events.end()));return out;
    }
};
} // namespace dts::storage
