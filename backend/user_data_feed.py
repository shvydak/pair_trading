"""
Binance Futures User Data Stream — real-time order execution reports.

Consumed by order_manager to receive instant fill notifications instead of
polling fetch_order() every 2 s.

Flow:
  1. start() obtains a listen key via REST, spawns WS task + keepalive task.
  2. WS receives ORDER_TRADE_UPDATE and ACCOUNT_UPDATE events.
  3. ORDER_TRADE_UPDATE: updates _fill_data and notifies order waiters.
  4. ACCOUNT_UPDATE: fires callbacks for LIQUIDATION / ADL / FUNDING_FEE events.
  5. order_manager calls wait_for_order_update() instead of asyncio.sleep().
  6. Auto-reconnect with exponential backoff; listen key refreshed on reconnect.
  7. Keepalive task extends listen key every 30 min (Binance expires it at 60 min).

Gracefully disabled when no API credentials are present.
"""
import asyncio
import json
from typing import Callable, Optional

import aiohttp

from logger import get_logger

log = get_logger("user_data_feed")

# Binance WS order status → ccxt-compatible status (matches LegState.absorb_order)
_STATUS_MAP: dict[str, str] = {
    "NEW": "open",
    "PARTIALLY_FILLED": "open",
    "FILLED": "closed",
    "CANCELED": "canceled",
    "EXPIRED": "canceled",
    "CALCULATED": "canceled",  # liquidation/ADL
    "NEW_INSURANCE": "open",
    "NEW_ADL": "open",
}


