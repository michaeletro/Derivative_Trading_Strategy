#pragma once

#include "domain.hpp"
#include <variant>
#include <vector>

namespace dts {
enum class ConnectionState { Disconnected, Connecting, Ready, Failed };
struct ConnectionEvent { ConnectionState state; };
struct PositionsComplete { RequestId request_id; };
struct PositionEvent { RequestId request_id; Position position; };
struct BrokerError { RequestId request_id; int code; std::string message; };
using BrokerEvent = std::variant<ConnectionEvent, Quote, PositionEvent, PositionsComplete, BrokerError>;

// Read-only capability. Deliberately excludes execution and IBKR SDK types.
// All public calls belong to one application thread. Future network adapters
// must marshal callback-thread data into a synchronized event queue; they must
// not execute pricing or strategy code from the broker callback thread.
class IBroker {
public:
    virtual ~IBroker() = default;
    virtual void connect() = 0;
    virtual void disconnect() noexcept = 0;
    virtual ConnectionState state() const noexcept = 0;
    virtual RequestId subscribe(const Contract& contract) = 0;
    virtual bool unsubscribe(RequestId subscription_id) = 0;
    virtual void request_positions(RequestId request_id) = 0;
    virtual std::vector<BrokerEvent> poll() = 0;
};
} // namespace dts
