import random
import string
from fastapi import FastAPI, Body, APIRouter, HTTPException , status , Depends
from fastapi.responses import RedirectResponse
import uvicorn
from pydantic import BaseModel , HttpUrl
from typing import Optional , Annotated
from datetime import datetime
from contextlib import asynccontextmanager

# mongodb imports
from motor.motor_asyncio import AsyncIOMotorClient , AsyncIOMotorDatabase

# --- DATABASE CONFIGURATION ---
MONGO_URI = "mongodb://localhost:27017"

BASE_DOMAIN = "https://short.io"
DATABASE_NAME = "url_shortener_db"
COLLECTION_NAME = "links"


# MongoDB Client initialization (Global variables)
mongo_client : Optional[AsyncIOMotorClient] = None
db : Optional[AsyncIOMotorDatabase] = None

class ShortenRequestBody(BaseModel):
    long_url : HttpUrl
    created_at : datetime
    expires_at : datetime
    is_active : bool = True

class ShortenResponse(BaseModel):
    unique_id : str
    short_url : str
    long_url : HttpUrl
    created_at : datetime
    expires_at : Optional[datetime] = None
    is_active : bool = False

class UpdateRequest(BaseModel):
    long_url : HttpUrl
    expires_at : None
    is_active : bool

class StoreInDB(BaseModel):
    long_url: HttpUrl
    short_code: str
    owner: str 
    created_at: datetime
    expires_at: Optional[datetime] = None

# Dependency to get the MongoDB collection
async def get_db_collection():
    if db:
        return db[AsyncIOMotorDatabase]
    raise HTTPException(status_code=500 , detail= "db connection error")

CollectionDep = Annotated[AsyncIOMotorClient , Depends(get_db_collection)]

# --- BASE62 Helper  ---
BASE62_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

def encode_base62(id_num: int) -> str:
    """Encodes a positive integer into a Base62 string."""
    if id_num == 0:
        return BASE62_CHARS[0]
    base62_string = ""
    base = len(BASE62_CHARS)
    current_num = id_num
    while current_num > 0:
        remainder = current_num % base
        base62_string = BASE62_CHARS[remainder] + base62_string
        current_num = current_num // base
    return base62_string

# Mongodb connection
@asynccontextmanager
async def lifespan(app : FastAPI):
    global mongo_client , db
    mongo_client = AsyncIOMotorClient(MONGO_URI)
    db = mongo_client[DATABASE_NAME]
    print("Successfully connected to MongoDB.")
    yield
    if mongo_client:
        mongo_client.close()
        print("🛑 MongoDB connection closed.")


# --- API Setup & Routes ---
app = FastAPI(lifespan=lifespan)
router = APIRouter(prefix="/api/v1")

# API methods   
#http://127.0.0.1:8000/api/v1/
@router.get("/")
def say_hello():
    return "Hello world !" 


# @router.post("/urlshortner/long_url")
# async def create_short_url(request : ShortenRequestBody):
#     short_code = "ABCD"
#     short_url = f"{BASE_DOMAIN}/{short_code}"

#     new_link = StoreInDB(
#         long_url=request.long_url,
#         short_code=short_code,
#         owner="rupa",
#         created_at=datetime.now(),
#         expires_at=request.expires_at,
#     )
#     return ShortenResponse(
#         unique_id = short_code,
#         short_url = short_url,
#         long_url = new_link.long_url,
#         created_at = new_link.created_at,
#         expires_at = None,
#         is_active = True
#     )

# @router.get("/urlshortner/{short_code}")
# async def get_long_url(short_code : str):
#     db ={
#     "unique_id": "Hnlqxfx",
#     "short_url": "https://short.io/Hnlqxfx",
#     "long_url": "https://example.com/v1/api",
#     "created_at": "2025-11-04T21:24:08.342418",
#     "expires_at": None,
#     "is_active": True
#     }

#     if short_code not in db["unique_id"]:
#         raise HTTPException(
#             status_code=status.HTTP_404_NOT_FOUND,
#             detail="Short URL not found."
#         )


#     # 2. Check for expiration
#     if db["expires_at"]:
#         expires_at = datetime.fromisoformat(db["expires_at"])
#         if expires_at < datetime.now():
#             raise HTTPException(
#                 status_code=status.HTTP_410_GONE,
#                 detail="This link has expired."
#             )

#     return RedirectResponse(
#         url=str(db["long_url"]), 
#         status_code=status.HTTP_307_TEMPORARY_REDIRECT
#     )

#     # return ShortenResponse(
#     #     unique_id = db.unique_id,
#     #     short_url = f"{BASE_DOMAIN}/{db.unique_id}",
#     #     long_url = db.long_url,
#     #     created_at =  db.created_at,
#     #     expires_at = db.expires_at,
#     #     is_active = db.is_active
#     # )


# @router.patch("/urlshortner/{short_code}")
# def expire_long_url(request : UpdateRequest):
#     short_code = generate_unique_code()
#     short_url = f"{BASE_DOMAIN}/{short_code}"
    
#     return ShortenResponse(
#         unique_id = short_code,
#         short_url = short_url,
#         long_url = request.long_url,
#         created_at =  datetime.now(),
#         expires_at = datetime.now(),
#         is_active = False
#     )


# Include the router in the main app
app.include_router(router)

# --- Run the App ---
if __name__ == "__main__":
    print("--- Starting URL Shortener API ---")
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)