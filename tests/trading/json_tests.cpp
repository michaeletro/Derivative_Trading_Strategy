#define CROW_USE_BOOST 1
#include <boost/asio.hpp>
namespace asio = boost::asio;
#include "trading_json.hpp"
#include <iostream>

namespace {
void check(bool ok, const char* message) {
    if (!ok) throw std::runtime_error(message);
}
class FixtureBroker final : public dts::IBroker {
public:
    dts::ConnectionState current = dts::ConnectionState::Disconnected;
    std::vector<dts::BrokerEvent> events;
    void connect() override { current = dts::ConnectionState::Ready; }
    void disconnect() noexcept override { current = dts::ConnectionState::Disconnected; }
    dts::ConnectionState state() const noexcept override { return current; }
    dts::RequestId subscribe(const dts::Contract&) override { return 1; }
    bool unsubscribe(dts::RequestId) override { return true; }
    void request_positions(dts::RequestId) override {}
    dts::RequestId start_trading_monitor(const std::string&) override { return 43; }
    void stop_trading_monitor() override {}
    std::vector<dts::BrokerEvent> poll() override {
        std::vector<dts::BrokerEvent> out; out.swap(events); return out;
    }
};
crow::json::rvalue view(const dts::ReadOnlyService& service) {
    return crow::json::load(dts::trading_http::current(service, "ibkr_tws", "fixture-1", false).dump());
}
}
int main() {
    try {
        auto fixture = std::make_unique<FixtureBroker>(); auto* broker = fixture.get();
        dts::ReadOnlyService service(std::move(fixture));
        auto data = view(service);
        check(data["accounts_status"].s() == "unavailable", "Missing accounts must be unavailable");
        check(!data["order_execution_enabled"].b(), "A trading-status read must never enable orders");
        service.connect();
        broker->events.emplace_back(dts::ManagedAccountsEvent{true,{"SYNTHETIC-ACCOUNT"}});
        service.poll();
        check(view(service)["monitor"]["start_available"].b(), "Explicit account monitoring must be available after managed accounts");
        const auto id = service.start_trading_monitor("SYNTHETIC-ACCOUNT");
        const dts::TradingContract contract{756733,"SPY","STK","BATS","USD"};
        const auto receipt = dts::Clock::now() - std::chrono::seconds(4);
        const auto event = [&](dts::TradingSection section, dts::TradingMonitorPayload payload) {
            return dts::TradingMonitorEvent{id,section,std::move(payload),receipt,std::chrono::system_clock::now()};
        };
        broker->events.emplace_back(event(dts::TradingSection::Positions,
            dts::TradingPosition{contract,"1234567890123456.125",123.5}));
        broker->events.emplace_back(event(dts::TradingSection::AccountValues,
            dts::TradingAccountValue{"NetLiquidation","1234567890123456.123456","USD"}));
        dts::TradingOpenOrder order; order.contract=contract; order.order_id=-2; order.client_id=0;
        order.perm_id=9876543210123456LL; order.action="BUY"; order.order_type="LMT";
        order.quantity="0.125"; order.limit_price=123.45; order.status="Submitted";
        order.order_ref="SYNTHETIC-REF";
        broker->events.emplace_back(event(dts::TradingSection::OpenOrders,order));
        broker->events.emplace_back(event(dts::TradingSection::Executions,
            dts::TradingExecution{contract,"SYNTHETIC-EXEC","20261009 09:30:00 US/Eastern","BOT","0.0625",-2,0,order.perm_id,123.4}));
        service.poll();
        data=view(service);
        check(data["monitor"]["components"]["positions"].s() == "pending", "Rows without end markers must stay pending");
        check(data["monitor"]["positions"].size() == 0, "Partial positions must not escape through HTTP");
        broker->events.emplace_back(event(dts::TradingSection::Positions,dts::TradingSectionEnd{}));
        service.poll(); data=view(service);
        check(data["monitor"]["positions"][0]["quantity"].s() == "1234567890123456.125", "Quantity precision must survive HTTP");
        check(data["monitor"]["account_values"].size() == 0, "One component completion must not publish another");
        check(data["monitor"]["components_meta"]["positions"]["completed_age_seconds"].d() >= 4,
            "Age must use steady receipt time, not epoch seconds");
        for (auto section : {dts::TradingSection::AccountValues,dts::TradingSection::OpenOrders,dts::TradingSection::Executions})
            broker->events.emplace_back(event(section,dts::TradingSectionEnd{}));
        service.poll(); data=view(service);
        check(data["monitor"]["state"].s() == "active", "Initial read completion must activate only the monitor");
        check(!data["order_execution_enabled"].b(), "Completed reconciliation reads must not enable trading");
        check(data["monitor"]["open_orders"][0]["order_id"].s() == "-2", "Manual TWS order IDs can be negative");
        check(data["monitor"]["open_orders"][0]["perm_id"].s() == "9876543210123456", "Native identifiers must survive JavaScript precision boundaries");
        check(data["monitor"]["open_orders"][0]["filled"].t() == crow::json::type::Null,
            "Open-order acknowledgement is not a fill");
        check(data["monitor"]["executions"][0]["quantity"].s() == "0.0625", "Execution quantity must remain exact");
        check(data["monitor"]["account_values"][0]["value"].s() == "1234567890123456.123456", "Account values must remain raw strings");
        broker->events.emplace_back(event(dts::TradingSection::AccountValues,dts::TradingSectionEnd{false,999}));
        service.poll(); data=view(service);
        check(data["monitor"]["state"].s() == "failed" && data["monitor"]["positions"].size() == 0,
            "Failure must revoke current account facts");
        service.disconnect(); data=view(service);
        check(data["accounts"].size() == 0 && data["monitor"]["account"].t() == crow::json::type::Null,
            "Disconnect must clear account identity and rows");
        std::cout << "Trading HTTP schema, precision, component completion, receipt ages and invalidation passed (synthetic fixtures).\n";
        return 0;
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
