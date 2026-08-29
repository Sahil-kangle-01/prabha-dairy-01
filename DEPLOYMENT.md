# Prabha Dairy API - Production Deployment Guide

## 🚀 Quick Start (Windows)

### 1. Install Dependencies

```powershell
# Create virtual environment
python -m venv venv
.\venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment

```powershell
# Copy example config
copy .env.example .env

# Edit .env and set:
# - DATABASE_URL (PostgreSQL connection)
# - SECRET_KEY (generate with: openssl rand -hex 32)
# - API_KEY (optional, for authentication)
# - TALLY_HOST and TALLY_PORT
```

### 3. Initialize Database

```powershell
# Run migrations
python -m database.migrate
```

### 4. Run in Development

```powershell
uvicorn api.main:app --reload --port 8000
```

Visit: http://localhost:8000

### 5. Install as Windows Service

```powershell
# Run as Administrator
.\deploy\install-windows-service.ps1

# Start service
Start-Service PrabhaDairyAPI

# Check status
Get-Service PrabhaDairyAPI
```

---

## 🐧 Linux/Ubuntu Deployment

### 1. System Setup

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python and PostgreSQL
sudo apt install python3 python3-venv python3-pip postgresql nginx -y

# Install WeasyPrint dependencies
sudo apt install python3-cffi python3-brotli libpango-1.0-0 libpangoft2-1.0-0 -y
```

### 2. Create Application User

```bash
sudo useradd -m -s /bin/bash prabha
sudo usermod -aG sudo prabha
```

### 3. Setup Application

```bash
# Switch to application user
sudo su - prabha

# Clone/copy application to /opt/prabha-dairy
sudo mkdir -p /opt/prabha-dairy
sudo chown prabha:prabha /opt/prabha-dairy
cd /opt/prabha-dairy

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
nano .env  # Edit configuration
```

### 4. Setup Database

```bash
# Create PostgreSQL database
sudo -u postgres psql
```

```sql
CREATE USER prabha_user WITH PASSWORD 'your_secure_password';
CREATE DATABASE prabha_dairy OWNER prabha_user;
GRANT ALL PRIVILEGES ON DATABASE prabha_dairy TO prabha_user;
\q
```

```bash
# Run migrations
python -m database.migrate
```

### 5. Install Systemd Service

```bash
# Copy service file
sudo cp deploy/prabha-dairy-api.service /etc/systemd/system/

# Create log directory
sudo mkdir -p /var/log/prabha-dairy
sudo chown prabha:prabha /var/log/prabha-dairy

# Reload systemd and start service
sudo systemctl daemon-reload
sudo systemctl enable prabha-dairy-api
sudo systemctl start prabha-dairy-api

# Check status
sudo systemctl status prabha-dairy-api
```

### 6. Setup Nginx Reverse Proxy

```bash
sudo nano /etc/nginx/sites-available/prabha-dairy
```

```nginx
server {
    listen 80;
    server_name your-domain.com;

    client_max_body_size 20M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }

    location /static {
        alias /opt/prabha-dairy/static;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
}
```

```bash
# Enable site
sudo ln -s /etc/nginx/sites-available/prabha-dairy /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

### 7. Setup SSL (Let's Encrypt)

```bash
sudo apt install certbot python3-certbot-nginx -y
sudo certbot --nginx -d your-domain.com
```

---

## 📊 Monitoring & Logs

### View Logs

**Linux:**
```bash
# Application logs
tail -f /var/log/prabha-dairy/error.log
tail -f /opt/prabha-dairy/logs/prabha_dairy_api.log

# Systemd logs
journalctl -u prabha-dairy-api -f
```

**Windows:**
```powershell
Get-Content logs\prabha_dairy_api.log -Tail 50 -Wait
Get-Content logs\service-stdout.log -Tail 50 -Wait
```

### Health Checks

```bash
# Basic health
curl http://localhost:8000/health

# Detailed status
curl http://localhost:8000/status

# Prometheus metrics
curl http://localhost:8000/metrics
```

---

## 🔒 Security Checklist

- [ ] Set strong `SECRET_KEY` (min 32 chars)
- [ ] Configure `API_KEY` for authentication
- [ ] Restrict `ALLOWED_ORIGINS` to your domain
- [ ] Use HTTPS in production
- [ ] Set `ENVIRONMENT=production`
- [ ] Disable `DEBUG=false`
- [ ] Use strong database password
- [ ] Firewall: Only allow ports 80, 443
- [ ] Regular backups of database
- [ ] Monitor logs for suspicious activity

---

## 🔄 Updates & Maintenance

### Update Application

```bash
# Linux
sudo systemctl stop prabha-dairy-api
cd /opt/prabha-dairy
git pull  # or copy new files
source venv/bin/activate
pip install -r requirements.txt --upgrade
python -m database.migrate
sudo systemctl start prabha-dairy-api
```

```powershell
# Windows
Stop-Service PrabhaDairyAPI
cd "D:\Prabha Dairy\prabha-dairy-purchase-milk-sync"
git pull  # or copy new files
.\venv\Scripts\activate
pip install -r requirements.txt --upgrade
python -m database.migrate
Start-Service PrabhaDairyAPI
```

### Database Backup

```bash
# Backup
pg_dump -U prabha_user prabha_dairy > backup_$(date +%Y%m%d).sql

# Restore
psql -U prabha_user prabha_dairy < backup_20260827.sql
```

---

## 🆘 Troubleshooting

### Service Won't Start

```bash
# Check logs
sudo journalctl -u prabha-dairy-api -n 100

# Check database connection
python -c "from database.db import check_database_health; print(check_database_health())"
```

### High Memory Usage

- Reduce `WORKERS` in .env
- Reduce `DB_POOL_SIZE`
- Check for memory leaks in logs

### Slow Performance

- Increase `DB_POOL_SIZE`
- Add database indexes
- Check Tally ERP connectivity
- Monitor with `/metrics` endpoint

---

## 📞 Support

Check logs at:
- `logs/prabha_dairy_api.log` - Application logs
- `logs/prabha_dairy_errors.log` - Error logs only
- `logs/sync_audit.log` - Sync operation audit trail
