#include "tws_callbacks.hpp"
#include <iostream>
#include "EDecoder.h"
#include "EClient.h"
#include <arpa/inet.h>
#include <google/protobuf/unknown_field_set.h>
#define CHECK(x) do { if (!(x)) throw std::runtime_error(#x); } while(false)
::Contract native_stock() {
    ::Contract c; c.conId=123; c.symbol="TEST"; c.secType="STK"; c.exchange="SMART"; c.currency="USD"; return c;
}
void proto_stock(protobuf::Contract* c) {
    c->set_conid(123); c->set_symbol("TEST"); c->set_sectype("STK"); c->set_exchange("SMART"); c->set_currency("USD");
}

// Exercise the official decoder, which invokes BOTH callback interfaces for
// one protobuf wire message. Direct callback-only tests miss that behavior.
void depth_decoder_regression() {
    using namespace dts;
    ibkr_detail::Callbacks callbacks;
    TwsState state;
    const auto now = Clock::now();
    state.start(now); callbacks.nextValidId(1); callbacks.drain(state); state.poll();
    DepthSpec spec; spec.contract=ibkr_detail::from_native(native_stock()); spec.venue="BATS"; spec.rows=50;
    const auto id=state.depth(spec,now);
    DepthBook book(50);
    for (const auto& e:state.poll()) if (const auto* d=std::get_if<DepthEvent>(&e)) book.apply(*d);
    EDecoder decoder(MIN_SERVER_VER_PROTOBUF_MARKET_DATA,&callbacks);
    auto process=[&](int type,const auto& payload) {
        const auto tag=htonl(static_cast<std::uint32_t>(ibapi::client_constants::PROTOBUF_MSG_ID+type));
        std::string wire(reinterpret_cast<const char*>(&tag),sizeof(tag));
        wire+=payload.SerializeAsString();
        const char* begin=wire.data();
        CHECK(decoder.parseAndProcessMsg(begin,wire.data()+wire.size())==static_cast<int>(wire.size()));
        callbacks.drain(state);
        auto events=state.poll();
        CHECK(events.size()==1);
        CHECK(std::holds_alternative<DepthEvent>(events.front()));
        const auto event=std::get<DepthEvent>(events.front());
        CHECK(event.raw_payload==payload.SerializeAsString());
        CHECK(event.callback_format==(type==MARKET_DEPTH ? "protobuf_depth" : "protobuf_depth_l2"));
        book.apply(event);
        return event;
    };
    protobuf::MarketDepth message; message.set_reqid(static_cast<int>(id));
    message.GetReflection()->MutableUnknownFields(&message)->AddVarint(999, 42);
    auto* row=message.mutable_marketdepthdata();
    row->set_operation(0); row->set_position(0); row->set_side(1); row->set_price(99); row->set_size("440");
    CHECK(process(MARKET_DEPTH,message).size=="440");
    row->set_side(0); row->set_price(101); process(MARKET_DEPTH,message);
    CHECK(book.bids().size()==1 && book.asks().size()==1);
    CHECK(book.quality()=="two_sided_unverified");
    row->set_operation(1); row->set_size("220.125");
    const auto first=process(MARKET_DEPTH,message);
    const auto second=process(MARKET_DEPTH,message);
    CHECK(second.sequence==first.sequence+1); // Equal wire updates must survive.
    CHECK(book.asks().front().size=="220.125");
    row->set_operation(2);
    process(MARKET_DEPTH,message);
    CHECK(book.structural_valid() && book.asks().empty() && book.bids().size()==1);
    protobuf::MarketDepthL2 l2; l2.set_reqid(static_cast<int>(id));
    *l2.mutable_marketdepthdata()=*row;
    l2.mutable_marketdepthdata()->set_operation(0);
    l2.mutable_marketdepthdata()->set_marketmaker("BATS");
    l2.mutable_marketdepthdata()->set_size("5");
    CHECK(process(MARKET_DEPTH_L2,l2).market_maker=="BATS");
    CHECK(book.quality()=="two_sided_unverified");
    // The application may request fifty rows; each delivered final position
    // survives decoding and reconstruction without padding missing rows.
    for(int side=0;side<2;++side)for(int position=1;position<50;++position) {
        auto* value=l2.mutable_marketdepthdata();value->set_side(side);value->set_position(position);
        value->set_price(side==0?101.+position:99.-position);value->set_operation(0);
        process(MARKET_DEPTH_L2,l2);
    }
    CHECK(book.bids().size()==50 && book.asks().size()==50);
    CHECK(book.quality()=="two_sided_unverified");
    l2.mutable_marketdepthdata()->set_side(0);l2.mutable_marketdepthdata()->set_position(49);
    l2.mutable_marketdepthdata()->set_operation(1);l2.mutable_marketdepthdata()->set_price(150);
    l2.mutable_marketdepthdata()->set_size("19.125");process(MARKET_DEPTH_L2,l2);
    CHECK(book.asks()[49].size=="19.125");
    auto error_message=[&](int request,int code) {
        protobuf::ErrorMessage message;message.set_id(request);message.set_errorcode(code);message.set_errormsg("decoder regression");
        const auto tag=htonl(static_cast<std::uint32_t>(ibapi::client_constants::PROTOBUF_MSG_ID+ERR_MSG));
        std::string wire(reinterpret_cast<const char*>(&tag),sizeof(tag));wire+=message.SerializeAsString();
        const char* begin=wire.data();CHECK(decoder.parseAndProcessMsg(begin,wire.data()+wire.size())==static_cast<int>(wire.size()));
        callbacks.drain(state);return state.poll();
    };
    for(int i=0;i<2;++i) {
        const auto events=error_message(static_cast<int>(id),317);
        int resets=0,errors=0;
        for(const auto& value:events) {
            if(const auto* e=std::get_if<DepthEvent>(&value)) {CHECK(e->kind=="reset");++resets;}
            if(std::holds_alternative<BrokerError>(value))++errors;
        }
        CHECK(resets==1 && errors==1);
    }
    callbacks.error(-1,0,2104,"genuine SDK legacy notification","");
    callbacks.drain(state);
    const auto local_errors=state.poll();
    CHECK(local_errors.size()==1 && std::holds_alternative<BrokerError>(local_errors.front()));
    const auto failed=error_message(-1,1100);
    int gaps=0,errors=0;
    for(const auto& value:failed) {
        if(const auto* e=std::get_if<DepthEvent>(&value)) {CHECK(e->kind=="gap");++gaps;}
        if(std::holds_alternative<BrokerError>(value))++errors;
    }
    CHECK(gaps==1 && errors==1 && state.state()==ConnectionState::Failed);
    // Format selection resets with the connection; old TWS legacy wire data
    // still produces one event, including consecutive identical messages.
    state.disconnect(); callbacks.clear(); state.start(now);
    callbacks.nextValidId(1); callbacks.drain(state); state.poll();
    const auto old_id=state.depth(spec,now); state.poll();
    EDecoder old_decoder(151,&callbacks);
    std::string legacy;
    for(const std::string& field:std::vector<std::string>{"12","1",std::to_string(old_id),"0","0","1","99","7"})
        {legacy+=field;legacy.push_back('\0');}
    for(int i=0;i<2;++i) {
        const char* begin=legacy.data();
        CHECK(old_decoder.parseAndProcessMsg(begin,legacy.data()+legacy.size())==static_cast<int>(legacy.size()));
        callbacks.drain(state);const auto events=state.poll();
        CHECK(events.size()==1 && depth_size(std::get<DepthEvent>(events.front()).size)==7);
        CHECK(std::get<DepthEvent>(events.front()).callback_format=="legacy_depth");
        CHECK(std::get<DepthEvent>(events.front()).raw_payload.empty());
    }
}

