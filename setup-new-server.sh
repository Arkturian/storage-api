#!/bin/bash
#
# Storage API - New Server Setup Script
#
# This script provisions a new production server for a customer deployment.
#
# Usage:
#   ./setup-new-server.sh <customer-name> <domain>
#
# Example:
#   ./setup-new-server.sh oneal api-oneal.arkturian.com
#

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Check arguments
if [ $# -lt 2 ]; then
    echo -e "${RED}Usage: $0 <customer-name> <domain>${NC}"
    echo -e "${YELLOW}Example: $0 oneal api-oneal.arkturian.com${NC}"
    exit 1
fi

CUSTOMER_NAME=$1
DOMAIN=$2
INSTALL_DIR="/var/www/api-storage-${CUSTOMER_NAME}"

echo -e "${BLUE}╔═══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║         Storage API - New Server Setup                        ║${NC}"
echo -e "${BLUE}║         Customer: ${CUSTOMER_NAME}                                  ║${NC}"
echo -e "${BLUE}║         Domain: ${DOMAIN}                          ║${NC}"
echo -e "${BLUE}╚═══════════════════════════════════════════════════════════════╝${NC}\n"

# Confirmation
read -p "$(echo -e ${YELLOW}This will set up a new production server. Continue? [y/N]${NC} ) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo -e "${RED}Aborted.${NC}"
    exit 1
fi

#############################################################################
# 1. SYSTEM DEPENDENCIES
#############################################################################

echo -e "\n${GREEN}[1/10] Installing system dependencies...${NC}"

apt-get update
apt-get install -y \
    python3.11 \
    python3.11-dev \
    python3-pip \
    git \
    nginx \
    supervisor \
    build-essential \
    libsqlite3-dev \
    curl \
    certbot \
    python3-certbot-nginx

#############################################################################
# 2. PYENV SETUP
#############################################################################

echo -e "\n${GREEN}[2/10] Setting up pyenv...${NC}"

if [ ! -d "/root/.pyenv" ]; then
    curl https://pyenv.run | bash

    # Add to bashrc
    echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.bashrc
    echo 'export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.bashrc
    echo 'eval "$(pyenv init -)"' >> ~/.bashrc

    source ~/.bashrc
fi

# Install Python 3.11.9
/root/.pyenv/bin/pyenv install -s 3.11.9
/root/.pyenv/bin/pyenv global 3.11.9

#############################################################################
# 3. CLONE REPOSITORY
#############################################################################

echo -e "\n${GREEN}[3/10] Cloning repository...${NC}"

mkdir -p /var/www
cd /var/www

if [ -d "$INSTALL_DIR" ]; then
    echo -e "${YELLOW}Directory ${INSTALL_DIR} already exists. Using existing.${NC}"
else
    git clone https://github.com/yourusername/storage-api.git "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"
git checkout main  # Use main branch for production

#############################################################################
# 4. PYTHON DEPENDENCIES
#############################################################################

echo -e "\n${GREEN}[4/10] Installing Python dependencies...${NC}"

/root/.pyenv/versions/3.11.9/bin/pip install --upgrade pip
/root/.pyenv/versions/3.11.9/bin/pip install -r requirements.txt

#############################################################################
# 5. ENVIRONMENT CONFIGURATION
#############################################################################

echo -e "\n${GREEN}[5/10] Creating environment configuration...${NC}"

# Prompt for sensitive information
read -p "OpenAI API Key: " OPENAI_API_KEY
read -p "Tenant ID [${CUSTOMER_NAME}]: " TENANT_ID
TENANT_ID=${TENANT_ID:-$CUSTOMER_NAME}

# Create .env file
cat > "$INSTALL_DIR/.env" <<EOF
# Environment
ENVIRONMENT=production
TENANT_ID=${TENANT_ID}

# Database
DATABASE_URL=sqlite:///${INSTALL_DIR}/storage.db

# OpenAI
OPENAI_API_KEY=${OPENAI_API_KEY}

# ChromaDB
# NOTE: the code reads CHROMA_DB_PATH. An earlier version of this script wrote
# CHROMA_PERSIST_DIR, which appears nowhere in the codebase — the setting was
# silently ignored and Chroma landed on its built-in default path.
CHROMA_DB_PATH=${INSTALL_DIR}/chroma_data

# File storage — absolute, so it never depends on the working directory
STORAGE_UPLOAD_DIR=${INSTALL_DIR}/uploads/storage

# Security
SECRET_KEY=$(openssl rand -hex 32)
# API_KEY gates the whole API. Without it config.py falls back to a value that
# is public in our own docs and treated as compromised — a customer install
# must never run on it. Generated fresh per install.
API_KEY=$(openssl rand -hex 32)
# Belongs to a DIFFERENT tenant of ours and defaults to a demo token in
# config.py. Emptied explicitly so no foreign tenant key is carried along.
ONEAL_API_KEY=

# Logging
LOG_LEVEL=INFO
EOF

chmod 600 "$INSTALL_DIR/.env"

#############################################################################
# 6. INITIALIZE DATABASE
#############################################################################

echo -e "\n${GREEN}[6/10] Initializing database...${NC}"

cd "$INSTALL_DIR"
source .env
/root/.pyenv/versions/3.11.9/bin/python3 -c "
from models import Base, engine
Base.metadata.create_all(bind=engine)
print('✅ Database initialized')
"

#############################################################################
# 7. SYSTEMD SERVICE
#############################################################################

echo -e "\n${GREEN}[7/10] Creating systemd service...${NC}"

cat > "/etc/systemd/system/storage-api-${CUSTOMER_NAME}.service" <<EOF
[Unit]
Description=Storage API Service for ${CUSTOMER_NAME}
After=network.target

[Service]
Type=notify
User=root
WorkingDirectory=${INSTALL_DIR}
Environment="PATH=/root/.pyenv/versions/3.11.9/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=${INSTALL_DIR}/.env

ExecStart=/root/.pyenv/versions/3.11.9/bin/gunicorn \\
    -w 4 \\
    -k uvicorn.workers.UvicornWorker \\
    --timeout 1200 \\
    --graceful-timeout 20 \\
    --keep-alive 5 \\
    --bind 0.0.0.0:8002 \\
    main:app \\
    --access-logfile /var/log/storage-api-${CUSTOMER_NAME}-access.log \\
    --error-logfile /var/log/storage-api-${CUSTOMER_NAME}-error.log \\
    --log-level info

Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "storage-api-${CUSTOMER_NAME}.service"

#############################################################################
# 8. NGINX CONFIGURATION
#############################################################################

echo -e "\n${GREEN}[8/10] Configuring Nginx...${NC}"

cat > "/etc/nginx/sites-available/storage-api-${CUSTOMER_NAME}" <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

    # Redirect HTTP to HTTPS
    return 301 https://\$server_name\$request_uri;
}

