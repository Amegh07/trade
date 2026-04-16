import asyncio
import logging
import time
import pandas as pd
import torch
import MetaTrader5 as mt5
from chronos import BaseChronosPipeline
from utils.risk_gates import risk_gates

logger = logging.getLogger("AlphaEngine")

TRADABLE_PAIRS = [
    ("AUDUSD", "NZDUSD"),   # Oceanic Forex spread
    ("AUDJPY", "NZDJPY"),   # Asian Session Engine
]

class Signal:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

class AlphaEngine:
    def __init__(self, signal_queue: asyncio.Queue):
        self.queue = signal_queue
        # Load Chronos-Bolt (Lightweight 9M parameter model)
        logger.info("Loading Chronos-Bolt-tiny model to GPU/CPU...")
        self.pipeline = BaseChronosPipeline.from_pretrained(
            "amazon/chronos-bolt-tiny",
            device_map="cuda" if torch.cuda.is_available() else "cpu",
            dtype=torch.bfloat16
        )
        logger.info("Model loaded successfully.")

    async def run(self):
        """Main async loop for generating signals."""
        while True:
            if risk_gates.is_rollover_window():
                logger.warning("Rollover Window Active. Alpha Engine pausing for 60s...")
                await asyncio.sleep(60)
                continue

            for asset_a, asset_b in TRADABLE_PAIRS:
                await self._process_pair(asset_a, asset_b)
            
            # Wait 1 minute before polling again (adjust as needed for M5 strategy)
            await asyncio.sleep(60)

    async def _process_pair(self, asset_a: str, asset_b: str):
        # Fetch last 512 M5 candles synchronously in a thread to not block async loop
        loop = asyncio.get_event_loop()
        rates_a = await loop.run_in_executor(None, mt5.copy_rates_from_pos, asset_a, mt5.TIMEFRAME_M5, 0, 512)
        rates_b = await loop.run_in_executor(None, mt5.copy_rates_from_pos, asset_b, mt5.TIMEFRAME_M5, 0, 512)

        if rates_a is None or rates_b is None or len(rates_a) < 512 or len(rates_b) < 512:
            return

        df = pd.DataFrame({'a': rates_a['close'], 'b': rates_b['close']})
        spread = df['a'] - df['b']

        # Zero-Shot Forecast (ChronosBolt uses quantile output, no num_samples)
        context = torch.tensor(spread.values, dtype=torch.float32)
        forecast = self.pipeline.predict(context, prediction_length=10)
        # forecast shape: (batch=1, quantiles, prediction_length) — take median (index 1)
        mean_forecast = forecast[0, len(forecast[0]) // 2].cpu().numpy()

        current_spread = spread.iloc[-1]
        future_spread = float(mean_forecast[0]) # Forecasted spread for the next M5 bar

        # Check Risk Gates
        info_a = mt5.symbol_info(asset_a)
        if info_a and risk_gates.is_spread_blown(asset_a, info_a.spread):
            logger.warning(f"Spread blown on {asset_a}. Vetoing signal.")
            return

        # Directional Logic
        action_a = action_b = None
        if future_spread > current_spread * 1.02: # Widen expectation
            action_a, action_b = "ENTER_SHORT", "ENTER_LONG"
        elif future_spread < current_spread * 0.98: # Narrow expectation
            action_a, action_b = "ENTER_LONG", "ENTER_SHORT"
        
        if action_a:
            b_id = f"BSKT_{asset_a}_{asset_b}_{int(time.time())}"
            sig_a = Signal(symbol=asset_a, action=action_a, basket_id=b_id, role="PRIMARY")
            sig_b = Signal(symbol=asset_b, action=action_b, basket_id=b_id, role="HEDGE")
            
            await self.queue.put(sig_a)
            await self.queue.put(sig_b)
            logger.info(f"AI Signal Generated: {b_id} | {action_a} {asset_a} & {action_b} {asset_b}")
