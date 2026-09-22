#pragma once
#include "broker.hpp"
#include <deque>
#include <limits>
#include <map>
#include <set>
#include <utility>

namespace dts {
// SDK-independent state machine. The adapter serializes access and supplies
// receipt times; tests can drive deadlines without a broker or wall-clock sleeps.
class TwsState {
public:
    explicit TwsState(std::chrono::milliseconds timeout = std::chrono::seconds(10))
        : timeout_(timeout) {
        if (timeout_.count() <= 0) throw std::invalid_argument("Invalid TWS timeout");
    }
    ConnectionState state() const noexcept { return state_; }
    bool positions_pending() const noexcept { return position_request_ != 0; }
    void start(Clock::time_point now) {
        clear(); state_ = ConnectionState::Connecting; deadline_ = now + timeout_;
        emit(ConnectionEvent{state_});
    }
    void ready() {
        if (state_ != ConnectionState::Connecting) return;
        state_ = ConnectionState::Ready; emit(ConnectionEvent{state_});
    }
    void disconnect() noexcept {
        clear(); state_ = ConnectionState::Disconnected;
    }
    void fail(int code, const std::string& message) {
        clear(); state_ = ConnectionState::Failed;
        events_.emplace_back(BrokerError{0, code, message});
        events_.emplace_back(ConnectionEvent{state_});
    }
    RequestId history(const HistorySpec& spec, HistoryWindow window, Clock::time_point now) {
        require_ready(); spec.validate(window);
        if (history_id_) throw std::length_error("One active native historical request permitted");
        throttle(now); history_id_=allocate(); history_spec_=spec; history_window_=window;
        history_deadline_=now+std::chrono::seconds(60); history_count_=0; return history_id_;
    }
    void historical_bar(RequestId id, const HistoricalBar& bar) {
        if (!history_id_ || id!=history_id_) return;
        try {
            const auto t=bar_coordinate(history_spec_,bar);
            if (++history_count_>1800) { finish_history("failed",-1020); return; }
            if(t>=history_window_.start && t<history_window_.end) {
                auto normalized=bar;
                if(history_spec_.price_type!="TRADES") {
                    normalized.volume.reset();normalized.wap.reset();normalized.count.reset();
                }
                emit(HistoricalBarEvent{id,std::move(normalized)});
            }
        } catch(const std::exception&) { finish_history("failed",-1021); }
    }
    void historical_end(RequestId id, const std::string& start, const std::string& end) {
        if (id==history_id_ && history_id_) finish_history("complete",0,start,end);
    }
    void cancel_history(RequestId id) {
        if(id==history_id_ && history_id_) finish_history("interrupted",-1022);
    }
    std::vector<RequestId> history_cancellations() {
        std::vector<RequestId> out;out.swap(history_cancels_);return out;
    }
    RequestId resolve(const ContractQuery& query, Clock::time_point now) {
        require_ready(); query.validate();
        if (resolutions_.size() >= 16) throw std::length_error("Too many contract requests");
        throttle(now);
        const auto id = allocate();
        resolutions_.emplace(id, Resolution{now + timeout_, {}});
        return id;
    }
    RequestId subscribe(const Contract& contract, Clock::time_point now) {
        require_ready(); contract.validate();
        if (subscriptions_.size() >= 16) throw std::length_error("Subscription limit reached");
        for (const auto& entry : subscriptions_)
            if (entry.second.quote.contract_id == contract.id)
                throw std::invalid_argument("Contract already subscribed");
        throttle(now);
        const auto id = allocate();
        Subscription sub; sub.quote.contract_id = contract.id;
        subscriptions_.emplace(id, sub);
        return id;
    }
    bool unsubscribe(RequestId id) {
        auto it = subscriptions_.find(id);
        if (it == subscriptions_.end()) return false;
        const auto contract_id = it->second.quote.contract_id;
        subscriptions_.erase(it);
        // Remove marks not yet delivered to the application.
        for (auto e = events_.begin(); e != events_.end();) {
            const auto* q = std::get_if<Quote>(&*e);
            if (q && q->contract_id == contract_id) e = events_.erase(e); else ++e;
        }
        return true;
    }
    void request_positions(RequestId id, Clock::time_point now) {
        require_ready();
        if (!id) throw std::invalid_argument("Position request ID must be nonzero");
        // reqPositions callbacks carry no reqId. Do not conflate late updates
        // from an old subscription with a second snapshot on this connection.
        if (position_seen_) throw std::logic_error("Reconnect before another position snapshot");
        throttle(now);
        position_seen_ = true; position_request_ = id; position_valid_ = true;
        position_deadline_ = now + timeout_;
    }
    void contract(RequestId id, const Contract& contract) {
        auto it = resolutions_.find(id);
        if (it == resolutions_.end()) return;
        contract.validate();
        if (!it->second.ids.insert(contract.id).second) return;
        if (it->second.ids.size() > 64) { error(id, -1003, "Contract result limit exceeded"); return; }
        emit(ContractEvent{id, contract});
    }
    void contract_end(RequestId id) {
        const auto it = resolutions_.find(id);
        if (it == resolutions_.end()) return;
        const bool success = !it->second.ids.empty();
        resolutions_.erase(it);
        emit(ContractsComplete{id, success});
    }
    void data_type(RequestId id, int type) {
        auto it = subscriptions_.find(id);
        if (it == subscriptions_.end()) return;
        if (type < 1 || type > 4) { error(id, -1004, "Unknown market data type"); return; }
        auto& sub = it->second;
        if (sub.type == type) return;
        sub.type = type; sub.quote.bid.reset(); sub.quote.ask.reset();
        const MarketDataType types[] = {MarketDataType::Realtime, MarketDataType::Frozen,
            MarketDataType::Delayed, MarketDataType::DelayedFrozen};
        sub.quote.data_type = types[type - 1];
        emit(sub.quote); // Invalidate cached marks when the feed mode changes.
    }
    void price(RequestId id, int field, double value, Clock::time_point now) {
        auto it = subscriptions_.find(id);
        if (it == subscriptions_.end()) return;
        if (field != 1 && field != 2 && field != 66 && field != 67) return;
        if (field == 66 || field == 67) {
            if (it->second.type != 3 && it->second.type != 4) data_type(id, 3);
        } else if (it->second.type == 3 || it->second.type == 4) {
            // A legacy tick cannot relabel a delayed feed as realtime.
            return;
        }
        it = subscriptions_.find(id);
        if (it == subscriptions_.end() || it->second.type == 0) return;
        auto& q = it->second.quote;
        auto& side = (field == 1 || field == 66) ? q.bid : q.ask;
        if (!std::isfinite(value) || value < 0.0) side.reset();
        else side = QuoteSide{value, now};
        emit(q);
    }
    void position(const Position& position) {
        if (!position_request_) return;
        (void)position.marked_value(0.0);
        emit(PositionEvent{position_request_, position});
    }
    void bad_position(const std::string& reason) {
        if (!position_request_) return;
        position_valid_ = false;
        emit(BrokerError{position_request_, -1005, reason});
    }
    void position_end() {
        if (!position_request_) return;
        const auto id = position_request_; position_request_ = 0;
        emit(PositionsComplete{id, position_valid_});
    }
    void error(RequestId id, int code, const std::string& message) {
        if (code == 1100 || code == 1101 || code == 1300 || code == 502 || code == 504 || code == 326) {
            fail(code, message); return;
        }
        if(history_id_ && id==history_id_) {
            // Errors are request-specific. Do not infer permanent unavailability
            // from generic HMDS/pacing/permission code 162.
            if(code==165)finish_history("unavailable",code);
            else if(code<2000)finish_history("failed",code);
        }
        emit(BrokerError{id, code, message});
        if (resolutions_.erase(id)) emit(ContractsComplete{id, false});
        auto it = subscriptions_.find(id);
        if (it != subscriptions_.end()) {
            it->second.quote.bid.reset(); it->second.quote.ask.reset();
            emit(it->second.quote);
        }
    }
    void expire(Clock::time_point now) {
        if (state_ == ConnectionState::Connecting && now >= deadline_) {
            fail(-1001, "TWS handshake timed out"); return;
        }
        if(history_id_ && now>=history_deadline_)finish_history("failed",-1023);
        std::vector<RequestId> expired;
        for (const auto& item : resolutions_)
            if (now >= item.second.deadline) expired.push_back(item.first);
        for (auto id : expired) error(id, -1002, "Contract resolution timed out");
        if (position_request_ && now >= position_deadline_) {
            bad_position("Position snapshot timed out"); position_end();
        }
    }
    std::vector<BrokerEvent> poll() {
        std::vector<BrokerEvent> out; out.swap(events_); return out;
    }
private:
    RequestId history_id_=0; std::size_t history_count_=0;
    HistorySpec history_spec_; HistoryWindow history_window_; Clock::time_point history_deadline_{};
    std::vector<RequestId> history_cancels_;
    void finish_history(const std::string& status,int code,const std::string& start="",const std::string& end="") {
        const auto id=history_id_;history_id_=0;
        if(status!="complete")history_cancels_.push_back(id);
        emit(HistoricalEnd{id,status,code,start.substr(0,64),end.substr(0,64)});
    }
    struct Resolution { Clock::time_point deadline; std::set<ContractId> ids; };
    struct Subscription { Quote quote; int type = 0; };
    ConnectionState state_ = ConnectionState::Disconnected;
    std::chrono::milliseconds timeout_;
    Clock::time_point deadline_{}, position_deadline_{};
    RequestId next_id_ = 1, position_request_ = 0;
    bool position_seen_ = false, position_valid_ = false;
    std::map<RequestId, Resolution> resolutions_;
    std::map<RequestId, Subscription> subscriptions_;
    std::vector<BrokerEvent> events_;
    std::deque<Clock::time_point> requests_;
    void clear() noexcept {
        resolutions_.clear(); subscriptions_.clear(); events_.clear(); requests_.clear();
        history_id_=0;history_cancels_.clear();
        position_request_ = 0; position_seen_ = false; position_valid_ = false;
        // Never reuse request IDs across connections on the same adapter.
    }
    void require_ready() const {
        if (state_ != ConnectionState::Ready) throw std::logic_error("TWS API is not ready");
    }
    RequestId allocate() {
        if (next_id_ > static_cast<RequestId>(std::numeric_limits<int>::max()))
            throw std::overflow_error("IBKR request IDs exhausted");
        return next_id_++;
    }
    void throttle(Clock::time_point now) {
        while (!requests_.empty() && now - requests_.front() >= std::chrono::seconds(1))
            requests_.pop_front();
        if (requests_.size() >= 10) throw std::length_error("Local request pacing limit: 10/second");
        requests_.push_back(now);
    }
    void emit(BrokerEvent event) {
        if (events_.size() >= 2048) { fail(-1006, "Broker event queue overflow; reconnect required"); return; }
        events_.push_back(std::move(event));
    }
};
} // namespace dts
