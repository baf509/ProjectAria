#!/bin/bash
# Helper script to start Docker and test GLM-4.7

set -e

echo "🐳 Checking Docker status..."

# Check if Docker daemon is running
if ! docker ps &> /dev/null; then
    echo "❌ Docker daemon is not running."
    echo ""
    echo "Please start Docker with one of these methods:"
    echo ""
    echo "Option 1 - System Docker:"
    echo "  sudo systemctl start docker"
    echo ""
    echo "Option 2 - Docker Desktop:"
    echo "  Open the Docker Desktop application"
    echo ""
    echo "After starting Docker, run this script again:"
    echo "  ./start-docker-and-test.sh"
    echo ""
    exit 1
fi

echo "✅ Docker is running!"
echo ""

# Now run the GLM-4.7 setup script
echo "🚀 Running GLM-4.7 setup script..."
./start-aria-glm4.sh
