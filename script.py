import sqlite3
import pandas as pd

# Connect to the database
conn = sqlite3.connect('journal.db')

# Query all closed trades with required fields
query = """
SELECT id, ticker, direction, entry_date, exit_date, entry_price, exit_price, size, fees
FROM trades
WHERE status = 'closed'
  AND exit_price IS NOT NULL
  AND entry_price IS NOT NULL
  AND size IS NOT NULL
"""
df = pd.read_sql_query(query, conn)

# Compute position value, P&L, and return for each trade
results = []

for _, row in df.iterrows():
    position_value = row['size'] * row['entry_price']
    fees = row['fees'] if row['fees'] is not None else 0

    if row['direction'] == 'long':
        pnl = (row['exit_price'] - row['entry_price']) * row['size'] - fees
        ret = (row['exit_price'] - row['entry_price']) / row['entry_price']
    else:
        pnl = (row['entry_price'] - row['exit_price']) * row['size'] - fees
        ret = (row['entry_price'] - row['exit_price']) / row['entry_price']

    results.append({
        'id': row['id'],
        'ticker': row['ticker'],
        'entry_date': row['entry_date'],
        'exit_date': row['exit_date'],
        'position_value': position_value,
        'pnl': pnl,
        'return': ret
    })

trades = pd.DataFrame(results)

# Impact = capital-weighted return
trades['impact'] = trades['position_value'] * trades['return']

# Worst trades by impact
worst_trades = trades.nsmallest(10, 'impact')

# Sizing rule suggestion
median_size = trades['position_value'].median()

print("Worst trades by capital impact:")
print(worst_trades[['id', 'ticker', 'entry_date', 'exit_date',
                    'position_value', 'pnl', 'return', 'impact']])

print(f"\nSuggested sizing rule: Cap position size at median = {median_size:.2f}")