int main() {
    try {
        using namespace dts;
        depth_decoder_regression();
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
        state.disconnect();state.start(now);callbacks.nextValidId(1);callbacks.drain(state);state.poll();
        DepthSpec ds;ds.contract=contract;ds.venue="TEST";ds.rows=5;
        auto did=state.depth(ds,now);state.poll();
        callbacks.updateMktDepth(static_cast<int>(did),0,0,0,101,DecimalFunctions::stringToDecimal("12.5"));
        callbacks.updateMktDepthL2(static_cast<int>(did),0,"TEST",0,1,99,DecimalFunctions::stringToDecimal("25"),false);
        callbacks.drain(state);events=state.poll();CHECK(events.size()==2);
        CHECK(depth_size(std::get<DepthEvent>(events[0]).size)==12.5);
        CHECK(std::get<DepthEvent>(events[1]).market_maker=="TEST");
        CHECK(std::get<DepthEvent>(events[0]).received.monotonic_ns>0);
        protobuf::MarketDepth md;md.set_reqid(static_cast<int>(did));
        auto* mdd=md.mutable_marketdepthdata();mdd->set_position(0);mdd->set_operation(1);mdd->set_side(0);mdd->set_price(102);mdd->set_size("3.125");
        callbacks.updateMarketDepthProtoBuf(md);callbacks.drain(state);events=state.poll();
        CHECK(events.size()==1 && std::get<DepthEvent>(events[0]).size=="3.125");
        protobuf::MarketDepthL2 ml2;ml2.set_reqid(static_cast<int>(did));*ml2.mutable_marketdepthdata()=*mdd;ml2.mutable_marketdepthdata()->set_marketmaker("MM");
        callbacks.updateMarketDepthL2ProtoBuf(ml2);callbacks.drain(state);events=state.poll();CHECK(std::get<DepthEvent>(events[0]).market_maker=="MM");
        error.set_id(static_cast<int>(did));error.set_errorcode(317);callbacks.errorProtoBuf(error);callbacks.drain(state);events=state.poll();
        CHECK(std::get<DepthEvent>(events[0]).kind=="reset");
        state.disconnect();state.start(now);callbacks.nextValidId(1);callbacks.drain(state);state.poll();
        TickSpec ts;ts.contract=contract;
        auto tid=state.ticks(ts,hw.start,now);
        ::HistoricalTickLast last{};last.time=hw.start;last.price=100;last.size=DecimalFunctions::stringToDecimal("2.5");
        last.exchange="TEST";last.specialConditions="synthetic";last.tickAttribLast.unreported=true;
        callbacks.historicalTicksLast(static_cast<int>(tid),{last},false);callbacks.drain(state);CHECK(state.poll().empty());
        callbacks.historicalTicksLast(static_cast<int>(tid),{last},true);callbacks.drain(state);events=state.poll();
        auto ticks=std::get<HistoricalTickPage>(events[0]);CHECK(ticks.ticks.size()==2&&ticks.ticks[0].unreported&&std::stold(ticks.ticks[0].size)==2.5L);
        tid=state.ticks(ts,hw.start,now+std::chrono::seconds(1));
        protobuf::HistoricalTicksLast tl;tl.set_reqid(static_cast<int>(tid));tl.set_isdone(true);auto* lt=tl.add_historicaltickslast();
        lt->set_time(hw.start);lt->set_price(100);lt->set_size("2.125");lt->set_exchange("TEST");lt->mutable_tickattriblast()->set_pastlimit(true);
        callbacks.historicalTicksLastProtoBuf(tl);callbacks.drain(state);events=state.poll();ticks=std::get<HistoricalTickPage>(events[0]);
        CHECK(ticks.ticks.size()==1&&ticks.ticks[0].size=="2.125"&&ticks.ticks[0].past_limit);
        ts.type="BID_ASK";tid=state.ticks(ts,hw.start,now+std::chrono::seconds(2));
        ::HistoricalTickBidAsk ba{};ba.time=hw.start;ba.priceBid=99;ba.priceAsk=101;
        ba.sizeBid=DecimalFunctions::stringToDecimal("10.5");ba.sizeAsk=DecimalFunctions::stringToDecimal("11.5");ba.tickAttribBidAsk.askPastHigh=true;
        callbacks.historicalTicksBidAsk(static_cast<int>(tid),{ba},true);callbacks.drain(state);events=state.poll();ticks=std::get<HistoricalTickPage>(events[0]);
        CHECK(ticks.ticks[0].bid==99&&ticks.ticks[0].ask_past_high&&std::stold(ticks.ticks[0].bid_size)==10.5L);
        tid=state.ticks(ts,hw.start,now+std::chrono::seconds(3));
        protobuf::HistoricalTicksBidAsk tb;tb.set_reqid(static_cast<int>(tid));tb.set_isdone(true);auto* bt=tb.add_historicalticksbidask();
        bt->set_time(hw.start);bt->set_pricebid(99);bt->set_priceask(101);bt->set_sizebid("1.125");bt->set_sizeask("3.125");bt->mutable_tickattribbidask()->set_bidpastlow(true);
        callbacks.historicalTicksBidAskProtoBuf(tb);callbacks.drain(state);events=state.poll();ticks=std::get<HistoricalTickPage>(events[0]);
        CHECK(ticks.ticks[0].ask_size=="3.125"&&ticks.ticks[0].bid_past_low);
        tid=state.ticks(ts,hw.start,now+std::chrono::seconds(4));tb.set_reqid(static_cast<int>(tid));bt->clear_priceask();
        callbacks.historicalTicksBidAskProtoBuf(tb);callbacks.drain(state);events=state.poll();CHECK(std::get<HistoricalTickPage>(events[0]).status=="failed");
        std::cout << "Native/protobuf callback mapping, BID conversion, tick fields and failure checks passed\n";
        return 0;
    } catch(const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
