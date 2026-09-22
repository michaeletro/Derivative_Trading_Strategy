#pragma once
#include <dts/tws_state.hpp>
#include "Contract.h"
#include "Decimal.h"
#include "DefaultEWrapper.h"
#include "protobufUnix/ContractData.pb.h"
#include "protobufUnix/ContractDataEnd.pb.h"
#include "protobufUnix/NextValidId.pb.h"
#include "protobufUnix/TickPrice.pb.h"
#include "protobufUnix/MarketDataType.pb.h"
#include "protobufUnix/Position.pb.h"
#include "protobufUnix/PositionEnd.pb.h"
#include "protobufUnix/ErrorMessage.pb.h"
#include <atomic>
#include <functional>
#include <mutex>

namespace dts::ibkr_detail {
inline double number(const std::string& text) {
    std::size_t used = 0;
    const double result = std::stod(text, &used);
    if (used != text.size() || !std::isfinite(result))
        throw std::invalid_argument("Invalid numeric field from IBKR");
    return result;
}
inline dts::Contract mapped_contract(ContractId id, const std::string& symbol,
    const std::string& type, const std::string& exchange, const std::string& currency,
    double multiplier, const std::string& expiry, double strike, const std::string& right) {
    dts::Contract out;
    out.id = id; out.symbol = symbol; out.currency = currency;
    // SMART is our explicit routing preference when position callbacks omit it;
    // it is not an inferred listing exchange or a claim of execution eligibility.
    out.exchange = exchange.empty() ? "SMART" : exchange;
    if (type == "STK") { out.security_type = SecurityType::Equity; out.multiplier = 1.0; }
    else if (type == "OPT") {
        out.security_type = SecurityType::Option; out.multiplier = multiplier;
        if (right != "C" && right != "P") throw std::invalid_argument("Unknown option right");
        out.option = OptionTerms{right == "C" ? OptionRight::Call : OptionRight::Put,
            strike, expiry.substr(0, 8), ExerciseStyle::Unknown};
    } else throw std::invalid_argument("Unsupported position/contract type: " + type);
    out.validate(); return out;
}
inline dts::Contract from_native(const ::Contract& c) {
    const double multiplier = c.secType == "STK" ? 1.0 : number(c.multiplier);
    return mapped_contract(c.conId, c.symbol, c.secType, c.exchange, c.currency,
        multiplier, c.lastTradeDateOrContractMonth, c.strike, c.right);
}
inline dts::Contract from_proto(const protobuf::Contract& c) {
    return mapped_contract(c.conid(), c.symbol(), c.sectype(), c.exchange(), c.currency(),
        c.multiplier(), c.lasttradedateorcontractmonth(), c.strike(), c.right());
}
inline ::Contract to_native(const dts::Contract& c) {
    c.validate();
    if (c.id > std::numeric_limits<int>::max()) throw std::invalid_argument("conId exceeds SDK range");
    ::Contract out; out.conId = static_cast<int>(c.id); out.exchange = c.exchange;
    out.currency = c.currency; return out; // Resolved identity, not ticker guessing.
}
inline ::Contract to_native(const ContractQuery& q) {
    q.validate();
    ::Contract out; out.symbol = q.symbol; out.exchange = q.exchange;
    out.currency = q.currency; out.primaryExchange = q.primary_exchange;
    out.secType = q.security_type == SecurityType::Equity ? "STK" : "OPT";
    if (q.option) {
        out.lastTradeDateOrContractMonth = q.option->expiry;
        out.strike = q.option->strike;
        out.right = q.option->right == OptionRight::Call ? "C" : "P";
    }
    return out;
}

// All SDK callbacks, including async connectAck, enqueue only. The owner drains
// this mailbox from poll(); no callback accesses application state or SQLite.
class Callbacks final : public DefaultEWrapper {
public:
    std::atomic<bool> acknowledged{false};
    void clear() {
        std::lock_guard<std::mutex> lock(mutex_);
        queue_.clear(); overflow_ = false; acknowledged.store(false);
    }
    void drain(TwsState& state) {
        std::vector<std::function<void(TwsState&)>> work;
        bool overflow;
        { std::lock_guard<std::mutex> lock(mutex_); work.swap(queue_); overflow = overflow_; overflow_ = false; }
        if (overflow) { state.fail(-1010, "SDK callback mailbox overflow"); return; }
        for (auto& action : work) {
            action(state);
            if (state.state() == ConnectionState::Failed) break;
        }
    }
    void connectAck() override { acknowledged.store(true); }
    void nextValidId(int) override { post([](TwsState& s) { s.ready(); }); }
    void connectionClosed() override { post([](TwsState& s) { s.fail(-1011, "TWS connection closed"); }); }
    void tickPrice(int id, TickType field, double price, const TickAttrib&) override {
        const auto now = Clock::now();
        post([=](TwsState& s) { s.price(static_cast<RequestId>(id), static_cast<int>(field), price, now); });
    }
    void marketDataType(int id, int type) override {
        post([=](TwsState& s) { s.data_type(static_cast<RequestId>(id), type); });
    }
    void contractDetails(int id, const ::ContractDetails& detail) override {
        try { deliver_contract(id, from_native(detail.contract)); }
        catch (const std::exception& e) { deliver_error(id, -1012, e.what()); }
    }
    void contractDetailsEnd(int id) override {
        post([=](TwsState& s) { s.contract_end(static_cast<RequestId>(id)); });
    }
    void position(const std::string& account, const ::Contract& c, Decimal quantity, double) override {
        try { deliver_position(Position{account, from_native(c), DecimalFunctions::decimalToDouble(quantity)}); }
        catch (const std::exception& e) { bad_position(e.what()); }
    }
    void positionEnd() override { post([](TwsState& s) { s.position_end(); }); }
    void error(int id, time_t, int code, const std::string& message, const std::string&) override {
        deliver_error(id, code, message);
    }
    // API 10.45 may deliver protobuf callbacks instead of legacy callbacks.
    // Handle BOTH explicitly; never assume DefaultEWrapper forwards them.
    void nextValidIdProtoBuf(const protobuf::NextValidId& p) override { nextValidId(p.orderid()); }
    void tickPriceProtoBuf(const protobuf::TickPrice& p) override {
        tickPrice(p.reqid(), static_cast<TickType>(p.ticktype()), p.price(), TickAttrib{});
    }
    void marketDataTypeProtoBuf(const protobuf::MarketDataType& p) override {
        marketDataType(p.reqid(), p.marketdatatype());
    }
    void contractDataProtoBuf(const protobuf::ContractData& p) override {
        try { deliver_contract(p.reqid(), from_proto(p.contract())); }
        catch (const std::exception& e) { deliver_error(p.reqid(), -1012, e.what()); }
    }
    void contractDataEndProtoBuf(const protobuf::ContractDataEnd& p) override { contractDetailsEnd(p.reqid()); }
    void positionProtoBuf(const protobuf::Position& p) override {
        try { deliver_position(Position{p.account(), from_proto(p.contract()), number(p.position())}); }
        catch (const std::exception& e) { bad_position(e.what()); }
    }
    void positionEndProtoBuf(const protobuf::PositionEnd&) override { positionEnd(); }
    void errorProtoBuf(const protobuf::ErrorMessage& p) override { deliver_error(p.id(), p.errorcode(), p.errormsg()); }
private:
    std::mutex mutex_;
    std::vector<std::function<void(TwsState&)>> queue_;
    bool overflow_ = false;
    void post(std::function<void(TwsState&)> action) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (queue_.size() >= 4096) { overflow_ = true; return; }
        queue_.push_back(std::move(action));
    }
    void deliver_contract(int id, dts::Contract c) {
        post([id, c = std::move(c)](TwsState& s) { s.contract(static_cast<RequestId>(id), c); });
    }
    void deliver_position(Position p) {
        (void)p.marked_value(0.0);
        post([p = std::move(p)](TwsState& s) { s.position(p); });
    }
    void bad_position(std::string reason) {
        post([reason = std::move(reason)](TwsState& s) { s.bad_position(reason); });
    }
    void deliver_error(int id, int code, std::string message) {
        // Advanced order-rejection JSON is deliberately ignored; no order support.
        if (message.size() > 1024) message.resize(1024);
        post([id, code, message = std::move(message)](TwsState& s) {
            s.error(id > 0 ? static_cast<RequestId>(id) : 0, code, message);
        });
    }
};
} // namespace dts::ibkr_detail
