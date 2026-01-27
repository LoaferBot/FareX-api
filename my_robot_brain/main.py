
from fastapi import FastAPI
app = FastAPI() # This 'app' variable is what uvicorn is looking for

@app.get("/")
def read_root():
    return {"Hello": "World"}

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import random

# 1. Setup the Brain
app = FastAPI()
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# 2. Allow your Frontend to talk to this Backend (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # This lets any website talk to your robot
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. The "Instruction Manual" for data (Schemas)
class RideRequest(BaseModel):
    pickup: str
    destination: str

# 4. THE ENDPOINTS (The Robot's Ears)

@app.get("/")
async def home():
    return {"message": "Robot brain is AWAKE!"}

@app.post("/auth/register")
@limiter.limit("5/minute")
async def register(request: Request, data: dict):
    # This is a simple mock registration
    return {"message": f"User {data.get('email')} is now registered!"}

@app.post("/estimates/compare")
@limiter.limit("10/minute")
async def compare_prices(request: Request, ride: RideRequest):
    # This simulates checking 3 different car services
    services = ["Service A", "Service B", "Service C"]
    results = []
    
    for name in services:
        results.append({
            "provider": name,
            "fare": round(random.uniform(10.0, 30.0), 2),
            "eta_minutes": random.randint(2, 15)
        })
    
    return {
        "from": ride.pickup,
        "to": ride.destination,
        "estimates": results
    }

# TO RUN THIS: 
# Open your terminal and type: uvicorn main:app --reload