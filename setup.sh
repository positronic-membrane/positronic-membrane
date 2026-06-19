#!/usr/bin/env bash

# ==============================================================================
# POSITRONIC MEMBRANE: ONBOARDING & SETUP BOOTSTRAP SCRIPT (v0.1.0)
# ==============================================================================

set -e

# Colored output helper functions
print_green() {
    echo -e "\033[0;32m$1\033[0m"
}
print_yellow() {
    echo -e "\033[1;33m$1\033[0m"
}
print_red() {
    echo -e "\033[0;31m$1\033[0m"
}
print_cyan() {
    echo -e "\033[0;36m$1\033[0m"
}

# Print ASCII Logo
print_cyan "========================================================"
print_cyan "      ___  ___   _   _  _   _  ___ "
print_cyan "     |_  |/ _ \\ | \\ | || | | |/ __|"
print_cyan "       | | /_\\ \\|  \\| || |_| |\\__ \\"
print_cyan "   \\__/ /_/   \\_\\_|\\__| \\___/ |___/"
print_cyan "   |___/                 Positronic Membrane v0.1.0"
print_cyan "========================================================"
echo ""
print_green "Welcome to Positronic Membrane! Evolving autonomous developer swarms."
echo ""

# 1. Verify Python Installation
if ! command -v python3 &> /dev/null; then
    print_red "Error: python3 is not installed. Please install Python 3.10+ and try again."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYTHON_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PYTHON_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')

print_cyan "Checking environment prerequisites..."
echo "Found Python version: $PYTHON_VERSION"

if [ "$PYTHON_MAJOR" -ne 3 ] || [ "$PYTHON_MINOR" -lt 10 ]; then
    print_red "Error: Positronic Membrane requires Python 3.10+. Current version: $PYTHON_VERSION"
    exit 1
fi

# 2. Setup Python Virtual Environment
if [ ! -d ".venv" ]; then
    print_yellow "Creating Python virtual environment in '.venv'..."
    python3 -m venv .venv
    print_green "Virtual environment created."
else
    print_green "Existing '.venv' directory detected. Skipping creation."
fi

# 3. Activate Virtual Environment and Install Dependencies
print_yellow "Activating virtual environment and installing python dependencies..."
source .venv/bin/activate

pip install --upgrade pip
pip install -e .[dev]
print_green "Python dependencies successfully installed."

# 4. Initialize Configuration File
if [ ! -f ".env" ]; then
    print_yellow "No '.env' configuration file detected. Copying template from '.env.example'..."
    cp .env.example .env
    print_green "Created '.env' settings file. Please open and verify model names and database paths."
else
    print_green "Existing '.env' configuration detected. Skipping template copy."
fi

# 5. alignment and setup wizard prompt
echo ""
print_cyan "--------------------------------------------------------"
print_green "Setup successfully bootstrapped!"
print_cyan "--------------------------------------------------------"
echo ""
print_yellow "To complete alignment, Positronic Membrane needs to run a first-time"
print_yellow "Socratic setup wizard to write your core constitution rules."
echo ""
read -p "Would you like to run the setup wizard right now? (y/n): " RUN_WIZARD

if [[ "$RUN_WIZARD" =~ ^[Yy]$ ]]; then
    print_green "Launching Positronic Membrane Setup Wizard..."
    python3 -m src.main
else
    echo ""
    print_yellow "Skipped wizard. You can run it manually at any time using:"
    echo "  source .venv/bin/activate"
    echo "  python3 -m src.main"
fi

echo ""
print_cyan "--------------------------------------------------------"
print_green "Getting Started Instructions:"
print_cyan "--------------------------------------------------------"
echo "1. Run the local backend web server:"
echo "   $ source .venv/bin/activate"
echo "   $ janus-server"
echo ""
echo "2. Run the alignment wizard or console chat:"
echo "   $ source .venv/bin/activate"
echo "   $ janus-cli"
echo ""
echo "3. Run verification unit tests (tip: select specific test files for speed):"
echo "   $ source .venv/bin/activate"
echo "   $ pytest tests/test_web_server_fastapi.py"
echo ""
echo "4. Run via Docker Compose:"
echo "   $ docker-compose up --build"
echo "--------------------------------------------------------"
echo ""
