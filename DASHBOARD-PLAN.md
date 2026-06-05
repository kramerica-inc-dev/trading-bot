# Crypto Trader Dashboard - Implementation Plan

**Created:** 2026-02-15  
**Status:** Planning Phase  

---

## 🎯 OBJECTIVES

1. **Real-time monitoring** of bot performance
2. **Manual control** over bot operation
3. **Historical data** visualization
4. **Risk management** controls

---

## 🖥️ TECHNICAL APPROACH

### Option A: Flask Web App (Recommended)

**Pros:**
- Lightweight (Python, already on container)
- Easy integration with bot code
- Fast to develop
- Low resource usage

**Stack:**
- Backend: Flask (Python)
- Frontend: HTML + Tailwind CSS + Alpine.js
- Charts: Chart.js
- API: RESTful JSON
- Auth: Basic auth or token

**Port:** 8080 (accessible via Tailscale)

### Option B: Node.js + React

**Pros:**
- More polished UI
- Better for complex dashboards

**Cons:**
- Need Node.js on container
- Heavier resource usage
- Longer development time

---

## 📊 DASHBOARD FEATURES

### Phase 1: Monitoring (MVP)

**Live Status:**
- Bot service status (running/stopped)
- Current balance
- Active positions count
- Last check timestamp
- Mode (live/dry run)

**Recent Activity:**
- Last 10 trades (table)
- Win/loss ratio today
- P&L today/week/month

**System Health:**
- Container uptime
- Memory usage
- Disk space
- Log file size

**URL:** `http://YOUR_CONTAINER_HOST:8080`

### Phase 2: Controls

**Bot Control:**
- Start/Stop bot button
- Switch dry run on/off
- Emergency stop (close all positions)

**Config Adjustments:**
- Risk per trade slider (1-10%)
- Min confidence slider (50-80%)
- Confluence threshold (2/4 or 3/4)

**Manual Actions:**
- Place manual order
- Close specific position
- View detailed logs

### Phase 3: Analytics

**Charts:**
- Balance over time (line chart)
- Win rate trend (%)
- Drawdown chart
- Daily P&L (bar chart)
- Trade distribution (buy/sell pie)

**Statistics:**
- Total trades
- Average win/loss size
- Best/worst trade
- Average holding time
- Sharpe ratio

**Export:**
- Download trades as CSV
- Download logs
- Generate PDF report

---

## 🏗️ IMPLEMENTATION

### File Structure

```
/opt/trading-bot/
├── scripts/
│   ├── trading_bot.py
│   ├── blofin_api.py
│   ├── advanced_strategy.py
│   └── dashboard_api.py       # NEW
├── dashboard/
│   ├── app.py                 # NEW Flask app
│   ├── templates/
│   │   ├── index.html         # NEW Main dashboard
│   │   └── login.html         # NEW Auth page
│   ├── static/
│   │   ├── css/
│   │   │   └── style.css      # NEW Tailwind output
│   │   └── js/
│   │       └── dashboard.js   # NEW Alpine.js components
│   └── requirements.txt       # NEW Flask, etc.
├── memory/
│   ├── trading-log.jsonl
│   ├── positions.json
│   └── trades.db              # NEW SQLite for analytics
└── config.json
```

### API Endpoints

**Status:**
- `GET /api/status` - Bot status, balance, uptime
- `GET /api/positions` - Active positions
- `GET /api/health` - System health metrics

**Trading Data:**
- `GET /api/trades?limit=50&offset=0` - Trade history
- `GET /api/performance` - Win rate, P&L, etc.
- `GET /api/logs?lines=100` - Recent logs

**Control:**
- `POST /api/bot/start` - Start bot
- `POST /api/bot/stop` - Stop bot
- `POST /api/bot/restart` - Restart bot
- `POST /api/config` - Update config (with validation)
- `POST /api/position/close` - Close specific position

**Manual Trading:**
- `POST /api/order` - Place manual order
- `GET /api/balance` - Current balance from API

### Authentication

**Simple Token Auth:**
```
Authorization: Bearer <random-token>
```

Token stored in config.json, required for all API calls.

---

## 🔒 SECURITY

**Access Control:**
- Dashboard only accessible via Tailscale
- Token authentication required
- Rate limiting on API endpoints
- No public internet exposure

**Safety:**
- Dry run toggle requires confirmation
- Emergency stop = immediate action
- Config changes validated before apply
- Audit log for all control actions

