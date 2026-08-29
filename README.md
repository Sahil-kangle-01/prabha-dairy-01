# Prabha Dairy - Purchase Milk & Inventory Management System

Production-ready Tally ERP integration system for dairy management with analytics dashboard and live stock tracking.

## 🚀 Features

### ✅ Core Functionality
- **Tally ERP Sync** - Bidirectional sync for Purchase Milk, Sales, Stock Movements
- **Analytics Dashboard** - Real-time weighted averages for FAT, SNF, Degree
- **Live Stock Lookup** - Search-as-you-type with real-time Tally balance
- **PDF Voucher Generation** - Print purchase milk vouchers
- **Master Data Sync** - Stock items, Godowns, Ledgers, Units

### 🔒 Production Features
- **API Key Authentication** - Secure API access control
- **Rate Limiting** - Prevent abuse with configurable limits
- **Health Checks** - `/health`, `/metrics`, `/status` endpoints
- **Structured Logging** - File rotation, error tracking, audit trails
- **Database Pooling** - Connection pool with automatic retry
- **CORS Protection** - Configurable origin restrictions
- **Graceful Shutdown** - SIGTERM handling
- **Security Headers** - XSS, clickjacking protection

### 📊 Monitoring
- **Prometheus Metrics** - Request counts, durations, DB pool status
- **System Status** - CPU, memory, uptime tracking
- **Audit Logs** - Sync operations logged to JSON
- **Access Logs** - All API requests logged with timing

---

## 📦 Quick Start

### 1. Install Dependencies

```powershell
# Windows
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt

# Linux
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env - set DATABASE_URL, SECRET_KEY, API_KEY, TALLY_HOST
```

**Generate secure keys:**
```bash
# SECRET_KEY (32+ characters)
openssl rand -hex 32

# API_KEY (optional, for authentication)
openssl rand -hex 16
```

### 3. Initialize Database

```bash
python -m database.migrate
```

### 4. Run Development Server

```bash
uvicorn api.main:app --reload --port 8000
```

**Open:** http://localhost:8000

---

## 🌐 Endpoints

### Dashboard
- `/` - Analytics dashboard
- `/stock-lookup` - Live stock search

### API - Analytics
- `GET /analytics/period-summary?from_date=20260801&to_date=20260831`
- `GET /analytics/milk-type-breakdown?from_date=YYYYMMDD&to_date=YYYYMMDD`
- `GET /analytics/godown-breakdown?from_date=YYYYMMDD&to_date=YYYYMMDD`
- `GET /analytics/supplier-breakdown?from_date=YYYYMMDD&to_date=YYYYMMDD&top_n=20`

### API - Stock
- `GET /stock/search?q=milk&limit=10` - Autocomplete search
- `GET /stock/godowns` - List all godowns
- `GET /stock/live?item=Milk` - Live balance from Tally

### Monitoring
- `GET /health` - Health check (200 = healthy, 503 = unhealthy)
- `GET /health/live` - Kubernetes liveness probe
- `GET /health/ready` - Kubernetes readiness probe
- `GET /metrics` - Prometheus metrics
- `GET /status` - Detailed system status

---

## 🔐 Security

### Authentication

Include API key in all requests (if enabled):
```bash
curl -H "X-API-Key: your-api-key" http://localhost:8000/analytics/period-summary?from_date=20260801&to_date=20260831
```

### Rate Limiting

Default: 100 requests per 60 seconds per IP address.

Configure in `.env`:
```env
RATE_LIMIT_ENABLED=true
RATE_LIMIT_REQUESTS=100
RATE_LIMIT_WINDOW=60
```

### Production Checklist

