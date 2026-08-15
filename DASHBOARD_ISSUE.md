# Dashboard Endpoint Issue on Vercel

## Problem

The comprehensive dashboard endpoint (`/dashboard/overview`) causes the serverless function to crash on Vercel with error:

```
500: INTERNAL_SERVER_ERROR
Code: FUNCTION_INVOCATION_FAILED
```

## Root Cause

**Vercel Serverless Limitations:**

1. **Memory Limit**: 1024 MB (Pro plan) or 3008 MB (Enterprise)
2. **Execution Timeout**: 10 seconds (Hobby), 60 seconds (Pro), 300 seconds (Enterprise)
3. **Cold Start**: Functions need to boot up quickly

**Dashboard Complexity:**
- Performs 10+ MongoDB queries (bills, gatepasses, deliveries, payments)
- Iterates through potentially thousands of documents
- Calculates complex aggregations
- Decrypts sensitive data for each document
- Generates trend data and alerts

This combination exceeds Vercel's serverless limits and causes crashes.

---

## Current Status

✅ **Dashboard endpoint is DISABLED** in production (Vercel)
- Import commented out in `main.py`
- Router not registered
- Service deploys successfully without crashes

✅ **Dashboard code is PRESERVED** in `routers/dashboard.py`
- Can be used for local development
- Can be deployed to a different platform

✅ **All other endpoints work fine**:
- `/bills` - ✅
- `/gatepasses` - ✅
- `/deliveries` - ✅  
- `/payments` - ✅
- `/admin/database/*` - ✅

---

## Solutions (Pick One)

### Option 1: Use Separate Analytics Service (Recommended)

Deploy the dashboard to a **long-running server** instead of serverless:

**Platforms:**
- **Railway** (recommended) - Supports long-running Python apps
- **Render** - Free tier available
- **DigitalOcean App Platform**
- **AWS ECS** / **Google Cloud Run**
- **Your own VPS** (DigitalOcean Droplet, Linode, etc.)

**Steps:**
1. Create a new FastAPI app with just the dashboard endpoint
2. Deploy to Railway/Render
3. Frontend calls the dashboard service directly
4. No 10-second timeout limits

### Option 2: Optimize Dashboard for Serverless

**Reduce complexity:**
1. Add caching (Redis) - cache results for 5-15 minutes
2. Use MongoDB aggregation pipelines instead of iterating
3. Limit data range (e.g., max 30 days)
4. Paginate results
5. Pre-calculate metrics via background job

**Example with caching:**
```python
import redis
r = redis.Redis()

@router.get("/dashboard/overview")
async def get_dashboard(period: str):
    cache_key = f"dashboard:{period}"
    cached = r.get(cache_key)
    if cached:
        return json.loads(cached)
    
    result = calculate_dashboard(period)
    r.setex(cache_key, 900, json.dumps(result))  # Cache for 15 min
    return result
```

### Option 3: Simplify Dashboard

Create a **lightweight version** with fewer metrics:
- Remove trend calculations
- Remove all-time client data
- Limit to financial summary only
- Fetch data in frontend (multiple API calls)

### Option 4: Upgrade Vercel Plan

**Vercel Pro** ($20/month per user):
- 60-second timeout (vs 10 seconds)
- 3008 MB memory (vs 1024 MB)
- May still timeout with large datasets

---

## Recommended Architecture

```
┌─────────────────────────────────────────────┐
│  Frontend (Vercel)                          │
│  - lovelaundry-manager.vercel.app           │
└──────────────┬──────────────────────────────┘
               │
               ├──────────────────────────────┐
               │                              │
               ▼                              ▼
┌──────────────────────────┐    ┌────────────────────────┐
│  bill-service (Vercel)    │    │  analytics-service     │
│  - CRUD operations        │    │  (Railway/Render)      │
│  - Fast endpoints         │    │  - Dashboard           │
│  - Works in serverless    │    │  - Reports             │
└──────────────────────────┘    │  - Heavy queries       │
                                 └────────────────────────┘
```

---

## Local Development

The dashboard **works fine locally** because there's no timeout:

```bash
cd bill_service

# Uncomment dashboard in main.py
# from .routers.dashboard import router as dashboard_router
# app.include_router(dashboard_router)

# Run locally
uv run fastapi dev src/bill_service/main.py

# Test
curl http://localhost:8001/dashboard/overview?period=month
```

---

## Frontend Update

Since dashboard is disabled, update the frontend to show a message:

```typescript
// business-dashboard-page.tsx

if (error && error.includes('Not Found')) {
  return (
    <div className="p-8 text-center">
      <h2 className="text-2xl font-bold mb-4">Dashboard Coming Soon</h2>
      <p className="text-gray-600 mb-4">
        The comprehensive dashboard is currently being optimized for production deployment.
      </p>
      <p className="text-sm text-gray-500">
        In the meantime, use individual reports: Bills, Gate Passes, Deliveries
      </p>
    </div>
  )
}
```

---

## Summary

| Feature | Status | Notes |
|---------|--------|-------|
| Dashboard Code | ✅ Complete | Fully functional, well-documented |
| Local Development | ✅ Works | No issues when running locally |
| Vercel Deployment | ❌ Disabled | Causes serverless crashes |
| Other Endpoints | ✅ Working | Bills, gatepasses, deliveries work fine |
| Recommended Fix | Use Railway/Render | Deploy analytics separately |

---

## Files

- **Code**: `src/bill_service/routers/dashboard.py` (preserved)
- **Documentation**: `DASHBOARD_API.md` (complete API docs)
- **Config**: `main.py` (dashboard commented out)
- **This file**: `DASHBOARD_ISSUE.md`

---

**Decision Required**: Choose one of the 4 solutions above based on your needs and budget.

**Quick Win**: Deploy to Railway (free tier) to get dashboard working immediately.
