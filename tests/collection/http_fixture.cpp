// Production Application/routes with a deliberately synthetic, socket-free peer.
// This executable is a test target, never installed or selected by the launcher.
#include <dts/tws_state.hpp>
#include <memory>
#include "../../src/backend/cpp_src/src/Utilities/server.cpp"

namespace {
class CollectionPeer final : public dts::IBroker {
    dts::TwsState state_;
    dts::RequestId depth_ = 0;
    dts::Clock::time_point next_{};
public:
    void connect() override { state_.start(dts::Clock::now());state_.ready(); }
    void disconnect() noexcept override { state_.disconnect();depth_=0; }
    dts::ConnectionState state() const noexcept override { return state_.state(); }
    dts::RequestId resolve(const dts::ContractQuery& query) override {
        if(query.symbol!="SYNTHETIC" || query.currency!="USD")throw std::invalid_argument("Fixture symbol only");
        const auto id=state_.resolve(query,dts::Clock::now());
        dts::Contract contract;contract.id=9001;contract.symbol="SYNTHETIC";contract.currency="USD";contract.exchange="BATS";contract.multiplier=1;
        state_.contract(id,contract);state_.contract_end(id);return id;
    }
    dts::RequestId subscribe(const dts::Contract&) override { throw std::logic_error("Depth fixture only"); }
    bool unsubscribe(dts::RequestId) override { return false; }
    void request_positions(dts::RequestId) override { throw std::logic_error("No account fixture"); }
    void update(int side,int pos,int op) {
        dts::DepthEvent event;event.request_id=depth_;event.side=side;event.position=pos;event.operation=op;
        event.price=side==0?101.+pos:99.-pos;event.size="100";event.origin="synthetic_collection_fixture";
        event.callback_format="synthetic_fixture";state_.depth_update(event);
    }
    dts::RequestId subscribe_depth(const dts::DepthSpec& spec) override {
        depth_=state_.depth(spec,dts::Clock::now());
        for(int side=0;side<2;++side)for(int pos=0;pos<spec.rows;++pos)update(side,pos,0);
        next_=dts::Clock::now()+std::chrono::milliseconds(100);return depth_;
    }
    bool unsubscribe_depth(dts::RequestId id) override {
        const auto cancelled=state_.cancel_depth(id);if(cancelled)depth_=0;return cancelled;
    }
    std::vector<dts::BrokerEvent> poll() override {
        if(depth_ && state_.state()==dts::ConnectionState::Ready && dts::Clock::now()>=next_) {
            update(0,0,1);update(1,0,1);next_=dts::Clock::now()+std::chrono::milliseconds(100);
        }
        return state_.poll();
    }
};
}
int main() {
    try { Application application(std::make_unique<CollectionPeer>());application.run();return 0; }
    catch(const std::exception& error) { std::cerr<<error.what()<<'\n';return 1; }
}