server {
    listen 443 ssl http2;
    server_name ${DOMAIN};

    # SSL certificates (will be generated by certbot)
    ssl_certificate /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;

    # Security headers
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;

    # Proxy to FastAPI
    location / {
        proxy_pass http://127.0.0.1:8002;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # WebSocket support (if needed)
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";

        # Timeouts
        proxy_connect_timeout 600;
        proxy_send_timeout 600;
        proxy_read_timeout 600;
    }

    # Health check endpoint
    location /health {
        proxy_pass http://127.0.0.1:8002/health;
        access_log off;
    }

    # File upload size
    client_max_body_size 100M;
}
EOF

# Enable site
ln -sf "/etc/nginx/sites-available/storage-api-${CUSTOMER_NAME}" "/etc/nginx/sites-enabled/"

# Test nginx config
nginx -t

#############################################################################
# 9. SSL CERTIFICATE
#############################################################################

echo -e "\n${GREEN}[9/10] Obtaining SSL certificate...${NC}"

# Temporarily start service for certbot verification
systemctl start "storage-api-${CUSTOMER_NAME}.service"
sleep 3

# Obtain certificate
certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos --email admin@${DOMAIN}

#############################################################################
# 10. START SERVICES
#############################################################################

echo -e "\n${GREEN}[10/10] Starting services...${NC}"

systemctl restart "storage-api-${CUSTOMER_NAME}.service"
systemctl restart nginx

sleep 3

# Check status
systemctl status "storage-api-${CUSTOMER_NAME}.service" --no-pager

#############################################################################
# SETUP COMPLETE
#############################################################################

echo -e "\n${BLUE}╔═══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║                  SETUP COMPLETE ✅                              ║${NC}"
echo -e "${BLUE}╚═══════════════════════════════════════════════════════════════╝${NC}\n"

echo -e "${GREEN}✅ Storage API deployed successfully!${NC}\n"

echo -e "${YELLOW}Service Details:${NC}"
echo -e "  - Customer: ${CUSTOMER_NAME}"
echo -e "  - Domain: https://${DOMAIN}"
echo -e "  - Install directory: ${INSTALL_DIR}"
echo -e "  - Service: storage-api-${CUSTOMER_NAME}.service"
echo -e "  - Database: ${INSTALL_DIR}/storage.db"
echo -e "  - ChromaDB: ${INSTALL_DIR}/chroma_data"
echo -e "  - Logs: /var/log/storage-api-${CUSTOMER_NAME}-*.log\n"

echo -e "${YELLOW}Management Commands:${NC}"
echo -e "  - Status: systemctl status storage-api-${CUSTOMER_NAME}.service"
echo -e "  - Restart: systemctl restart storage-api-${CUSTOMER_NAME}.service"
echo -e "  - Logs: tail -f /var/log/storage-api-${CUSTOMER_NAME}-error.log"
echo -e "  - Health: curl https://${DOMAIN}/health\n"

echo -e "${YELLOW}Next Steps:${NC}"
echo -e "  1. Test API: curl https://${DOMAIN}/health"
echo -e "  2. Create API keys for customer"
echo -e "  3. Import initial data (if required)"
echo -e "  4. Set up monitoring (Sentry, etc.)"
echo -e "  5. Configure backups\n"

# Test health endpoint
echo -e "${YELLOW}Testing health endpoint...${NC}"
sleep 2
curl -f "https://${DOMAIN}/health" && echo -e "\n${GREEN}✅ Health check passed!${NC}" || echo -e "\n${RED}❌ Health check failed!${NC}"

echo -e "\n${GREEN}Setup completed successfully! 🎉${NC}\n"
