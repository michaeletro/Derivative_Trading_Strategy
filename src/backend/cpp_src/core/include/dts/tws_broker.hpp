#pragma once
#include "broker.hpp"
#include <memory>

namespace dts {
struct TwsConfig {
    std::string host = "127.0.0.1";
    int port = 4002;
    int client_id = 17;
    int market_data_type = 3; // Request delayed data; report the ACTUAL returned type.
    std::chrono::milliseconds timeout{10000};
};
// Native official-SDK adapter. No IBKR types cross this header; no order methods.
class TwsBroker final : public IBroker {
public:
    explicit TwsBroker(TwsConfig config = {});
    ~TwsBroker() override;
    TwsBroker(const TwsBroker&) = delete;
    TwsBroker& operator=(const TwsBroker&) = delete;
    void connect() override;
    void disconnect() noexcept override;
    ConnectionState state() const noexcept override;
    RequestId resolve(const ContractQuery&) override;
    RequestId subscribe(const Contract&) override;
    bool unsubscribe(RequestId) override;
    void request_positions(RequestId) override;
    std::vector<BrokerEvent> poll() override;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace dts
