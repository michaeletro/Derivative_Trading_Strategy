#include <dts/read_only_service.hpp>
#include <iostream>
#include <stdexcept>
#define CHECK(x) do { if (!(x)) throw std::runtime_error(#x); } while(false)
class FixtureBroker final : public dts::IBroker {
    dts::ConnectionState state_ = dts::ConnectionState::Disconnected;
    std::vector<dts::BrokerEvent> events_;
    dts::RequestId next_ = 1;
public:
    int subscription_calls = 0;
    void connect() override { state_ = dts::ConnectionState::Ready; }
    void disconnect() noexcept override { state_ = dts::ConnectionState::Disconnected; events_.clear(); }
    dts::ConnectionState state() const noexcept override { return state_; }
    dts::RequestId resolve(const dts::ContractQuery&) override {
        const auto request = next_++;
        dts::Contract c; c.id = 42; c.symbol = "DEMO"; c.exchange = "SIM"; c.currency = "USD"; c.multiplier = 1;
        events_.push_back(dts::ContractEvent{request,c}); events_.push_back(dts::ContractsComplete{request,true}); return request;
    }
    dts::RequestId subscribe(const dts::Contract&) override { ++subscription_calls; return next_++; }
    bool unsubscribe(dts::RequestId) override { return true; }
    void request_positions(dts::RequestId request) override { events_.push_back(dts::PositionsComplete{request,true}); }
    std::vector<dts::BrokerEvent> poll() override { std::vector<dts::BrokerEvent> out; out.swap(events_); return out; }
};
int main() {
    try {
        auto fixture = std::make_unique<FixtureBroker>(); auto* transport = fixture.get();
        dts::ReadOnlyService service(std::move(fixture));
        CHECK(service.generation() == 0 && service.subscriptions().empty());
        service.connect(); const auto generation = service.generation(); CHECK(generation == 1);
        service.connect(); CHECK(service.generation() == generation);
        dts::ContractQuery query; query.symbol = "DEMO";
        const auto request = service.resolve(query); service.poll();
        CHECK(service.resolution(request).status == dts::SnapshotStatus::Complete);
        const auto subscription = service.subscribe(42);
        CHECK(service.subscribe(42) == subscription && transport->subscription_calls == 1);
        CHECK(service.subscriptions().size() == 1 && service.subscriptions().at(subscription) == 42);
        CHECK(service.contract(42).symbol == "DEMO");
        CHECK(service.positions().status == dts::SnapshotStatus::Unavailable);
        service.request_positions(); service.poll();
        CHECK(service.positions().status == dts::SnapshotStatus::Complete && service.positions().positions.empty());
        CHECK(service.unsubscribe(subscription) && service.subscriptions().empty());
        service.disconnect(); CHECK(service.generation() != generation);
        CHECK(service.positions().status == dts::SnapshotStatus::Unavailable);
        service.connect(); CHECK(service.generation() > generation);
        CHECK(service.subscriptions().empty());
        std::cout << "Dashboard service views, duplicate intent, generation and invalidation checks passed\n";
        return 0;
    } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
