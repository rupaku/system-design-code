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
    print("inside get_db_collection")
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

# POST
@router.post(
        "/shorten",
        response_model=ShortenResponse,
        status_code = status.HTTP_201_CREATED,
        summary="Create a new short URL")
async def create_short_url(
    request : ShortenRequestBody,
    links_collection : CollectionDep = None
    ):
    short_code : str
    link_data = request.model_dump(exclude_unset=True) # to convert it into dict
    link_data["created_at"] = datetime.now()
    link_data['short_code'] = short_code
    
    # Insert the document
    placeholder_data = link_data.copy()
    placeholder_data['short_code'] = "TEMP_CODE"
    result = await links_collection.insert_one(placeholder_data)
    new_link_id = result.inserted_id

    seq_num = int.from_bytes(new_link_id.binary, 'big') % (10**10) # Get a large unique integer
    short_code = encode_base62(seq_num)

    #Update the document with the final short code
    await links_collection.update_one(
        {
            "_id" : new_link_id
        },
        {
            "$set" : {"short_code" : short_code}
        }
    )

    # Fetch the final created document
    final_document = await links_collection.find_one({"short_code": short_code})
    new_link = StoreInDB(**final_document)
    return ShortenResponse(
        unique_id = new_link.short_code,
        short_url = f"{BASE_DOMAIN}/{new_link.short_code}",
        long_url = new_link.long_url,
        created_at = new_link.created_at,
        expires_at = new_link.expires_at,
        is_active = True
    )

# Redirect
@app.get(
    "/{short_code}",
    summary="Redirect a short URL",
    responses={
        307: {"description": "Temporary Redirect to the long URL."},
        404: {"description": "Short URL not found."},
        410: {"description": "Link has expired."}
    }
)
async def handle_redirect(short_code: str, links_collection: CollectionDep = None):
    # Find the link
    document = await links_collection.find_one({"shortCode": short_code})
    if not document:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Short URL not found.")
    
    link_in_db = StoreInDB(**document)

    # Check for expiration
    if link_in_db.expires_at and link_in_db.expires_at < datetime.now():
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="This link has expired.")


    # Issue the redirect
    return RedirectResponse(
        url=str(link_in_db.long_url),
        status_code=status.HTTP_307_TEMPORARY_REDIRECT
    )

# GET
@router.get("/Links/{short_code}",
            response_model=ShortenResponse,
            summary="Get link details and analytics")
async def get_long_url(
    short_code : str,
    links_collection: CollectionDep = None):

    document = await links_collection.find_one({"short_code": short_code})
    if not document:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Short URL not found.")
    
    link_in_db = StoreInDB(**document)

    # check for expiration 
    if link_in_db["expires_at"]:
        expires_at = datetime.fromisoformat(link_in_db["expires_at"])
        if expires_at < datetime.now():
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="This link has expired."
            )

    return ShortenResponse(
        unique_id = link_in_db.unique_id,
        short_url = f"{BASE_DOMAIN}/{link_in_db.short_code}",
        long_url = link_in_db.long_url,
        created_at =  link_in_db.created_at,
        expires_at = link_in_db.expires_at,
        is_active = link_in_db.is_active
    )


@router.patch("/links/{short_code}",
              response_model=ShortenResponse,
              summary="Update a short URL")
async def expire_long_url(short_code: str,
                    request : UpdateRequest,
                    links_collection: CollectionDep = None):
    
    document = await links_collection.find_one({"short_code": short_code})
    if not document:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Short URL not found.")
    
    link_in_db = StoreInDB(**document)

    #  Prepare update data
    update_data = request.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No update data provided.")
    
    #  Perform the update in MongoDB
    result = await links_collection.update_one(
        {"short_code": short_code},
        {"$set": update_data}
    )

    # Fetch the updated document (or simulate the update for the response)
    if result.modified_count == 0 and result.matched_count == 1:
        # No actual change, but document found. Use the current data.
        updated_document = document
    else:
        # Fetch the newly updated document
        updated_document = await links_collection.find_one({"short_code": short_code})

    updated_link = StoreInDB(**updated_document)

    return ShortenResponse(
        unique_id = updated_link.short_code,
        short_url = f"{BASE_DOMAIN}/{updated_link.shortCode}",
        long_url = updated_link.long_url,
        created_at =  updated_link.created_at,
        expires_at = updated_link.expires_at,
        is_active = False
    )


# Include the router in the main app
app.include_router(router)

# --- Run the App ---
if __name__ == "__main__":
    print("--- Starting URL Shortener API ---")
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)