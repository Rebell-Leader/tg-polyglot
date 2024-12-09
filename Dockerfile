# Base image
FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    wget \
    sqlite3 \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Install youtube-dl (or yt-dlp as an alternative)
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp && \
    chmod a+rx /usr/local/bin/yt-dlp

# Create working directory
WORKDIR /app

# Copy Python dependencies
COPY requirements.txt /app/

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy translation script and other files
COPY . /app/

# Install Node.js dependencies for the translation script
RUN npm install -g vot-cli

# Expose port (optional if needed for health checks)
EXPOSE 8080

# Entry point
CMD ["python", "bot.py"]
