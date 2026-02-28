import os
from fastapi import FastAPI, Depends, HTTPException, Security, Query, Request
from fastapi.security.api_key import APIKeyHeader
from dotenv import load_dotenv
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
import secrets

# Import the local tracker classes
from trackers import BlueDartTracker, DelhiveryTracker, BrowserManager, chrome_semaphore
from keys_db import APIKeyManager

# Load environment variables
load_dotenv()

# Initialize API Key DB Manager
key_manager = APIKeyManager()
key_manager.setup()

# Initialize FastAPI app
app = FastAPI(
    title="HyTrack API",
    description="API for tracking Blue Dart and Delhivery shipments",
    version="2.0.0",
)

# Initialize Rate Limiter
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# API Key Security Setup
API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


def get_admin_key(api_key_header: str = Security(api_key_header)):
    """Validates if the user provides the master admin key from .env"""
    expected_admin_key = os.getenv("API_KEY")

    if not expected_admin_key:
        raise HTTPException(
            status_code=500, detail="Master API_KEY not configured on server"
        )

    if secrets.compare_digest(api_key_header, expected_admin_key):
        return api_key_header
    else:
        raise HTTPException(
            status_code=403, detail="Forbidden. Master Key Required for Admin Actions"
        )


def get_api_key(api_key_header: str = Security(api_key_header)):
    """Validates the API key against the SQLite database of active hashes."""
    if not api_key_header:
        raise HTTPException(status_code=401, detail="Missing API Key header")

    # Strictly check the SQLite DB
    if key_manager.validate_key(api_key_header):
        return api_key_header

    raise HTTPException(
        status_code=401, detail="Could not validate API Key or Key has been revoked"
    )


@app.post("/admin/keys/generate", summary="Generate a new API key for a client")
async def generate_client_key(
    name: str = Query(
        ..., description="A friendly name identifying the application or user"
    ),
    admin_key: str = Depends(get_admin_key),
):
    """
    (Admin Only) Generates a new secure randomized API Token.
    Stores the hash of the token in SQLite.
    Returns the plain-text token. YOU WILL ONLY SEE THIS ONCE.
    """
    raw_key = key_manager.generate_key(name)
    return {
        "message": f"Successfully created API Key for '{name}'. Store it securely, as it cannot be retrieved again.",
        "api_key": raw_key,
    }


@app.get("/admin/keys", summary="List all generated keys")
async def list_keys(admin_key: str = Depends(get_admin_key)):
    """(Admin Only) Lists metadata for all registered keys."""
    return key_manager.list_keys()


@app.post("/admin/keys/revoke", summary="Revoke an existing API Key")
async def revoke_key(
    key_id: int = Query(
        ..., description="The ID of the key to revoke (from /admin/keys)"
    ),
    admin_key: str = Depends(get_admin_key),
):
    """(Admin Only) Permanently deactivates an API key."""
    success = key_manager.revoke_key(key_id)
    if success:
        return {"message": f"Successfully revoked Key ID {key_id}"}
    else:
        raise HTTPException(
            status_code=404, detail="Key ID not found or already inactive"
        )


@app.get("/track", summary="Get shipment status")
@limiter.limit("10/minute")
async def track_shipment(
    request: Request,
    waybill: str = Query(
        ...,
        min_length=1,
        max_length=50,
        pattern="^[a-zA-Z0-9_-]+$",
        description="The tracking number/waybill",
    ),
    courier: str = Query(
        ..., description="The courier service (BLUEDART or DELHIVERY)"
    ),
    api_key: str = Depends(get_api_key),
):
    courier = courier.strip().upper()

    if courier == "BLUEDART":
        tracker = BlueDartTracker(waybill)
        event = tracker.fetch_latest_event()

        if not event:
            raise HTTPException(
                status_code=404, detail="Tracking information not found or fetch failed"
            )

        return event

    elif courier == "DELHIVERY":
        # Launching a browser driver for each request can be slow (up to 25s)
        try:
            # Enforce global semaphore limit to prevent RAM exhaustion from headless browsers
            async with chrome_semaphore:
                # BrowserManager is synchronous __enter__ and __exit__
                with BrowserManager() as driver:
                    tracker = DelhiveryTracker(waybill)
                    event = await tracker.fetch_latest_event(driver=driver)

                    if not event:
                        raise HTTPException(
                            status_code=404,
                            detail="Tracking information not found or fetch failed",
                        )

                    return event
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Internal server error during Delhivery tracking: {str(e)}",
            )

    else:
        raise HTTPException(
            status_code=400, detail="Unsupported courier. Must be BLUEDART or DELHIVERY"
        )
