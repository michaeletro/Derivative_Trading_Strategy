# Execution rule planner

Trading status at `/#trading` includes a stateless **Execution rule planner**.
Load rules explicitly, select a preset, enter a hypothetical instrument and
account state, and choose **Preview scenario**. It calculates whole-share sizing
and explains blocked scenarios. It neither imports actual brokerage risk nor
sends orders. All presets are disabled; paper/live names do not select or certify
an account. These are provisional research defaults, not an investment
recommendation or an entry/exit strategy.

| Preset | Owner-declared balance | Scenario budget | Cash reserve | New buy cap | Gross exposure cap | Daily new-buy loss gate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Live planning | $500 | $500 | $50 | $100 | $450 | $5 |
| Paper capital mirror | $1,000,000 | $500 | $50 | $100 | $450 | $5 |
| Paper research | $1,000,000 | $1,000,000 | $900,000 | $10,000 | $100,000 | $1,000 |

The paper capital mirror tests small-account affordability despite the simulator's
larger balance. Paper research permits larger experiments, whose fills do not
establish suitability for the $500 account. Owner-declared balances are planning
assumptions and remain unverified.

The initial scope is USD stocks/ETFs, positive explicitly supplied conIds,
long-only whole shares, and LMT DAY during regular trading hours. No borrowing,
shorting, options or fractional-share fallback is provided. Instrument eligibility
is a scenario declaration, not a broker permission or an authoritative allowlist.
Supply price increments in cents; subcent contracts are unsupported. BATS
collection is independent of any future order route: an observed BZX book does
not select an execution venue.

Shared limits are 20 new-buy decisions per day, one outstanding order, maximum
2-second quote age, maximum 5-second risk-state age and a 10-basis-point spread.
The same tolerance bounds aggressively priced buy limits above the ask and sell
limits below the bid. Locked/crossed, stale, delayed or missing quotes are blocked.
Ages are supplied scenario values, not measured broker timestamps. The $2 fee
reserve is hypothetical, not an estimate of IBKR commissions or exchange fees.

For a BUY at limit price `P`, quantity is the requested ceiling bounded by:

```text
floor(min(order cap,
          gross cap - current committed gross,
          available settled cash - cash reserve - hypothetical fee) / P)
```

Available cash must already be net of outstanding buy commitments and settlement
restrictions. Committed gross includes all holdings and outstanding buys. Missing
risk state must be marked unknown, never zero. The day-loss gate uses realized
plus marked unrealized change from the session baseline, including fees. It blocks
new buys when reached; it is not a guaranteed maximum loss, an enforced stop order
or automatic liquidation.

SELL quantity cannot exceed available whole long shares after outstanding sells.
Entry cash, loss and buy-count limits do not prevent reductions; instrument,
quote/state freshness, price collar, session and outstanding-order checks still
apply. SELL totals are gross proceeds before fees; BUY totals include the
hypothetical fee. A hypothetical $700 SPY share is blocked by the small-account
$100 order cap. No alternative instrument or leverage is selected automatically.

## Automatic transmission remains a separate implementation

IBKR's `Order.transmit=true` forwards an API order through TWS. Read-Only API blocks
modifications; ordinary transmission and precaution warnings are separate
controls. This planner changes neither. Blanket `bypassOrderPrecautions` is not a
prerequisite for this increment. Live activation remains owner-controlled after
execution implementation and validation.

Actual transmission still requires an entry/exit strategy, verified account
identity and restrictions, actual settled USD cash/P&L, portfolio-wide reservations,
contract price/quantity increments, fee/margin previews, durable order IDs and
intent/send journaling, continuous order/fill reconciliation, uncertain-outcome
recovery, and ownership-aware stop/cancel controls. The current read monitor's
order snapshot and periodic account summary do not supply these inputs.
Forecasting RV/BPV alone does not define trade direction or exits.

Future SDK integration must retain one broker owner and event poller. Journal
failure or stale state must stop new submissions without preventing order callback
draining or cancellation of owned orders. Restart/reconnect must disarm; an
uncertain send must never be retried blindly. Strategy stop, order cancellation
and liquidation remain separate actions.

Official references:

- [Order.transmit](https://www.interactivebrokers.com/docs/tws-api/ref/order)
- [Read-Only API settings](https://www.interactivebrokers.com/docs/tws-api/protobuf/api-settings-config)
- [Order precautions](https://www.interactivebrokers.com/docs/tws-api/protobuf/api-precautions-config)
- [API order-type restrictions](https://www.interactivebrokers.com/campus/?p=195739&post_type=ibkr-api-page)
- [Paper fill limitations](https://www.ibkrguides.com/clientportal/aboutpapertradingaccounts.htm)

## API and validation

Authenticated `GET /api/trading/rules` returns presets; authenticated
`POST /api/trading/rules/preview` accepts the exact scenario schema. No account
number or credential is accepted. Integer cents/counts, explicit Boolean flags,
bounded text and positive decimal conId strings are required. Unknown, missing
and duplicate fields are rejected. There is no persistence, broker call, order
submission endpoint, strategy activation or market-archive change.

Core tests exercise sizing/loss boundaries, locked/crossed/stale quotes, price
increments, ownership and overflow. HTTP tests run the actual native executable
with disposable data and check access protection, invalid input and absence of
orders. Frontend/browser tests use labeled protocol fixtures. Passing checks
establishes software behavior, not live executions or trading performance.
