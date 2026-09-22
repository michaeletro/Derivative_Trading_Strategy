#pragma once
#include "domain.hpp"
#include "historical.hpp"
#include <variant>
#include <vector>

namespace dts {
enum class ConnectionState { Disconnected, Connecting, Ready, Failed };
struct ConnectionEvent { ConnectionState state; };
struct PositionsComplete { RequestId request_id; bool success = true; };
struct PositionEvent { RequestId request_id; Position position; };
struct BrokerError { RequestId request_id; int code; std::string message; };
struct ContractEvent { RequestId request_id; Contract contract; };
struct ContractsComplete { RequestId request_id; bool success; };
using BrokerEvent = std::variant<ConnectionEvent, Quote, PositionEvent,
    PositionsComplete, BrokerError, ContractEvent, ContractsComplete, HistoricalBarEvent, HistoricalEnd>;

struct ContractQuery {
    std::string symbol;
    std::string exchange = "SMART";
    std::string currency = "USD";
    std::string primary_exchange;
    SecurityType security_type = SecurityType::Equity;
    std::optional<OptionTerms> option;
    void validate() const {
        for (const auto* text : {&symbol, &exchange, &currency, &primary_exchange}) {
            if (text->size() > 64 || text->find('\0') != std::string::npos)
                throw std::invalid_argument("Invalid contract query text");
        }
        Contract c;
        c.id = 1; c.symbol = symbol; c.exchange = exchange; c.currency = currency;
        c.multiplier = 1; c.security_type = security_type; c.option = option;
        c.validate();
    }
};

// Externally serialize ALL calls (including poll/state). Callbacks must only
// enqueue events, never call application/pricing code. No execution capability.
class IBroker {
public:
    virtual ~IBroker() = default;
    virtual void connect() = 0;
    virtual void disconnect() noexcept = 0;
    virtual ConnectionState state() const noexcept = 0;
    virtual RequestId resolve(const ContractQuery&) {
        throw std::logic_error("Contract resolution is not supported by this broker");
    }
    virtual RequestId request_history(const HistorySpec&, HistoryWindow) {
        throw std::logic_error("Historical acquisition is not supported by this adapter");
    }
    virtual void cancel_history(RequestId) {}
    virtual RequestId subscribe(const Contract& contract) = 0;
    virtual bool unsubscribe(RequestId subscription_id) = 0;
    virtual void request_positions(RequestId request_id) = 0;
    virtual std::vector<BrokerEvent> poll() = 0;
};
} // namespace dts