---

## 📱 UI MOCKUP (Phase 1)

```
┌─────────────────────────────────────────┐
│  🦀 Crypto Trader Dashboard             │
├─────────────────────────────────────────┤
│                                         │
│  STATUS                                 │
│  ● Running                              │
│  Mode: 🔴 LIVE                          │
│  Balance: €115.56                       │
│  Active Positions: 0                    │
│  Last Check: 2s ago                     │
│                                         │
│  TODAY                                  │
│  Trades: 2 (1W / 1L)                    │
│  P&L: +€2.34 (+2.0%)                    │
│  Win Rate: 50%                          │
│                                         │
│  RECENT TRADES                          │
│  ┌────────────────────────────────────┐ │
│  │ Time  │Side│Entry │Exit  │P&L    │ │
│  ├────────────────────────────────────┤ │
│  │ 11:23 │BUY │70100 │70450 │+€3.45 │ │
│  │ 10:15 │SELL│70800 │71000 │-€2.10 │ │
│  └────────────────────────────────────┘ │
│                                         │
│  SYSTEM                                 │
│  Uptime: 2h 34m                         │
│  Memory: 85MB / 512MB                   │
│  Disk: 1.8GB / 4GB                      │
│                                         │
│  [View Logs] [Download Report]         │
└─────────────────────────────────────────┘
```

---

## 🛠️ DEVELOPMENT STEPS

### Step 1: Basic Flask App (2-3 hours)

1. Install Flask on container
2. Create `/api/status` endpoint
3. Serve static HTML page
4. Test via Tailscale

### Step 2: Real-time Data (2-3 hours)

1. Parse trading-log.jsonl for trades
2. Read positions.json
3. System metrics (memory, disk)
4. Auto-refresh every 5s (JS polling)

### Step 3: Controls (3-4 hours)

1. Start/stop bot via systemctl
2. Config update endpoint (with backup)
3. Position close functionality
4. Safety confirmations in UI

### Step 4: Analytics (5-6 hours)

1. SQLite database for trade history
2. Chart.js integration
3. Statistics calculations
4. Export functionality

**Total Time: ~12-16 hours of development**

---

## 🚀 DEPLOYMENT

### Install Dashboard

```bash
ssh root@YOUR_CONTAINER_HOST

# Install dependencies
apt-get install -y python3-flask python3-pip
pip3 install flask flask-cors

# Create dashboard
cd /opt/trading-bot
mkdir -p dashboard/{templates,static/{css,js}}

# Copy files (to be provided)
# ...

# Create systemd service
cat > /etc/systemd/system/trading-dashboard.service << 'EOF'
[Unit]
Description=Trading Bot Dashboard
After=network.target

[Service]
Type=simple
User=botuser
WorkingDirectory=/opt/trading-bot/dashboard
ExecStart=/usr/bin/python3 app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
systemctl daemon-reload
systemctl enable trading-dashboard
systemctl start trading-dashboard
```

### Access Dashboard

**Via Tailscale:**
```
http://YOUR_CONTAINER_HOST:8080
```

**Login:**
- User: admin
- Token: (from config.json)

---

## 📈 FUTURE ENHANCEMENTS

**Phase 4: Advanced Features**
- WebSocket for real-time updates (no polling)
- Mobile responsive design
- Push notifications (Telegram alerts)
- Strategy backtesting tool
- Multi-timeframe analysis
- Paper trading simulator

**Phase 5: AI Features**
- Trade pattern recognition
- Anomaly detection
- Performance predictions
- Auto-optimization suggestions

---

## 💰 COST/BENEFIT

**Development Cost:**
- Time: 12-16 hours
- Resources: Minimal (Flask is lightweight)
- Maintenance: Low

**Benefits:**
- No SSH required for monitoring
- Faster response to issues
- Better decision making (visual data)
- Reduced risk (quick controls)
- Professional appearance

**ROI:** High (for €115 test capital + future scaling)

---

## ✅ NEXT STEPS

1. Approve this plan
2. Schedule development time
3. Build Phase 1 (MVP monitoring)
4. Test via Tailscale
5. Add Phase 2 controls
6. Add Phase 3 analytics

**Estimated timeline:** 2-3 days for full Phase 1-3

---

**Ready to start? Let me know and I'll begin with Phase 1.** 🦀
