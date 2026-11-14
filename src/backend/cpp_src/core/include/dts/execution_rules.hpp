#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

namespace dts::execution_rules {

// Research scenarios only. This module has no broker, account, order ID, or
// transmission capability. Caller-supplied assertions are not verified facts.
using Integer = std::int64_t;
constexpr Integer max_money_cents = 1'000'000'000'000;
constexpr Integer max_quantity_shares = 100'000'000;

struct Policy {
    std::string id;
    std::string label;
    Integer declared_balance_cents = 0;
    Integer budget_cents = 0;
    Integer cash_reserve_cents = 0;
    Integer max_order_notional_cents = 0;
    Integer max_gross_exposure_cents = 0;
    Integer daily_loss_limit_cents = 0;
    Integer fee_reserve_cents = 0; // Hypothetical reserve, not a broker fee quote.
    Integer max_buy_decisions_per_day = 20;
    Integer max_open_orders = 1;
    Integer max_quote_age_ms = 2'000;
    Integer max_risk_age_ms = 5'000;
    Integer max_spread_bps = 10;

    static constexpr bool execution_enabled = false;
};

inline std::vector<std::string> validate_policy(const Policy& p) {
    std::vector<std::string> reasons;
    if (p.id.empty() || p.id.size() > 64 || p.label.empty() || p.label.size() > 160)
        reasons.emplace_back("invalid_policy_identity");
    const auto positive_money = [](Integer n) { return n > 0 && n <= max_money_cents; };
    const auto nonnegative_money = [](Integer n) { return n >= 0 && n <= max_money_cents; };
    if (!positive_money(p.declared_balance_cents) || !positive_money(p.budget_cents) ||
        p.budget_cents > p.declared_balance_cents ||
        !nonnegative_money(p.cash_reserve_cents) || p.cash_reserve_cents >= p.budget_cents ||
        !positive_money(p.max_order_notional_cents) ||
        !positive_money(p.max_gross_exposure_cents) ||
        p.max_order_notional_cents > p.max_gross_exposure_cents ||
        p.max_gross_exposure_cents > p.budget_cents - p.cash_reserve_cents ||
        !positive_money(p.daily_loss_limit_cents) || p.daily_loss_limit_cents > p.budget_cents ||
        !nonnegative_money(p.fee_reserve_cents) || p.fee_reserve_cents > p.max_order_notional_cents)
        reasons.emplace_back("invalid_policy_money_limits");
    if (p.max_buy_decisions_per_day <= 0 || p.max_buy_decisions_per_day > max_quantity_shares ||
        p.max_open_orders <= 0 || p.max_open_orders > max_quantity_shares ||
        p.max_quote_age_ms <= 0 || p.max_quote_age_ms > 60'000 ||
        p.max_risk_age_ms <= 0 || p.max_risk_age_ms > 60'000 ||
        p.max_spread_bps <= 0 || p.max_spread_bps > 1'000)
        reasons.emplace_back("invalid_policy_operating_limits");
    return reasons;
}

inline Policy policy_for(const std::string& id) {
    Policy p;
    p.id = id;
    p.fee_reserve_cents = 200;
    if (id == "live_500" || id == "paper_500") {
        p.label = id == "live_500" ? "USD 500 live-account planning (disabled)"
                                  : "USD 500 paper capital mirror (disabled)";
        p.declared_balance_cents = id == "paper_500" ? 100'000'000 : 50'000;
        p.budget_cents = 50'000;
        p.cash_reserve_cents = 5'000;
        p.max_order_notional_cents = 10'000;
        p.max_gross_exposure_cents = 45'000;
        p.daily_loss_limit_cents = 500;
    } else if (id == "paper_1m") {
        p.label = "USD 1M paper research (disabled)";
        p.declared_balance_cents = 100'000'000;
        p.budget_cents = 100'000'000;
        p.cash_reserve_cents = 90'000'000;
        p.max_order_notional_cents = 1'000'000;
        p.max_gross_exposure_cents = 10'000'000;
        p.daily_loss_limit_cents = 100'000;
    } else {
        throw std::invalid_argument("Unknown research rule profile");
    }
    return p;
}

struct Scenario {
    std::string side = "BUY";
    Integer contract_id = 0;
    std::string symbol;
    std::string security_type = "STK";
    std::string currency = "USD";
    bool instrument_allowed = false;
    Integer bid_cents = 0;
    Integer ask_cents = 0;
    Integer limit_price_cents = 0;
    Integer min_tick_cents = 0;
    Integer requested_shares = 0; // Ceiling; a BUY may be sized down.
    // Cash is net of settlement restrictions and all outstanding buy commitments.
    Integer available_cash_cents = 0;
    // Gross includes every holding and outstanding buy commitment, not just this symbol.
    Integer committed_gross_exposure_cents = 0;
    // Sell availability is net of outstanding sell commitments.
    Integer available_long_shares = 0;
    Integer day_pnl_cents = 0; // Account-wide realized plus marked unrealized P&L.
    Integer buy_decisions_today = 0;
    Integer open_orders = 0;
    Integer quote_age_ms = 0; // Older of the bid and ask receipt ages.
    Integer risk_age_ms = 0;
    bool realtime = false;
    bool rth = false;
    bool risk_state_known = false;
};

struct Evaluation {
    std::string profile_id;
    bool scenario_only = true;
    bool execution_enabled = false;
    bool eligible = false;
    Integer quantity_shares = 0;
    Integer order_notional_cents = 0;
    Integer estimated_total_cents = 0; // BUY cash debit; SELL gross proceeds before fees.
    Integer limit_price_cents = 0;
    std::vector<std::string> blocking_reasons;
};

// Strict numeric adapters for callers accepting floating-point form inputs.
// No rounding of fractional shares or subcent prices is performed.
inline std::optional<Integer> whole_shares_from_number(double value) {
    if (!std::isfinite(value) || value < 0.0 || value > static_cast<double>(max_quantity_shares) ||
        std::floor(value) != value)
        return std::nullopt;
    return static_cast<Integer>(value);
}

inline std::optional<Integer> cents_from_dollars(double value) {
    if (!std::isfinite(value) || value < 0.0 ||
        value > static_cast<double>(max_money_cents) / 100.0)
        return std::nullopt;
    const long double scaled = static_cast<long double>(value) * 100.0L;
    const long double rounded = std::round(scaled);
    // Accommodate only double representation error, not subcent input.
    const long double tolerance = std::max(1.0L, std::fabs(scaled)) *
                                  static_cast<long double>(2.2204460492503131e-16) * 4.0L;
    if (std::fabs(scaled - rounded) > tolerance || rounded > max_money_cents)
        return std::nullopt;
    return static_cast<Integer>(rounded);
}

inline Evaluation evaluate(const Policy& p, const Scenario& s) {
    Evaluation result;
    result.profile_id = p.id;
    result.blocking_reasons = validate_policy(p);
    if (!result.blocking_reasons.empty()) return result;
    auto block = [&](const char* reason) { result.blocking_reasons.emplace_back(reason); };
    const auto positive_money = [](Integer n) { return n > 0 && n <= max_money_cents; };
    const auto nonnegative_money = [](Integer n) { return n >= 0 && n <= max_money_cents; };
    const auto count = [](Integer n) { return n >= 0 && n <= max_quantity_shares; };
    if (s.side != "BUY" && s.side != "SELL") block("invalid_side");
    if (s.contract_id <= 0 || s.symbol.empty() || s.symbol.size() > 32 ||
        s.security_type != "STK" || s.currency != "USD" || !s.instrument_allowed)
        block("instrument_not_qualified");
    if (!positive_money(s.bid_cents) || !positive_money(s.ask_cents) ||
        s.bid_cents >= s.ask_cents)
        block("invalid_quote");
    if (!positive_money(s.limit_price_cents) || !positive_money(s.min_tick_cents) ||
        (s.min_tick_cents > 0 && s.limit_price_cents % s.min_tick_cents != 0))
        block("invalid_limit_tick");
    if (s.requested_shares <= 0 || s.requested_shares > max_quantity_shares)
        block("invalid_requested_quantity");
    if (!nonnegative_money(s.available_cash_cents) ||
        !nonnegative_money(s.committed_gross_exposure_cents) || !count(s.available_long_shares) ||
        s.day_pnl_cents < -max_money_cents || s.day_pnl_cents > max_money_cents ||
        !count(s.buy_decisions_today) || !count(s.open_orders))
        block("invalid_risk_inputs");
    if (!s.risk_state_known) block("risk_state_unknown");
    if (s.risk_age_ms < 0 || s.risk_age_ms > p.max_risk_age_ms) block("risk_state_stale");
    if (!s.realtime) block("quote_not_realtime");
    if (s.quote_age_ms < 0 || s.quote_age_ms > p.max_quote_age_ms) block("quote_stale");
    if (!s.rth) block("outside_regular_session");
    if (s.open_orders >= p.max_open_orders) block("open_order_limit");
    if (positive_money(s.bid_cents) && positive_money(s.ask_cents) && s.ask_cents >= s.bid_cents) {
        const long double spread = static_cast<long double>(s.ask_cents - s.bid_cents);
        const long double midpoint = static_cast<long double>(s.bid_cents) / 2.0L +
                                     static_cast<long double>(s.ask_cents) / 2.0L;
        if (spread * 10'000.0L > midpoint * static_cast<long double>(p.max_spread_bps))
            block("spread_limit");
        // Also bound an aggressively mispriced limit using the preset tolerance.
        // Resting BUYs below ask and SELLs above bid remain valid scenarios.
        if (positive_money(s.limit_price_cents)) {
            const long double limit = static_cast<long double>(s.limit_price_cents) * 10'000.0L;
            if ((s.side == "BUY" && limit > static_cast<long double>(s.ask_cents) *
                                            (10'000.0L + p.max_spread_bps)) ||
                (s.side == "SELL" && limit < static_cast<long double>(s.bid_cents) *
                                             (10'000.0L - p.max_spread_bps)))
                block("limit_price_outside_quote_collar");
        }
    }
    if (s.side == "BUY") {
        if (s.day_pnl_cents <= -p.daily_loss_limit_cents) block("daily_loss_limit");
        if (s.buy_decisions_today >= p.max_buy_decisions_per_day) block("daily_buy_limit");
    }
    if (!result.blocking_reasons.empty()) return result;

    Integer quantity = 0;
    if (s.side == "BUY") {
        // Subtract before adding/multiplying so large or inconsistent inputs cannot wrap.
        Integer cash_budget = s.available_cash_cents >= p.cash_reserve_cents
                                  ? s.available_cash_cents - p.cash_reserve_cents : 0;
        cash_budget = cash_budget >= p.fee_reserve_cents ? cash_budget - p.fee_reserve_cents : 0;
        const Integer gross_budget = s.committed_gross_exposure_cents < p.max_gross_exposure_cents
                                  ? p.max_gross_exposure_cents - s.committed_gross_exposure_cents : 0;
        const Integer notional_budget = std::min({p.max_order_notional_cents, gross_budget, cash_budget});
        quantity = std::min(s.requested_shares, notional_budget / s.limit_price_cents);
        if (quantity == 0) {
            if (s.limit_price_cents > p.max_order_notional_cents) block("whole_share_exceeds_order_cap");
            if (s.limit_price_cents > gross_budget) block("insufficient_gross_capacity");
            if (s.limit_price_cents > cash_budget) block("insufficient_reserved_cash");
            return result;
        }
    } else {
        // Loss/reserve/entry notional fences must not prevent a verified reduction.
        quantity = std::min(s.requested_shares, s.available_long_shares);
        if (quantity == 0) { block("no_available_long_shares"); return result; }
    }
    if (quantity > max_money_cents / s.limit_price_cents) {
        block("notional_overflow");
        return result;
    }
    const Integer notional = quantity * s.limit_price_cents;
    if (s.side == "BUY" && notional > max_money_cents - p.fee_reserve_cents) {
        block("total_overflow");
        return result;
    }
    result.eligible = true;
    result.quantity_shares = quantity;
    result.order_notional_cents = notional;
    result.estimated_total_cents = notional + (s.side == "BUY" ? p.fee_reserve_cents : 0);
    result.limit_price_cents = s.limit_price_cents;
    return result;
}

} // namespace dts::execution_rules
