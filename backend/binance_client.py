import os
import asyncio
import ccxt.async_support as ccxt
import pandas as pd
from dotenv import load_dotenv

load_dotenv()


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
        """Return list of USDT-M futures symbols."""
        try:
            markets = await self.exchange.load_markets()
            symbols = [
                symbol for symbol, market in markets.items()
                if market.get("quote") == "USDT"
                and market.get("type") in ("swap", "future")
                and market.get("active", False)
                and market.get("linear", True)
                and not market.get("expiry")  # perpetual only
            ]
            return sorted(symbols)
        except Exception as e:
            raise RuntimeError(f"Failed to fetch futures symbols: {e}")

    async def place_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        order_type: str = "market",
    ) -> dict:
        """Place a market order. side: 'buy' or 'sell'."""
        try:
            order = await self.exchange.create_order(
                symbol=symbol,
                type=order_type,
                side=side,
                amount=amount,
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

    async def get_balance(self) -> dict:
        """Return USDT balance."""
        try:
            balance = await self.exchange.fetch_balance()
            usdt = balance.get("USDT", {})
            return {
                "total": usdt.get("total", 0.0),
                "free": usdt.get("free", 0.0),
                "used": usdt.get("used", 0.0),
            }
        except Exception as e:
            raise RuntimeError(f"Failed to fetch balance: {e}")
