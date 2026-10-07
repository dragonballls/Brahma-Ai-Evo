"""
Feature: crypto_live_price
Description: Fetches the live price of a specified cryptocurrency in USD using CoinGecko's public API. Defaults to Bitcoin.
"""

FEATURE_METADATA = {
    "name": "crypto_live_price",
    "description": "Fetches the live price of a specified cryptocurrency in USD using CoinGecko's public API. Defaults to Bitcoin.",
    "parameters": {"type": "OBJECT", "properties": {"coin_id": {"type": "STRING", "description": "The CoinGecko ID of the cryptocurrency (e.g., 'bitcoin', 'ethereum', 'solana'). Defaults to 'bitcoin'."}}, "required": []},
    "version": "1.0.0",
    "active": True
}

import json
import re
from core.network_safety import open_fixed_https, read_bounded

def execute(**kwargs):
    """
    Fetches the live price of a specified cryptocurrency in USD.

    Args:
        coin_id (str, optional): The CoinGecko ID of the cryptocurrency.
                                 Defaults to 'bitcoin'.

    Returns:
        dict: A dictionary containing the coin ID, its live price in USD, and the currency.
              Returns an error message if the price cannot be fetched.
    """
    coin_id = str(kwargs.get('coin_id', 'bitcoin') or '').strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", coin_id):
        return {"error": "Invalid cryptocurrency identifier."}
    currency = 'usd'
    api_url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies={currency}"

    try:
        with open_fixed_https(
            api_url,
            allowed_hosts={"api.coingecko.com"},
            timeout=8,
            headers={"User-Agent": "Brahma-Evo/1.0"},
        ) as response:
            data = json.loads(read_bounded(response, 128 * 1024).decode("utf-8"))

        if coin_id in data and currency in data[coin_id]:
            price = data[coin_id][currency]
            return {
                "coin_id": coin_id,
                "price": price,
                "currency": currency,
                "message": f"The live price of {coin_id.capitalize()} is {price} {currency.upper()}."
            }
        else:
            return {
                "error": f"Could not retrieve price for {coin_id}. It might be an invalid CoinGecko ID or the API did not return data."
            }
    except urllib.request.URLError as e:
        return {
            "error": f"Network or API error while fetching price for {coin_id}: {e.reason}"
        }
    except json.JSONDecodeError:
        return {
            "error": f"Failed to decode JSON response from CoinGecko for {coin_id}."
        }
    except Exception as e:
        return {
            "error": f"An unexpected error occurred: {str(e)}"
        }