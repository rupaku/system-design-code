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
import uuid

# mongodb imports
from motor.motor_asyncio import AsyncIOMotorClient , AsyncIOMotorDatabase , AsyncIOMotorCollection

# --- DATABASE CONFIGURATION ---
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/user_db")
DATABASE_NAME = "user_db"
COLLECTION_NAME = "users"
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CACHE_PREFIX = "user:"
# --- SLIDING LOG CONFIGURATION ---
SLIDING_WINDOW_SECONDS = 60  # 1 minute window
MAX_REQUESTS_IN_WINDOW = 10  # Max requests allowed in that 1 minute


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
    mongo_client = AsyncIOMotorClient(MONGO_URI)
    db = mongo_client[DATABASE_NAME]
    print("Successfully connected to MongoDB.")
    app.state.db = db
    yield
    if mongo_client:
        mongo_client.close()
        print("🛑 MongoDB connection closed.")
        await redis.close()
        print("🛑 Redis connection closed.")


async def sliding_log_limiter(request: Request):
    # 1. Identify client (IP address)
    user_ip = request.client.host if request.client else "127.0.0.1"
    key = f"sliding_log:{user_ip}"
    
    now = time.time()
    window_start = now - SLIDING_WINDOW_SECONDS
    
    # We use a pipeline to ensure all commands hit Redis in one go
    async with redis.pipeline(transaction=True) as pipe:
        # 2. Remove timestamps older than our current window (the "sliding" part)
        pipe.zremrangebyscore(key, 0, window_start)
        
        # 3. Add the current request timestamp
        # We add a unique UUID to the member to ensure every request is unique 
        # even if they happen at the exact same microsecond.
        request_id = f"{now}-{uuid.uuid4()}"
        pipe.zadd(key, {request_id: now})
        
        # 4. Count how many requests are left in the set
        pipe.zcard(key)
        
        # 5. Set expiration so Redis cleans up inactive users
        pipe.expire(key, SLIDING_WINDOW_SECONDS + 10)
        
        # Execute the pipeline
        results = await pipe.execute()
        
    current_count = results[2]  # The result of ZCARD

    # 6. Check if limit is exceeded
    if current_count > MAX_REQUESTS_IN_WINDOW:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Max {MAX_REQUESTS_IN_WINDOW} requests per minute."
        )
    
    return True

# Type Alias
SlidingLogDep = Annotated[bool, Depends(sliding_log_limiter)]

# --- API Setup & Routes ---
app = FastAPI(lifespan=lifespan)
router = APIRouter(prefix="/api/v1", dependencies=[Depends(sliding_log_limiter)])

# Dependency to get the MongoDB collection
async def get_db_collection(request: Request) -> AsyncIOMotorCollection:
    global db
    _db: Optional[AsyncIOMotorDatabase] = getattr(request.app.state, "db", None)
    if _db is not None:
        return _db[COLLECTION_NAME]
    raise HTTPException(status_code=500 , detail= "db connection error")

CollectionDep = Annotated[AsyncIOMotorCollection, Depends(get_db_collection)]


# API methods   
#http://127.0.0.1:8000/api/v1/
@router.get("/")
def say_hello():
    return "Hello world !" 

# POST
@router.post(
        "/create",
        status_code = status.HTTP_201_CREATED,
        summary="Create a new user")
async def create_user(
    user_data: UserCreate,
    users_collection : CollectionDep
    ):

    users_collection.create_index("email", unique=True)
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
    print("--- Starting User Service API ---")
    print("Connect to MongoDB at:", MONGO_URI)
    print("Access docs at: http://127.0.0.1:8000/docs")
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)