#!/bin/bash
# Run this once on each Pi to configure and start services.
# Automatically detects the current user.

USER=$(whoami)
REPO_DIR="/home/$USER/Egocentric_Data_capture_Hardware_mono16"

echo "Setting up services for user: $USER"

# Create .env.polling file with correct BACKEND_URL
cat > "$REPO_DIR/.env.polling" << 'EOF'
BACKEND_URL=https://robotics-backend-service-dev-966418509400.europe-west1.run.app
EOF

echo ".env.polling created ✓"

# Copy service files from repo to systemd, replacing %i with actual username
sed "s/%i/$USER/g" "$REPO_DIR/capture-daemon.service" | sudo tee /etc/systemd/system/capture-daemon.service > /dev/null
sed "s/%i/$USER/g" "$REPO_DIR/polling-agent.service"  | sudo tee /etc/systemd/system/polling-agent.service  > /dev/null

# Reload systemd and start services
sudo systemctl daemon-reload

sudo systemctl enable capture-daemon
sudo systemctl start capture-daemon

sudo systemctl enable polling-agent
sudo systemctl start polling-agent

echo "✅ Services started for $USER"
sudo systemctl status capture-daemon --no-pager
sudo systemctl status polling-agent --no-pager
