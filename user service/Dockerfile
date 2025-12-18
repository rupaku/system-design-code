# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /usr/src/app

# Copy dependency files
COPY requirements.txt .

# Install dependencies, including uvicorn and gunicorn for serving
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY ./app .

# Command to run the application using Gunicorn/Uvicorn
# We use Gunicorn to manage Uvicorn workers, binding to port 8000
CMD ["gunicorn", "main:app", "--workers", "4", "--worker-class", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000"]