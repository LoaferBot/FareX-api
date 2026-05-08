# =============================================================================
#  FAREX — Ride Price Comparison API
#  FastAPI backend for Uber (real), Ola (mock), Rapido (mock)
#
#  TO RUN:  uvicorn main:app --reload
#  DOCS:    http://localhost:8000/docs
# =============================================================================

import os
import time
import json
import math
import random
import httpx


from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 0. Load environment variables from .env file
# ---------------------------------------------------------------------------
load_dotenv()

UBER_CLIENT_ID = os.getenv("UBER_CLIENT_ID")
UBER_CLIENT_SECRET = os.getenv("UBER_CLIENT_SECRET")

# ---------------------------------------------------------------------------
# 1. App + Rate Limiter setup
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Farex API",
    description="Real-time ride fare comparison for Uber, Ola, and Rapido",
    version="1.0.0",
)

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ---------------------------------------------------------------------------
# 2. CORS — allow your frontend origin(s)
#    Add your deployed frontend URL here when you go live.
# ---------------------------------------------------------------------------

ALLOWED_ORIGINS = json.loads(
    os.getenv("ALLOWED_ORIGINS",
              '["http://localhost:3000","http://127.0.0.1:5500","http://localhost:5500"]')
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# ---------------------------------------------------------------------------
# 3. Pydantic schemas
# ---------------------------------------------------------------------------


class EstimateRequest(BaseModel):
    pickup_lat: float
    pickup_lng: float
    drop_lat: float
    drop_lng: float
    pickup_name: str = "Pickup"   # frontend should send this
    drop_name: str = "Drop"       # frontend should send this


# ---------------------------------------------------------------------------
# 4. Uber OAuth token cache
#    Uber tokens are valid for 30 days (2,592,000 seconds).
#    We cache the token in memory so we don't call the auth endpoint
#    on every user request — that would be slow AND rate-limited.
# ---------------------------------------------------------------------------
_uber_token_cache: dict = {
    "access_token": None,
    "expires_at":   0,       # Unix timestamp when the token expires
}


async def get_uber_token() -> str:
    """
    Returns a valid Uber Bearer token.
    Fetches a new one only if the cached token is missing or within
    60 seconds of expiry (safety buffer).
    """
    now = time.time()

    # Return cached token if it's still fresh
    if _uber_token_cache["access_token"] and now < _uber_token_cache["expires_at"] - 60:
        return _uber_token_cache["access_token"]

    # No valid token — fetch a new one
    if not UBER_CLIENT_ID or not UBER_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Uber credentials missing. Set UBER_CLIENT_ID and UBER_CLIENT_SECRET in your .env file."
        )

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://auth.uber.com/oauth/v2/token",
            data={
                "client_id":     UBER_CLIENT_ID,
                "client_secret": UBER_CLIENT_SECRET,
                "grant_type":    "client_credentials",
                "scope": "price_estimate",
            },
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Uber auth failed: {response.text}"
        )

    data = response.json()

    # Store in cache
    _uber_token_cache["access_token"] = data["access_token"]
    _uber_token_cache["expires_at"] = now + data.get("expires_in", 2592000)

    return _uber_token_cache["access_token"]

# ---------------------------------------------------------------------------
# 5. Uber — real price estimate
# ---------------------------------------------------------------------------


