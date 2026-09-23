#pragma once
#include "domain.hpp"
#include <algorithm>
#include <array>
#include <limits>
#include <vector>

namespace dts {
// Direct USD equity depth only. A row can identify a market maker, not a unique
// price level or individual order. No exchange sequence/time is fabricated.
struct DepthSpec {
    Contract contract;
    std::string venue;
    int rows = 5;
    void validate() const {
        contract.validate();
        if (contract.security_type != SecurityType::Equity || contract.currency != "USD" ||
            venue.empty() || venue.size() > 32 || venue == "SMART" || rows < 1 || rows > 10 ||
            venue.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-") != std::string::npos)
            throw std::invalid_argument("Depth requires a resolved USD equity, explicit direct venue (not SMART), and 1..10 rows");
    }
};
struct DepthStamp {
    std::int64_t unix_us = 0, monotonic_ns = 0;
    static DepthStamp now() {
        return {std::chrono::duration_cast<std::chrono::microseconds>(
                    std::chrono::system_clock::now().time_since_epoch()).count(),
                std::chrono::duration_cast<std::chrono::nanoseconds>(
                    Clock::now().time_since_epoch()).count()};
    }
};
struct DepthEvent {
    RequestId request_id = 0;
    std::uint64_t sequence = 0; // Local per subscription; NOT exchange sequence.
    std::string kind = "update"; // start/update/reset/stop/gap/error
    DepthStamp received;
    int operation = -1, side = -1, position = -1; // IBKR: insert=0/update=1/delete=2; ask=0/bid=1
    double price = 0;
    std::string size, market_maker;
    bool smart_depth = false;
    int code = 0;
};
inline double depth_size(const std::string& value) {
    if (value.empty() || value.size() > 64 ||
        value.find_first_not_of("0123456789.eE+-") != std::string::npos)
        throw std::invalid_argument("Invalid depth size");
    std::size_t n = 0;
    const auto out = std::stod(value, &n);
    if (n != value.size() || !std::isfinite(out) || out < 0 || out >= 1e30)
        throw std::invalid_argument("Invalid depth size");
    return out; // Only for diagnostics; source decimal string is retained.
}
struct DepthLevel {
    double price = 0;
    std::string size, market_maker;
};
class DepthBook {
public:
    explicit DepthBook(int rows = 5) : rows_(rows) {
        if (rows < 1 || rows > 10) throw std::invalid_argument("Invalid requested depth rows");
    }
    void apply(const DepthEvent& e) {
        const bool contiguous = e.sequence == sequence_ + 1;
        if (e.sequence <= sequence_) { invalidate("nonincreasing_local_sequence"); return; }
        sequence_ = e.sequence; received_ = e.received;
        if (e.kind == "start" || e.kind == "reset") {
            clear(); valid_ = contiguous; active_ = true; ++epoch_;
            reason_ = contiguous ? "building" : "local_sequence_gap";
            return;
        }
        if (e.kind == "stop" || e.kind == "gap" || e.kind == "error") {
            clear(); active_ = false; valid_ = false; reason_ = e.kind;
            return;
        }
        if (!contiguous) { invalidate("local_sequence_gap"); return; }
        if (!active_ || !valid_) return;
        if (e.kind != "update" || e.smart_depth || e.side < 0 || e.side > 1 ||
            e.position < 0 || e.position >= rows_ || e.operation < 0 || e.operation > 2 ||
            e.market_maker.size() > 64 || e.market_maker.find('\0') != std::string::npos) {
            invalidate("invalid_depth_update"); return;
        }
        auto& levels = sides_[static_cast<std::size_t>(e.side)];
        const auto position = static_cast<std::size_t>(e.position);
        if (position > levels.size() || (e.operation != 0 && position == levels.size())) {
            invalidate("missing_row_position"); return;
        }
        if (e.operation == 2) { levels.erase(levels.begin() + e.position); return; }
        try { (void)depth_size(e.size); }
        catch (const std::exception&) { invalidate("invalid_depth_size"); return; }
        if (!std::isfinite(e.price) || e.price <= 0 || e.price >= 1e12) {
            invalidate("invalid_depth_price"); return;
        }
        DepthLevel level{e.price, e.size, e.market_maker};
        if (e.operation == 0) {
            levels.insert(levels.begin() + e.position, std::move(level));
            if (levels.size() > static_cast<std::size_t>(rows_)) levels.pop_back();
        } else levels[position] = std::move(level);
    }
    bool structural_valid() const noexcept { return valid_; }
    bool active() const noexcept { return active_; }
    std::uint64_t sequence() const noexcept { return sequence_; }
    std::uint64_t epoch() const noexcept { return epoch_; }
    DepthStamp received() const noexcept { return received_; }
    const std::vector<DepthLevel>& asks() const noexcept { return sides_[0]; }
    const std::vector<DepthLevel>& bids() const noexcept { return sides_[1]; }
    std::string quality() const {
        if (!valid_ || !active_) return reason_;
        if (asks().empty() || bids().empty()) return "one_sided_or_building";
        for (std::size_t i = 1; i < asks().size(); ++i)
            if (asks()[i-1].price > asks()[i].price) return "unordered_rows";
        for (std::size_t i = 1; i < bids().size(); ++i)
            if (bids()[i-1].price < bids()[i].price) return "unordered_rows";
        for (const auto& side : sides_)
            for (const auto& row : side) if (depth_size(row.size) == 0) return "zero_size_row";
        if (bids().front().price > asks().front().price) return "crossed";
        if (bids().front().price == asks().front().price) return "locked";
        return "two_sided_unverified"; // Never a complete-market or fill guarantee.
    }
private:
    int rows_;
    std::array<std::vector<DepthLevel>, 2> sides_;
    std::uint64_t sequence_ = 0, epoch_ = 0;
    DepthStamp received_;
    bool valid_ = false, active_ = false;
    std::string reason_ = "not_started";
    void clear() { for (auto& side : sides_) side.clear(); }
    void invalidate(const char* reason) { clear(); valid_ = false; reason_ = reason; }
};
} // namespace dts
