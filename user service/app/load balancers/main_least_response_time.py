import random
import string
from fastapi import FastAPI, Body, APIRouter, HTTPException , status , Depends, Request, Query
from fastapi.responses import RedirectResponse
import uvicorn
from pydantic import BaseModel , HttpUrl , ConfigDict , Field, EmailStr
from typing import Optional , Annotated
from datetime import datetime , timezone, timedelta
from contextlib import asynccontextmanager
import pymongo
from pymongo.errors import DuplicateKeyError
from pymongo.results import InsertOneResult, UpdateResult, DeleteResult
from bson import ObjectId
import redis.asyncio as redis
import os
import time
import asyncio

# mongodb imports
from motor.motor_asyncio import AsyncIOMotorClient , AsyncIOMotorDatabase , AsyncIOMotorCollection

# --- DATABASE CONFIGURATION ---
# Least Response Time Load Balancing Configuration
# Configure multiple MongoDB instances for response time-based routing
MONGO_URI_LIST = os.getenv("MONGO_URI", "mongodb://localhost:27017").split(",") if "," in os.getenv("MONGO_URI", "") else [
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    os.getenv("MONGO_URI_SECONDARY", "mongodb://localhost:27017")
]
DATABASE_NAME = "user_db"
COLLECTION_NAME = "users"
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6377/0")
CACHE_PREFIX = "user:"


# MongoDB Client initialization (Global variables)
mongo_client : Optional[AsyncIOMotorClient] = None
db : Optional[AsyncIOMotorDatabase] = None

class UserCreate(BaseModel):
    name: str = Field(min_length=1)
    email: EmailStr
    age: int = Field(gt=0, le=100)
    status: Optional[str] = "inactive"

class UserUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[EmailStr]=None
    age: Optional[int]= Field(None, gt=0, le=100)
    status: Optional[str] = None

class UserInDB(BaseModel):
    model_config = ConfigDict(extra="ignore") # ignore extra _id field
    name: str = Field(min_length=1)
    email: EmailStr
    age: int = Field(gt=0, le=100)
    status: Optional[str] = "inactive"

class UserResponse(BaseModel):
    name: str 
    email: EmailStr
    age: int 
    status: str

# Initialize the asynchronous Redis client
redis = redis.from_url(REDIS_URL, decode_responses=True)
print(f"Connected to Redis at {REDIS_URL}")

# --- Helper Method to Convert Internal Model to Response Model ---
def _to_response(user_in_db: UserInDB) -> UserResponse:
    """Converts a UserInDB object to a UserResponse object."""
    # This converts the internal model to the public-facing model
    return UserResponse(
        name=user_in_db.name,
        email=user_in_db.email,
        age=user_in_db.age,
        status=user_in_db.status
        )

def _get_cache_key(user_id: str) -> str:
        """Generates a Redis key for a user ID."""
        return f"{CACHE_PREFIX}{user_id}"


# Mongodb connection
@asynccontextmanager
async def lifespan(app : FastAPI):
    global mongo_client , db

    #--- MongoDB Setup with Least Response Time Pool ---
    mongo_clients = [AsyncIOMotorClient(uri) for uri in MONGO_URI_LIST]

    # Store as a fixed list for response time-based selection
    app.state.db_clients = mongo_clients

    # Initialize connection counter for each client starting at 0
    app.state.active_connections = [0] * len(mongo_clients)

    # Initial latency estimate (in seconds) for each server. 
    # We start with a small non-zero value to avoid division/multiplication by zero.
    app.state.avg_latencies = [0.01] * len(mongo_clients)
    
    # Weight for moving average (alpha). 0.1 means 10% new data, 90% history.
    app.state.latency_alpha = 0.1
    
    # Thread-safe lock for connection counting and latency updates
    app.state.metrics_lock = asyncio.Lock()
    
    print(f"✅ Initialized {len(mongo_clients)} MongoDB clients for least response time load balancing")
    print(f"📊 MongoDB URIs: {MONGO_URI_LIST}")
    print(f"⏱️  Initial latency estimates: {app.state.avg_latencies} seconds")

    yield
    for client in app.state.db_clients:
        if client:
            client.close()
            print("🛑 MongoDB connection closed.")
            await redis.close()
            print("🛑 Redis connection closed.")


# --- API Setup & Routes ---
app = FastAPI(lifespan=lifespan)
router = APIRouter(prefix="/api/v1")

