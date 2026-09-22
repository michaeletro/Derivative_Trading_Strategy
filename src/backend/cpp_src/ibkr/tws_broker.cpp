#include <dts/tws_broker.hpp>
#include "tws_callbacks.hpp"
#include "EClientSocket.h"
#include "EReader.h"
#include "EReaderOSSignal.h"
#include <arpa/inet.h>

namespace dts {
struct TwsBroker::Impl {
    TwsConfig config;
    TwsState session;
    ibkr_detail::Callbacks callbacks;
    EReaderOSSignal signal{20};
    std::unique_ptr<EClientSocket> client;
    std::unique_ptr<EReader> reader;
    bool started_api = false;
    explicit Impl(TwsConfig c) : config(std::move(c)), session(config.timeout) {
        if (config.host == "localhost") config.host = "127.0.0.1";
        in_addr address{};
        if (inet_pton(AF_INET, config.host.c_str(), &address) != 1)
            throw std::invalid_argument("IB_HOST must be a numeric IPv4 address or localhost");
        if (config.port < 1 || config.port > 65535 || config.client_id <= 0 ||
            config.market_data_type < 1 || config.market_data_type > 4)
            throw std::invalid_argument("Invalid TWS connection configuration");
    }
    void shutdown() noexcept {
        if (client) client->eDisconnect();
        signal.issueSignal();
        reader.reset(); // Join SDK reader before destroying callback/client state.
        client.reset(); callbacks.clear(); started_api = false;
    }
};
TwsBroker::TwsBroker(TwsConfig config) : impl_(std::make_unique<Impl>(std::move(config))) {}
TwsBroker::~TwsBroker() { disconnect(); }
void TwsBroker::connect() {
    auto& p = *impl_;
    if (p.session.state() == ConnectionState::Ready || p.session.state() == ConnectionState::Connecting) return;
    p.shutdown(); p.session.start(Clock::now());
    try {
        p.client = std::make_unique<EClientSocket>(&p.callbacks, &p.signal);
        p.client->asyncEConnect(true);
        if (!p.client->eConnect(p.config.host.c_str(), p.config.port, p.config.client_id)) {
            p.session.fail(502, "Cannot connect to TWS/IB Gateway"); p.shutdown(); return;
        }
        p.reader = std::make_unique<EReader>(p.client.get(), &p.signal);
        p.reader->start();
    } catch (...) { p.shutdown(); p.session.fail(-1013, "TWS connection setup failed"); throw; }
}
void TwsBroker::disconnect() noexcept { impl_->shutdown(); impl_->session.disconnect(); }
ConnectionState TwsBroker::state() const noexcept { return impl_->session.state(); }
RequestId TwsBroker::resolve(const ContractQuery& query) {
    const auto native = ibkr_detail::to_native(query);
    const auto id = impl_->session.resolve(query, Clock::now());
    impl_->client->reqContractDetails(static_cast<int>(id), native); return id;
}
RequestId TwsBroker::subscribe(const Contract& contract) {
    const auto native = ibkr_detail::to_native(contract);
    const auto id = impl_->session.subscribe(contract, Clock::now());
    impl_->client->reqMktData(static_cast<int>(id), native, "", false, false, TagValueListSPtr{});
    return id;
}
bool TwsBroker::unsubscribe(RequestId id) {
    if (!impl_->session.unsubscribe(id)) return false;
    if (impl_->client && impl_->client->isConnected()) impl_->client->cancelMktData(static_cast<int>(id));
    return true;
}
void TwsBroker::request_positions(RequestId id) {
    impl_->session.request_positions(id, Clock::now()); impl_->client->reqPositions();
}
std::vector<BrokerEvent> TwsBroker::poll() {
    auto& p = *impl_;
    const auto before = p.session.state();
    const bool positions_were_pending = p.session.positions_pending();
    try {
        if (p.reader) p.reader->processMsgs();
        if (p.callbacks.acknowledged.exchange(false) && p.client && !p.started_api) {
            p.client->startApi(); p.started_api = true;
        }
        p.callbacks.drain(p.session);
        p.session.expire(Clock::now());
        if (before != ConnectionState::Ready && p.session.state() == ConnectionState::Ready)
            p.client->reqMarketDataType(p.config.market_data_type);
        if (positions_were_pending && !p.session.positions_pending() && p.client && p.client->isConnected())
            p.client->cancelPositions();
        if (p.session.state() == ConnectionState::Failed) p.shutdown();
    } catch (const std::exception&) {
        p.session.fail(-1014, "TWS message processing failed"); p.shutdown();
    }
    return p.session.poll();
}
} // namespace dts
