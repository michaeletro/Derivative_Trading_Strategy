#pragma once
#include <crow_all.h>
#include <dts/execution_rules.hpp>
#include <algorithm>
#include <cmath>
#include <set>

namespace dts::execution_rules_http {
using Json = crow::json::wvalue;
using Read = crow::json::rvalue;
inline std::vector<std::string> limitations() {
    return {
        "Provisional research limits, not a trading strategy or a profitability claim.",
        "All prices, holdings, cash, permissions and timing in this preview are supplied scenario inputs, not verified broker state.",
        "Paper/live labels declare planning purpose; they do not verify or select a brokerage account.",
        "USD whole-share stocks/ETFs only; no shorting, borrowing, options or fractional-share fallback.",
        "LMT DAY during regular trading hours only. Market-rule increments and instrument permissions still require broker verification.",
        "The preset spread tolerance also bounds aggressive buy limits above the ask and sell limits below the bid.",
        "Cash must be settled USD cash net of outstanding buy commitments; gross exposure includes holdings and outstanding buys.",
        "Daily loss is realized plus unrealized change from the session baseline, including fees; reaching the threshold blocks new buys, not guaranteed losses or automatic liquidation.",
        "The fee reserve is a hypothetical USD 2 per new order, not an IBKR commission estimate.",
        "No orders, durable intents or automatic execution are created by this calculator. Strategy, account binding, journal and continuous reconciliation remain unimplemented."
    };
}
inline Json policy_json(const execution_rules::Policy& p) {
    Json j; j["id"] = p.id; j["label"] = p.label;
    j["declared_balance_cents"] = p.declared_balance_cents;
    j["budget_cents"] = p.budget_cents;
    j["cash_reserve_cents"] = p.cash_reserve_cents;
    j["max_order_notional_cents"] = p.max_order_notional_cents;
    j["max_gross_exposure_cents"] = p.max_gross_exposure_cents;
    j["daily_loss_limit_cents"] = p.daily_loss_limit_cents;
    j["fee_reserve_cents"] = p.fee_reserve_cents;
    j["max_buy_decisions_per_day"] = p.max_buy_decisions_per_day;
    j["max_open_orders"] = p.max_open_orders;
    j["max_quote_age_ms"] = p.max_quote_age_ms;
    j["max_risk_age_ms"] = p.max_risk_age_ms;
    j["max_spread_bps"] = p.max_spread_bps;
    return j;
}
inline Json presets() {
    Json j; j["schema_version"] = 1; j["kind"] = "execution_rule_presets";
    j["execution_enabled"] = false; j["scenario_only"] = true;
    j["provisional"] = true; j["capital_source"] = "user_declared_not_broker_verified";
    std::vector<Json> profiles;
    for (const auto* id : {"live_500", "paper_500", "paper_1m"})
        profiles.push_back(policy_json(execution_rules::policy_for(id)));
    j["profiles"] = std::move(profiles); j["limitations"] = limitations(); return j;
}
inline void fields(const Read& j, const std::set<std::string>& required) {
    if (!j || j.t() != crow::json::type::Object || j.size() != required.size())
        throw std::invalid_argument("Rule preview requires the exact scenario fields");
    std::set<std::string> seen;
    for (const auto& key : j.keys())
        if (!required.count(key) || !seen.insert(key).second)
            throw std::invalid_argument("Unknown or duplicate rule-preview field");
    if (seen != required) throw std::invalid_argument("Missing rule-preview field");
}
inline std::int64_t integer(const Read& j, const char* key,
                          std::int64_t minimum = 0, std::int64_t maximum = 1000000000000LL) {
    const auto& value = j[key];
    if (value.t() != crow::json::type::Number ||
        (value.nt() != crow::json::num_type::Signed_integer && value.nt() != crow::json::num_type::Unsigned_integer))
        throw std::invalid_argument("Rule preview requires whole integer cents/counts");
    const double number = value.d();
    if (!std::isfinite(number) || number < minimum || number > maximum)
        throw std::invalid_argument("Rule-preview integer outside supported range");
    return static_cast<std::int64_t>(number);
}
inline bool boolean(const Read& j, const char* key) {
    if (j[key].t() != crow::json::type::True && j[key].t() != crow::json::type::False)
        throw std::invalid_argument("Rule preview requires explicit Boolean scenario flags");
    return j[key].b();
}
inline std::string text(const Read& j, const char* key, std::size_t maximum = 64) {
    if (j[key].t() != crow::json::type::String)
        throw std::invalid_argument("Rule preview requires string labels");
    const std::string value = j[key].s();
    if (value.empty() || value.size() > maximum ||
        std::any_of(value.begin(), value.end(), [](unsigned char c) { return c < 32 || c == 127; }))
        throw std::invalid_argument("Invalid rule-preview label");
    return value;
}
inline Json preview(const Read& j) {
    fields(j, {"profile_id", "side", "contract_id", "symbol", "security_type", "currency", "instrument_allowed",
        "bid_cents", "ask_cents", "limit_price_cents", "requested_shares", "min_tick_cents",
        "available_cash_cents", "committed_gross_exposure_cents", "available_long_shares", "day_pnl_cents",
        "buy_decisions_today", "open_orders", "quote_age_ms", "risk_age_ms", "realtime", "rth", "risk_state_known"});
    const auto id = text(j, "profile_id", 32);
    if (id != "live_500" && id != "paper_500" && id != "paper_1m")
        throw std::invalid_argument("Choose a supported provisional profile");
    const auto contract = text(j, "contract_id", 18);
    if (contract.front() == '0' || contract.find_first_not_of("0123456789") != std::string::npos)
        throw std::invalid_argument("Contract ID must be a positive decimal string");
    execution_rules::Scenario s;
    s.contract_id = std::stoll(contract);
    s.side = text(j, "side", 4); s.symbol = text(j, "symbol", 32);
    s.security_type = text(j, "security_type", 8); s.currency = text(j, "currency", 3);
    s.instrument_allowed = boolean(j, "instrument_allowed");
    s.bid_cents = integer(j, "bid_cents", 1); s.ask_cents = integer(j, "ask_cents", 1);
    s.limit_price_cents = integer(j, "limit_price_cents", 1);
    s.min_tick_cents = integer(j, "min_tick_cents", 1);
    s.requested_shares = integer(j, "requested_shares", 1, 100000000);
    s.available_cash_cents = integer(j, "available_cash_cents");
    s.committed_gross_exposure_cents = integer(j, "committed_gross_exposure_cents");
    s.available_long_shares = integer(j, "available_long_shares", 0, 100000000);
    s.day_pnl_cents = integer(j, "day_pnl_cents", -1000000000000LL);
    s.buy_decisions_today = integer(j, "buy_decisions_today", 0, 100000000);
    s.open_orders = integer(j, "open_orders", 0, 100000000);
    s.quote_age_ms = integer(j, "quote_age_ms"); s.risk_age_ms = integer(j, "risk_age_ms");
    s.realtime = boolean(j, "realtime"); s.rth = boolean(j, "rth");
    s.risk_state_known = boolean(j, "risk_state_known");
    const auto result = execution_rules::evaluate(execution_rules::policy_for(id), s);
    Json out; out["schema_version"] = 1; out["kind"] = "execution_rule_preview";
    out["profile_id"] = id; out["execution_enabled"] = false; out["scenario_only"] = true;
    out["decision"] = result.eligible ? "pass" : "blocked";
    out["quantity_shares"] = result.quantity_shares;
    out["order_notional_cents"] = result.order_notional_cents;
    out["estimated_total_cents"] = result.estimated_total_cents;
    out["blocking_reasons"] = result.blocking_reasons;
    out["limitations"] = limitations(); return out;
}
} // namespace dts::execution_rules_http
