#include <dts/mock_broker.hpp>
#include <dts/read_only_service.hpp>
#include <dts/tws_state.hpp>
#include <iostream>
#define CHECK(x) do { if(!(x)) throw std::runtime_error(#x); } while(false)
using namespace dts;
template<class F> void rejects(F fn) { bool rejected=false; try {fn();} catch(const std::exception&) {rejected=true;} CHECK(rejected); }
TradingContract contract(ContractId id=123) { return {id,"TEST","STK","SMART","USD"}; }
TradingMonitorEvent event(RequestId id,TradingSection section,TradingMonitorPayload value) { return {id,section,std::move(value)}; }
void model_tests() {
    TradingMonitorModel model; model.invalidate(5); CHECK(model.accounts().status==SnapshotStatus::Unavailable);
    model.accounts({true,{}},5); CHECK(model.accounts().status==SnapshotStatus::Complete); rejects([&]{model.begin(1,"TEST_ACCOUNT",5);});
    model.accounts({true,{"TEST_ACCOUNT","TEST_ACCOUNT"}},5); CHECK(model.accounts().status==SnapshotStatus::Failed);
    model.accounts({true,{"TEST_ACCOUNT"}},5); model.begin(20,"TEST_ACCOUNT",5);
    model.apply(event(19,TradingSection::Positions,TradingSectionEnd{})); CHECK(model.view().positions.status==SnapshotStatus::Pending);
    model.apply(event(20,TradingSection::Positions,TradingPosition{contract(),"-1.234567890123456789",12.5}));
    CHECK(model.view().positions.rows.empty());
    model.apply(event(20,TradingSection::Positions,TradingSectionEnd{})); CHECK(model.view().positions.rows[0].quantity=="-1.234567890123456789");
    model.apply(event(20,TradingSection::Positions,TradingPosition{contract(),"2",12.5,"MODEL_A"}));
    CHECK(model.view().positions.rows.size()==2);
    model.apply(event(20,TradingSection::AccountValues,TradingAccountValue{"NetLiquidation","1234.123456789","USD"}));
    model.apply(event(20,TradingSection::AccountValues,TradingSectionEnd{}));
    TradingOpenOrder order; order.contract=contract(); order.order_id=-5; order.client_id=-1; order.perm_id=111;
    order.action="BUY";order.order_type="LMT";order.quantity="3.125";order.status="Submitted";
    model.apply(event(20,TradingSection::OpenOrders,order)); order.perm_id=112; model.apply(event(20,TradingSection::OpenOrders,order));
    model.apply(event(20,TradingSection::OpenOrders,TradingSectionEnd{})); CHECK(model.view().open_orders.rows.size()==2);
    TradingExecution execution;execution.contract=contract();execution.exec_id="TEST_EXEC";execution.time="20261009 12:00:00";
    execution.side="BOT";execution.quantity="0.000000000000000001";execution.order_id=-5;execution.client_id=-1;execution.perm_id=111;execution.price=10;
    model.apply(event(20,TradingSection::Executions,execution));model.apply(event(20,TradingSection::Executions,execution));
    model.apply(event(20,TradingSection::Executions,TradingSectionEnd{})); CHECK(model.view().executions.rows.size()==1);
    CHECK(model.view().state==TradingMonitorState::Active && model.view().started_wall && model.view().last_update_wall);
    model.apply(event(20,TradingSection::Positions,TradingPosition{contract(),"0",std::nullopt})); CHECK(model.view().positions.rows[0].quantity=="0");
    model.apply(event(20,TradingSection::AccountValues,TradingAccountValue{"NetLiquidation","999","USD"})); CHECK(model.view().account_values.rows[0].value=="999");
    model.stop(); model.apply(event(20,TradingSection::Positions,TradingSectionEnd{}));
    CHECK(model.view().state==TradingMonitorState::Stopped && model.view().positions.rows.empty());
    model.accounts({true,{"OTHER_ACCOUNT"}},5); CHECK(model.view().state==TradingMonitorState::Stopped);
    model.accounts({true,{"TEST_ACCOUNT"}},5);
    model.begin(21,"TEST_ACCOUNT",5);model.apply(event(21,TradingSection::AccountValues,TradingSectionEnd{}));
    CHECK(model.view().state==TradingMonitorState::Failed && model.view().positions.status==SnapshotStatus::Failed);
    model.begin(22,"TEST_ACCOUNT",5);model.apply(event(22,TradingSection::Executions,execution));execution.quantity="2";
    model.apply(event(22,TradingSection::Executions,execution));CHECK(model.view().state==TradingMonitorState::Failed);
    model.begin(23,"TEST_ACCOUNT",5);
    for(std::size_t i=0;i<=trading_row_limit;++i) model.apply(event(23,TradingSection::Positions,TradingPosition{contract(static_cast<ContractId>(i+1)),"1",std::nullopt}));
    CHECK(model.view().state==TradingMonitorState::Failed && model.view().positions.rows.empty());
    model.invalidate(6);model.apply(event(23,TradingSection::Executions,TradingSectionEnd{}));
    CHECK(model.view().state==TradingMonitorState::Unavailable && model.accounts().generation==6);
}
void state_tests() {
    TwsState state(std::chrono::milliseconds(100));const auto now=Clock::now();
    state.start(now);state.ready();state.poll();rejects([&]{state.start_trading_monitor("TEST_ACCOUNT",now);});
    state.managed_accounts({true,{"TEST_ACCOUNT"}});state.poll();
    const auto first=state.start_trading_monitor("TEST_ACCOUNT",now);state.monitor_end(TradingSection::OpenOrders,0,now);CHECK(state.poll().empty());
    state.monitor_value(first.summary,"OTHER_ACCOUNT",{"NetLiquidation","100","USD"},now); CHECK(state.poll().empty());
    state.monitor_position(first.positions,"TEST_ACCOUNT",{contract(),"5.25",10},now);
    state.monitor_end(TradingSection::Positions,first.positions,now); CHECK(state.poll().size()==2);
    state.monitor_position(first.positions,"TEST_ACCOUNT",{contract(),"6.25",10},now);CHECK(state.poll().size()==1);
    state.expire(now+std::chrono::milliseconds(101));auto failed=state.poll();CHECK(failed.size()==1);
    const auto& failure=std::get<TradingMonitorEvent>(failed[0]);CHECK(!std::get<TradingSectionEnd>(failure.value).success);
    CHECK(state.trading_monitor_cancellations().size()==1);rejects([&]{state.start_trading_monitor("TEST_ACCOUNT",now);});
    state.disconnect();state.start(now);state.ready();state.managed_accounts({true,{"TEST_ACCOUNT"}});state.poll();
    const auto second=state.start_trading_monitor("TEST_ACCOUNT",now);CHECK(second.monitor>first.executions);
    state.monitor_end(TradingSection::Executions,first.executions,now);state.monitor_position(first.positions,"TEST_ACCOUNT",{contract(),"1",10},now);CHECK(state.poll().empty());
    state.stop_trading_monitor();state.monitor_end(TradingSection::Positions,second.positions,now);CHECK(state.poll().empty());
    state.disconnect();state.managed_accounts({true,{"TEST_ACCOUNT"}});CHECK(state.poll().empty());
    state.start(now);state.ready();state.managed_accounts({true,{"TEST_ACCOUNT"}});state.poll();
    const auto late=state.start_trading_monitor("TEST_ACCOUNT",now);
    state.monitor_end(TradingSection::Positions,late.positions,now+std::chrono::milliseconds(101));
    const auto late_events=state.poll();CHECK(late_events.size()==1);
    CHECK(!std::get<TradingSectionEnd>(std::get<TradingMonitorEvent>(late_events[0]).value).success);
}
void service_tests() {
    ReadOnlyService service(std::make_unique<MockBroker>());service.connect();service.poll();
    CHECK(!service.trading_monitor_start_available());rejects([&]{service.start_trading_monitor("TEST_ACCOUNT");});
    CHECK(service.trading_accounts().status==SnapshotStatus::Unavailable);service.disconnect();CHECK(service.trading_monitor().state==TradingMonitorState::Unavailable);
}
struct Peer final : IBroker {
    TwsState session;
    TradingMonitorRequests requests;
    void connect() override { session.start(Clock::now()); session.ready(); session.managed_accounts({true,{"TEST_ACCOUNT"}}); }
    void disconnect() noexcept override { session.disconnect(); }
    ConnectionState state() const noexcept override { return session.state(); }
    RequestId subscribe(const Contract&) override { throw std::logic_error("unused"); }
    bool unsubscribe(RequestId) override { return false; }
    void request_positions(RequestId) override { throw std::logic_error("unused"); }
    RequestId start_trading_monitor(const std::string& account) override { requests=session.start_trading_monitor(account,Clock::now()); return requests.monitor; }
    void stop_trading_monitor() override { session.stop_trading_monitor(); }
    std::vector<BrokerEvent> poll() override { return session.poll(); }
};
void service_generations() {
    auto peer=std::make_unique<Peer>();auto* raw=peer.get();ReadOnlyService service(std::move(peer));
    service.connect();CHECK(service.trading_accounts().status==SnapshotStatus::Unavailable);service.poll();
    CHECK(service.trading_monitor_start_available());rejects([&]{service.start_trading_monitor("OTHER_ACCOUNT");});
    const auto id=service.start_trading_monitor("TEST_ACCOUNT");CHECK(id!=0 && service.trading_monitor_restart_requires_reconnect());
    raw->session.monitor_position(raw->requests.positions,"TEST_ACCOUNT",{contract(),"1",10});
    service.poll();CHECK(service.trading_monitor().positions.rows.empty());
    service.stop_trading_monitor();service.poll();CHECK(service.trading_monitor().state==TradingMonitorState::Stopped);
    CHECK(!service.trading_monitor_start_available());rejects([&]{service.start_trading_monitor("TEST_ACCOUNT");});
    const auto generation=service.generation();service.disconnect();CHECK(service.generation()>generation);
    CHECK(service.trading_accounts().status==SnapshotStatus::Unavailable && service.trading_monitor().state==TradingMonitorState::Unavailable);
    service.connect();service.poll();CHECK(service.trading_monitor_start_available());
    CHECK(service.start_trading_monitor("TEST_ACCOUNT")>id);
    raw->session.managed_accounts({true,{"OTHER_ACCOUNT"}});service.poll();
    CHECK(service.trading_monitor().state==TradingMonitorState::Failed);
}
int main() {try {model_tests();state_tests();service_tests();service_generations();std::cout<<"Read-only monitor model/state tests passed\n";}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}}