async def fetch_uber_estimates(
    pickup_lat: float, pickup_lng: float,
    drop_lat:   float, drop_lng:   float,
) -> list[dict]:
    """
    Calls Uber's official price-estimates endpoint.
    Returns a list of ride options (UberGo, UberX, Premier, etc.)
    """
    token = await get_uber_token()

    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.uber.com/v1.2/estimates/price",
            headers={
                "Authorization":  f"Bearer {token}",
                "Accept-Language": "en_US",
                "Content-Type":    "application/json",
            },
            params={
                "start_latitude":  pickup_lat,
                "start_longitude": pickup_lng,
                "end_latitude":    drop_lat,
                "end_longitude":   drop_lng,
            },
            timeout=10.0,
        )

    if response.status_code != 200:
        # Don't crash the whole comparison — return empty list with an error note
        return [{
            "provider":    "Uber",
            "product":     "Unavailable",
            "fare_min":    None,
            "fare_max":    None,
            "currency":    "INR",
            "eta_minutes": None,
            "surge":       1.0,
            "deep_link":   _uber_deep_link(pickup_lat, pickup_lng, drop_lat, drop_lng),
            "error":       f"Uber API error {response.status_code}",
        }]

    prices = response.json().get("prices", [])
    if not prices:
        return [{"provider": "Uber", "product": "Unavailable", "error": "No products returned — check API region/scope"}]
    if not prices:
        return [{
            "provider":    "Uber",
            "product":     "Unavailable",
            "fare_min":    None,
            "fare_max":    None,
            "currency":    "INR",
            "eta_minutes": None,
            "surge":       1.0,
            "deep_link":   _uber_deep_link(pickup_lat, pickup_lng, drop_lat, drop_lng),
            "error":       "No Uber products returned — check API region or OAuth scope",
        }]

    return [
        {
            "provider":    "Uber",
            "product":     p.get("display_name", "Uber"),
            "fare_min":    p.get("low_estimate"),
            "fare_max":    p.get("high_estimate"),
            "currency":    p.get("currency_code", "INR"),
            "eta_minutes": round(p["duration"] / 60) if p.get("duration") else None,
            "surge":       p.get("surge_multiplier", 1.0),
            "deep_link":   _uber_deep_link(pickup_lat, pickup_lng, drop_lat, drop_lng),
        }
        for p in prices
    ]

# ---------------------------------------------------------------------------
# 6. Ola — mock estimate  (no public API available)
#    Pricing formula is modelled on real Ola fares in Indian metros.
#    Replace this function body with a real API call if/when Ola opens one.
# ---------------------------------------------------------------------------


def fetch_ola_estimates(
    pickup_lat: float, pickup_lng: float,
    drop_lat:   float, drop_lng:   float,
) -> list[dict]:
    """
    Returns realistic mock fare estimates for Ola ride categories.
    Formula: base_fare + (per_km_rate × distance) + (per_min_rate × time)
    with a small random surge factor to simulate real-world variation.
    """
    distance_km = _haversine_km(pickup_lat, pickup_lng, drop_lat, drop_lng)
    duration_min = _estimate_duration_min(distance_km)
    categories = [
        ("Ola Mini",   40,  10, 1.0, 4),
        ("Ola Prime",  60,  14, 1.5, 4),
        ("Ola Auto",   25,   7, 0.5, 3),
        ("Ola Bike",   15,   4, 0.5, 1),
    ]


results = []
for name, base, per_km, per_min, capacity in categories:
    surge = round(random.choice([1.0, 1.0, 1.0, 1.2, 1.5]), 1)  # per category
    raw_fare = base + (per_km * distance_km) + (per_min * duration_min)
    fare = round(raw_fare * surge)
    results.append({
        "provider":    "Ola",
        "product":     name,
        "fare_min":    int(fare * 0.92),   # ±8% band
        "fare_max":    int(fare * 1.08),
        "currency":    "INR",
        "eta_minutes": random.randint(3, 12),
        "surge":       surge,
        "deep_link":   _ola_deep_link(pickup_lat, pickup_lng, drop_lat, drop_lng),
        "note":        "Estimated fare — Ola has no public API",
    })

    return results

# ---------------------------------------------------------------------------
# 7. Rapido — mock estimate  (no public API available)
# ---------------------------------------------------------------------------


def fetch_rapido_estimates(
    pickup_lat: float, pickup_lng: float,
    drop_lat:   float, drop_lng:   float,
) -> list[dict]:
    """
    Returns realistic mock fare estimates for Rapido ride categories.
    Rapido is primarily bike + auto — fares are lower than Ola/Uber.
    """
    distance_km = _haversine_km(pickup_lat, pickup_lng, drop_lat, drop_lng)
    duration_min = _estimate_duration_min(distance_km)
    categories = [
        ("Rapido Bike",  20,  4.0, 0.4),
        ("Rapido Auto",  30,  7.0, 0.8),
        ("Rapido Cab",   50, 11.0, 1.2),
    ]


results = []
for name, base, per_km, per_min in categories:
    surge = round(random.choice([1.0, 1.0, 1.0, 1.1, 1.3]), 1)  # per category
    raw_fare = base + (per_km * distance_km) + (per_min * duration_min)
    fare = round(raw_fare * surge)
    results.append({
        "provider":    "Rapido",
        "product":     name,
        "fare_min":    int(fare * 0.92),
        "fare_max":    int(fare * 1.08),
        "currency":    "INR",
        "eta_minutes": random.randint(2, 10),
        "surge":       surge,
        "deep_link":   _rapido_deep_link(),
        "note":        "Estimated fare — Rapido has no public API",
    })

    return results

