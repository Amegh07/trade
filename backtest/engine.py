import pandas as pd
import numpy as np

def run_backtest(df, strategy, initial_capital=10000, commission=0.001):
    """
    Runs a vectorised backtest for a given strategy.
    
    Args:
        df (pd.DataFrame): DataFrame containing OHLCV data.
        strategy (object): Strategy instance with a generate_signals(df) method.
        initial_capital (float): Starting account balance.
        commission (float): Commission per trade (e.g., 0.001 for 0.1%).
        
    Returns:
        pd.DataFrame: Updated DataFrame with backtest results.
    """
    # 1. Generate signals using the strategy instance
    df = strategy.generate_signals(df)
    
    # Identify the correct close column (to handle both MT5's 'close' or standard 'Close')
    close_col = 'close' if 'close' in df.columns else 'Close'
    
    # 2. Calculate the daily percentage returns of the close prices
    df['Returns'] = df[close_col].pct_change()
    
    # 3. Calculate 'Strategy_Returns' by shifting the 'Signal' column by 1
    # We shift by 1 to prevent forward-looking bias (signals impact the next period's return)
    # The signal value is either 1 (long) or -1 (short), or 0
    df['Strategy_Returns'] = df['Signal'].shift(1) * df['Returns']
    df['Strategy_Returns'] = df['Strategy_Returns'].fillna(0)
    
    # 4. Deduct the commission amount whenever the 'Position' column is not 0
    trade_happened = df['Position'].fillna(0) != 0
    df['Net_Returns'] = df['Strategy_Returns'] - np.where(trade_happened, commission, 0.0)
    
    # 5. Calculate an 'Equity' column showing the account balance over time
    df['Equity'] = initial_capital * (1 + df['Net_Returns']).cumprod()
    
    return df
