import os
import asyncio
from typing import Optional
import ccxt.async_support as ccxt
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

SUPPORTED_MARGIN_ASSETS = ("USDT", "USDC")


class BinanceClient:
    def __init__(self):
        self.api_key = os.getenv("BINANCE_API_KEY", "")
        self.secret = os.getenv("BINANCE_SECRET", "")
        # Only pass credentials if they look real (not placeholder values)
        has_creds = bool(
            self.api_key
            and self.secret
            and self.api_key != "your_api_key_here"
            and self.secret != "your_secret_here"
        )
        self.has_creds = has_creds
        config = {
            "options": {"defaultType": "future"},
            "enableRateLimit": True,
        }
        if has_creds:
            config["apiKey"] = self.api_key
            config["secret"] = self.secret
        self.exchange = ccxt.binanceusdm(config)

    async def close(self):
        await self.exchange.close()

    async def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 500) -> pd.DataFrame:
        """Fetch OHLCV data and return as pandas DataFrame."""
        try:
            raw = await self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("timestamp", inplace=True)
            return df
        except Exception as e:
            raise RuntimeError(f"Failed to fetch OHLCV for {symbol}: {e}")

    async def fetch_ticker(self, symbol: str) -> dict:
        """Fetch current ticker for a symbol."""
        try:
            ticker = await self.exchange.fetch_ticker(symbol)
            return {
                "symbol": symbol,
                "last": ticker.get("last"),
                "bid": ticker.get("bid"),
                "ask": ticker.get("ask"),
                "volume": ticker.get("baseVolume"),
            }
        except Exception as e:
            raise RuntimeError(f"Failed to fetch ticker for {symbol}: {e}")

    async def get_available_futures(self) -> list[str]:
        """Return list of active USDT-M and USDC-M perpetual futures ids."""
        try:
            meta = await self.get_available_futures_meta()
            symbols = [item["id"] for item in meta]
            return sorted(set(symbols))
        except Exception as e:
            raise RuntimeError(f"Failed to fetch futures symbols: {e}")

    async def get_available_futures_meta(self) -> list[dict]:
        """Return active USDT-M and USDC-M perpetual futures with metadata."""
        try:
            markets = await self.exchange.load_markets()
            items = []
            for symbol, market in markets.items():
                if market.get("quote") not in SUPPORTED_MARGIN_ASSETS:
                    continue
                if market.get("type") not in ("swap", "future"):
                    continue
                if not market.get("active", False):
                    continue
                if not market.get("linear", True):
                    continue
                if market.get("expiry"):
                    continue

                margin_asset = (
                    market.get("settle")
                    or market.get("quote")
                    or (market.get("info") or {}).get("marginAsset")
                )
                items.append({
                    "id": market.get("id") or symbol,
                    "symbol": market.get("symbol") or symbol,
                    "base": market.get("base"),
                    "quote": market.get("quote"),
                    "margin_asset": margin_asset,
                })
            items.sort(key=lambda item: item["id"])
            return items
        except Exception as e:
            raise RuntimeError(f"Failed to fetch futures symbols: {e}")

    async def _ensure_markets(self) -> None:
        """Load markets if not yet cached."""
        if not self.exchange.markets:
            await self.exchange.load_markets()

    async def get_market_info(self, symbol: str) -> dict:
        """Resolve user input to canonical ccxt market metadata."""
        await self._ensure_markets()
        try:
            market = self.exchange.market(symbol)
        except Exception as e:
            raise RuntimeError(f"Unknown futures symbol {symbol}: {e}")

        margin_asset = (
            market.get("settle")
            or market.get("quote")
            or (market.get("info") or {}).get("marginAsset")
        )
        return {
            "symbol": market.get("symbol", symbol),
            "id": market.get("id", symbol),
            "quote": market.get("quote"),
            "settle": market.get("settle"),
            "margin_asset": margin_asset,
            "base": market.get("base"),
            "maker": market.get("maker"),
            "taker": market.get("taker"),
        }

    async def round_amount(self, symbol: str, amount: float) -> float:
        """Round amount to exchange lot-size (stepSize) precision."""
        await self._ensure_markets()
        try:
            return float(self.exchange.amount_to_precision(symbol, amount))
        except Exception:
            return amount  # fallback: return as-is

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        """Set cross-margin leverage for a futures symbol."""
        try:
            result = await self.exchange.set_leverage(leverage, symbol)
            return result
        except Exception as e:
            raise RuntimeError(f"Failed to set leverage for {symbol}: {e}")

    async def check_min_notional(
        self, symbol: str, amount: float, price: float
    ) -> tuple[bool, float, float]:
        """
        Check whether the order meets the minimum notional requirement.
        Returns (ok, actual_notional, min_notional).
        """
        await self._ensure_markets()
        try:
            market = self.exchange.market(symbol)  # resolves BTC/USDT → BTC/USDT:USDT
        except Exception:
            market = {}
        try:
            rounded_amount = float(
                self.exchange.amount_to_precision(market.get("symbol", symbol), amount)
            )
        except Exception:
            rounded_amount = float(amount or 0.0)
        min_notional = (market.get("limits") or {}).get("cost", {}).get("min")
        # Fall back to raw Binance filter array
        if not min_notional:
            for f in (market.get("info") or {}).get("filters", []):
                if f.get("filterType") == "MIN_NOTIONAL":
                    min_notional = float(f.get("notional") or 0)
                    break
        min_notional = float(min_notional or 0.0)
        actual_notional = rounded_amount * price
        return actual_notional >= min_notional, actual_notional, min_notional

    async def place_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        order_type: str = "market",
        params: Optional[dict] = None,
    ) -> dict:
        """Place a market order. Rounds amount to exchange lot-size before submitting."""
        try:
            await self._ensure_markets()
            rounded = self.exchange.amount_to_precision(symbol, amount)
            order = await self.exchange.create_order(
                symbol=symbol,
                type=order_type,
                side=side,
                amount=rounded,
                params=params or {},
            )
            return order
        except Exception as e:
            raise RuntimeError(f"Failed to place order for {symbol}: {e}")

    async def get_positions(self) -> list[dict]:
        """Return open futures positions."""
        try:
            positions = await self.exchange.fetch_positions()
            open_positions = [
                {
                    "symbol": p["symbol"],
                    "side": p["side"],
                    "size": p["contracts"],
                    "notional": p["notional"],
                    "entry_price": p["entryPrice"],
                    "mark_price": p["markPrice"],
                    "unrealized_pnl": p["unrealizedPnl"],
                    "leverage": p["leverage"],
                    "liquidation_price": p.get("liquidationPrice"),
                }
                for p in positions
                if p.get("contracts") and abs(p["contracts"]) > 0
            ]
            return open_positions
        except Exception as e:
            raise RuntimeError(f"Failed to fetch positions: {e}")

    async def get_all_balances(self) -> dict:
        """Return supported futures balances keyed by margin asset."""
        try:
            balance = await self.exchange.fetch_balance()
            balances = {}
            for asset in SUPPORTED_MARGIN_ASSETS:
                bucket = balance.get(asset, {})
                balances[asset] = {
                    "total": bucket.get("total", 0.0),
                    "free": bucket.get("free", 0.0),
                    "used": bucket.get("used", 0.0),
                }
            return balances
        except Exception as e:
            raise RuntimeError(f"Failed to fetch balances: {e}")

    async def get_balance(self, asset: str = "USDT") -> dict:
        """Return a specific futures balance plus all supported balances."""
        asset = (asset or "USDT").upper()
        try:
            balances = await self.get_all_balances()
            selected = balances.get(asset, {"total": 0.0, "free": 0.0, "used": 0.0})
            return {
                "asset": asset,
                "total": selected.get("total", 0.0),
                "free": selected.get("free", 0.0),
                "used": selected.get("used", 0.0),
                "assets": balances,
            }
        except Exception as e:
            raise RuntimeError(f"Failed to fetch balance: {e}")

    async def fetch_order_book(self, symbol: str, limit: int = 5) -> dict:
        """Fetch top-of-book and return {bid, ask, spread_pct}."""
        try:
            ob = await self.exchange.fetch_order_book(symbol, limit=limit)
            bid = ob["bids"][0][0] if ob["bids"] else None
            ask = ob["asks"][0][0] if ob["asks"] else None
            spread_pct = ((ask - bid) / bid * 100) if (bid and ask) else 0.0
            return {"bid": bid, "ask": ask, "spread_pct": spread_pct}
        except Exception as e:
            raise RuntimeError(f"Failed to fetch order book for {symbol}: {e}")

    async def place_limit_order(
        self, symbol: str, side: str, amount: float, price: float,
        params: Optional[dict] = None,
    ) -> dict:
        """Place a limit order. Rounds amount and price to exchange precision."""
        try:
            await self._ensure_markets()
            rounded = self.exchange.amount_to_precision(symbol, amount)
            price_str = self.exchange.price_to_precision(symbol, price)
            order = await self.exchange.create_order(
                symbol=symbol,
                type="limit",
                side=side,
                amount=rounded,
                price=price_str,
                params=params or {},
            )
            return order
        except Exception as e:
            raise RuntimeError(f"Failed to place limit order for {symbol}: {e}")

    async def cancel_order(self, symbol: str, order_id: str) -> dict:
        """Cancel an open order by id."""
        try:
            return await self.exchange.cancel_order(order_id, symbol)
        except Exception as e:
            raise RuntimeError(f"Failed to cancel order {order_id} for {symbol}: {e}")

    async def fetch_order(self, symbol: str, order_id: str) -> dict:
        """Fetch a single order by id."""
        try:
            return await self.exchange.fetch_order(order_id, symbol)
        except Exception as e:
            raise RuntimeError(f"Failed to fetch order {order_id} for {symbol}: {e}")

    async def create_listen_key(self) -> str:
        """Create a Binance Futures User Data Stream listen key."""
        try:
            response = await self.exchange.fapiPrivatePostListenKey()
            return response["listenKey"]
        except Exception as e:
            raise RuntimeError(f"Failed to create listen key: {e}")

    async def keepalive_listen_key(self, listen_key: str) -> None:
        """Extend listen key validity (call every 30–60 min to prevent expiry)."""
        try:
            await self.exchange.fapiPrivatePutListenKey({"listenKey": listen_key})
        except Exception as e:
            raise RuntimeError(f"Failed to keepalive listen key: {e}")
