"""Read-only, bounded 15-minute order history; no raw account response is exposed."""
from __future__ import annotations

from decimal import Decimal
import re

from .positions import PositionError, _account_ticker, _iso, _number, _stamp

_TICKER = re.compile(r'KXBTC15M-[A-Za-z0-9_.:-]{1,119}\Z')
_ID = re.compile(r'[A-Za-z0-9_.:-]{1,200}\Z')
_STATE = re.compile(r'[a-z_]{1,40}\Z')


def _identifier(value):
    if value is None or value == '':
        return None
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise PositionError('Order identifier is unavailable or invalid')
    return value


def _quantity(value):
    quantity = _number(value, maximum=Decimal('100000000'))
    if quantity.as_tuple().exponent < -2:
        raise PositionError('Order quantity precision is unsupported')
    return format(quantity, 'f')


def _price(value):
    if value is None:
        return None
    price = _number(value, maximum=Decimal('1'))
    return format(price, 'f')


def _direction(raw):
    """Keep canonical exposure independent of deprecated action/side aliases.

    Kalshi's canonical fields encode long YES (yes/bid) or long NO (no/ask).
    Pairing `outcome_side=no` with an old `action=sell` would reverse that bit.
    Preserve a legacy action/side presentation only when the complete pair
    agrees; otherwise display the equivalent outcome purchase explicitly.
    """
    canonical = []
    for field, choices in (('outcome_side', {'yes': 'yes', 'no': 'no'}),
                           ('book_side', {'bid': 'yes', 'ask': 'no'})):
        value = raw.get(field)
        if value is not None:
            if not isinstance(value, str) or value not in choices:
                raise PositionError('Order direction is unavailable or invalid')
            canonical.append(choices[value])
    if len(set(canonical)) > 1:
        raise PositionError('Canonical order directions disagree')
    side, action = raw.get('side'), raw.get('action')
    legacy = side in ('yes', 'no') and action in ('buy', 'sell')
    exposure = side if action == 'buy' else 'no' if side == 'yes' else 'yes'
    if canonical:
        if legacy and exposure == canonical[0]:
            return side, action, 'legacy_action_side'
        return canonical[0], 'buy', 'canonical_exposure'
    if legacy:
        return side, action, 'legacy_action_side'
    if side in ('bid', 'ask'):
        return 'yes' if side == 'bid' else 'no', 'buy', 'canonical_exposure'
    raise PositionError('Order direction is unavailable or invalid')


def _row(raw, *, paper):
    ticker = raw.get('ticker')
    if not (isinstance(ticker, str) and _TICKER.fullmatch(ticker) if paper else _account_ticker(ticker)):
        return None
    if paper:
        outcome, action, direction_basis = raw.get('outcome'), 'buy', 'paper_review'
    else:
        outcome, action, direction_basis = _direction(raw)
    if outcome not in ('yes', 'no'):
        raise PositionError('Order outcome is unavailable or invalid')
    status = raw.get('status')
    if not isinstance(status, str) or not _STATE.fullmatch(status):
        raise PositionError('Order state is unavailable or invalid')
    count = _quantity(raw.get('count') if paper else raw.get('initial_count_fp'))
    uncertain = paper and status in ('sending', 'unknown')
    filled = None if uncertain else _quantity(raw.get('fill_count') if paper else raw.get('fill_count_fp'))
    execution = raw.get('execution', {}) if paper else {}
    if not isinstance(execution, dict):
        raise PositionError('Order execution metadata is invalid')
    remaining = None if uncertain else (_quantity(execution.get('remaining_count', '0')) if paper
                                       else _quantity(raw.get('remaining_count_fp')))
    if Decimal(count) <= 0 or (filled is not None and Decimal(filled) + Decimal(remaining) > Decimal(count)):
        raise PositionError('Order quantities are inconsistent')
    created = raw.get('created_at') if paper else raw.get('created_time')
    created = None if created is None else _iso(_stamp(created))
    expiration = execution.get('expiration_time') if paper else raw.get('expiration_time')
    if expiration is not None:
        if not paper:
            expiration = int(_stamp(expiration))
        if type(expiration) is not int or not 0 < expiration <= 253402300799:
            raise PositionError('Order expiration is unavailable or invalid')
    return dict(order_id=_identifier(raw.get('order_id')),
                client_order_id=_identifier(raw.get('client_order_id')), ticker=ticker,
                outcome=outcome, action=action, direction_basis=direction_basis,
                count=count, fill_count=filled, remaining_count=remaining,
                price=_price(raw.get('price') if paper else raw.get(outcome + '_price_dollars')),
                status=status, created_at=created,
                order_type=execution.get('order_type', 'ioc') if paper else 'unknown',
                expiration_time=expiration, cancelable=False)


