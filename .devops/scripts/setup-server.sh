#!/usr/bin/env bash

#############################################
# Server Setup Script
#
# Initial server provisioning for a deployment target.
# Sets up Python venv, dependencies, systemd service, and nginx config.
#############################################

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Default values
SERVER_CONFIG=""
SERVER_NAME=""
FORCE_REINSTALL=false

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEVOPS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$DEVOPS_DIR/.." && pwd)"

usage() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

Initial server setup for deployment target.

OPTIONS:
    --server NAME           Server name (reads .devops/servers/NAME.yaml)
    --config PATH          Direct path to server config YAML
    --force                Force reinstall even if already set up
    -h, --help             Show this help message

EXAMPLES:
    $(basename "$0") --server arkserver
    $(basename "$0") --server production --force
    $(basename "$0") --config /path/to/custom-config.yaml

EOF
    exit 1
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --server)
            SERVER_NAME="$2"
            SERVER_CONFIG="$DEVOPS_DIR/servers/$2.yaml"
            shift 2
            ;;
        --config)
            SERVER_CONFIG="$2"
            shift 2
            ;;
        --force)
            FORCE_REINSTALL=true
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            usage
            ;;
    esac
done

# Validation
if [ -z "$SERVER_CONFIG" ]; then
    echo -e "${RED}❌ Error: --server or --config required${NC}"
    usage
fi

if [ ! -f "$SERVER_CONFIG" ]; then
    echo -e "${RED}❌ Error: Server config not found: $SERVER_CONFIG${NC}"
    exit 1
fi

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}🚀 Server Setup Script${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "Config: ${YELLOW}$SERVER_CONFIG${NC}"
echo ""

# Simple YAML parser (parse key: value pairs)
parse_yaml() {
    local file=$1
    local prefix=$2
    local s='[[:space:]]*' w='[a-zA-Z0-9_]*' fs=$(echo @|tr @ '\034')
    sed -ne "s|^\($s\):|\1|" \
         -e "s|^\($s\)\($w\)$s:$s[\"']\(.*\)[\"']$s\$|\1$fs\2$fs\3|p" \
         -e "s|^\($s\)\($w\)$s:$s\(.*\)$s\$|\1$fs\2$fs\3|p" "$file" |
    awk -F$fs '{
        indent = length($1)/2;
        vname[indent] = $2;
        for (i in vname) {if (i > indent) {delete vname[i]}}
        if (length($3) > 0) {
            vn=""; for (i=0; i<indent; i++) {vn=(vn)(vname[i])("_")}
            printf("%s%s%s=\"%s\"\n", "'$prefix'", vn, $2, $3);
        }
    }'
}

# Load server configuration
echo -e "${YELLOW}📋 Loading server configuration...${NC}"
eval $(parse_yaml "$SERVER_CONFIG" "CFG_")

# Extract config values
SERVER_HOST="${CFG_server_host:-}"
SERVER_USER="${CFG_server_user:-root}"
SERVER_TYPE="${CFG_server_type:-python-api}"
DEPLOY_PATH="${CFG_server_deploy_path:-}"
SERVICE_NAME="${CFG_service_name:-}"
SERVICE_PORT="${CFG_service_port:-8000}"
NGINX_ENABLED="${CFG_nginx_enabled:-true}"
NGINX_SERVER_NAME="${CFG_nginx_server_name:-}"
PYTHON_VERSION="${CFG_python_version:-3.11}"
REQUIREMENTS_FILE="${CFG_python_requirements:-requirements.txt}"
VENV_PATH="${CFG_python_venv_path:-venv}"

# Validate required fields
if [ -z "$SERVER_HOST" ] || [ -z "$DEPLOY_PATH" ] || [ -z "$SERVICE_NAME" ]; then
    echo -e "${RED}❌ Error: Missing required config fields (server.host, server.deploy_path, service.name)${NC}"
    exit 1
fi

echo -e "${GREEN}✅ Configuration loaded${NC}"
echo -e "  Server: ${YELLOW}$SERVER_USER@$SERVER_HOST${NC}"
echo -e "  Deploy path: ${YELLOW}$DEPLOY_PATH${NC}"
echo -e "  Service: ${YELLOW}$SERVICE_NAME${NC}"
echo -e "  Port: ${YELLOW}$SERVICE_PORT${NC}"
echo ""

# Check if already installed
echo -e "${YELLOW}🔍 Checking if server is already set up...${NC}"
if ssh "$SERVER_USER@$SERVER_HOST" "[ -d '$DEPLOY_PATH' ] && [ -f '/etc/systemd/system/$SERVICE_NAME.service' ]" 2>/dev/null; then
    if [ "$FORCE_REINSTALL" = false ]; then
        echo -e "${YELLOW}⚠️  Server appears to be already set up${NC}"
        echo -e "${YELLOW}   Use --force to reinstall${NC}"
        exit 0
    else
        echo -e "${YELLOW}⚠️  --force specified, reinstalling...${NC}"
    fi
fi

# Create deploy directory
echo -e "${YELLOW}📁 Creating deploy directory...${NC}"
ssh "$SERVER_USER@$SERVER_HOST" "mkdir -p '$DEPLOY_PATH'"

# Copy source files
echo -e "${YELLOW}📦 Copying source files...${NC}"
rsync -az --exclude='venv' --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' --exclude='node_modules' \
    "$REPO_ROOT/" "$SERVER_USER@$SERVER_HOST:$DEPLOY_PATH/"

# Python API setup
if [ "$SERVER_TYPE" = "python-api" ]; then
    echo -e "${YELLOW}🐍 Setting up Python environment...${NC}"

    ssh "$SERVER_USER@$SERVER_HOST" bash << EOF
