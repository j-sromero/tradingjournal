# sector_etf_map.py
# Maps sector names to representative ETFs

def get_etf_for_sector(sector):
    sector_map = {
        "Technology": "XLK",
        "Health Care": "XLV",
        "Financials": "XLF",
        "Consumer Discretionary": "XLY",
        "Consumer Cyclical": "XLY",
        "Consumer Staples": "XLP",
        "Energy": "XLE",
        "Industrials": "XLI",
        "Materials": "XLB",
        "Utilities": "XLU",
        "Real Estate": "XLRE",
        "Communication Services": "XLC",
    }
    return sector_map.get(sector)
