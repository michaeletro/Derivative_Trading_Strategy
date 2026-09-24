#pragma once

#include <chrono>
#include <cmath>
#include <cstdint>
#include <initializer_list>
#include <optional>
#include <stdexcept>
#include <string>

namespace dts {
using ContractId = std::int64_t;
using RequestId = std::uint64_t;
using Clock = std::chrono::steady_clock;

enum class SecurityType { Equity, Option };
enum class OptionRight { Call, Put };
enum class ExerciseStyle { Unknown, European, American };
enum class MarketDataType { Realtime, Delayed, Frozen, DelayedFrozen, Simulation };
enum class Side { Buy, Sell };

inline bool valid_expiry(const std::string& value) {
    if (value.size() != 8 || value.find_first_not_of("0123456789") != std::string::npos)
        return false;
    const int year = std::stoi(value.substr(0, 4));
    const int month = std::stoi(value.substr(4, 2));
    const int day = std::stoi(value.substr(6, 2));
    if (year < 1900 || month < 1 || month > 12) return false;
    const int days[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    const bool leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    return day >= 1 && day <= days[month - 1] + (month == 2 && leap ? 1 : 0);
}

struct OptionTerms {
    OptionRight right = OptionRight::Call;
    double strike = 0.0;
    std::string expiry; // YYYYMMDD; C++17, not C++20 chrono calendar types.
    ExerciseStyle exercise_style = ExerciseStyle::Unknown;
};

struct Contract {
    ContractId id = 0; // Positive, resolved vendor ID; not just a ticker.
    std::string symbol;
    std::string exchange;
    std::string currency;
    SecurityType security_type = SecurityType::Equity;
    double multiplier = 0.0; // Must be supplied explicitly from contract metadata.
    std::optional<OptionTerms> option;

    void validate() const {
        if (id <= 0 || symbol.empty() || exchange.empty() || currency.empty())
            throw std::invalid_argument("Contract identity is incomplete");
        if (!std::isfinite(multiplier) || multiplier <= 0.0)
            throw std::invalid_argument("Contract multiplier must be finite and positive");
        if (security_type == SecurityType::Option) {
            if (!option || !std::isfinite(option->strike) || option->strike <= 0.0 ||
                !valid_expiry(option->expiry) ||
                (option->right != OptionRight::Call && option->right != OptionRight::Put) ||
                (option->exercise_style != ExerciseStyle::Unknown &&
                 option->exercise_style != ExerciseStyle::European &&
                 option->exercise_style != ExerciseStyle::American))
                throw std::invalid_argument("Option terms are invalid");
        } else if (security_type != SecurityType::Equity || option) {
            throw std::invalid_argument("Unsupported or inconsistent security type");
        }
    }
};

struct QuoteSide {
    double price = 0.0;
    Clock::time_point received_at{};
};

struct Quote {
    ContractId contract_id = 0;
    std::optional<QuoteSide> bid;
    std::optional<QuoteSide> ask;
    MarketDataType data_type = MarketDataType::Simulation;
    std::optional<std::chrono::system_clock::time_point> exchange_time;

    // Receipt timestamps are per side: a fresh ask must not refresh an old bid.
    // A midpoint is an indicative mark, never an executable price or trade signal.
    std::optional<double> mid(Clock::time_point now, std::chrono::milliseconds max_age) const {
        if (contract_id <= 0 || !bid || !ask || max_age.count() < 0) return std::nullopt;
        if (!std::isfinite(bid->price) || !std::isfinite(ask->price) ||
            bid->price < 0.0 || ask->price <= 0.0 || bid->price > ask->price)
            return std::nullopt;
        for (const auto* side : {&*bid, &*ask}) {
            if (side->received_at > now || now - side->received_at > max_age)
                return std::nullopt;
        }
        return bid->price / 2.0 + ask->price / 2.0;
    }
};

struct Position {
    std::string account;
    Contract contract;
    double quantity = 0.0;

    double marked_value(double mark) const {
        contract.validate();
        if (account.empty() || !std::isfinite(quantity) || !std::isfinite(mark) || mark < 0.0)
            throw std::invalid_argument("Invalid position or mark");
        const double value = quantity * contract.multiplier * mark;
        if (!std::isfinite(value)) throw std::overflow_error("Position value overflow");
        return value; // Contract currency; no implicit FX conversion.
    }
};

// A domain object only. No implementation in this tranche transmits orders.
struct OrderIntent {
    std::string client_order_id;
    std::string account;
    Contract contract;
    Side side = Side::Buy;
    double quantity = 0.0;
    double limit_price = 0.0;

    void validate() const {
        contract.validate();
        if (client_order_id.empty() || account.empty() ||
            !std::isfinite(quantity) || quantity <= 0.0 ||
            !std::isfinite(limit_price) || limit_price <= 0.0 ||
            (side != Side::Buy && side != Side::Sell))
            throw std::invalid_argument("Invalid limit order intent");
    }
};
} // namespace dts