def build_order_report(client, intents, *, mode, environment, source, now):
    report = dict(kind='kalshi_order_report', version=1, mode=mode, environment=environment,
                  source=source, status='unavailable', fetched_at=None,
                  reason='Order history is unavailable', orders=[])
    try:
        if source not in ('paper_journal', 'kalshi_account'):
            raise PositionError('Unsupported order history source')
        if source == 'paper_journal':
            if not isinstance(intents, list) or len(intents) > 100000:
                raise PositionError('Order journal exceeds supported bounds')
            # A live journal is never mislabeled as simulated account history.
            rows = intents if mode == 'paper' else []
            more = len(rows) > 1000
            rows = rows[-1000:]
            reason = 'Paper journal intents only; these are simulated orders.'
        else:
            if client is None:
                raise PositionError('Account order access is not enabled for this console')
            if getattr(client, 'environment', environment) != environment:
                raise PositionError('Account order environment differs from the console')
            page = client.get_account_order_page()
            if (not isinstance(page, dict) or not isinstance(page.get('orders'), list)
                    or len(page['orders']) > 1000 or not isinstance(page.get('cursor', ''), str)):
                raise PositionError('Account order history page is incomplete')
            # The account-display reader explicitly requests primary account
            # zero. A conflicting scope invalidates the whole page, including
            # any earlier valid rows; silently filtering it could hide an
            # accidentally unscoped cross-account acquisition.
            for raw in page['orders']:
                if isinstance(raw, dict):
                    for field in ('subaccount_number', 'subaccount'):
                        # The canonical venue alias is nullable. The explicit
                        # request establishes scope when an alias is unknown.
                        if raw.get(field) is not None and (type(raw[field]) is not int or raw[field] != 0):
                            raise PositionError('Account order history differs from primary-account scope')
            rows, more = page['orders'], bool(page.get('cursor'))
            report.update(account_scope='primary_subaccount_0', account_scope_verified=True)
            reason = ('Available supported 15-minute account orders (BTC, ETH, SOL, XRP and Gold); '
                      'excludes archived order history.')
        local = {intent.get('order_id'): intent for intent in intents
                 if isinstance(intent, dict) and intent.get('order_id')} if isinstance(intents, list) else {}
        invalid, seen = 0, set()
        for raw in rows:
            try:
                if not isinstance(raw, dict):
                    raise PositionError('Invalid order row')
                row = _row(raw, paper=source == 'paper_journal')
                if row is None:
                    continue
                identity = row['order_id'] or row['client_order_id']
                if identity is None or identity in seen:
                    raise PositionError('Duplicate or missing order identifier')
                owned = raw if source == 'paper_journal' else local.get(row['order_id'])
                if isinstance(owned, dict) and _TICKER.fullmatch(row['ticker']):
                    execution = owned.get('execution', {})
                    if (isinstance(execution, dict) and execution.get('order_type') == 'limit'
                            and owned.get('ticker') == row['ticker']
                            and owned.get('client_order_id') == row['client_order_id']):
                        row['order_type'] = 'limit'
                        row['expiration_time'] = execution.get('expiration_time')
                        # V2 uses YES-book asks for a purchase of NO. Present
                        # the journal-owned purchase in its reviewed outcome.
                        row['outcome'] = owned['outcome']
                        row['action'] = 'buy'
                        row['direction_basis'] = 'journal_review'
                        row['price'] = _price(owned['price'])
                        row['cancelable'] = (owned.get('status') in ('resting', 'paper_resting')
                                             and not execution.get('cancel_requested', False)
                                             and row['status'] in ('resting', 'paper_resting')
                                             and row['remaining_count'] is not None
                                             and Decimal(row['remaining_count']) > 0)
                seen.add(identity)
                report['orders'].append(row)
            except PositionError:
                invalid += 1
        report['orders'].sort(key=lambda row: row['created_at'] or '', reverse=True)
        if more:
            reason += ' Display is limited to one thousand returned account records.'
        if invalid:
            reason += f' {invalid} invalid order record(s) were omitted.'
        if any(row['direction_basis'] == 'canonical_exposure' for row in report['orders']):
            reason += (' Canonical YES/NO exposure is shown as an equivalent BUY when the '
                       'deprecated action/side pair is absent or disagrees; the price belongs to that outcome.')
        report.update(status='partial' if more or invalid else 'ready', fetched_at=_iso(now), reason=reason)
    except PositionError as error:
        report['reason'] = str(error)
    except Exception:
        report['reason'] = 'Account or journal order history is unavailable'
    return report
