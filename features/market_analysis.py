"""
Feature: market_analysis
Description: Analyzes the current Bitcoin price and displays a dark-mode graph card with recent price data. By default, it fetches BTCUSDT 1-hour klines for the last 24 hours.
"""

FEATURE_METADATA = {
    "name": "market_analysis",
    "description": "Analyzes the current Bitcoin price and displays a dark-mode graph card with recent price data. By default, it fetches BTCUSDT 1-hour klines for the last 24 hours.",
    "parameters": {"type": "OBJECT", "properties": {"symbol": {"type": "STRING", "description": "The trading pair symbol (e.g., 'BTCUSDT', 'ETHUSDT'). Defaults to 'BTCUSDT'."}, "interval": {"type": "STRING", "description": "The candlestick interval (e.g., '1m', '5m', '1h', '1d'). Defaults to '1h'."}, "limit": {"type": "INTEGER", "description": "The number of historical data points to fetch for the graph. Defaults to 24."}}, "required": []},
    "version": "1.0.0",
    "active": True
}

import json
import os
import re
import datetime
from core.network_safety import open_fixed_https, read_bounded
import matplotlib
matplotlib.use('Agg') # Use the 'Agg' backend for non-interactive plotting
import matplotlib.pyplot as plt

def execute(**kwargs):
    symbol = str(kwargs.get('symbol', 'BTCUSDT') or '').strip().upper()
    interval = str(kwargs.get('interval', '1h') or '').strip().lower()
    if not re.fullmatch(r"[A-Z0-9]{2,20}", symbol):
        return {"error": "Invalid trading pair symbol."}
    valid_intervals = {"1s", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"}
    if interval not in valid_intervals:
        return {"error": f"Invalid interval '{interval}'."}
    try:
        limit = int(kwargs.get('limit', 24))
    except (TypeError, ValueError):
        return {"error": "Invalid 'limit' parameter. Must be an integer."}
    if not 1 <= limit <= 1000:
        return {"error": "Invalid 'limit' parameter. Must be between 1 and 1000."}

    ticker_url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
    try:
        with open_fixed_https(
            ticker_url,
            allowed_hosts={"api.binance.com"},
            timeout=8,
            headers={"User-Agent": "Brahma-Evo/1.0"},
        ) as response:
            ticker_data = json.loads(read_bounded(response, 256 * 1024).decode("utf-8"))
        current_price = float(ticker_data["lastPrice"])
        price_change_percent = float(ticker_data["priceChangePercent"])
    except Exception as e:
        return {"error": f"Failed to fetch live ticker data for {symbol}: {e}"}

    times = []
    prices = []
    klines_url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        with open_fixed_https(
            klines_url,
            allowed_hosts={"api.binance.com"},
            timeout=8,
            headers={"User-Agent": "Brahma-Evo/1.0"},
        ) as response:
            klines_data = json.loads(read_bounded(response, 1024 * 1024).decode("utf-8"))
            if not isinstance(klines_data, list):
                return {"error": "Binance returned an invalid historical data payload."}
            for kline in klines_data:
                close_time_ms = kline[6]  # Close time in milliseconds
                close_price = float(kline[4]) # Close price
                times.append(datetime.datetime.fromtimestamp(close_time_ms / 1000))
                prices.append(close_price)
    except urllib.error.URLError as e:
        return {"error": f"Failed to fetch historical data for {symbol}: {e}"}
    except json.JSONDecodeError as e:
        return {"error": f"Failed to decode historical data JSON for {symbol}: {e}"}
    except Exception as e:
        return {"error": f"An unexpected error occurred fetching historical data: {e}"}

    if not times or not prices:
        return {"error": f"No historical data available for {symbol} with interval {interval} and limit {limit}."}

    # --- Generate Dark-Mode Graph Card ---
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(10, 6))

    # Set dark HUD theme colors
    fig.patch.set_facecolor('#0B0F19')
    ax.set_facecolor('#0B0F19')

    # Plotting the data
    ax.plot(times, prices, color='#00F0FF', marker='o', markersize=4, linestyle='-', linewidth=2)

    # Title and labels
    ax.set_title(f'{symbol} Price Trend ({interval} Interval)', color='white', fontsize=16)
    ax.set_xlabel('Time', color='white', fontsize=12)
    ax.set_ylabel('Price (USD)', color='white', fontsize=12)

    # Grid
    ax.grid(True, linestyle='--', alpha=0.6, color='#1E293B')

    # Tick parameters
    ax.tick_params(axis='x', colors='white', rotation=45)
    ax.tick_params(axis='y', colors='white')

    # Spines
    for spine in ax.spines.values():
        spine.set_edgecolor('#1E293B')

    # Layout adjustment
    fig.tight_layout()

    # --- Save the plot to a file ---
    # Create the deliverables directory if it doesn't exist
    deliverables_dir = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'BrahmaAI', 'deliverables')
    os.makedirs(deliverables_dir, exist_ok=True)

    image_path = os.path.join(deliverables_dir, f'{symbol}_price_trend_{interval}.png')
    plt.savefig(image_path)
    plt.close(fig) # Close the figure to free up memory

    # --- Prepare summary ---
    summary_text = f"Current {symbol} Price: ${current_price:,.2f}. "
    if price_change_percent != 'N/A':
        change_sign = '+' if price_change_percent >= 0 else ''
        summary_text += f"24hr Change: {change_sign}{price_change_percent:.2f}%. "
    summary_text += f"Displaying {len(prices)} {interval} data points."

    return {
        "success": True,
        "image_path": image_path,
        "title": f"{symbol} Market Analysis",
        "summary": summary_text
    }
