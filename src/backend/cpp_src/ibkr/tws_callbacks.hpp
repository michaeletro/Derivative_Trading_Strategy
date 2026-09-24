#pragma once
#include <dts/tws_state.hpp>
#include "Contract.h"
#include "Decimal.h"
#include "DefaultEWrapper.h"
#include "bar.h"
#include "protobufUnix/ContractData.pb.h"
#include "protobufUnix/ContractDataEnd.pb.h"
#include "protobufUnix/NextValidId.pb.h"
#include "protobufUnix/TickPrice.pb.h"
#include "protobufUnix/MarketDataType.pb.h"
#include "protobufUnix/Position.pb.h"
#include "protobufUnix/PositionEnd.pb.h"
#include "protobufUnix/ErrorMessage.pb.h"
#include "protobufUnix/HistoricalData.pb.h"
#include "protobufUnix/HistoricalDataEnd.pb.h"
#include "protobufUnix/MarketDepth.pb.h"
#include "protobufUnix/MarketDepthL2.pb.h"
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
    // SMART is a routing preference, not an inferred listing exchange.
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
    out.currency = c.currency; return out;
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
// Callback mailbox only: no SQLite, HTTP, application state or pricing here.
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
    void error(int id, time_t, int code, const std::string& message, const std::string&) override { deliver_error(id, code, message); }
    void updateMktDepth(int id, int position, int operation, int side, double price, Decimal size) override {
        updateMktDepthL2(id, position, "", operation, side, price, size, false);
    }
    void updateMktDepthL2(int id, int position, const std::string& maker, int operation,
                         int side, double price, Decimal size, bool smart) override {
        DepthEvent e; e.received = DepthStamp::now(); e.request_id = static_cast<RequestId>(id);
        e.position = position; e.operation = operation; e.side = side; e.price = price;
        e.market_maker = maker; e.smart_depth = smart;
        try { e.size = operation == 2 ? "" : DecimalFunctions::decimalToString(size); deliver_depth(std::move(e)); }
        catch (const std::exception&) { deliver_error(static_cast<int>(id), -1030, "Invalid native depth payload"); }
    }
    void updateMarketDepthProtoBuf(const protobuf::MarketDepth& p) override { proto_depth(p.reqid(), p.marketdepthdata(), p.has_marketdepthdata()); }
    void updateMarketDepthL2ProtoBuf(const protobuf::MarketDepthL2& p) override { proto_depth(p.reqid(), p.marketdepthdata(), p.has_marketdepthdata()); }
    void historicalData(int id,const ::Bar& bar) override {
        try {
            HistoricalBar b;b.time=bar.time;b.open=bar.open;b.high=bar.high;b.low=bar.low;b.close=bar.close;
            const auto volume=DecimalFunctions::decimalToDouble(bar.volume);
            if(std::isfinite(volume)&&volume>=0&&volume<1e30)b.volume=DecimalFunctions::decimalToString(bar.volume);
            const auto wap=DecimalFunctions::decimalToDouble(bar.wap);
            if(std::isfinite(wap)&&wap>=0&&wap<1e12)b.wap=wap;
            if(bar.count>=0)b.count=bar.count;
            deliver_bar(static_cast<int>(id),std::move(b));
        }catch(const std::exception&){deliver_error(static_cast<int>(id),-1021,"Invalid historical bar fields");}
    }
    void historicalDataEnd(int id,const std::string& start,const std::string& end) override {
        post([id,start=start.substr(0,64),end=end.substr(0,64)](TwsState& s){s.historical_end(static_cast<RequestId>(id),start,end);});
    }
    // Both callback forms are implemented; DefaultEWrapper does not forward them.
    void historicalDataProtoBuf(const protobuf::HistoricalData& p) override {
        if(p.historicaldatabars_size()>1800){deliver_error(p.reqid(),-1020,"Historical response exceeds bar limit");return;}
        for(const auto& bar:p.historicaldatabars())try{
            if(!bar.has_date()||!bar.has_open()||!bar.has_high()||!bar.has_low()||!bar.has_close())
                throw std::invalid_argument("Missing historical OHLC fields");
            HistoricalBar b;b.time=bar.date();b.open=bar.open();b.high=bar.high();b.low=bar.low();b.close=bar.close();
            if(!bar.volume().empty()&&number(bar.volume())>=0&&number(bar.volume())<1e30)b.volume=bar.volume();
            if(!bar.wap().empty()&&number(bar.wap())>=0&&number(bar.wap())<1e12)b.wap=number(bar.wap());
            if(bar.has_barcount()&&bar.barcount()>=0)b.count=bar.barcount();
            deliver_bar(p.reqid(),std::move(b));
        }catch(const std::exception&){deliver_error(p.reqid(),-1021,"Invalid protobuf historical bar fields");}
    }
    void historicalDataEndProtoBuf(const protobuf::HistoricalDataEnd& p) override {historicalDataEnd(p.reqid(),p.startdatestr(),p.enddatestr());}
    void nextValidIdProtoBuf(const protobuf::NextValidId& p) override { nextValidId(p.orderid()); }
    void tickPriceProtoBuf(const protobuf::TickPrice& p) override { tickPrice(p.reqid(), static_cast<TickType>(p.ticktype()), p.price(), TickAttrib{}); }
    void marketDataTypeProtoBuf(const protobuf::MarketDataType& p) override { marketDataType(p.reqid(), p.marketdatatype()); }
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
    void deliver_depth(DepthEvent e) {
        if(e.size.size()>64 || e.market_maker.size()>64 || e.market_maker.find('\0')!=std::string::npos)
            throw std::invalid_argument("Depth payload exceeds bounds");
        post([e=std::move(e)](TwsState& s){ s.depth_update(e); });
    }
    void proto_depth(int id, const protobuf::MarketDepthData& p, bool present) {
        DepthEvent e; e.received=DepthStamp::now(); e.request_id=static_cast<RequestId>(id);
        e.position=p.has_position()?p.position():-1; e.operation=p.has_operation()?p.operation():-1;
        e.side=p.has_side()?p.side():-1; e.price=p.has_price()?p.price():std::numeric_limits<double>::quiet_NaN();
        e.size=p.size(); e.market_maker=p.marketmaker(); e.smart_depth=p.issmartdepth();
        try {
            if(!present)throw std::invalid_argument("Missing depth payload");
            deliver_depth(std::move(e));
        }catch(const std::exception&){deliver_error(id,-1030,"Invalid protobuf depth payload");}
    }
    void deliver_bar(int id,HistoricalBar b) {b.validate();post([id,b=std::move(b)](TwsState& s){s.historical_bar(static_cast<RequestId>(id),b);});}
    void deliver_contract(int id, dts::Contract c) { post([id, c = std::move(c)](TwsState& s) { s.contract(static_cast<RequestId>(id), c); }); }
    void deliver_position(Position p) { (void)p.marked_value(0.0);post([p = std::move(p)](TwsState& s) { s.position(p); }); }
    void bad_position(std::string reason) { post([reason = std::move(reason)](TwsState& s) { s.bad_position(reason); }); }
    void deliver_error(int id, int code, std::string message) {
        if (message.size() > 1024) message.resize(1024);
        const auto stamp = DepthStamp::now();
        post([id, code, stamp, message = std::move(message)](TwsState& s) {s.error(id > 0 ? static_cast<RequestId>(id) : 0, code, message, stamp);});
    }
};
} // namespace dts::ibkr_detail