# Dependency to get the MongoDB collection using least response time algorithm
async def get_db_collection(request: Request) -> AsyncIOMotorCollection:
    clients = getattr(request.app.state, "db_clients", None)
    connections = getattr(request.app.state, "active_connections", None)
    latencies = getattr(request.app.state, "avg_latencies", None)
    alpha = getattr(request.app.state, "latency_alpha", 0.1)
    lock = getattr(request.app.state, "metrics_lock", None)
    
    if not clients or connections is None or latencies is None or lock is None:
        raise HTTPException(status_code=500, detail="Database pool not initialized")

    # Thread-safe selection of best server based on response time and load
    async with lock:
        # 1. CALCULATE SCORES: Connections * Average Latency
        # We want the lowest score (best performance with least load)
        best_index = 0
        lowest_score = float('inf')

        scores = []
        for i in range(len(clients)):
            # Score combines current load (connections) with historical performance (latency)
            score = (connections[i] + 1) * latencies[i]  # +1 for the new connection
            scores.append(score)
            if score < lowest_score:
                lowest_score = score
                best_index = i

        # 2. INCREMENT CONNECTION COUNTER
        connections[best_index] += 1
        
        # Log for debugging (remove in production)
        print(f"🔍 Selected server {best_index} (score: {lowest_score:.4f}) - Scores: {[f'{s:.4f}' for s in scores]}")
        print(f"📊 Connections: {connections}, Latencies: {[f'{l:.4f}s' for l in latencies]}")
    
    # Store metrics for cleanup and timing
    request.state.selected_server_index = best_index
    request.state.start_time = time.perf_counter()
    
    # 3. RETURN THE COLLECTION
    return clients[best_index][DATABASE_NAME][COLLECTION_NAME]

# Cleanup dependency to update metrics after request
async def update_metrics(request: Request):
    """Update response time metrics and decrement connection count after request"""
    if hasattr(request.state, 'selected_server_index') and hasattr(request.state, 'start_time'):
        connections = getattr(request.app.state, "active_connections", None)
        latencies = getattr(request.app.state, "avg_latencies", None)
        alpha = getattr(request.app.state, "latency_alpha", 0.1)
        lock = getattr(request.app.state, "metrics_lock", None)
        
        if connections is not None and latencies is not None and lock is not None:
            # 4. MEASURE DURATION
            end_time = time.perf_counter()
            duration = end_time - request.state.start_time
            
            async with lock:
                server_idx = request.state.selected_server_index
                
                # 5. UPDATE MOVING AVERAGE (Exponential Moving Average)
                # Formula: new_avg = (alpha * current_duration) + ((1 - alpha) * old_avg)
                old_latency = latencies[server_idx]
                latencies[server_idx] = (alpha * duration) + ((1 - alpha) * old_latency)
                
                # 6. DECREMENT CONNECTION COUNTER
                connections[server_idx] -= 1
                
                # Log for debugging (remove in production)
                print(f"🔄 Updated server {server_idx}: duration={duration:.4f}s, new_avg={latencies[server_idx]:.4f}s, connections={connections[server_idx]}")

CollectionDep = Annotated[AsyncIOMotorCollection, Depends(get_db_collection)]

# Middleware to handle metrics updates
@app.middleware("http")
async def metrics_update_middleware(request: Request, call_next):
    """Middleware to automatically update metrics after each request"""
    response = await call_next(request)
    
    # Update metrics if a server was selected
    await update_metrics(request)
    
    return response


# API methods   
#http://127.0.0.1:8000/api/v1/
@router.get("/")
def say_hello():
    return "Hello world from Least Response Time Load Balancer!"

@router.get("/response-time-status")
async def get_response_time_status(request: Request):
    """Get current response time metrics for each MongoDB server"""
    try:
        clients = getattr(request.app.state, "db_clients", [])
        connections = getattr(request.app.state, "active_connections", [])
        latencies = getattr(request.app.state, "avg_latencies", [])
        alpha = getattr(request.app.state, "latency_alpha", 0.1)
        
        server_status = []
        for i in range(len(clients)):
            score = (connections[i] + 1) * latencies[i]
            server_status.append({
                "server_id": i,
                "uri": MONGO_URI_LIST[i] if i < len(MONGO_URI_LIST) else "unknown",
                "active_connections": connections[i] if i < len(connections) else 0,
                "avg_response_time_ms": round(latencies[i] * 1000, 2) if i < len(latencies) else 0,
                "performance_score": round(score, 4),
                "status": "fast" if latencies[i] < 0.1 else "moderate" if latencies[i] < 0.5 else "slow"
            })
        
        # Sort by performance score (lower is better)
        server_status_sorted = sorted(server_status, key=lambda x: x["performance_score"])
        
        return {
            "load_balancing_method": "Least Response Time",
            "total_servers": len(clients),
            "mongodb_uris": MONGO_URI_LIST,
            "ema_alpha": alpha,
            "servers": server_status_sorted,
            "total_active_connections": sum(connections) if connections else 0,
            "explanation": "Lower performance_score indicates better server (combines response time and current load)"
        }
    except Exception as e:
        return {"error": str(e), "status": "error"} 

# POST
@router.post(
        "/create",
        status_code = status.HTTP_201_CREATED,
        summary="Create a new user")
