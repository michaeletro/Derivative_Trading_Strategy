#include "tws_callbacks.hpp"
#include "EDecoder.h"
#include "EClient.h"
#include <arpa/inet.h>
#include <iostream>
#define CHECK(x) do { if(!(x)) throw std::runtime_error(#x); } while(false)
using namespace dts;
using namespace ibapi::client_constants;
void stock(protobuf::Contract* c) {c->set_conid(123);c->set_symbol("TEST");c->set_sectype("STK");c->set_exchange("SMART");c->set_currency("USD");}
struct Fixture {
    ibkr_detail::Callbacks callbacks;
    TwsState state;
    TradingMonitorModel model;
    EDecoder decoder{MIN_SERVER_VER_PROTOBUF_ACCOUNTS_POSITIONS,&callbacks};
    TradingMonitorRequests ids;
    Fixture() {
        state.start(Clock::now());callbacks.nextValidId(100);callbacks.drain(state);state.poll();
        protobuf::ManagedAccounts accounts;accounts.set_accountslist("TEST_ACCOUNT,OTHER_ACCOUNT");
        const auto events=process(MANAGED_ACCTS,accounts);CHECK(events.size()==1);
        callbacks.openOrderEnd(); // Receipt before start: token zero remains zero after begin.
        ids=state.start_trading_monitor("TEST_ACCOUNT",Clock::now());callbacks.configure_monitor(ids.monitor,"TEST_ACCOUNT");
        model.begin(ids.monitor,"TEST_ACCOUNT",1);callbacks.drain(state);CHECK(state.poll().empty());
    }
    template<class Message> std::vector<BrokerEvent> process(int type,const Message& message) {
        const auto tag=htonl(static_cast<std::uint32_t>(PROTOBUF_MSG_ID+type));
        std::string wire(reinterpret_cast<const char*>(&tag),sizeof(tag));wire+=message.SerializeAsString();
        return process_wire(wire);
    }
    std::vector<BrokerEvent> process_wire(const std::string& wire) {
        const char* begin=wire.data();CHECK(decoder.parseAndProcessMsg(begin,wire.data()+wire.size())==static_cast<int>(wire.size()));
        callbacks.drain(state);auto events=state.poll();
        for(const auto& event:events) {
            if(const auto* p=std::get_if<ManagedAccountsEvent>(&event)) model.accounts(*p,1);
            if(const auto* p=std::get_if<TradingMonitorEvent>(&event)) model.apply(*p);
        }
        return events;
    }
};
void decoder_mirrors() {
    Fixture f;
    protobuf::PositionMulti p;p.set_reqid(static_cast<int>(f.ids.positions));p.set_account("TEST_ACCOUNT");stock(p.mutable_contract());p.set_position("-1.234567890123456789");p.set_avgcost(12);
    CHECK(f.process(POSITION_MULTI,p).size()==1);CHECK(f.model.view().positions.rows.empty());
    protobuf::PositionMultiEnd pe;pe.set_reqid(static_cast<int>(f.ids.positions));CHECK(f.process(POSITION_MULTI_END,pe).size()==1);
    CHECK(f.model.view().positions.rows[0].quantity==p.position());p.set_position("0");CHECK(f.process(POSITION_MULTI,p).size()==1);CHECK(f.model.view().positions.rows[0].quantity=="0");
    p.set_modelcode("MODEL_A");p.set_position("2");CHECK(f.process(POSITION_MULTI,p).size()==1);CHECK(f.model.view().positions.rows.size()==2);
    p.set_account("OTHER_ACCOUNT");CHECK(f.process(POSITION_MULTI,p).empty());
    protobuf::AccountSummary a;a.set_reqid(static_cast<int>(f.ids.summary));a.set_account("TEST_ACCOUNT");a.set_tag("NetLiquidation");a.set_value("12345.67890123456789");a.set_currency("USD");
    CHECK(f.process(ACCOUNT_SUMMARY,a).size()==1);
    // A later independent legacy wire message is not the immediate mirror.
    const auto legacy_tag=htonl(static_cast<std::uint32_t>(ACCOUNT_SUMMARY));
    std::string legacy(reinterpret_cast<const char*>(&legacy_tag),sizeof(legacy_tag));
    for(const auto& field:std::vector<std::string>{"1",std::to_string(f.ids.summary),"TEST_ACCOUNT","BuyingPower","55.25","USD"}) {legacy+=field;legacy.push_back('\0');}
    CHECK(f.process_wire(legacy).size()==1);
    protobuf::AccountSummaryEnd ae;ae.set_reqid(static_cast<int>(f.ids.summary));CHECK(f.process(ACCOUNT_SUMMARY_END,ae).size()==1);CHECK(f.model.view().account_values.rows.size()==2);
    protobuf::OpenOrder o;o.set_orderid(-10);stock(o.mutable_contract());auto* order=o.mutable_order();order->set_account("TEST_ACCOUNT");order->set_clientid(-1);order->set_permid(9007199254740993LL);order->set_action("BUY");order->set_ordertype("LMT");order->set_totalquantity("2.1234567890123456789");order->set_lmtprice(15);order->set_orderref("TEST_REF");o.mutable_orderstate()->set_status("Submitted");
    CHECK(f.process(OPEN_ORDER,o).size()==1);protobuf::OpenOrdersEnd oe;CHECK(f.process(OPEN_ORDER_END,oe).size()==1);
    CHECK(f.model.view().open_orders.rows.size()==1 && f.model.view().open_orders.rows[0].order_id==-10);
    CHECK(f.model.view().open_orders.rows[0].perm_id==9007199254740993LL && !f.model.view().open_orders.rows[0].filled);
    protobuf::ExecutionDetails e;e.set_reqid(static_cast<int>(f.ids.executions));stock(e.mutable_contract());auto* execution=e.mutable_execution();execution->set_execid("TEST_EXEC");execution->set_acctnumber("TEST_ACCOUNT");execution->set_time("20261009 12:00:00");execution->set_side("BOT");execution->set_shares("0.1234567890123456789");execution->set_price(15);execution->set_orderid(-10);execution->set_clientid(-1);execution->set_permid(9007199254740993LL);
    CHECK(f.process(EXECUTION_DATA,e).size()==1);CHECK(f.process(EXECUTION_DATA,e).size()==1);
    protobuf::ExecutionDetailsEnd ee;ee.set_reqid(static_cast<int>(f.ids.executions));CHECK(f.process(EXECUTION_DATA_END,ee).size()==1);
    CHECK(f.model.view().executions.rows.size()==1 && f.model.view().executions.rows[0].quantity==execution->shares());CHECK(f.model.view().state==TradingMonitorState::Active);
    CHECK(f.process(OPEN_ORDER,o).empty());CHECK(f.process(EXECUTION_DATA,e).empty()); // snapshots do not claim continuing order/fill coverage.
    f.callbacks.configure_monitor(0,"");f.state.stop_trading_monitor();CHECK(f.process(OPEN_ORDER_END,oe).empty());
}
void invalid_and_stale() {
    Fixture f;protobuf::PositionMulti p;p.set_reqid(static_cast<int>(f.ids.positions));p.set_account("TEST_ACCOUNT");stock(p.mutable_contract());p.set_position("NaN");
    const auto failed=f.process(POSITION_MULTI,p);CHECK(failed.size()==1);CHECK(f.model.view().state==TradingMonitorState::Failed);
    protobuf::PositionMultiEnd end;end.set_reqid(static_cast<int>(f.ids.positions));CHECK(f.process(POSITION_MULTI_END,end).empty());
    Fixture g;end.set_reqid(static_cast<int>(g.ids.positions+100));CHECK(g.process(POSITION_MULTI_END,end).empty());CHECK(g.model.view().positions.status==SnapshotStatus::Pending);
    protobuf::AccountSummaryEnd summary;summary.set_reqid(static_cast<int>(g.ids.summary));CHECK(g.process(ACCOUNT_SUMMARY_END,summary).size()==1);CHECK(g.model.view().state==TradingMonitorState::Failed);
    Fixture h;h.callbacks.connectionClosed();h.callbacks.drain(h.state);const auto lost=h.state.poll();CHECK(h.state.state()==ConnectionState::Failed && !lost.empty());
    h.callbacks.clear();h.model.invalidate(2);p.set_reqid(static_cast<int>(h.ids.positions));p.set_position("1");CHECK(h.process(POSITION_MULTI,p).empty());CHECK(h.model.view().state==TradingMonitorState::Unavailable);
}
int main() {try{decoder_mirrors();invalid_and_stale();std::cout<<"Read-only monitor decoder tests passed\n";}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
