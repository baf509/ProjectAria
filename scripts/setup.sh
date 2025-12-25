#!/bin/bash
set -e

# ARIA Setup Script
# Installs all dependencies needed to run ARIA locally

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_command() {
    if command -v "$1" &> /dev/null; then
        log_success "$1 is already installed"
        return 0
    else
        log_warning "$1 is not installed"
        return 1
    fi
}

# Detect OS
detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS=$ID
        VERSION=$VERSION_ID
    else
        log_error "Cannot detect OS"
        exit 1
    fi
    log_info "Detected OS: $OS $VERSION"
}

# Install Docker
install_docker() {
    log_info "Installing Docker..."

    case "$OS" in
        arch|cachyos|manjaro)
            sudo pacman -S --noconfirm docker docker-compose
            ;;
        ubuntu|debian|pop)
            sudo apt-get update
            sudo apt-get install -y docker.io docker-compose
            ;;
        fedora)
            sudo dnf install -y docker docker-compose
            ;;
        *)
            log_error "Unsupported OS for automatic Docker installation: $OS"
            log_info "Please install Docker manually: https://docs.docker.com/engine/install/"
            return 1
            ;;
    esac

    # Enable and start Docker service
    sudo systemctl enable docker
    sudo systemctl start docker

    # Add user to docker group
    sudo usermod -aG docker $USER

    log_success "Docker installed successfully"
    log_warning "You need to log out and back in for group changes to take effect"
}

# Install Ollama
install_ollama() {
    log_info "Installing Ollama..."

    if check_command ollama; then
        return 0
    fi

    curl -fsSL https://ollama.com/install.sh | sh

    # Enable and start Ollama service
    sudo systemctl enable ollama
    sudo systemctl start ollama

    log_success "Ollama installed successfully"
}

# Pull embedding model
pull_embedding_model() {
    log_info "Pulling qwen3-embedding:0.6b model..."

    # Wait for Ollama to be ready
    local max_attempts=30
    local attempt=0

    while [ $attempt -lt $max_attempts ]; do
        if curl -s http://localhost:11434/api/tags &> /dev/null; then
            break
        fi
        log_info "Waiting for Ollama to start... ($((attempt + 1))/$max_attempts)"
        sleep 2
        attempt=$((attempt + 1))
    done

    if [ $attempt -eq $max_attempts ]; then
        log_error "Ollama failed to start"
        return 1
    fi

    # Check if model is already pulled
    if ollama list | grep -q "qwen3-embedding:0.6b"; then
        log_success "qwen3-embedding:0.6b is already pulled"
        return 0
    fi

    ollama pull qwen3-embedding:0.6b
    log_success "Embedding model pulled successfully"
}

# Setup .env file
setup_env_file() {
    log_info "Checking .env file..."

    if [ -f .env ]; then
        log_success ".env file already exists"
        return 0
    fi

    if [ ! -f .env.example ]; then
        log_error ".env.example not found"
        return 1
    fi

    log_info "Creating .env file from .env.example..."
    cp .env.example .env
    log_success ".env file created"
    log_warning "Please edit .env and add your API keys if needed"
}

# Install Python dependencies (for CLI)
install_python_deps() {
    log_info "Installing Python dependencies..."

    # Check if Python 3 is installed
    if ! check_command python3; then
        log_error "Python 3 is not installed"
        return 1
    fi

    # Install pip if not present
    if ! check_command pip3; then
        log_info "Installing pip..."
        case "$OS" in
            arch|cachyos|manjaro)
                sudo pacman -S --noconfirm python-pip
                ;;
            ubuntu|debian|pop)
                sudo apt-get install -y python3-pip
                ;;
            fedora)
                sudo dnf install -y python3-pip
                ;;
        esac
    fi

    # Install CLI in development mode
    if [ -d cli ]; then
        log_info "Installing ARIA CLI..."
        cd cli
        pip3 install --user -e .
        cd ..
        log_success "CLI installed"
    fi
}

# Main installation flow
main() {
    echo "================================================"
    echo "         ARIA Setup Script"
    echo "================================================"
    echo ""

    # Change to project root
    cd "$(dirname "$0")/.."

    # Detect OS
    detect_os
    echo ""

    # Check what's already installed
    log_info "Checking existing installations..."
    echo ""

    DOCKER_INSTALLED=false
    OLLAMA_INSTALLED=false

    if check_command docker; then
        DOCKER_INSTALLED=true
    fi

    if check_command ollama; then
        OLLAMA_INSTALLED=true
    fi

    echo ""

    # Install Docker if needed
    if [ "$DOCKER_INSTALLED" = false ]; then
        read -p "Install Docker? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            install_docker
            NEEDS_RELOGIN=true
        fi
    fi
    echo ""

    # Install Ollama if needed
    if [ "$OLLAMA_INSTALLED" = false ]; then
        read -p "Install Ollama? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            install_ollama
        fi
    fi
    echo ""

    # Pull embedding model
    if check_command ollama; then
        read -p "Pull qwen3-embedding:0.6b model? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            pull_embedding_model
        fi
    fi
    echo ""

    # Setup .env file
    setup_env_file
    echo ""

    # Install Python dependencies
    read -p "Install Python CLI dependencies? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        install_python_deps
    fi
    echo ""

    # Summary
    echo "================================================"
    log_success "Setup complete!"
    echo "================================================"
    echo ""

    if [ "$NEEDS_RELOGIN" = true ]; then
        log_warning "IMPORTANT: You need to log out and back in for Docker group changes to take effect"
        echo ""
    fi

    log_info "Next steps:"
    echo "  1. Edit .env file and add any API keys you need"
    echo "  2. Start the services:"
    echo "     docker compose up -d"
    echo ""
    echo "  3. Check service health:"
    echo "     docker compose ps"
    echo "     curl http://localhost:8000/api/v1/health"
    echo ""
    echo "  4. Access the UI:"
    echo "     http://localhost:3000"
    echo ""

    log_info "For more information, see:"
    echo "  - README.md"
    echo "  - GETTING_STARTED.md"
    echo "  - CLAUDE.md"
    echo ""
}

# Run main function
main
