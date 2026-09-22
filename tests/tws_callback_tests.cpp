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
        CHECK(quote.data_type==MarketDataType::Delayed && quote.mid(Clock::now(),std::chrono::seconds(5))==100);
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
        protobuf::ErrorMessage error; error.set_id(-1); error.set_errorcode(1100); error.set_errormsg("synthetic disconnect");
        callbacks.errorProtoBuf(error); callbacks.drain(state); CHECK(state.state()==ConnectionState::Failed);
        std::cout << "Native/protobuf callback mapping, BID conversion, and failure checks passed\n";
        return 0;
    } catch(const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