async def create_user(
    user_data: UserCreate,
    users_collection : CollectionDep
    ):

    await users_collection.create_index("email", unique=True)
    data_to_insert = user_data.model_dump(exclude_unset=True) # to convert it into dict
    
    try:
        result = await users_collection.insert_one(data_to_insert)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User with email '{user_data.email}' already exists."
        )
    except Exception as e:
        # Catch any exceptions during insert (like DuplicateKeyError if email has a unique index)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail=f"User creation failed due to database error: {e}"
        )
    
    new_user_id= result.inserted_id

    # Fetch the final created document
    final_document = await users_collection.find_one({"_id": new_user_id})
    if not final_document:
        # Fallback error: should be rare after a successful insert
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail="User created but could not be retrieved immediately."
        )
    
    if final_document:
        user_in_db = UserInDB(**final_document)
        response_model = _to_response(user_in_db)

        #CACHE WRITE
        await redis.set(
             _get_cache_key(str(new_user_id)),
             response_model.model_dump_json(),
             ex=3600
        )
        return response_model
    
    return None

# GET
@router.get("/fetch/{user_id}",
            summary="Get user details")
async def get_user_by_id(
    user_id : str,
    users_collection: CollectionDep):

    cache_key = _get_cache_key(user_id)
    cached_data = await redis.get(cache_key)

    if cached_data:
         print(f"✅ Cache Hit for user: {user_id}")
         return UserResponse.model_validate_json(cached_data)
    
    print(f"❌ Cache Miss for user: {user_id}. Querying DB.")

    try:
        document = await users_collection.find_one({"_id": ObjectId(user_id)})
        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user id is not found.")
        
        if document:
            user_in_db= UserInDB(**document)
            response_model =  _to_response(user_in_db)

            await redis.set(
                 cache_key,
                 response_model.model_dump_json(),
                 ex=3600
            )
            return response_model
        return None

    except ValueError:
            # Catches error if user_id is not a valid ObjectId
            return None
    except Exception as e:
            print(f"Error finding user by ID: {e}")
            return None

@router.patch("/update/{user_id}",
              summary="update user details")
async def update_user(user_id: str,
                    new_values : UserUpdate,
                    users_collection: CollectionDep):
    
    # document = await users_collection.find_one({"_id": user_id})
    # if not document:
    #     raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user id is not found.")

    #  Prepare update data
    update_data = new_values.model_dump(exclude_unset=True)
    

    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No update data provided.")
    
    try:
        #  Perform the update in MongoDB
        final_document = await users_collection.find_one_and_update(
            {"_id": ObjectId(user_id)},
            {"$set": update_data},
            return_document=pymongo.ReturnDocument.AFTER
        )

        if final_document:
            user_in_db = UserInDB(**final_document)
            response_model = _to_response(user_in_db)

            await redis.set(
                 _get_cache_key(user_id),
                 response_model.model_dump_json(),
                 ex=3600
            )
            return response_model
        return None

    except Exception:
            return None
@router.delete("/delete/{user_id}",
              summary="delete user details")
async def delete_user(user_id: str, users_collection: CollectionDep) -> bool:
        """
        Deletes a user document by ID.
        """
        try:
            result: DeleteResult = await users_collection.delete_one({"_id": ObjectId(user_id)})
            if result.deleted_count == 1:
                 await redis.delete(_get_cache_key(user_id))
                 return True
            return False
        except ValueError:
            # Catches error if user_id is not a valid ObjectId
            return False
        except Exception as e:
            print(f"Error deleting user: {e}")
            return False


@router.get("/fetchall/",
            summary="get all users")       
async def get_all_users(users_collection: CollectionDep, 
                        min_age: Optional[int] = Query(None, description="Filter for users older than this age."),
                        status_filter: Optional[str] = Query(None, description="Filter users by status (e.g., 'active', 'inactive')."),
                        limit: int = Query(20, ge=0, description="Max number of users to return.")):
        
        #query = query if query is not None else {}
        mongo_query = {}
        if min_age is not None:
            mongo_query["age"] = {"$gt": min_age}
        if status_filter is not None:
            mongo_query["status"] = status_filter

        cursor = users_collection.find(mongo_query).limit(limit)
        
        users_response_list = []
        async for user_doc in cursor:
            # 1. Validate against UserInDB
            user_in_db = UserInDB(**user_doc)
            # 2. Convert to UserResponse and append
            users_response_list.append(_to_response(user_in_db))
            
        return users_response_list


# Include the router in the main app
app.include_router(router)

# --- Run the App ---
if __name__ == "__main__":
    print("--- Starting User Service API with Least Response Time Load Balancing ---")
    print("MongoDB URIs:", MONGO_URI_LIST)
    print("Load Balancing Method: Least Response Time (routes to fastest server with least load)")
    print("Access docs at: http://127.0.0.1:8000/docs")
    uvicorn.run("main_least_response_time:app", host="127.0.0.1", port=8000, reload=True)