#!/bin/bash
# Run this once on each Pi to configure and start services.
# Automatically detects the current user.
# .env.polling must already exist with BACKEND_URL set.

USER=$(whoami)
REPO_DIR="/home/$USER/Egocentric_Data_capture_Hardware_mono16"

echo "Setting up services for user: $USER"

# Check .env.polling exists
if [ ! -f "$REPO_DIR/.env.polling" ]; then
    echo "❌ Error: .env.polling not found at $REPO_DIR/.env.polling"
    exit 1
fi

echo ".env.polling found ✓"

# Copy service files from repo to systemd, replacing %i with actual username
sed "s/%i/$USER/g" "$REPO_DIR/capture-daemon.service" | sudo tee /etc/systemd/system/capture-daemon.service > /dev/null
sed "s/%i/$USER/g" "$REPO_DIR/polling-agent.service"  | sudo tee /etc/systemd/system/polling-agent.service  > /dev/null

echo "Service files copied ✓"

# Reload systemd and start services
sudo systemctl daemon-reload

sudo systemctl enable capture-daemon
sudo systemctl start capture-daemon

sudo systemctl enable polling-agent
sudo systemctl start polling-agent

echo "✅ Services started for $USER"
sudo systemctl status capture-daemon --no-pager
sudo systemctl status polling-agent --no-pager
