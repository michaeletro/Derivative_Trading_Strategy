#include <dts/execution_rules.hpp>

#include <functional>
#include <iostream>
#include <limits>
#include <utility>

namespace {
using namespace dts::execution_rules;
void check(bool value) { if (!value) throw std::runtime_error("Scenario fixture check failed"); }
bool blocked(const Evaluation& result, const std::string& reason) {
    return !result.eligible && result.quantity_shares == 0 &&
           std::find(result.blocking_reasons.begin(), result.blocking_reasons.end(), reason) !=
               result.blocking_reasons.end();
}
Scenario fixture() {
    Scenario s;
    s.contract_id = 1;
    s.symbol = "FIXTURE";
    s.instrument_allowed = true;
    s.bid_cents = 4'998;
    s.ask_cents = 5'002;
    s.limit_price_cents = 5'002;
    s.min_tick_cents = 1;
    s.requested_shares = 10;
    s.available_cash_cents = 50'000;
    s.available_long_shares = 10;
    s.realtime = s.rth = s.risk_state_known = true;
    return s;
}
}

int main() {
    const auto live = policy_for("live_500");
    const std::vector<std::pair<std::string, std::function<void()>>> tests = {
        {"disabled provisional presets", [] {
            for (const auto& id : {"live_500", "paper_500", "paper_1m"}) {
                const auto p = policy_for(id);
                check(validate_policy(p).empty());
                check(!p.execution_enabled);
            }
            check(policy_for("paper_500").budget_cents == 50'000);
            check(policy_for("paper_500").declared_balance_cents == 100'000'000);
            check(policy_for("paper_1m").cash_reserve_cents == 90'000'000);
            try { policy_for("unknown"); } catch (const std::invalid_argument&) { return; }
            throw std::runtime_error("Unknown preset accepted");
        }},
        {"whole-share cap and hypothetical fee", [&] {
            const auto result = evaluate(live, fixture());
            check(result.eligible && result.quantity_shares == 1);
            check(result.order_notional_cents == 5'002 && result.estimated_total_cents == 5'202);
            check(result.scenario_only && !result.execution_enabled);
        }},
        {"SPY illustrative USD 700 fails small-capital whole-share rules", [&] {
            auto s = fixture(); s.symbol = "SPY";
            s.bid_cents = 69'998; s.ask_cents = s.limit_price_cents = 70'002;
            const auto result = evaluate(live, s);
            check(blocked(result, "whole_share_exceeds_order_cap"));
            check(blocked(result, "insufficient_gross_capacity"));
            check(blocked(result, "insufficient_reserved_cash"));
            check(!evaluate(policy_for("paper_500"), s).eligible);
        }},
        {"paper million still honors research exposure cap", [] {
            const auto p = policy_for("paper_1m"); auto s = fixture();
            s.available_cash_cents = 100'000'000; s.requested_shares = 100'000;
            auto result = evaluate(p, s); check(result.eligible && result.quantity_shares == 199);
            s.committed_gross_exposure_cents = 9'997'000;
            check(blocked(evaluate(p, s), "insufficient_gross_capacity"));
        }},
        {"net cash and all-symbol outstanding buy exposure are reserved", [&] {
            auto s = fixture(); s.available_cash_cents = 10'201;
            check(blocked(evaluate(live, s), "insufficient_reserved_cash"));
            s.available_cash_cents = 10'202;
            check(evaluate(live, s).quantity_shares == 1);
            s.committed_gross_exposure_cents = 39'999;
            check(blocked(evaluate(live, s), "insufficient_gross_capacity"));
            s.committed_gross_exposure_cents = 39'998;
            check(evaluate(live, s).quantity_shares == 1);
        }},
        {"exact gross cash fee and order boundary", [&] {
            auto s = fixture(); s.bid_cents = 9'999; s.ask_cents = s.limit_price_cents = 10'000;
            s.available_cash_cents = 15'200; s.committed_gross_exposure_cents = 35'000;
            check(evaluate(live, s).quantity_shares == 1);
            --s.available_cash_cents;
            check(blocked(evaluate(live, s), "insufficient_reserved_cash"));
        }},
        {"daily loss applies at limit and includes unrealized loss input", [&] {
            auto s = fixture(); s.day_pnl_cents = -499;
            check(evaluate(live, s).eligible);
            s.day_pnl_cents = -500; check(blocked(evaluate(live, s), "daily_loss_limit"));
            s.day_pnl_cents = -501; check(blocked(evaluate(live, s), "daily_loss_limit"));
        }},
        {"entry counts and open orders", [&] {
            auto s = fixture(); s.buy_decisions_today = 19;
            check(evaluate(live, s).eligible);
            s.buy_decisions_today = 20; check(blocked(evaluate(live, s), "daily_buy_limit"));
            s.buy_decisions_today = 0; s.open_orders = 1;
            check(blocked(evaluate(live, s), "open_order_limit"));
        }},
        {"unknown stale delayed and outside-hours states fail closed", [&] {
            auto s = fixture(); s.risk_state_known = false;
            check(blocked(evaluate(live, s), "risk_state_unknown"));
            s = fixture(); s.risk_age_ms = 5'000; check(evaluate(live, s).eligible);
            s.risk_age_ms = 5'001; check(blocked(evaluate(live, s), "risk_state_stale"));
            s = fixture(); s.quote_age_ms = 2'000; check(evaluate(live, s).eligible);
            s.quote_age_ms = 2'001; check(blocked(evaluate(live, s), "quote_stale"));
            s = fixture(); s.realtime = false;
            check(blocked(evaluate(live, s), "quote_not_realtime"));
            s = fixture(); s.rth = false;
            check(blocked(evaluate(live, s), "outside_regular_session"));
        }},
        {"spread midpoint basis-point boundary", [&] {
            auto s = fixture(); s.bid_cents = 9'995; s.ask_cents = 10'005;
            s.limit_price_cents = 10'000; check(evaluate(live, s).eligible);
            s.ask_cents = 10'006; check(blocked(evaluate(live, s), "spread_limit"));
            s.bid_cents = s.ask_cents = 10'000;
            check(blocked(evaluate(live, s), "invalid_quote"));
        }},
        {"resolved allowlist and limit tick required", [&] {
            auto s = fixture(); s.contract_id = 0;
            check(blocked(evaluate(live, s), "instrument_not_qualified"));
            s = fixture(); s.instrument_allowed = false;
            check(blocked(evaluate(live, s), "instrument_not_qualified"));
            s = fixture(); s.security_type = "OPT";
            check(blocked(evaluate(live, s), "instrument_not_qualified"));
            s = fixture(); s.currency = "EUR";
            check(blocked(evaluate(live, s), "instrument_not_qualified"));
            s = fixture(); s.min_tick_cents = 5;
            check(blocked(evaluate(live, s), "invalid_limit_tick"));
            s.limit_price_cents = 5'005; check(evaluate(live, s).eligible);
        }},
        {"aggressive limit collar and resting limits", [&] {
            auto s = fixture(); s.bid_cents = 4'999; s.ask_cents = 5'000;
            s.limit_price_cents = 5'005; check(evaluate(live, s).eligible);
            s.limit_price_cents = 5'006;
            check(blocked(evaluate(live, s), "limit_price_outside_quote_collar"));
            s.limit_price_cents = 4'000; check(evaluate(live, s).eligible);
            s.side = "SELL"; s.limit_price_cents = 4'995;
            check(evaluate(live, s).eligible);
            s.limit_price_cents = 4'994;
            check(blocked(evaluate(live, s), "limit_price_outside_quote_collar"));
            s.limit_price_cents = 7'000; check(evaluate(live, s).eligible);
        }},
        {"sell reduction limited to long shares net outstanding sells", [&] {
            auto s = fixture(); s.side = "SELL"; s.available_long_shares = 3;
            s.available_cash_cents = 0; s.committed_gross_exposure_cents = 100'000;
            s.day_pnl_cents = -20'000; s.buy_decisions_today = 99;
            const auto result = evaluate(live, s);
            check(result.eligible && result.quantity_shares == 3);
            check(result.order_notional_cents == 15'006 && result.estimated_total_cents == 15'006);
            s.available_long_shares = 0;
            check(blocked(evaluate(live, s), "no_available_long_shares"));
            s.available_long_shares = 3; s.open_orders = 1;
            check(blocked(evaluate(live, s), "open_order_limit"));
        }},
        {"malformed and oversized values never size", [&] {
            auto s = fixture(); s.bid_cents = 0;
            check(blocked(evaluate(live, s), "invalid_quote"));
            s = fixture(); s.bid_cents = s.ask_cents + 1;
            check(blocked(evaluate(live, s), "invalid_quote"));
            s = fixture(); s.available_cash_cents = std::numeric_limits<Integer>::max();
            check(blocked(evaluate(live, s), "invalid_risk_inputs"));
            s = fixture(); s.day_pnl_cents = std::numeric_limits<Integer>::min();
            check(blocked(evaluate(live, s), "invalid_risk_inputs"));
            s = fixture(); s.requested_shares = max_quantity_shares + 1;
            check(blocked(evaluate(live, s), "invalid_requested_quantity"));
            s = fixture(); s.quote_age_ms = -1;
            check(blocked(evaluate(live, s), "quote_stale"));
            s = fixture(); s.risk_age_ms = -1;
            check(blocked(evaluate(live, s), "risk_state_stale"));
        }},
        {"large sale notional rejected before overflow", [&] {
            auto s = fixture(); s.side = "SELL";
            s.bid_cents = max_money_cents - 1; s.ask_cents = s.limit_price_cents = max_money_cents;
            s.requested_shares = s.available_long_shares = max_quantity_shares;
            check(blocked(evaluate(live, s), "notional_overflow"));
        }},
        {"invalid custom policy is rejected", [&] {
            auto p = live; p.max_gross_exposure_cents = 45'001;
            check(blocked(evaluate(p, fixture()), "invalid_policy_money_limits"));
            p = live; p.budget_cents = 0;
            check(!validate_policy(p).empty());
            p = live; p.cash_reserve_cents = std::numeric_limits<Integer>::max();
            check(!validate_policy(p).empty());
            p = live; p.max_risk_age_ms = 0;
            check(blocked(evaluate(p, fixture()), "invalid_policy_operating_limits"));
        }},
        {"numeric adapters reject fractional and nonfinite inputs", [] {
            check(cents_from_dollars(50.02) == 5'002);
            check(cents_from_dollars(0.01) == 1);
            check(!cents_from_dollars(1.001));
            check(!cents_from_dollars(std::numeric_limits<double>::quiet_NaN()));
            check(!cents_from_dollars(std::numeric_limits<double>::infinity()));
            check(!cents_from_dollars(10'000'000'001.0));
            check(!cents_from_dollars(-1.0));
            check(whole_shares_from_number(1.0) == 1);
            check(!whole_shares_from_number(0.5));
            check(!whole_shares_from_number(std::numeric_limits<double>::quiet_NaN()));
            check(!whole_shares_from_number(std::numeric_limits<double>::infinity()));
            check(!whole_shares_from_number(100'000'001.0));
        }},
        {"sizing invariants across synthetic budgets and prices", [&] {
            for (Integer price : {1, 100, 1'000, 9'999, 10'000, 10'001, 50'000}) {
                for (Integer cash : {0, 5'000, 5'200, 6'200, 15'200, 50'000, 1'000'000}) {
                    for (Integer gross : {0, 35'000, 44'999, 45'000, 45'001}) {
                        auto s = fixture();
                        s.bid_cents = s.limit_price_cents = price;
                        s.ask_cents = price + 1;
                        s.available_cash_cents = cash;
                        s.committed_gross_exposure_cents = gross;
                        const auto result = evaluate(live, s);
                        check(!result.execution_enabled && result.scenario_only);
                        if (result.eligible) {
                            check(result.quantity_shares > 0 && result.quantity_shares <= s.requested_shares);
                            check(result.order_notional_cents <= live.max_order_notional_cents);
                            check(result.order_notional_cents + gross <= live.max_gross_exposure_cents);
                            check(result.estimated_total_cents + live.cash_reserve_cents <= cash);
                            check(result.blocking_reasons.empty());
                        } else {
                            check(result.quantity_shares == 0 && result.order_notional_cents == 0 &&
                                  result.estimated_total_cents == 0 && !result.blocking_reasons.empty());
                        }
                    }
                }
            }
        }},
    };
    int failures = 0;
    for (const auto& test : tests) {
        try { test.second(); std::cout << "PASS synthetic fixture: " << test.first << '\n'; }
        catch (const std::exception& error) {
            ++failures;
            std::cerr << "FAIL " << test.first << ": " << error.what() << '\n';
        }
    }
    return failures == 0 ? 0 : 1;
}
