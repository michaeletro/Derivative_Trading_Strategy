#include <dts/mock_broker.hpp>
#include <functional>
#include <iostream>
#include <limits>

namespace {
void check(bool result) { if (!result) throw std::runtime_error("check failed"); }
template<class Exception, class F> void throws(F action) {
    try { action(); } catch (const Exception&) { return; }
    throw std::runtime_error("expected exception was not thrown");
}
dts::Contract equity() {
    dts::Contract c;
    c.id = 1; c.symbol = "DEMO"; c.exchange = "SIM"; c.currency = "USD";
    c.multiplier = 1.0;
    return c;
}
dts::Quote quote(dts::Clock::time_point now) {
    dts::Quote q;
    q.contract_id = 1;
    q.bid = dts::QuoteSide{99.0, now}; q.ask = dts::QuoteSide{101.0, now};
    return q;
}
}

int main() {
    using namespace std::chrono;
    const auto now = dts::Clock::time_point(seconds(100));
    const auto age = milliseconds(1000);
    const double nan = std::numeric_limits<double>::quiet_NaN();
    const std::vector<std::pair<std::string, std::function<void()>>> tests = {
        {"resolved equity", [] { equity().validate(); }},
        {"missing contract ID", [] {
            auto c = equity(); c.id = 0;
            throws<std::invalid_argument>([&] { c.validate(); });
        }},
        {"invalid multiplier", [&] {
            auto c = equity(); c.multiplier = nan;
            throws<std::invalid_argument>([&] { c.validate(); });
        }},
        {"option expiry and exercise style", [] {
            auto c = equity(); c.security_type = dts::SecurityType::Option;
            c.option = dts::OptionTerms{dts::OptionRight::Call, 100.0, "20280229", dts::ExerciseStyle::American};
            c.multiplier = 100.0; c.validate();
            c.option->expiry = "20270229";
            throws<std::invalid_argument>([&] { c.validate(); });
        }},
        {"calendar edge cases", [] {
            check(dts::valid_expiry("20000229")); check(!dts::valid_expiry("19000229"));
            check(!dts::valid_expiry("20261301")); check(!dts::valid_expiry("20260431"));
            check(!dts::valid_expiry("2026-09-22")); check(!dts::valid_expiry("abcdefgh"));
        }},
        {"missing option terms", [] {
            auto c = equity(); c.security_type = dts::SecurityType::Option;
            throws<std::invalid_argument>([&] { c.validate(); });
        }},
        {"fresh quote midpoint", [&] { check(quote(now).mid(now, age) == 100.0); }},
        {"one-sided quote", [&] {
            auto q = quote(now); q.bid.reset(); check(!q.mid(now, age));
        }},
        {"crossed quote", [&] {
            auto q = quote(now); q.bid->price = 102.0; check(!q.mid(now, age));
        }},
        {"NaN quote", [&] {
            auto q = quote(now); q.ask->price = nan; check(!q.mid(now, age));
        }},
        {"stale bid cannot be refreshed by ask", [&] {
            auto q = quote(now); q.bid->received_at = now - seconds(2); check(!q.mid(now, age));
        }},
        {"future receipt and negative age", [&] {
            auto q = quote(now); q.ask->received_at = now + seconds(1); check(!q.mid(now, age));
            check(!quote(now).mid(now, milliseconds(-1)));
        }},
        {"zero bid supported, zero ask rejected", [&] {
            auto q = quote(now); q.bid->price = 0.0; check(q.mid(now, age) == 50.5);
            q.ask->price = 0.0; check(!q.mid(now, age));
        }},
        {"short position uses contract multiplier", [] {
            auto c = equity(); c.multiplier = 100.0;
            dts::Position position{"SIM_ACCOUNT", c, -2.0}; check(position.marked_value(3.0) == -600.0);
        }},
        {"nonfinite position rejected", [&] {
            dts::Position p{"SIM_ACCOUNT", equity(), nan};
            throws<std::invalid_argument>([&] { p.marked_value(10.0); });
        }},
        {"limit intent validation", [] {
            dts::OrderIntent o{"example-1", "SIM_ACCOUNT", equity(), dts::Side::Buy, 1.0, 100.0};
            o.validate(); o.quantity = 0.0;
            throws<std::invalid_argument>([&] { o.validate(); });
        }},
        {"disconnected broker rejects requests", [] {
            dts::MockBroker b; throws<std::logic_error>([&] { b.subscribe(equity()); });
            throws<std::logic_error>([&] { b.request_positions(1); });
        }},
        {"subscriptions, quotes, and simulation label", [&] {
            dts::MockBroker b; b.connect(); b.connect();
            check(b.poll().size() == 1); const auto id = b.subscribe(equity());
            auto q = quote(now); q.data_type = dts::MarketDataType::Realtime; b.publish(q);
            const auto events = b.poll(); check(events.size() == 1);
            check(std::get<dts::Quote>(events[0]).data_type == dts::MarketDataType::Simulation);
            check(b.poll().empty()); check(b.unsubscribe(id)); check(!b.unsubscribe(id));
            throws<std::invalid_argument>([&] { b.publish(q); });
        }},
        {"position snapshots have completion event", [] {
            dts::MockBroker b({dts::Position{"SIM_ACCOUNT", equity(), 2.0}});
            b.connect(); b.poll(); b.request_positions(42); const auto events = b.poll();
            check(events.size() == 2); check(std::get<dts::PositionEvent>(events[0]).request_id == 42);
            check(std::get<dts::PositionsComplete>(events[1]).request_id == 42);
        }},
        {"reconnect invalidates queued quotes and subscriptions", [&] {
            dts::MockBroker b; b.connect(); const auto first = b.subscribe(equity());
            b.publish(quote(now)); b.disconnect(); check(b.poll().empty()); b.connect();
            throws<std::invalid_argument>([&] { b.publish(quote(now)); });
            check(b.subscribe(equity()) > first);
        }},
        {"bounded event queue", [&] {
            dts::MockBroker b; b.connect(); b.poll(); b.subscribe(equity());
            for (int i = 0; i < 1024; ++i) b.publish(quote(now));
            throws<std::overflow_error>([&] { b.publish(quote(now)); });
            check(b.poll().size() == 1024); b.publish(quote(now)); check(b.poll().size() == 1);
        }}
    };
    std::size_t passed = 0;
    for (const auto& test : tests) {
        try { test.second(); ++passed; std::cout << "PASS " << test.first << '\n'; }
        catch (const std::exception& error) {
            std::cerr << "FAIL " << test.first << ": " << error.what() << '\n'; return 1;
        }
    }
    std::cout << passed << " cases passed\n";
}
