#include "tws_callbacks.hpp"
#include <iostream>
#define CHECK(x) do { if (!(x)) throw std::runtime_error(#x); } while(false)
::Contract native_stock() {
    ::Contract c; c.conId=123; c.symbol="TEST"; c.secType="STK"; c.exchange="SMART"; c.currency="USD"; return c;
}
void proto_stock(protobuf::Contract* c) {
    c->set_conid(123); c->set_symbol("TEST"); c->set_sectype("STK"); c->set_exchange("SMART"); c->set_currency("USD");
}
int main() {
    try {
        using namespace dts;
        ibkr_detail::Callbacks callbacks; TwsState state;
        const auto now=Clock::now(); state.start(now);
        protobuf::NextValidId next; next.set_orderid(100);
        callbacks.nextValidIdProtoBuf(next);
        CHECK(state.state()==ConnectionState::Connecting);
        callbacks.drain(state); CHECK(state.state()==ConnectionState::Ready);
        ContractQuery query; query.symbol="TEST";
        auto request=state.resolve(query,now);
        protobuf::ContractData data; data.set_reqid(static_cast<int>(request)); proto_stock(data.mutable_contract());
        protobuf::ContractDataEnd end; end.set_reqid(static_cast<int>(request));
        callbacks.contractDataProtoBuf(data); callbacks.contractDataEndProtoBuf(end); callbacks.drain(state);
        auto events=state.poll(); CHECK(std::get<ContractsComplete>(events.back()).success);
        const auto contract=ibkr_detail::from_native(native_stock());
        CHECK(contract.multiplier==1 && contract.security_type==SecurityType::Equity);
        auto id=state.subscribe(contract,now);
        protobuf::MarketDataType mode; mode.set_reqid(static_cast<int>(id)); mode.set_marketdatatype(3);
        callbacks.marketDataTypeProtoBuf(mode);
        protobuf::TickPrice bid; bid.set_reqid(static_cast<int>(id)); bid.set_ticktype(66); bid.set_price(99);
        callbacks.tickPriceProtoBuf(bid); bid.set_ticktype(67); bid.set_price(101); callbacks.tickPriceProtoBuf(bid);
        callbacks.drain(state); events=state.poll(); const auto quote=std::get<Quote>(events.back());
        CHECK(quote.data_type==dts::MarketDataType::Delayed && quote.mid(Clock::now(),std::chrono::seconds(5))==100);
        state.request_positions(999,now);
        protobuf::Position position; position.set_account("SYNTHETIC_ACCOUNT"); proto_stock(position.mutable_contract()); position.set_position("-2.5");
        callbacks.positionProtoBuf(position); callbacks.positionEndProtoBuf(protobuf::PositionEnd{}); callbacks.drain(state);
        events=state.poll(); CHECK(std::get<PositionEvent>(events.front()).position.quantity==-2.5); CHECK(std::get<PositionsComplete>(events.back()).success);
        auto option=native_stock(); option.secType="OPT"; option.multiplier="100"; option.right="C"; option.strike=100; option.lastTradeDateOrContractMonth="20261218";
        CHECK(ibkr_detail::from_native(option).multiplier==100);
        CHECK(ibkr_detail::from_native(option).option->exercise_style==ExerciseStyle::Unknown);
        bool rejected=false; option.multiplier="";
        try {(void)ibkr_detail::from_native(option);} catch(const std::exception&){rejected=true;} CHECK(rejected);
        state.disconnect(); state.start(now); callbacks.nextValidId(100); callbacks.drain(state);
        request=state.resolve(query,now); ::ContractDetails detail; detail.contract=native_stock();
        callbacks.contractDetails(static_cast<int>(request),detail); callbacks.contractDetailsEnd(static_cast<int>(request)); callbacks.drain(state);
        events=state.poll(); CHECK(std::get<ContractsComplete>(events.back()).success);
        state.request_positions(1000,now);
        const auto decimal = DecimalFunctions::stringToDecimal("2.5");
        CHECK(DecimalFunctions::decimalToDouble(decimal)==2.5);
        callbacks.position("SYNTHETIC_ACCOUNT",native_stock(),decimal,0); callbacks.positionEnd(); callbacks.drain(state);
        events=state.poll(); CHECK(std::get<PositionEvent>(events.front()).position.quantity==2.5);
        state.disconnect(); state.start(now); callbacks.nextValidId(100); callbacks.drain(state); state.poll(); state.request_positions(1001,now);
        proto_stock(position.mutable_contract()); position.mutable_contract()->set_sectype("FUT"); callbacks.positionProtoBuf(position);
        callbacks.positionEnd(); callbacks.drain(state); events=state.poll(); CHECK(!std::get<PositionsComplete>(events.back()).success);
        // The actual official SDK callback types are compiled and invoked here.
        state.disconnect();state.start(now);callbacks.nextValidId(1);callbacks.drain(state);state.poll();
        HistorySpec hs;hs.contract=contract;const HistoryWindow hw{1767571200,1768003200};
        auto hid=state.history(hs,hw,now);
        ::Bar hb;hb.time="20260105";hb.open=100;hb.high=102;hb.low=99;hb.close=101;
        hb.volume=DecimalFunctions::stringToDecimal("123.5");hb.wap=DecimalFunctions::stringToDecimal("100.5");hb.count=3;
        callbacks.historicalData(static_cast<int>(hid),hb);callbacks.historicalDataEnd(static_cast<int>(hid),"20260105","20260109");
        callbacks.drain(state);events=state.poll();
        CHECK(events.size()==2 && std::get<HistoricalBarEvent>(events[0]).bar.volume.has_value());
        CHECK(std::stold(*std::get<HistoricalBarEvent>(events[0]).bar.volume)==123.5L);
        CHECK(std::get<HistoricalEnd>(events[1]).status=="complete");
        hid=state.history(hs,hw,now+std::chrono::seconds(1));
        protobuf::HistoricalData hd;hd.set_reqid(static_cast<int>(hid));auto* pb=hd.add_historicaldatabars();
        pb->set_date("20260105");pb->set_open(100);pb->set_high(102);pb->set_low(99);pb->set_close(101);
        pb->set_volume("123.5");pb->set_wap("100.5");pb->set_barcount(3);
        protobuf::HistoricalDataEnd he;he.set_reqid(static_cast<int>(hid));he.set_startdatestr("20260105");he.set_enddatestr("20260109");
        callbacks.historicalDataProtoBuf(hd);callbacks.historicalDataEndProtoBuf(he);callbacks.drain(state);events=state.poll();
        CHECK(events.size()==2 && *std::get<HistoricalBarEvent>(events[0]).bar.volume=="123.5");
        CHECK(std::get<HistoricalEnd>(events[1]).status=="complete");
        hs.price_type="MIDPOINT";hid=state.history(hs,hw,now+std::chrono::seconds(2));hd.set_reqid(static_cast<int>(hid));he.set_reqid(static_cast<int>(hid));
        callbacks.historicalDataProtoBuf(hd);callbacks.historicalDataEndProtoBuf(he);callbacks.drain(state);events=state.poll();
        CHECK(!std::get<HistoricalBarEvent>(events[0]).bar.volume && !std::get<HistoricalBarEvent>(events[0]).bar.count);
        hid=state.history(hs,hw,now+std::chrono::seconds(3));hd.set_reqid(static_cast<int>(hid));hd.mutable_historicaldatabars(0)->clear_close();
        callbacks.historicalDataProtoBuf(hd);callbacks.drain(state);events=state.poll();
        CHECK(std::get<HistoricalEnd>(events[0]).status=="failed");
        protobuf::ErrorMessage error; error.set_id(-1); error.set_errorcode(1100); error.set_errormsg("synthetic disconnect");
        callbacks.errorProtoBuf(error); callbacks.drain(state); CHECK(state.state()==ConnectionState::Failed);
        std::cout << "Native/protobuf callback mapping, BID conversion, and failure checks passed\n";
        return 0;
    } catch(const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
