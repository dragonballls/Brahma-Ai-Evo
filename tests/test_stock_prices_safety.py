import json

def test_stock_rejects_path_traversal_symbol_before_network(monkeypatch):
    from features import check_current_stock_prices as stock

    monkeypatch.setattr(stock, "open_fixed_https", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network must not run")))
    result = stock.execute(symbol="../../outside", period="1mo")
    assert result == {"error": "Invalid stock symbol format."}


def test_stock_bounds_response_and_uses_fixed_host(monkeypatch):
    from features import check_current_stock_prices as stock

    captured = {}

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self, n):
            captured["limit"] = n
            return b"x" * n

    def opener(url, **kwargs):
        captured["url"] = url
        return Response()

    monkeypatch.setattr(stock, "open_fixed_https", opener)
    result = stock.execute(symbol="AAPL", period="1mo")
    assert "safety limit" in result["error"]
    assert captured["limit"] == 4 * 1024 * 1024 + 1
    assert "query1.finance.yahoo.com" in captured["url"]


def test_stock_handles_missing_market_price_without_claiming_success(monkeypatch):
    from features import check_current_stock_prices as stock

    payload = {
        "chart": {
            "result": [{
                "meta": {"currency": "USD", "regularMarketPrice": None},
                "timestamp": [1704067200],
                "indicators": {"quote": [{"close": [100.0]}]},
            }]
        }
    }

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self, _n):
            return json.dumps(payload).encode()

    monkeypatch.setattr(stock, "open_fixed_https", lambda *a, **k: Response())
    monkeypatch.setattr(stock.plt, "savefig", lambda *a, **k: None)
    monkeypatch.setattr(stock.plt, "close", lambda *a, **k: None)
    result = stock.execute(symbol="AAPL", period="1d")
    assert "Could not retrieve current price" in result["summary"]
    assert "image_path" in result
