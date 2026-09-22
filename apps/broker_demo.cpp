#include <dts/mock_broker.hpp>
#include <iostream>

int main() {
    try {
        std::cout << "SIMULATION ONLY - no connection to Interactive Brokers\n";
        dts::Contract contract;
        contract.id = 1; // Synthetic fixture ID, not an IBKR contract identifier.
        contract.symbol = "DEMO";
        contract.exchange = "SIM";
        contract.currency = "USD";
        contract.multiplier = 1.0;

        dts::MockBroker broker;
        broker.connect();
        const auto subscription = broker.subscribe(contract);
        const auto now = dts::Clock::now();
        dts::Quote quote;
        quote.contract_id = contract.id;
        quote.bid = dts::QuoteSide{99.0, now};
        quote.ask = dts::QuoteSide{101.0, now};
        broker.publish(quote);
        broker.request_positions(1);
        for (const auto& event : broker.poll()) {
            if (const auto* tick = std::get_if<dts::Quote>(&event)) {
                const auto mid = tick->mid(dts::Clock::now(), std::chrono::seconds(5));
                if (mid) std::cout << "DEMO indicative midpoint: " << *mid << '\n';
            }
        }
        broker.unsubscribe(subscription);
        broker.disconnect();
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
