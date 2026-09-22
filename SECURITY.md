# Security and operational boundaries

This repository is a research prototype, not an approved live-trading system.

## Exposed provider credential

An earlier commit contained a hard-coded Polygon credential in
`src/backend/Arch/python_src/Utilities/Utilities_Resources.py`.
The development branch replaces that literal with an environment lookup.
**The owner must revoke/rotate the exposed key at the provider.** Changing code,
adding .gitignore, opening a PR, or deleting a file does not revoke a key.
The credential also remains in earlier commits and on branches not yet updated.
No history rewrite is performed by this change. Coordinate any later history
cleanup with collaborators after revocation. Do not paste replacement secrets
into issues, pull requests, terminal transcripts, or committed configuration.

## Current defaults

The legacy HTTP service binds to IPv4 loopback. `POST /ib/send` returns HTTP 403
and never forwards a payload. Client Portal WebSocket startup is off by default.
The new C++ `IBroker` interface is read-only; `MockBroker` cannot connect to IBKR
or submit orders. The optional TWS probe only completes a handshake and requests
server time; it does not request positions, market data, or submit orders.
A port number is not proof that an account is a paper account.

## Remaining work before enabling broker services

The retained experimental Client Portal transport still needs verified TLS
certificate/hostname handling, an authenticated brokerage session state machine,
bounded shutdown/reconnect, and sanitized logging. Do not enable it against a
live account or expose its surrounding service to a network. The legacy server
still needs application-owned state, concurrency review, authentication, request
size limits, and end-to-end tests. Loopback binding alone is not authentication.

Before execution is implemented, require explicit paper-account identity checks,
reconciliation of positions/open orders/executions, persistent client order IDs,
duplicate suppression, quote-age/type checks, buying-power and exposure limits,
and tested cancel/kill-switch behavior. Timeouts must never trigger blind order
resubmission. Never put broker logins or two-factor secrets in this repository.