class UserDataFeed:
    """
    Binance Futures User Data Stream.

    Receives ORDER_TRADE_UPDATE events and notifies registered waiters
    using the same "replace event" pattern as SymbolFeed.

    Requires API credentials — gracefully disabled (start() returns False) if none.
    """

    WS_BASE = "wss://fstream.binance.com/ws"
    KEEPALIVE_INTERVAL = 1800  # 30 min; listen key expires after 60 min

    def __init__(self, client) -> None:
        self._client = client
        self._listen_key: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._stopped: bool = False
        # Order IDs we are watching (string form)
        self._watched: set[str] = set()
        # Latest fill snapshot per order_id — ccxt-compatible dict
        self._fill_data: dict[str, dict] = {}
        # "Replace event" pattern — same as SymbolFeed
        self._generation: int = 0
        self._update_event: asyncio.Event = asyncio.Event()
        # ACCOUNT_UPDATE callbacks
        self._liquidation_callbacks: list[Callable] = []
        self._adl_callbacks: list[Callable] = []
        self._funding_callbacks: list[Callable] = []

    # ── public API ────────────────────────────────────────────────────────────

    async def start(self) -> bool:
        """
        Obtain a listen key and start WS + keepalive tasks.
        Returns False if no credentials are available (graceful no-op).
        """
        if self._client is None or not getattr(self._client, "has_creds", False):
            log.info("UserDataFeed: no credentials — disabled")
            return False
        lk = await self._fetch_listen_key()
        if not lk:
            return False
        self._listen_key = lk
        self._stopped = False
        self._task = asyncio.create_task(self._run(), name="user-data-feed")
        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop(), name="user-data-keepalive"
        )
        log.info("UserDataFeed: started")
        return True

    def stop(self) -> None:
        """Request graceful shutdown."""
        self._stopped = True
        for t in (self._task, self._keepalive_task):
            if t and not t.done():
                t.cancel()

    def register_order(self, order_id: str) -> None:
        """Start watching an order. Call immediately after placing it."""
        self._watched.add(str(order_id))

    def unregister_order(self, order_id: str) -> None:
        """Stop watching an order and discard its fill data."""
        oid = str(order_id)
        self._watched.discard(oid)
        self._fill_data.pop(oid, None)

    def on_liquidation(self, callback: Callable) -> None:
        """Register callback(symbol, position_amount) for liquidation events."""
        self._liquidation_callbacks.append(callback)

    def on_adl(self, callback: Callable) -> None:
        """Register callback(symbol, position_amount) for ADL events."""
        self._adl_callbacks.append(callback)

    def on_funding(self, callback: Callable) -> None:
        """Register callback(asset, amount) for funding fee events."""
        self._funding_callbacks.append(callback)

    def get_fill_data(self, order_id: str) -> Optional[dict]:
        """
        Return latest fill snapshot as a ccxt-compatible dict, or None if
        no WS update has arrived yet for this order.
        Keys: id, status, filled, remaining, amount, average
        """
        return self._fill_data.get(str(order_id))

    def get_generation(self) -> int:
        return self._generation

    async def wait_for_order_update(self, after_gen: int = -1) -> int:
        """
        Await the next order fill event after generation `after_gen`.
        Returns the new generation number.
        Same "replace event" pattern as SymbolFeed.wait_for_update().
        """
        while self._generation <= after_gen:
            evt = self._update_event
            await evt.wait()
        return self._generation

    # ── internals ─────────────────────────────────────────────────────────────

    def _notify(self) -> None:
        """Increment generation and wake all current waiters."""
        self._generation += 1
        old = self._update_event
        self._update_event = asyncio.Event()
        old.set()

    def _handle_order_update(self, order_data: dict) -> None:
        """Process the 'o' object from an ORDER_TRADE_UPDATE event."""
        order_id = str(order_data.get("i", ""))
        if order_id not in self._watched:
            return

        ws_status = order_data.get("X", "NEW")
        ccxt_status = _STATUS_MAP.get(ws_status, "open")

        qty = float(order_data.get("q", 0) or 0)
        filled = float(order_data.get("z", 0) or 0)
        avg_raw = order_data.get("ap", "0") or "0"
        avg_price: Optional[float] = float(avg_raw) if avg_raw != "0" else None

        # "n" is the per-fill commission delta; accumulate across fills for this order
        commission_delta = float(order_data.get("n", 0) or 0)
        commission_asset = order_data.get("N", "") or ""
        prev = self._fill_data.get(order_id, {})
        cumulative_commission = prev.get("commission", 0.0) + commission_delta

        self._fill_data[order_id] = {
            "id": order_id,
            "status": ccxt_status,
            "filled": filled,
            "remaining": max(0.0, qty - filled),
            "amount": qty,
            "average": avg_price,
            "commission": cumulative_commission,
            "commission_asset": commission_asset or prev.get("commission_asset", ""),
        }
        self._notify()
        log.debug(
            "UserDataFeed: order %s → %s filled=%.6f/%.6f commission=%.8f %s",
            order_id, ccxt_status, filled, qty, commission, commission_asset,
        )

    def _handle_account_update(self, data: dict) -> None:
        """Process ACCOUNT_UPDATE events: LIQUIDATION, ADL, FUNDING_FEE."""
        account = data.get("a", {})
        reason = account.get("m", "")

        if reason in ("LIQUIDATION", "ADL"):
            callbacks = self._liquidation_callbacks if reason == "LIQUIDATION" else self._adl_callbacks
            for pos_entry in account.get("P", []):
                symbol = pos_entry.get("s", "")
                position_amount = float(pos_entry.get("pa", 0) or 0)
                log.warning("UserDataFeed: %s event for %s amount=%s", reason, symbol, position_amount)
                for cb in callbacks:
                    try:
                        if asyncio.iscoroutinefunction(cb):
                            asyncio.create_task(cb(symbol, position_amount))
                    except Exception as e:
                        log.warning("UserDataFeed: %s callback error: %s", reason, e)

        elif reason == "FUNDING_FEE":
            for balance_entry in account.get("B", []):
                asset = balance_entry.get("a", "")
                balance_change = float(balance_entry.get("bc", 0) or 0)
                if balance_change != 0:
                    log.info("UserDataFeed: FUNDING_FEE %s %s", balance_change, asset)
                    for cb in self._funding_callbacks:
                        try:
                            if asyncio.iscoroutinefunction(cb):
                                asyncio.create_task(cb(asset, balance_change))
                        except Exception as e:
                            log.warning("UserDataFeed: FUNDING callback error: %s", e)

    async def _run(self) -> None:
        """Main WS loop with exponential backoff reconnect."""
        backoff = 1.0
        while not self._stopped:
            if not self._listen_key:
                await asyncio.sleep(5)
                continue
            url = f"{self.WS_BASE}/{self._listen_key}"
            try:
                timeout = aiohttp.ClientTimeout(total=None, connect=10)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(
                        url, heartbeat=20, max_msg_size=0
                    ) as ws:
                        backoff = 1.0
                        log.info("UserDataFeed: WS connected")
                        async for msg in ws:
                            if self._stopped:
                                return
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                event_type = data.get("e")
                                if event_type == "ORDER_TRADE_UPDATE":
                                    self._handle_order_update(data.get("o", {}))
                                elif event_type == "ACCOUNT_UPDATE":
                                    self._handle_account_update(data)
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                log.warning(f"UserDataFeed: WS {msg.type.name}")
                                break
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning(f"UserDataFeed: connection error: {e}")

            if self._stopped:
                return
            log.info(f"UserDataFeed: reconnecting in {backoff:.0f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            # Refresh listen key on reconnect (old key may have expired)
            lk = await self._fetch_listen_key()
            if lk:
                self._listen_key = lk

    async def _keepalive_loop(self) -> None:
        """Extend listen key every 30 min to prevent expiry."""
        while not self._stopped:
            await asyncio.sleep(self.KEEPALIVE_INTERVAL)
            if self._stopped or not self._listen_key:
                break
            try:
                await self._client.keepalive_listen_key(self._listen_key)
                log.debug("UserDataFeed: listen key extended")
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.warning(f"UserDataFeed: keepalive failed: {e}")
                lk = await self._fetch_listen_key()
                if lk:
                    self._listen_key = lk

    async def _fetch_listen_key(self) -> Optional[str]:
        try:
            return await self._client.create_listen_key()
        except Exception as e:
            log.warning(f"UserDataFeed: failed to get listen key: {e}")
            return None