- [ ] Set unique `SECRET_KEY` (min 32 chars)
- [ ] Configure `API_KEY` for authentication
- [ ] Restrict `ALLOWED_ORIGINS` to your domain
- [ ] Enable HTTPS (use nginx + Let's Encrypt)
- [ ] Set `ENVIRONMENT=production`
- [ ] Set `DEBUG=false`
- [ ] Use strong database password
- [ ] Configure firewall (allow only 80, 443)
- [ ] Setup automated backups
- [ ] Configure monitoring alerts

---

## 🚢 Production Deployment

### Windows Service

```powershell
# Run as Administrator
.\deploy\install-windows-service.ps1

# Start service
Start-Service PrabhaDairyAPI

# Check status
Get-Service PrabhaDairyAPI
```

### Linux/Ubuntu (Systemd)

```bash
# Copy service file
sudo cp deploy/prabha-dairy-api.service /etc/systemd/system/

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable prabha-dairy-api
sudo systemctl start prabha-dairy-api

# Check status
sudo systemctl status prabha-dairy-api
```

### Docker (Optional)

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["gunicorn", "api.main:app", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8000"]
```

**See [DEPLOYMENT.md](DEPLOYMENT.md) for complete production setup guide.**

---

## 📁 Project Structure

```
prabha-dairy-purchase-milk-sync/
├── api/
│   ├── main.py              # FastAPI application
│   ├── routes/
│   │   ├── analytics.py     # Analytics endpoints
│   │   ├── stock.py         # Stock lookup endpoints
│   │   └── print.py         # PDF generation
│   ├── deps.py              # Dependency injection
│   └── live_stock.py        # Live Tally queries
├── database/
│   ├── models.py            # SQLAlchemy models
│   ├── db.py                # Database connection & pooling
│   └── migrate.py           # Database migrations
├── static/
│   ├── dashboard.html       # Analytics dashboard
│   └── stock-lookup.html    # Stock search page
├── templates/
│   └── purchase_milk_voucher.html  # PDF template
├── deploy/
│   ├── install-windows-service.ps1
│   └── prabha-dairy-api.service
├── config.py                # Environment configuration
├── security.py              # Auth & rate limiting
├── logging_config.py        # Logging setup
├── monitoring.py            # Health & metrics
├── analytics_service.py     # Weighted average calculations
├── tally_connector.py       # Tally XML-RPC client
├── sync_now_service.py      # Sync orchestration
├── requirements.txt         # Python dependencies
├── .env.example             # Example configuration
├── DEPLOYMENT.md            # Production deployment guide
└── README.md                # This file
```

---

## 📊 Database Schema

### Core Tables
- `purchase_milk` - Purchase milk vouchers from Tally
- `sales_vouchers` - Sales transactions
- `sales_inventory` - Sales line items
- `stock_movements` - All inventory movements
- `sync_runs` - Sync operation audit trail

### Master Data
- `stock_items` - Product master
- `godowns` - Location master
- `ledgers` - Account master
- `units` - Unit of measure master

---

## 🔄 Sync Operations

### Manual Sync
```bash
python cli.py sync --from 20260801 --to 20260831
```

### Scheduled Sync (Windows)
```powershell
# Install scheduled task (runs daily at 2 AM)
.\install_sync_now_service.ps1
```

### Sync Monitoring
```bash
# View sync audit log
cat logs/sync_audit.log | jq .

# Check last sync status
curl http://localhost:8000/status
```

---

## 📈 Monitoring & Logs

### Application Logs
```bash
# All logs
tail -f logs/prabha_dairy_api.log

# Errors only
tail -f logs/prabha_dairy_errors.log

# Sync audit trail
tail -f logs/sync_audit.log
```

### Health Monitoring
```bash
# Basic health
curl http://localhost:8000/health

# Detailed status (CPU, memory, DB pool)
curl http://localhost:8000/status

# Prometheus metrics
curl http://localhost:8000/metrics
```

### Database Monitoring
```sql
-- Check sync history
SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT 10;

-- Purchase milk summary
SELECT DATE(date), COUNT(*), SUM(litres), AVG(fat), AVG(snf) 
FROM purchase_milk 
GROUP BY DATE(date) 
ORDER BY DATE(date) DESC;
```

---

## 🛠 Troubleshooting

### Service Won't Start
```bash
# Check logs
journalctl -u prabha-dairy-api -n 100

# Test database connection
python -c "from database.db import check_database_health; print(check_database_health())"
```

### Slow Performance
- Check `/metrics` for database pool exhaustion
- Increase `DB_POOL_SIZE` in `.env`
- Verify Tally ERP connectivity
- Check database indexes

### High Memory Usage
- Reduce `WORKERS` count
- Reduce `DB_POOL_SIZE`
- Check for long-running queries

---

## 🔒 Security Best Practices

1. **Never commit `.env`** - Already in `.gitignore`
2. **Use strong passwords** - Database, API keys
3. **Enable HTTPS** - Use nginx + Let's Encrypt
4. **Restrict CORS** - Set `ALLOWED_ORIGINS` to your domain
5. **Enable authentication** - Set `API_KEY` in production
6. **Monitor logs** - Check for suspicious activity
7. **Regular backups** - Automate PostgreSQL backups
8. **Update dependencies** - `pip list --outdated`

---

## 📞 Support & Maintenance

### Regular Maintenance
```bash
# Update dependencies
pip install -r requirements.txt --upgrade

# Database backup
pg_dump prabha_dairy > backup_$(date +%Y%m%d).sql

# Rotate logs (automatic with configuration)
# Check disk space
df -h
```

### Performance Tuning
- Adjust `DB_POOL_SIZE` based on load
- Increase `WORKERS` for high traffic
- Configure PostgreSQL connection limits
- Monitor with `/metrics` endpoint

---

## 📄 License

Proprietary - Prabha Dairy Internal Use Only

---

## 🎯 Version

**v1.0.0** - Production Release (2026-08-27)

- ✅ Complete Tally integration
- ✅ Production-grade security
- ✅ Monitoring & logging
- ✅ Windows & Linux deployment
- ✅ API authentication
- ✅ Rate limiting
- ✅ Health checks
#   p r a b h a - d a i r y - 0 1  
 