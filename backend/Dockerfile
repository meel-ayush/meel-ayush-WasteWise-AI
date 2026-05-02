# Use a slim Python 3.11 image to keep the size down
FROM python:3.11-slim

# Install system dependencies required for OpenCV, EasyOCR, and ML libraries
# (libgl1 and libglib are strictly required by OpenCV/EasyOCR to prevent crashes)
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set up a new user named "user" with user ID 1000
# THIS IS STRICTLY REQUIRED by Hugging Face Spaces for security
RUN useradd -m -u 1000 user

# Switch to the new "user"
USER user

# Set environment variables
# PYTHONUNBUFFERED=1 ensures that logs print immediately in the Hugging Face dashboard
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PORT=7860

# Set the working directory
WORKDIR $HOME/app

# Copy the requirements file first to leverage Docker cache (makes rebuilding faster)
COPY --chown=user requirements.txt .

# Install Python dependencies 
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the rest of your backend code into the container
COPY --chown=user . .

# Expose the specific port Hugging Face requires
EXPOSE 7860

# Command to run the FastAPI server on port 7860
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
