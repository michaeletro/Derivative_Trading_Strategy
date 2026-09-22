#include <dts/tws_state.hpp>
#include <dts/read_only_service.hpp>
#include <functional>
#include <iostream>
#include <limits>
#define CHECK(x) do { if (!(x)) throw std::runtime_error(#x); } while (false)
template<class F> void rejects(F fn) { bool threw = false; try { fn(); } catch (const std::exception&) { threw = true; } CHECK(threw); }
dts::Contract stock(std::int64_t id = 1) {
    dts::Contract c; c.id = id; c.symbol = "TEST"; c.exchange = "SMART"; c.currency = "USD"; c.multiplier = 1; return c;
}
dts::ContractQuery query() { dts::ContractQuery q; q.symbol = "TEST"; return q; }
struct FixtureBroker : dts::IBroker {
    dts::TwsState s;
    void connect() override { s.start(dts::Clock::now()); s.ready(); }
    void disconnect() noexcept override { s.disconnect(); }
    dts::ConnectionState state() const noexcept override { return s.state(); }
    dts::RequestId resolve(const dts::ContractQuery& q) override { return s.resolve(q, dts::Clock::now()); }
    dts::RequestId subscribe(const dts::Contract& c) override { return s.subscribe(c, dts::Clock::now()); }
    bool unsubscribe(dts::RequestId id) override { return s.unsubscribe(id); }
    void request_positions(dts::RequestId id) override { s.request_positions(id, dts::Clock::now()); }
    std::vector<dts::BrokerEvent> poll() override { return s.poll(); }
};
int main() {
    using namespace dts;
    const auto now = Clock::now();
    int passed = 0;
    const auto test = [&](const char* name, const std::function<void()>& fn) {
        try { fn(); ++passed; std::cout << "PASS " << name << '\n'; }
        catch (const std::exception& e) { std::cerr << "FAIL " << name << ": " << e.what() << '\n'; std::exit(1); }
    };
    test("handshake gate", [&] { TwsState s; s.start(now); rejects([&] { s.subscribe(stock(), now); }); s.ready(); CHECK(s.state() == ConnectionState::Ready); });
    test("handshake deadline", [&] { TwsState s(std::chrono::milliseconds(20)); s.start(now); s.expire(now + std::chrono::milliseconds(20)); CHECK(s.state() == ConnectionState::Failed); });
    test("no unclassified quote", [&] { TwsState s; s.start(now); s.ready(); auto id = s.subscribe(stock(), now); s.poll(); s.price(id, 1, 99, now); CHECK(s.poll().empty()); });
    test("per-side ages", [&] { TwsState s; s.start(now); s.ready(); auto id = s.subscribe(stock(), now); s.data_type(id, 1); s.price(id, 1, 99, now); s.price(id, 2, 101, now + std::chrono::seconds(6)); auto e = s.poll(); CHECK(!std::get<Quote>(e.back()).mid(now + std::chrono::seconds(6), std::chrono::seconds(5))); });
    test("delayed identification", [&] { TwsState s; s.start(now); s.ready(); auto id = s.subscribe(stock(), now); s.price(id, 66, 99, now); s.price(id, 67, 101, now); auto e=s.poll(); auto q=std::get<Quote>(e.back()); CHECK(q.data_type == MarketDataType::Delayed); CHECK(q.mid(now, std::chrono::seconds(5)) == 100); });
    test("feed change invalidates sides", [&] { TwsState s; s.start(now); s.ready(); auto id=s.subscribe(stock(),now); s.data_type(id,1); s.price(id,1,99,now); s.price(id,2,101,now); s.data_type(id,2); auto e=s.poll(); const auto& q=std::get<Quote>(e.back()); CHECK(!q.bid && !q.ask && q.data_type==MarketDataType::Frozen); });
    test("unavailable price clears side", [&] { TwsState s; s.start(now); s.ready(); auto id=s.subscribe(stock(),now); s.data_type(id,1); s.price(id,1,99,now); s.price(id,1,-1,now); auto e=s.poll(); CHECK(!std::get<Quote>(e.back()).bid); });
    test("unsubscribe purges pending quote", [&] { TwsState s; s.start(now); s.ready(); auto id=s.subscribe(stock(),now); s.poll(); s.data_type(id,1); s.price(id,1,99,now); CHECK(s.unsubscribe(id)); CHECK(s.poll().empty()); });
    test("IDs survive reconnect", [&] { TwsState s; s.start(now); s.ready(); auto a=s.subscribe(stock(),now); s.disconnect(); s.start(now); s.ready(); CHECK(s.subscribe(stock(),now)>a); });
    test("contract completion and duplicates", [&] { TwsState s; s.start(now); s.ready(); auto id=s.resolve(query(),now); s.poll(); s.contract(id,stock()); s.contract(id,stock()); s.contract_end(id); auto e=s.poll(); CHECK(e.size()==2); CHECK(std::get<ContractsComplete>(e.back()).success); });
    test("empty resolution fails", [&] { TwsState s; s.start(now); s.ready(); auto id=s.resolve(query(),now); s.contract_end(id); auto e=s.poll(); CHECK(!std::get<ContractsComplete>(e.back()).success); });
    test("resolution timeout", [&] { TwsState s; s.start(now); s.ready(); auto id=s.resolve(query(),now); s.expire(now+std::chrono::seconds(11)); auto e=s.poll(); CHECK(std::get<ContractsComplete>(e.back()).request_id==id); CHECK(!std::get<ContractsComplete>(e.back()).success); });
    test("one position snapshot per connection", [&] { TwsState s; s.start(now); s.ready(); s.request_positions(10,now); s.position_end(); rejects([&]{s.request_positions(11,now);}); });
    test("position timeout is failure", [&] { TwsState s; s.start(now); s.ready(); s.request_positions(10,now); s.expire(now+std::chrono::seconds(11)); auto e=s.poll(); CHECK(!std::get<PositionsComplete>(e.back()).success); });
    test("unsupported position poisons snapshot", [&] { TwsState s; s.start(now); s.ready(); s.request_positions(10,now); s.bad_position("Unsupported FUT"); s.position_end(); auto e=s.poll(); CHECK(!std::get<PositionsComplete>(e.back()).success); });
    test("connectivity loss fails closed", [&] { TwsState s; s.start(now); s.ready(); s.subscribe(stock(),now); s.error(0,1100,"lost"); CHECK(s.state()==ConnectionState::Failed); rejects([&]{s.subscribe(stock(),now);}); });
    test("pacing bound", [&] { TwsState s; s.start(now); s.ready(); for(int i=0;i<10;++i)s.resolve(query(),now); rejects([&]{s.resolve(query(),now);}); });
    test("event overflow invalidates session", [&] { TwsState s; s.start(now); s.ready(); auto id=s.subscribe(stock(),now); s.data_type(id,1); for(int i=0;i<2100;++i)s.price(id,1,99,now); CHECK(s.state()==ConnectionState::Failed); });
    test("disabled means unavailable", [&] { ReadOnlyService svc(nullptr); CHECK(svc.positions().status==SnapshotStatus::Unavailable); rejects([&]{svc.connect();}); });
    test("publish positions only after completion", [&] { auto b=std::make_unique<FixtureBroker>(); auto* p=b.get(); ReadOnlyService svc(std::move(b)); svc.connect(); svc.request_positions(); p->s.position(Position{"TEST_ACCOUNT",stock(),2}); svc.poll(); CHECK(svc.positions().status==SnapshotStatus::Pending && svc.positions().positions.empty()); p->s.position_end(); svc.poll(); CHECK(svc.positions().status==SnapshotStatus::Complete && svc.positions().positions.size()==1); svc.disconnect(); CHECK(svc.positions().status==SnapshotStatus::Unavailable); });
    test("failed partial positions never committed", [&] { auto b=std::make_unique<FixtureBroker>(); auto* p=b.get(); ReadOnlyService svc(std::move(b)); svc.connect(); svc.request_positions(); p->s.position(Position{"TEST",stock(),2}); p->s.bad_position("Unsupported"); p->s.position_end(); svc.poll(); CHECK(svc.positions().status==SnapshotStatus::Failed && svc.positions().positions.empty()); });
    test("ambiguous resolution requires explicit conId", [&] { auto b=std::make_unique<FixtureBroker>(); auto* p=b.get(); ReadOnlyService svc(std::move(b)); svc.connect(); auto id=svc.resolve(query()); p->s.contract(id,stock(1)); p->s.contract(id,stock(2)); svc.poll(); rejects([&]{svc.subscribe(1);}); p->s.contract_end(id); svc.poll(); CHECK(svc.resolution(id).contracts.size()==2); CHECK(svc.subscribe(2)>0); rejects([&]{svc.subscribe(999);}); });
    test("failure clears cached marks", [&] { auto b=std::make_unique<FixtureBroker>(); auto* p=b.get(); ReadOnlyService svc(std::move(b)); svc.connect(); auto id=svc.resolve(query()); p->s.contract(id,stock()); p->s.contract_end(id); svc.poll(); auto sub=svc.subscribe(1); p->s.data_type(sub,1); p->s.price(sub,1,99,now); svc.poll(); CHECK(svc.quote(1).bid.has_value()); p->s.error(0,1100,"lost"); svc.poll(); rejects([&]{(void)svc.quote(1);}); });
    std::cout << passed << " state/service cases passed\n";
}
