#pragma once
#include <dts/tws_state.hpp>
#include "Contract.h"
#include "Decimal.h"
#include "DefaultEWrapper.h"
#include "bar.h"
#include "Order.h"
#include "OrderState.h"
#include "Execution.h"
#include "protobufUnix/ManagedAccounts.pb.h"
#include "protobufUnix/PositionMulti.pb.h"
#include "protobufUnix/PositionMultiEnd.pb.h"
#include "protobufUnix/AccountSummary.pb.h"
#include "protobufUnix/AccountSummaryEnd.pb.h"
#include "protobufUnix/OpenOrder.pb.h"
#include "protobufUnix/OpenOrdersEnd.pb.h"
#include "protobufUnix/ExecutionDetails.pb.h"
#include "protobufUnix/ExecutionDetailsEnd.pb.h"
#include "HistoricalTickLast.h"
#include "HistoricalTickBidAsk.h"
#include "protobufUnix/HistoricalTicksLast.pb.h"
#include "protobufUnix/HistoricalTicksBidAsk.pb.h"
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
inline TradingContract monitor_contract(const ::Contract& c) {
    return TradingContract{c.conId,c.symbol,c.secType,c.exchange,c.currency};
}
inline TradingContract monitor_contract(const protobuf::Contract& c) {
    return TradingContract{c.conid(),c.symbol(),c.sectype(),c.exchange(),c.currency()};
}
inline std::optional<double> monitor_number(double value) {
    return std::isfinite(value) && std::abs(value)<1e100 ? std::optional<double>{value}:std::nullopt;
}
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
    void configure_monitor(RequestId token,const std::string& account) { monitor_token_.store(token); monitor_account_=account; }
    void clear() {
        std::lock_guard<std::mutex> lock(mutex_);
        queue_.clear(); overflow_ = false; depth_protobuf_ = false; error_protobuf_mirror_pending_ = false; acknowledged.store(false);
        monitor_token_.store(0); monitor_account_.clear(); monitor_protobuf_.fill(false);
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
    void managedAccounts(const std::string& accounts) override {
        if(!consume_monitor_mirror(0)) deliver_accounts(accounts);
    }
    void managedAccountsProtoBuf(const protobuf::ManagedAccounts& p) override {
        monitor_protobuf_[0]=true;
        if(!p.has_accountslist()) { post([](TwsState& s){s.managed_accounts(ManagedAccountsEvent{});}); return; }
        deliver_accounts(p.accountslist());
    }
    void positionMulti(int id,const std::string& account,const std::string& model,const ::Contract& c,Decimal quantity,double average) override {
        if(consume_monitor_mirror(1)) return;
        if(account.empty()) { bad_monitor(TradingSection::Positions,id); return; }
        if(!monitor_matches(account)) return;
        TradingPosition row{monitor_contract(c),DecimalFunctions::decimalToString(quantity),monitor_number(average),model};
        post_monitor_position(id,account,std::move(row));
    }
    void positionMultiProtoBuf(const protobuf::PositionMulti& p) override {
        monitor_protobuf_[1]=true;
        if(!p.has_account() || p.account().empty()) { bad_monitor(TradingSection::Positions,p.reqid()); return; }
        if(!monitor_matches(p.account())) return;
        if(!p.has_contract() || !p.has_position()) { bad_monitor(TradingSection::Positions,p.reqid()); return; }
        TradingPosition row{monitor_contract(p.contract()),p.position(),p.has_avgcost()?monitor_number(p.avgcost()):std::nullopt,p.modelcode()};
        post_monitor_position(p.reqid(),p.account(),std::move(row));
    }
    void positionMultiEnd(int id) override { if(!consume_monitor_mirror(2)) end_monitor(TradingSection::Positions,id); }
    void positionMultiEndProtoBuf(const protobuf::PositionMultiEnd& p) override {
        monitor_protobuf_[2]=true; end_monitor(TradingSection::Positions,p.reqid());
    }
    void accountSummary(int id,const std::string& account,const std::string& tag,const std::string& value,const std::string& currency) override {
        if(consume_monitor_mirror(3)) return;
        if(account.empty()) { bad_monitor(TradingSection::AccountValues,id); return; }
        if(!monitor_matches(account)) return;
        post_monitor_value(id,account,TradingAccountValue{tag,value,currency});
    }
    void accountSummaryProtoBuf(const protobuf::AccountSummary& p) override {
        monitor_protobuf_[3]=true;
        if(!p.has_account() || p.account().empty()) { bad_monitor(TradingSection::AccountValues,p.reqid()); return; }
        if(monitor_matches(p.account())) post_monitor_value(p.reqid(),p.account(),TradingAccountValue{p.tag(),p.value(),p.currency()});
    }
    void accountSummaryEnd(int id) override { if(!consume_monitor_mirror(4)) end_monitor(TradingSection::AccountValues,id); }
    void accountSummaryEndProtoBuf(const protobuf::AccountSummaryEnd& p) override {
        monitor_protobuf_[4]=true; end_monitor(TradingSection::AccountValues,p.reqid());
    }
    void openOrder(int id,const ::Contract& c,const ::Order& order,const ::OrderState& state) override {
        if(consume_monitor_mirror(5)) return;
        if(order.account.empty()) { bad_monitor(TradingSection::OpenOrders,static_cast<int>(monitor_token_.load())); return; }
        if(!monitor_matches(order.account)) return;
        TradingOpenOrder row;
        row.contract=monitor_contract(c); row.order_id=id; row.client_id=order.clientId; row.perm_id=order.permId;
        row.action=order.action; row.order_type=order.orderType; row.quantity=DecimalFunctions::decimalToString(order.totalQuantity);
        row.status=state.status; row.order_ref=order.orderRef; row.limit_price=monitor_number(order.lmtPrice);
        post_monitor_order(order.account,std::move(row));
    }
    void openOrderProtoBuf(const protobuf::OpenOrder& p) override {
        monitor_protobuf_[5]=true;
        if(!p.has_order()) { bad_monitor(TradingSection::OpenOrders,static_cast<int>(monitor_token_.load())); return; }
        const auto& order=p.order();
        if(!order.has_account() || order.account().empty()) { bad_monitor(TradingSection::OpenOrders,static_cast<int>(monitor_token_.load())); return; }
        if(!monitor_matches(order.account())) return;
        if(!p.has_orderid() || !p.has_contract() || !order.has_clientid() || !order.has_totalquantity() || !order.has_action() || !order.has_ordertype()) {
            bad_monitor(TradingSection::OpenOrders,static_cast<int>(monitor_token_.load())); return;
        }
        TradingOpenOrder row;
        row.contract=monitor_contract(p.contract()); row.order_id=p.orderid(); row.client_id=order.clientid(); row.perm_id=order.permid();
        row.action=order.action(); row.order_type=order.ordertype(); row.quantity=order.totalquantity(); row.order_ref=order.orderref();
        if(p.has_orderstate()) row.status=p.orderstate().status();
        if(order.has_lmtprice()) row.limit_price=monitor_number(order.lmtprice());
        post_monitor_order(order.account(),std::move(row));
    }
    void openOrderEnd() override {
        if(!consume_monitor_mirror(6)) end_monitor(TradingSection::OpenOrders,static_cast<int>(monitor_token_.load()));
    }
    void openOrdersEndProtoBuf(const protobuf::OpenOrdersEnd&) override {
        monitor_protobuf_[6]=true; end_monitor(TradingSection::OpenOrders,static_cast<int>(monitor_token_.load()));
    }
    void execDetails(int id,const ::Contract& c,const ::Execution& execution) override {
        if(consume_monitor_mirror(7)) return;
        if(execution.acctNumber.empty()) { bad_monitor(TradingSection::Executions,id); return; }
        if(!monitor_matches(execution.acctNumber)) return;
        TradingExecution row;
        row.contract=monitor_contract(c); row.exec_id=execution.execId; row.time=execution.time; row.side=execution.side;
        row.quantity=DecimalFunctions::decimalToString(execution.shares); row.order_id=execution.orderId;
        row.client_id=execution.clientId; row.perm_id=execution.permId; row.price=monitor_number(execution.price);
        post_monitor_execution(id,execution.acctNumber,std::move(row));
    }
    void execDetailsProtoBuf(const protobuf::ExecutionDetails& p) override {
        monitor_protobuf_[7]=true;
        if(!p.has_execution()) { bad_monitor(TradingSection::Executions,p.reqid()); return; }
        const auto& execution=p.execution();
        if(!execution.has_acctnumber() || execution.acctnumber().empty()) { bad_monitor(TradingSection::Executions,p.reqid()); return; }
        if(!monitor_matches(execution.acctnumber())) return;
        if(!p.has_contract() || !execution.has_orderid() || !execution.has_clientid() || !execution.has_shares()) { bad_monitor(TradingSection::Executions,p.reqid()); return; }
        TradingExecution row;
        row.contract=monitor_contract(p.contract()); row.exec_id=execution.execid(); row.time=execution.time(); row.side=execution.side();
        row.quantity=execution.shares(); row.order_id=execution.orderid(); row.client_id=execution.clientid(); row.perm_id=execution.permid();
        if(execution.has_price()) row.price=monitor_number(execution.price());
        post_monitor_execution(p.reqid(),execution.acctnumber(),std::move(row));
    }
    void execDetailsEnd(int id) override { if(!consume_monitor_mirror(8)) end_monitor(TradingSection::Executions,id); }
    void execDetailsEndProtoBuf(const protobuf::ExecutionDetailsEnd& p) override {
        monitor_protobuf_[8]=true; end_monitor(TradingSection::Executions,p.reqid());
    }
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
        // Only consume the immediate mirror. SDK-local errors still use this
        // legacy callback on protobuf connections and must remain visible.
        if (error_protobuf_mirror_pending_) { error_protobuf_mirror_pending_=false; return; }
        deliver_error(id, code, message);
    }
    void updateMktDepth(int id, int position, int operation, int side, double price, Decimal size) override {
        native_depth(id, position, "", operation, side, price, size, false, "legacy_depth");
    }
    void updateMktDepthL2(int id, int position, const std::string& maker, int operation,
                         int side, double price, Decimal size, bool smart) override {
        native_depth(id, position, maker, operation, side, price, size, smart, "legacy_depth_l2");
    }
    void updateMarketDepthProtoBuf(const protobuf::MarketDepth& p) override { proto_depth(p, "protobuf_depth"); }
    void updateMarketDepthL2ProtoBuf(const protobuf::MarketDepthL2& p) override { proto_depth(p, "protobuf_depth_l2"); }
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
    void historicalTicksLast(int id,const std::vector<::HistoricalTickLast>& ticks,bool done) override {
        try {
            if(ticks.size()>max_tick_page)throw std::length_error("Tick page too large");
            std::vector<dts::HistoricalTick> rows;rows.reserve(ticks.size());
            for(const auto& p:ticks){dts::HistoricalTick r;r.time=p.time;r.price=p.price;r.size=DecimalFunctions::decimalToString(p.size);
                r.exchange=p.exchange;r.conditions=p.specialConditions;r.past_limit=p.tickAttribLast.pastLimit;r.unreported=p.tickAttribLast.unreported;rows.push_back(std::move(r));}
            deliver_ticks(id,"TRADES",std::move(rows),done);
        }catch(const std::exception&){deliver_error(id,-1041,"Invalid historical trade ticks");}
    }
    void historicalTicksBidAsk(int id,const std::vector<::HistoricalTickBidAsk>& ticks,bool done) override {
        try {
            if(ticks.size()>max_tick_page)throw std::length_error("Tick page too large");
            std::vector<dts::HistoricalTick> rows;rows.reserve(ticks.size());
            for(const auto& p:ticks){dts::HistoricalTick r;r.time=p.time;r.bid=p.priceBid;r.ask=p.priceAsk;
                r.bid_size=DecimalFunctions::decimalToString(p.sizeBid);r.ask_size=DecimalFunctions::decimalToString(p.sizeAsk);
                r.bid_past_low=p.tickAttribBidAsk.bidPastLow;r.ask_past_high=p.tickAttribBidAsk.askPastHigh;rows.push_back(std::move(r));}
            deliver_ticks(id,"BID_ASK",std::move(rows),done);
        }catch(const std::exception&){deliver_error(id,-1041,"Invalid historical bid/ask ticks");}
    }
    void historicalTicksLastProtoBuf(const protobuf::HistoricalTicksLast& p) override {
        try {
            if(p.historicaltickslast_size()>static_cast<int>(max_tick_page))throw std::length_error("Tick page too large");
            std::vector<dts::HistoricalTick> rows;
            for(const auto& t:p.historicaltickslast()){
                if(!t.has_time()||!t.has_price()||!t.has_size())throw std::invalid_argument("Missing tick fields");
                dts::HistoricalTick r;r.time=t.time();r.price=t.price();r.size=t.size();r.exchange=t.exchange();r.conditions=t.specialconditions();
                r.past_limit=t.tickattriblast().pastlimit();r.unreported=t.tickattriblast().unreported();rows.push_back(std::move(r));
            }
            deliver_ticks(p.reqid(),"TRADES",std::move(rows),p.isdone());
        }catch(const std::exception&){deliver_error(p.reqid(),-1041,"Invalid protobuf trade ticks");}
    }
    void historicalTicksBidAskProtoBuf(const protobuf::HistoricalTicksBidAsk& p) override {
        try {
            if(p.historicalticksbidask_size()>static_cast<int>(max_tick_page))throw std::length_error("Tick page too large");
            std::vector<dts::HistoricalTick> rows;
            for(const auto& t:p.historicalticksbidask()){
                if(!t.has_time()||!t.has_pricebid()||!t.has_priceask()||!t.has_sizebid()||!t.has_sizeask())throw std::invalid_argument("Missing tick fields");
                dts::HistoricalTick r;r.time=t.time();r.bid=t.pricebid();r.ask=t.priceask();r.bid_size=t.sizebid();r.ask_size=t.sizeask();
                r.bid_past_low=t.tickattribbidask().bidpastlow();r.ask_past_high=t.tickattribbidask().askpasthigh();rows.push_back(std::move(r));
            }
            deliver_ticks(p.reqid(),"BID_ASK",std::move(rows),p.isdone());
        }catch(const std::exception&){deliver_error(p.reqid(),-1041,"Invalid protobuf bid/ask ticks");}
    }
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
    void errorProtoBuf(const protobuf::ErrorMessage& p) override { error_protobuf_mirror_pending_=true; deliver_error(p.id(), p.errorcode(), p.errormsg()); }
