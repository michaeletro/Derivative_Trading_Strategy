#pragma once
#include "broker.hpp"
#include <deque>
#include <limits>
#include <map>
#include <memory>
#include <utility>

namespace dts {
struct ResolutionView {
    SnapshotStatus status = SnapshotStatus::Pending;
    std::vector<Contract> contracts;
};
struct DepthView {
    RequestId request_id;
    DepthSpec spec;
    DepthBook book;
    std::uint64_t update_event_count = 0, lifecycle_event_count = 0;
    std::optional<DepthStamp> last_update = std::nullopt;
};
struct PositionsView {
    SnapshotStatus status = SnapshotStatus::Unavailable;
    std::vector<Position> positions;
    std::optional<std::chrono::system_clock::time_point> completed_at;
};

// One application-owned service. Its caller serializes access, including reads.
// Never publish a partial position snapshot as a complete or zero-risk portfolio.
class ReadOnlyService {
public:
    explicit ReadOnlyService(std::unique_ptr<IBroker> broker) : broker_(std::move(broker)) {}
    ~ReadOnlyService() { disconnect(); }
    ReadOnlyService(const ReadOnlyService&) = delete;
    ReadOnlyService& operator=(const ReadOnlyService&) = delete;
    bool enabled() const noexcept { return bool(broker_); }
    ConnectionState state() const noexcept {
        return broker_ ? broker_->state() : ConnectionState::Disconnected;
    }
    void connect() {
        require_broker();
        if (state() == ConnectionState::Ready || state() == ConnectionState::Connecting) return;
        ++generation_;
        invalidate(); errors_.clear(); broker_->connect();
    }
    void disconnect() noexcept {
        if (state() != ConnectionState::Disconnected) ++generation_;
        if (broker_) broker_->disconnect();
        invalidate();
    }
    // Read-only views. The application holds its service mutex for the whole snapshot.
    std::uint64_t generation() const noexcept { return generation_; }
    const std::map<RequestId, ContractId>& subscriptions() const noexcept { return subscriptions_; }
    const Contract& contract(ContractId id) const { return contracts_.at(id); }
    RequestId resolve(const ContractQuery& query) {
        require_broker(); query.validate();
        if (resolutions_.size() >= 128) throw std::length_error("Resolution cache full; reconnect to clear");
        const auto id = broker_->resolve(query);
        resolutions_.emplace(id, ResolutionView{}); return id;
    }
    const ResolutionView& resolution(RequestId id) const {
        const auto it = resolutions_.find(id);
        if (it == resolutions_.end()) throw std::out_of_range("Unknown contract request");
        return it->second;
    }
    RequestId subscribe(ContractId id) {
        require_broker();
        if (state() != ConnectionState::Ready) throw std::logic_error("Broker is not ready");
        const auto it = contracts_.find(id);
        if (it == contracts_.end()) throw std::invalid_argument("Resolve the contract successfully before subscribing");
        // One subscription per conId across tabs; a repeated intent does not leak a line.
        for (const auto& entry : subscriptions_) if (entry.second == id) return entry.first;
        if (subscriptions_.size() >= 16) throw std::length_error("Subscription limit reached");
        const auto request = broker_->subscribe(it->second);
        subscriptions_.emplace(request, id); return request;
    }
    bool unsubscribe(RequestId id) {
        require_broker();
        const auto it = subscriptions_.find(id);
        if (it == subscriptions_.end()) return false;
        broker_->unsubscribe(id); quotes_.erase(it->second); subscriptions_.erase(it); return true;
    }
    const Quote& quote(ContractId id) const {
        const auto it = quotes_.find(id);
        if (it == quotes_.end()) throw std::out_of_range("No quote received for contract");
        return it->second;
    }
    RequestId request_history(const HistorySpec& spec, HistoryWindow window) {
        require_broker(); spec.validate(window); return broker_->request_history(spec, window);
    }
    void cancel_history(RequestId id) { require_broker(); broker_->cancel_history(id); }
    RequestId request_ticks(const TickSpec& spec,std::int64_t start) {
        require_broker();spec.validate();return broker_->request_ticks(spec,start);
    }
    void cancel_ticks(RequestId id) {require_broker();broker_->cancel_ticks(id);tick_pages_.clear();}
    std::vector<HistoricalTickPage> take_tick_pages() {
        std::vector<HistoricalTickPage> out;out.swap(tick_pages_);return out;
    }
    RequestId subscribe_depth(ContractId id, const std::string& venue, int rows) {
        require_broker();
        if (state() != ConnectionState::Ready) throw std::logic_error("Broker is not ready");
        if (depth_) throw std::logic_error("Stop the previous depth request first");
        DepthSpec spec{contract(id), venue, rows}; spec.validate();
        const auto request = broker_->subscribe_depth(spec);
        depth_.emplace(DepthView{request, spec, DepthBook(rows)}); return request;
    }
    bool unsubscribe_depth(RequestId id) {
        require_broker();
        if (!depth_ || depth_->request_id != id) return false;
        const bool cancelled = broker_->unsubscribe_depth(id);
        depth_.reset(); return cancelled;
    }
    const std::optional<DepthView>& depth() const noexcept { return depth_; }
    RequestId request_positions() {
        require_broker();
        if (positions_.status == SnapshotStatus::Pending) throw std::logic_error("Position snapshot already pending");
        if (next_position_id_ == std::numeric_limits<RequestId>::max())
            throw std::overflow_error("Position request IDs exhausted");
        const auto id = next_position_id_++;
        broker_->request_positions(id);
        position_id_ = id; staged_positions_.clear(); positions_ = {};
        positions_.status = SnapshotStatus::Pending; return id;
    }
    const PositionsView& positions() const noexcept { return positions_; }
    const TradingAccountsView& trading_accounts() const noexcept { return trading_.accounts(); }
    const TradingMonitorView& trading_monitor() const noexcept { return trading_.view(); }
    bool trading_monitor_start_available() const noexcept {
        return enabled() && state()==ConnectionState::Ready && !trading_monitor_seen_ &&
            trading_.accounts().status==SnapshotStatus::Complete && !trading_.accounts().accounts.empty();
    }
    bool trading_monitor_restart_requires_reconnect() const noexcept { return trading_monitor_seen_; }
    RequestId start_trading_monitor(const std::string& account) {
        require_broker();
        if(state()!=ConnectionState::Ready) throw std::logic_error("Broker is not ready");
        if(!trading_.allowed(account)) throw std::invalid_argument("Select a confirmed managed account");
        if(trading_monitor_seen_) throw std::logic_error("Reconnect before another account monitor");
        const auto monitor_state=trading_.view().state;
        if(monitor_state==TradingMonitorState::Pending || monitor_state==TradingMonitorState::Active)
            throw std::logic_error("Account monitor already active");
        const auto id=broker_->start_trading_monitor(account);
        trading_monitor_seen_=true;
        trading_.begin(id,account,generation_); return id;
    }
    void stop_trading_monitor() {
        require_broker(); broker_->stop_trading_monitor(); trading_.stop();
    }
    const std::deque<BrokerError>& errors() const noexcept { return errors_; }
    void poll() {
        if (!broker_) return;
        const auto events = broker_->poll();
        for (const auto& event : events) std::visit([this](const auto& value) { apply(value); }, event);
        if (state() == ConnectionState::Failed || state() == ConnectionState::Disconnected) invalidate();
    }
private:
    std::unique_ptr<IBroker> broker_;
    std::uint64_t generation_ = 0;
    std::map<RequestId, ResolutionView> resolutions_;
    std::map<ContractId, Contract> contracts_;
    std::map<RequestId, ContractId> subscriptions_;
    std::map<ContractId, Quote> quotes_;
    PositionsView positions_;
    std::optional<DepthView> depth_;
    std::map<std::pair<std::string, ContractId>, Position> staged_positions_;
    RequestId position_id_ = 0, next_position_id_ = RequestId{1} << 32;
    std::deque<BrokerError> errors_;
    std::vector<HistoricalTickPage> tick_pages_;
    TradingMonitorModel trading_;
    bool trading_monitor_seen_=false;
    void require_broker() const {
        if (!broker_) throw std::logic_error("Broker is disabled");
    }
    void invalidate() noexcept {
        contracts_.clear(); subscriptions_.clear(); quotes_.clear(); resolutions_.clear();
        positions_ = {}; staged_positions_.clear(); position_id_ = 0; depth_.reset();tick_pages_.clear();
        trading_.invalidate(generation_);
        trading_monitor_seen_=false;
    }
    void apply(const ManagedAccountsEvent& e) { trading_.accounts(e,generation_); }
    void apply(const TradingMonitorEvent& e) { trading_.apply(e); }
    void apply(const DepthEvent& e) {
        if (!depth_ || depth_->request_id != e.request_id) return;
        if (e.kind == "update") { ++depth_->update_event_count; depth_->last_update = e.received; }
        else ++depth_->lifecycle_event_count;
        depth_->book.apply(e);
    }
    void apply(const HistoricalBarEvent&) {} // Committed by the recording decorator.
    void apply(const HistoricalEnd&) {}
    void apply(const HistoricalTickPage& e) {
        if(tick_pages_.size()>=2)throw std::overflow_error("Historical tick delivery backlog");
        tick_pages_.push_back(e);
    }
    void apply(const ConnectionEvent& e) {
        if (e.state != ConnectionState::Ready && e.state != ConnectionState::Connecting) invalidate();
    }
    void apply(const BrokerError& e) {
        if (errors_.size() == 64) errors_.pop_front();
        errors_.push_back(e);
        if (position_id_ && e.request_id == position_id_) {
            positions_.status = SnapshotStatus::Failed; staged_positions_.clear();
        }
    }
    void apply(const Quote& e) {
        for (const auto& sub : subscriptions_)
            if (sub.second == e.contract_id) { quotes_[e.contract_id] = e; return; }
    }
    void apply(const ContractEvent& e) {
        auto it = resolutions_.find(e.request_id);
        if (it == resolutions_.end() || it->second.status != SnapshotStatus::Pending) return;
        if (it->second.contracts.size() >= 64) {
            it->second.status = SnapshotStatus::Failed; it->second.contracts.clear(); return;
        }
        it->second.contracts.push_back(e.contract);
    }
    void apply(const ContractsComplete& e) {
        auto it = resolutions_.find(e.request_id);
        if (it == resolutions_.end() || it->second.status != SnapshotStatus::Pending) return;
        auto& view = it->second;
        if (!e.success || view.contracts.empty() || contracts_.size() + view.contracts.size() > 512) {
            view.status = SnapshotStatus::Failed; view.contracts.clear(); return;
        }
        view.status = SnapshotStatus::Complete;
        for (const auto& c : view.contracts) contracts_[c.id] = c;
    }
    void apply(const PositionEvent& e) {
        if (e.request_id != position_id_ || positions_.status != SnapshotStatus::Pending) return;
        if (staged_positions_.size() >= 10000) {
            positions_.status = SnapshotStatus::Failed; staged_positions_.clear(); return;
        }
        staged_positions_[{e.position.account, e.position.contract.id}] = e.position;
    }
    void apply(const PositionsComplete& e) {
        if (!position_id_ || e.request_id != position_id_) return;
        if (!e.success || positions_.status == SnapshotStatus::Failed) {
            positions_.status = SnapshotStatus::Failed; positions_.positions.clear();
        } else {
            positions_.positions.clear();
            for (const auto& item : staged_positions_)
                if (item.second.quantity != 0.0) positions_.positions.push_back(item.second);
            positions_.status = SnapshotStatus::Complete;
            positions_.completed_at = std::chrono::system_clock::now();
        }
        staged_positions_.clear(); position_id_ = 0;
    }
};
} // namespace dts
