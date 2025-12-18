
from fastapi.testclient import TestClient
from main import app
import uuid

client = TestClient(app)

def test_duplicate_user_creation():
    # Generate a unique email for this run to avoid collision with existing data
    email = f"test_{uuid.uuid4()}@example.com"
    user_data = {
        "name": "Test User",
        "email": email,
        "age": 25,
        "status": "active"
    }

    print(f"Attempting to create first user with email: {email}")
    response1 = client.post("/api/v1/create", json=user_data)
    if response1.status_code == 201:
        print("First user created successfully.")
    else:
        print(f"Failed to create first user: {response1.status_code} - {response1.text}")
        return

    print(f"Attempting to create second user with SAME email: {email}")
    response2 = client.post("/api/v1/create", json=user_data)
    
    if response2.status_code == 201:
        print("FAIL: Second user created successfully! Duplicate record exception is NOT working.")
    elif response2.status_code == 409:
        print("SUCCESS: Second user creation failed with 409 Conflict. Duplicate record exception IS working.")
    else:
        print(f"Unexpected response for second user: {response2.status_code} - {response2.text}")

if __name__ == "__main__":
    test_duplicate_user_creation()
