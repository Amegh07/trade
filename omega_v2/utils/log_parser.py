import re
import pandas as pd
from datetime import datetime

# Path to the log file (Ensure your main.py has a FileHandler or redirect output)
LOG_FILE = "omega.log"

def parse_logs():
    trades = []
    # Regex to catch: AI Signal Generated: BSKT_AUDUSD_NZDUSD_1713303600 | ENTER_LONG AUDUSD & ENTER_SHORT NZDUSD
    pattern = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*AI Signal Generated: (BSKT_\w+_\w+_\d+) \| (\w+) (\w+) & (\w+) (\w+)"
    
    try:
        with open(LOG_FILE, "r") as f:
            for line in f:
                match = re.search(pattern, line)
                if match:
                    trades.append({
                        "timestamp": match.group(1),
                        "basket_id": match.group(2),
                        "action_a": match.group(3),
                        "symbol_a": match.group(4),
                        "action_b": match.group(5),
                        "symbol_b": match.group(6)
                    })
        
        df = pd.DataFrame(trades)
        if not df.empty:
            print("\n--- OMEGA V2 TRADE JOURNAL ---")
            print(df.to_string(index=False))
            df.to_csv("trade_journal.csv", index=False)
            print(f"\nSaved {len(df)} signals to trade_journal.csv")
        else:
            print("No signals found in logs yet. The AI is still hunting.")
            
    except FileNotFoundError:
        print("Log file not found. Make sure your bot is running and logging to omega.log")

if __name__ == "__main__":
    parse_logs()
