#pragma once

#include "broker.hpp"
#include <limits>
#include <map>
#include <utility>

namespace dts {
// Deterministic, single-threaded test double, NOT IBKR paper trading.
// No sockets, order transmission, synthetic fills, or claimed broker balances.
class MockBroker final : public IBroker {
public:
    explicit MockBroker(std::vector<Position> positions = {}) : positions_(std::move(positions)) {
        for (const auto& position : positions_) (void)position.marked_value(0.0);
    }

    void connect() override {
        if (state_ == ConnectionState::Ready) return;
        require_capacity(1);
        events_.emplace_back(ConnectionEvent{ConnectionState::Ready});
        state_ = ConnectionState::Ready;
    }

    void disconnect() noexcept override {
        subscriptions_.clear();
        events_.clear(); // Never deliver stale pre-disconnect quotes after reconnect.
        state_ = ConnectionState::Disconnected;
    }

    ConnectionState state() const noexcept override { return state_; }

    RequestId subscribe(const Contract& contract) override {
        require_ready();
        contract.validate();
        if (next_id_ == std::numeric_limits<RequestId>::max())
            throw std::overflow_error("Subscription ID exhausted");
        const auto id = next_id_++;
        subscriptions_.emplace(id, contract.id);
        return id;
    }

    bool unsubscribe(RequestId id) override { return subscriptions_.erase(id) != 0; }

    void request_positions(RequestId request_id) override {
        require_ready();
        if (request_id == 0) throw std::invalid_argument("Request ID must be nonzero");
        require_capacity(positions_.size() + 1);
        for (const auto& position : positions_)
            events_.emplace_back(PositionEvent{request_id, position});
        events_.emplace_back(PositionsComplete{request_id});
    }

    // Explicit fixture injection. Bad quote values remain visible to consumers,
    // which use Quote::mid to reject unusable marks rather than inventing values.
    void publish(Quote quote) {
        require_ready();
        bool subscribed = false;
        for (const auto& entry : subscriptions_)
            if (entry.second == quote.contract_id) subscribed = true;
        if (!subscribed) throw std::invalid_argument("No subscription for quote");
        require_capacity(1);
        quote.data_type = MarketDataType::Simulation;
        events_.emplace_back(std::move(quote));
    }

    std::vector<BrokerEvent> poll() override {
        std::vector<BrokerEvent> output;
        output.swap(events_);
        return output;
    }

private:
    static constexpr std::size_t capacity_ = 1024;
    ConnectionState state_ = ConnectionState::Disconnected;
    RequestId next_id_ = 1;
    std::map<RequestId, ContractId> subscriptions_;
    std::vector<Position> positions_;
    std::vector<BrokerEvent> events_;

    void require_ready() const {
        if (state_ != ConnectionState::Ready) throw std::logic_error("Broker is not ready");
    }
    void require_capacity(std::size_t count) const {
        if (count > capacity_ - events_.size())
            throw std::overflow_error("Mock event queue full; poll before publishing");
    }
};
} // namespace dts