set -e
cd "$DEPLOY_PATH"

# Create venv
echo "Creating Python venv..."
python3 -m venv $VENV_PATH

# Install dependencies
echo "Installing dependencies..."
source $VENV_PATH/bin/activate
pip install --upgrade pip -q
pip install -r $REQUIREMENTS_FILE -q

echo "✅ Python environment ready"
EOF
fi

# Create necessary directories for databases
echo -e "${YELLOW}📁 Creating database directories...${NC}"
ssh "$SERVER_USER@$SERVER_HOST" bash << EOF
cd "$DEPLOY_PATH"

# Create chroma_db directory if CHROMA_DB_PATH is set
if grep -q "CHROMA_DB_PATH" <<< "$DEPLOY_PATH"; then
    mkdir -p chroma_db
    echo "✅ Created chroma_db directory"
fi
EOF

# Create systemd service
echo -e "${YELLOW}⚙️  Creating systemd service...${NC}"

# Extract environment variables from config and build Environment lines
ENV_LINES=""
while IFS= read -r line; do
    if [[ $line == CFG_environment_* ]]; then
        var_name=$(echo "$line" | sed 's/CFG_environment_//' | cut -d= -f1)
        var_value=$(echo "$line" | cut -d= -f2- | sed 's/^"//' | sed 's/"$//')
        ENV_LINES="${ENV_LINES}Environment=\"${var_name}=${var_value}\"\n"
    fi
done <<< "$(set | grep "^CFG_environment_")"

ssh "$SERVER_USER@$SERVER_HOST" bash << EOF
cat > /etc/systemd/system/$SERVICE_NAME.service << 'SERVICEEOF'
[Unit]
Description=$SERVICE_NAME Service
After=network.target

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=$DEPLOY_PATH
Environment="PATH=$DEPLOY_PATH/$VENV_PATH/bin"
$(echo -e "$ENV_LINES")
ExecStart=$DEPLOY_PATH/$VENV_PATH/bin/python -m uvicorn main:app --host 127.0.0.1 --port $SERVICE_PORT
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICEEOF

systemctl daemon-reload
systemctl enable $SERVICE_NAME
systemctl start $SERVICE_NAME
EOF

echo -e "${GREEN}✅ Systemd service created and started${NC}"

# Setup nginx if enabled
if [ "$NGINX_ENABLED" = "true" ] && [ -n "$NGINX_SERVER_NAME" ]; then
    echo -e "${YELLOW}🌐 Configuring nginx...${NC}"

    ssh "$SERVER_USER@$SERVER_HOST" bash << EOF
cat > /etc/nginx/sites-available/$SERVICE_NAME << 'NGINXEOF'
server {
    listen 80;
    server_name $NGINX_SERVER_NAME;

    client_max_body_size 100M;

    location / {
        proxy_pass http://127.0.0.1:$SERVICE_PORT;
        proxy_set_header Host \\\$host;
        proxy_set_header X-Real-IP \\\$remote_addr;
        proxy_set_header X-Forwarded-For \\\$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \\\$scheme;
    }
}
NGINXEOF

ln -sf /etc/nginx/sites-available/$SERVICE_NAME /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
EOF

    echo -e "${GREEN}✅ Nginx configured${NC}"
fi

# Set permissions
echo -e "${YELLOW}🔒 Setting permissions...${NC}"
ssh "$SERVER_USER@$SERVER_HOST" "chown -R www-data:www-data '$DEPLOY_PATH' && chmod -R 755 '$DEPLOY_PATH'"

# Health check
echo ""
echo -e "${YELLOW}🏥 Running health check...${NC}"
sleep 3

if ssh "$SERVER_USER@$SERVER_HOST" "systemctl is-active --quiet $SERVICE_NAME"; then
    echo -e "${GREEN}✅ Service is running${NC}"

    # Try HTTP health check if available
    if [ -n "$NGINX_SERVER_NAME" ]; then
        if curl -sf "http://$NGINX_SERVER_NAME/health" > /dev/null 2>&1; then
            echo -e "${GREEN}✅ HTTP health check passed${NC}"
        else
            echo -e "${YELLOW}⚠️  HTTP health check not available (this may be normal)${NC}"
        fi
    fi
else
    echo -e "${RED}❌ Service failed to start${NC}"
    echo -e "${YELLOW}Check logs: ssh $SERVER_USER@$SERVER_HOST 'journalctl -u $SERVICE_NAME -n 50'${NC}"
    exit 1
fi

echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}✅ Server setup completed successfully!${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${GREEN}📊 Summary:${NC}"
echo -e "  Server: ${YELLOW}$SERVER_USER@$SERVER_HOST${NC}"
echo -e "  Deploy path: ${YELLOW}$DEPLOY_PATH${NC}"
echo -e "  Service: ${YELLOW}$SERVICE_NAME${NC}"
if [ "$NGINX_ENABLED" = "true" ]; then
    echo -e "  URL: ${YELLOW}http://$NGINX_SERVER_NAME${NC}"
fi
echo ""
echo -e "${YELLOW}Next steps:${NC}"
echo -e "  • Check logs: ${BLUE}ssh $SERVER_USER@$SERVER_HOST 'journalctl -u $SERVICE_NAME -f'${NC}"
echo -e "  • Deploy updates: ${BLUE}./.devops/scripts/deploy-to-server.sh --server $SERVER_NAME${NC}"
echo -e "  • Remove deployment: ${BLUE}./.devops/scripts/cleanup-server.sh --server $SERVER_NAME${NC}"
echo ""
