#include <dts/read_only_service.hpp>
#include <iostream>
#define CHECK(x) do { if (!(x)) throw std::runtime_error(#x); } while (false)
namespace {
class FixtureBroker final : public dts::IBroker {
public:
    dts::ConnectionState connection = dts::ConnectionState::Disconnected;
    dts::RequestId next = 1;
    int subscription_calls = 0;
    std::vector<dts::BrokerEvent> events;
    void connect() override { connection = dts::ConnectionState::Ready; }
    void disconnect() noexcept override { connection = dts::ConnectionState::Disconnected; events.clear(); }
    dts::ConnectionState state() const noexcept override { return connection; }
    dts::RequestId resolve(const dts::ContractQuery& q) override {
        const auto id = next++;
        dts::Contract c; c.id = static_cast<dts::ContractId>(id); c.symbol = q.symbol;
        c.exchange = q.exchange; c.currency = q.currency; c.multiplier = 1;
        events.emplace_back(dts::ContractEvent{id,c}); events.emplace_back(dts::ContractsComplete{id,true});
        return id;
    }
    dts::RequestId subscribe(const dts::Contract&) override { ++subscription_calls; return next++; }
    bool unsubscribe(dts::RequestId) override { return true; }
    void request_positions(dts::RequestId) override {}
    std::vector<dts::BrokerEvent> poll() override { std::vector<dts::BrokerEvent> out; out.swap(events); return out; }
};
}
int main() {
    try {
        auto adapter = std::make_unique<FixtureBroker>(); auto* fixture = adapter.get();
        dts::ReadOnlyService service(std::move(adapter)); service.connect();
        dts::ContractQuery q; q.symbol = "DEMO";
        auto id = service.resolve(q); service.poll();
        CHECK(service.contract(static_cast<dts::ContractId>(id)).symbol == "DEMO");
        const auto subscription = service.subscribe(static_cast<dts::ContractId>(id));
        CHECK(service.subscribe(static_cast<dts::ContractId>(id)) == subscription);
        CHECK(fixture->subscription_calls == 1);
        CHECK(service.subscriptions().size() == 1);
        for (int i = 1; i < 16; ++i) { id = service.resolve(q); service.poll(); service.subscribe(static_cast<dts::ContractId>(id)); }
        id = service.resolve(q); service.poll();
        bool full = false;
        try { service.subscribe(static_cast<dts::ContractId>(id)); } catch (const std::length_error&) { full = true; }
        CHECK(full && fixture->subscription_calls == 16);
        CHECK(service.unsubscribe(subscription)); CHECK(service.subscriptions().size() == 15);
        service.disconnect(); CHECK(service.subscriptions().empty());
        bool absent = false;
        try { (void)service.contract(static_cast<dts::ContractId>(id)); } catch (const std::out_of_range&) { absent = true; }
        CHECK(absent);
        std::cout << "Dashboard service checks passed: idempotency, inventory, bounds, invalidation\n";
        return 0;
    } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
