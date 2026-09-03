"""Locust load tests for the Polymarket bot API.

Run: cd mini-services/polymarket-bot && locust -f tests/load/locustfile.py --host=http://localhost:8080
"""
from locust import HttpUser, task, between, events
import random
import os

API_TOKEN = os.environ.get("API_TOKEN", "I76FCamSbBw0e1r_V0RRX81uG-3DUCS_pofbYNC-RgHO3x9b3DIovCPe01iDREBT")

class PolymarketBotUser(HttpUser):
    """Simulates a dashboard user polling various endpoints."""
    
    wait_time = between(0.5, 2.0)  # Realistic polling interval
    host = os.environ.get("LOCUST_HOST", "http://localhost:8080")
    
    def on_start(self):
        self.headers = {"Authorization": f"Bearer {API_TOKEN}"}
        # Warm up
        self.client.get("/api/health", headers=self.headers, name="warmup")
    
    # === Read-heavy endpoints (simulating dashboard polling) ===
    
    @task(10)  # Weight 10 — most frequently polled
    def get_snapshot(self):
        self.client.get("/api/snapshot", headers=self.headers, name="GET /api/snapshot")
    
    @task(8)
    def get_positions(self):
        self.client.get("/api/positions", headers=self.headers, name="GET /api/positions")
    
    @task(8)
    def get_orders(self):
        self.client.get("/api/orders", headers=self.headers, name="GET /api/orders")
    
    @task(6)
    def get_markets(self):
        self.client.get("/api/markets", headers=self.headers, name="GET /api/markets")
    
    @task(6)
    def get_orderbooks(self):
        self.client.get("/api/orderbooks", headers=self.headers, name="GET /api/orderbooks")
    
    @task(5)
    def get_trades(self):
        self.client.get("/api/trades?limit=50", headers=self.headers, name="GET /api/trades")
    
    @task(4)
    def get_ml_metrics(self):
        self.client.get("/api/ml/metrics", headers=self.headers, name="GET /api/ml/metrics")
    
    @task(3)
    def get_analytics(self):
        self.client.get("/api/analytics", headers=self.headers, name="GET /api/analytics")
    
    @task(3)
    def get_observability(self):
        self.client.get("/api/observability", headers=self.headers, name="GET /api/observability")
    
    @task(2)
    def get_attribution(self):
        self.client.get("/api/attribution?range=24h", headers=self.headers, name="GET /api/attribution")
    
    @task(2)
    def get_alerts(self):
        self.client.get("/api/alerts", headers=self.headers, name="GET /api/alerts")
    
    @task(1)
    def get_health(self):
        self.client.get("/api/health", headers=self.headers, name="GET /api/health")


class HeavyComputeUser(HttpUser):
    """Simulates a user triggering expensive operations (less frequent)."""
    
    wait_time = between(10, 30)
    host = os.environ.get("LOCUST_HOST", "http://localhost:8080")
    
    def on_start(self):
        self.headers = {"Authorization": f"Bearer {API_TOKEN}"}
    
    @task(3)
    def get_ml_versions(self):
        self.client.get("/api/ml/versions", headers=self.headers, name="GET /api/ml/versions [heavy]")
    
    @task(2)
    def get_decisions(self):
        self.client.get("/api/decisions/rejected?limit=50", headers=self.headers, name="GET /api/decisions/rejected [heavy]")
    
    @task(1)
    def get_execution_quality(self):
        self.client.get("/api/execution-quality", headers=self.headers, name="GET /api/execution-quality [heavy]")
    
    @task(1)
    def get_closed_positions(self):
        self.client.get("/api/positions/closed?limit=50", headers=self.headers, name="GET /api/positions/closed [heavy]")


# Event hooks for custom stats
@events.request.add_listener
def on_request(request_type, name, response_time, response_length, response, context, exception, **kwargs):
    if exception:
        # Log slow requests
        if response_time and response_time > 1000:
            print(f"\n⚠ SLOW: {name} took {response_time:.0f}ms")