private:
    std::atomic<RequestId> monitor_token_{0};
    std::string monitor_account_;
    std::array<bool,9> monitor_protobuf_{};
    bool consume_monitor_mirror(std::size_t index) {
        const bool mirror=monitor_protobuf_[index]; monitor_protobuf_[index]=false; return mirror;
    }
    bool monitor_matches(const std::string& account) const { return monitor_token_.load()!=0 && account==monitor_account_; }
    void deliver_accounts(const std::string& text) {
        ManagedAccountsEvent event; event.success=text.size()<=8192;
        if(event.success && !text.empty()) {
            std::size_t start=0;
            for(;;) {
                const auto end=text.find(',',start); auto account=text.substr(start,end==std::string::npos?end:end-start);
                const auto first=account.find_first_not_of(" "); const auto last=account.find_last_not_of(" ");
                account=first==std::string::npos?std::string{}:account.substr(first,last-first+1);
                if(!trading_account(account) || event.accounts.size()>=128) { event.success=false; event.accounts.clear(); break; }
                event.accounts.push_back(std::move(account)); if(end==std::string::npos) break; start=end+1;
            }
        }
        post([event=std::move(event)](TwsState& s){s.managed_accounts(event);});
    }
    void bad_monitor(TradingSection section,int id) {
        post([section,id](TwsState& s){s.monitor_bad(section,id>0?static_cast<RequestId>(id):0);});
    }
    void end_monitor(TradingSection section,int id) {
        const auto now=Clock::now(); const auto wall=std::chrono::system_clock::now();
        post([section,id,now,wall](TwsState& s){s.monitor_end(section,id>0?static_cast<RequestId>(id):0,now,wall);});
    }
    void post_monitor_position(int id,std::string account,TradingPosition row) {
        if(!row.valid()) { bad_monitor(TradingSection::Positions,id); return; }
        const auto now=Clock::now(); const auto wall=std::chrono::system_clock::now(); post([id,account=std::move(account),row=std::move(row),now,wall](TwsState& s){s.monitor_position(static_cast<RequestId>(id),account,row,now,wall);});
    }
    void post_monitor_value(int id,std::string account,TradingAccountValue row) {
        if(!row.valid()) { bad_monitor(TradingSection::AccountValues,id); return; }
        const auto now=Clock::now(); const auto wall=std::chrono::system_clock::now(); post([id,account=std::move(account),row=std::move(row),now,wall](TwsState& s){s.monitor_value(static_cast<RequestId>(id),account,row,now,wall);});
    }
    void post_monitor_order(std::string account,TradingOpenOrder row) {
        if(!row.valid()) { bad_monitor(TradingSection::OpenOrders,static_cast<int>(monitor_token_.load())); return; }
        const auto token=monitor_token_.load(); const auto now=Clock::now(); const auto wall=std::chrono::system_clock::now();
        post([token,account=std::move(account),row=std::move(row),now,wall](TwsState& s){s.monitor_order(token,account,row,now,wall);});
    }
    void post_monitor_execution(int id,std::string account,TradingExecution row) {
        if(!row.valid()) { bad_monitor(TradingSection::Executions,id); return; }
        const auto now=Clock::now(); const auto wall=std::chrono::system_clock::now(); post([id,account=std::move(account),row=std::move(row),now,wall](TwsState& s){s.monitor_execution(static_cast<RequestId>(id),account,row,now,wall);});
    }
    std::mutex mutex_;
    std::vector<std::function<void(TwsState&)>> queue_;
    bool overflow_ = false;
    // processMsgs serializes callbacks. clear() runs after the reader stops.
    bool depth_protobuf_ = false;
    bool error_protobuf_mirror_pending_ = false;
    void deliver_ticks(int id,std::string type,std::vector<dts::HistoricalTick> rows,bool done) {
        for(const auto& r:rows)r.validate(type);
        post([id,type=std::move(type),rows=std::move(rows),done](TwsState& s)mutable{s.historical_ticks(static_cast<RequestId>(id),type,std::move(rows),done);});
    }
    void post(std::function<void(TwsState&)> action) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (queue_.size() >= 4096) { overflow_ = true; return; }
        queue_.push_back(std::move(action));
    }
    void deliver_depth(DepthEvent e) {
        if(e.raw_payload.size()>65536 || e.size.size()>64 || e.market_maker.size()>64 || e.market_maker.find('\0')!=std::string::npos)
            throw std::invalid_argument("Depth payload exceeds bounds");
        post([e=std::move(e)](TwsState& s){ s.depth_update(e); });
    }
    void native_depth(int id, int position, const std::string& maker, int operation,
                      int side, double price, Decimal size, bool smart, const char* format) {
        // SDK 10.45 dispatches protobuf first and then its converted legacy
        // callback. Select one format per connection, never deduplicate values.
        if (depth_protobuf_) return;
        DepthEvent e; e.received=DepthStamp::now(); e.request_id=static_cast<RequestId>(id);
        e.position=position; e.operation=operation; e.side=side; e.price=price;
        e.market_maker=maker; e.smart_depth=smart; e.callback_format=format;
        try { e.size=DecimalFunctions::decimalToString(size); deliver_depth(std::move(e)); }
        catch(const std::exception&) { deliver_error(id,-1030,"Invalid native depth payload"); }
    }
    template<class Message> void proto_depth(const Message& message, const char* format) {
        depth_protobuf_ = true;
        const int id=message.reqid();
        const auto& p=message.marketdepthdata();
        DepthEvent e; e.received=DepthStamp::now(); e.request_id=static_cast<RequestId>(id);
        e.position=p.has_position()?p.position():-1; e.operation=p.has_operation()?p.operation():-1;
        e.side=p.has_side()?p.side():-1; e.price=p.has_price()?p.price():std::numeric_limits<double>::quiet_NaN();
        e.size=p.size(); e.market_maker=p.marketmaker(); e.smart_depth=p.issmartdepth();
        e.callback_format=format;
        try {
            if(!message.has_marketdepthdata())throw std::invalid_argument("Missing depth payload");
            if(message.ByteSizeLong()>65536)throw std::length_error("Depth protobuf exceeds bounds");
            e.raw_payload=message.SerializeAsString();
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