# ---------------------------------------------------------------------------
# 8. Deep link helpers
#    These open the respective app (on mobile) or website (on desktop)
#    with pickup/drop pre-filled where the platform supports it.
# ---------------------------------------------------------------------------


def _uber_deep_link(pickup_lat, pickup_lng, drop_lat, drop_lng) -> str:
    """
    Uber's universal deep link — pre-fills pickup & destination.
    Works on both the mobile app and web fallback.
    """
    return (
        f"https://m.uber.com/ul/?"
        f"action=setPickup"
        f"&pickup[latitude]={pickup_lat}"
        f"&pickup[longitude]={pickup_lng}"
        f"&dropoff[latitude]={drop_lat}"
        f"&dropoff[longitude]={drop_lng}"
    )


def _ola_deep_link(pickup_lat, pickup_lng, drop_lat, drop_lng) -> str:
    """
    Ola does not have an official deep link spec for third parties.
    This opens the Ola website booking page as the best available fallback.
    The coordinates are passed as custom query params — Ola may or may not
    honour them depending on the app version.
    """
    return (
        f"https://book.olacabs.com/?"
        f"pickup_lat={pickup_lat}&pickup_lng={pickup_lng}"
        f"&drop_lat={drop_lat}&drop_lng={drop_lng}"
    )


def _rapido_deep_link() -> str:
    """
    Rapido does not expose a public deep link scheme.
    Redirects to their homepage — user books manually in the app.
    """
    return "https://rapido.bike"

# ---------------------------------------------------------------------------
# 9. Geo helpers
# ---------------------------------------------------------------------------


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance between two lat/lng points in kilometres."""
    R = 6371  # Earth radius in km
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * \
        math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _estimate_duration_min(distance_km: float) -> float:
    """
    Rough duration estimate assuming 20 km/h average city speed.
    Replace with a real Maps API call for higher accuracy.
    """
    avg_speed_kmh = 20
    return (distance_km / avg_speed_kmh) * 60

# ---------------------------------------------------------------------------
# 10. API Endpoints
# ---------------------------------------------------------------------------


@app.get("/")
async def health_check():
    return {
        "status": "online",
        "service": "Farex API",
        "endpoints": {
            "compare_all":   "POST /estimates/compare",
            "uber_only":     "GET  /estimates/uber",
            "docs":          "GET  /docs",
        }
    }


@app.post("/estimates/compare")
@limiter.limit("20/minute")
async def compare_all(request: Request, body: EstimateRequest):
    """
    Main endpoint — returns fare estimates from Uber, Ola, and Rapido
    for the given pickup and drop coordinates.

    Example request body:
    {
        "pickup_lat": 19.0760,
        "pickup_lng": 72.8777,
        "drop_lat":   19.0330,
        "drop_lng":   72.8697,
        "pickup_name": "Bandra Station",
        "drop_name":   "Churchgate"
    }
    """
    # Run Uber (async/real) first, then mock providers
    uber_results = await fetch_uber_estimates(
        body.pickup_lat, body.pickup_lng,
        body.drop_lat,   body.drop_lng)
    ola_results = fetch_ola_estimates(
        body.pickup_lat, body.pickup_lng,
        body.drop_lat,   body.drop_lng)
    rapido_results = fetch_rapido_estimates(
        body.pickup_lat, body.pickup_lng,
        body.drop_lat,   body.drop_lng)

    all_results = uber_results + ola_results + rapido_results

    # Sort cheapest first (by fare_min, None values go last)
    all_results.sort(key=lambda x: (x["fare_min"] is None, x["fare_min"] or 0))

    distance_km = _haversine_km(
        body.pickup_lat, body.pickup_lng,
        body.drop_lat,   body.drop_lng
    )

    return {
        "pickup":       body.pickup_name,
        "drop":         body.drop_name,
        "distance_km":  round(distance_km, 2),
        "total_options": len(all_results),
        "estimates":    all_results,
    }


@app.get("/estimates/uber")
@limiter.limit("20/minute")
async def uber_only(
    request:     Request,
    pickup_lat:  float,
    pickup_lng:  float,
    drop_lat:    float,
    drop_lng:    float,
):
    """
    Fetch only Uber estimates. Useful for debugging your Uber API key
    without running the full comparison.
    """
    return await fetch_uber_estimates(pickup_lat, pickup_lng, drop_lat, drop_lng)
