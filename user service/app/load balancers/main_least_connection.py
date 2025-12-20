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
import asyncio
from threading import Lock

# mongodb imports
from motor.motor_asyncio import AsyncIOMotorClient , AsyncIOMotorDatabase , AsyncIOMotorCollection

# --- DATABASE CONFIGURATION ---
# Least Connection Load Balancing Configuration
# Configure multiple MongoDB instances for connection-based routing
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

    #--- MongoDB Setup with Least Connection Pool ---
    mongo_clients = [AsyncIOMotorClient(uri) for uri in MONGO_URI_LIST]

    # Store as a fixed list for least connection selection
    app.state.db_clients = mongo_clients

    # Initialize a counter for each client starting at 0
    # Each index in this list matches the index in db_clients
    app.state.active_connections = [0] * len(mongo_clients)
    
    # Thread-safe lock for connection counting
    app.state.connection_lock = asyncio.Lock()
    
    print(f"✅ Initialized {len(mongo_clients)} MongoDB clients for least connection load balancing")
    print(f"📊 MongoDB URIs: {MONGO_URI_LIST}")

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

# Middleware to handle connection cleanup
@app.middleware("http")
async def connection_cleanup_middleware(request: Request, call_next):
    """Middleware to automatically cleanup connections after each request"""
    response = await call_next(request)
    
    # Cleanup connection count if a server was selected
    await cleanup_connection(request)
    
    return response

# Dependency to get the MongoDB collection using least connection algorithm
async def get_db_collection(request: Request) -> AsyncIOMotorCollection:
    clients = getattr(request.app.state, "db_clients", None)
    connections = getattr(request.app.state, "active_connections", None)
    lock = getattr(request.app.state, "connection_lock", None)
    
    if not clients or connections is None or lock is None:
        raise HTTPException(status_code=500, detail="DB clients not initialized")
    
    # Thread-safe selection of least busy server
    async with lock:
        # 1. FIND THE LEAST BUSY SERVER
        # Find the index of the minimum value in the active_connections list
        min_val = min(connections)
        index = connections.index(min_val)

        # 2. INCREMENT THE COUNTER
        connections[index] += 1
        
        # Log for debugging (remove in production)
        print(f"🔍 Selected server {index} (connections: {connections[index]}) - Current load: {connections}")
    
    # Store the selected index for cleanup
    request.state.selected_server_index = index
    
    # 3. RETURN THE COLLECTION
    return clients[index][DATABASE_NAME][COLLECTION_NAME]

# Cleanup dependency to decrement connection count
async def cleanup_connection(request: Request):
    """Cleanup function to decrement connection count after request"""
    if hasattr(request.state, 'selected_server_index'):
        connections = getattr(request.app.state, "active_connections", None)
        lock = getattr(request.app.state, "connection_lock", None)
        
        if connections is not None and lock is not None:
            async with lock:
                # 4. DECREMENT THE COUNTER (Cleanup)
                connections[request.state.selected_server_index] -= 1
                print(f"🔄 Cleaned up server {request.state.selected_server_index} - Current load: {connections}")

CollectionDep = Annotated[AsyncIOMotorCollection, Depends(get_db_collection)]


# API methods   
#http://127.0.0.1:8000/api/v1/
@router.get("/")
def say_hello():
    return "Hello world from Least Connection Load Balancer!"

@router.get("/connection-status")
async def get_connection_status(request: Request):
    """Get current connection counts for each MongoDB server"""
    try:
        clients = getattr(request.app.state, "db_clients", [])
        connections = getattr(request.app.state, "active_connections", [])
        
        return {
            "load_balancing_method": "Least Connection",
            "total_servers": len(clients),
            "mongodb_uris": MONGO_URI_LIST,
            "current_connections": {
                f"server_{i}": {
                    "uri": MONGO_URI_LIST[i] if i < len(MONGO_URI_LIST) else "unknown",
                    "active_connections": connections[i] if i < len(connections) else 0
                }
                for i in range(len(clients))
            },
            "total_active_connections": sum(connections) if connections else 0
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
    print("--- Starting User Service API with Least Connection Load Balancing ---")
    print("MongoDB URIs:", MONGO_URI_LIST)
    print("Load Balancing Method: Least Connection (routes to server with fewest active connections)")
    print("Access docs at: http://127.0.0.1:8000/docs")
    uvicorn.run("main_least_connection:app", host="127.0.0.1", port=8000, reload=True)