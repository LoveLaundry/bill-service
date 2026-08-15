# Comprehensive Business Dashboard API

## Overview
The comprehensive dashboard provides real-time business intelligence metrics based on industry best practices for laundry management systems.

**Research Sources:**
- Spindle Live (Commercial Laundry KPIs)
- Modeliks (Laundry Services KPIs Dashboard)
- BusinessPlan Templates (Subscription Laundry Metrics)
- Intuit ERP (Business Performance Dashboards)
- ClearPoint Strategy (KPI Dashboard Best Practices)

## Endpoint

```
GET /dashboard/overview
```

### Query Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| period | string | No | "month" | Time period for metrics: `day`, `week`, `month`, `quarter`, `year` |

### Authentication
Requires `dashboard:read` capability

### Response Structure

```json
{
  "period": "month",
  "generated_at": "2026-08-15T05:00:00.000Z",
  
  "financial": {
    "total_revenue": 125000.50,
    "total_paid": 95000.00,
    "total_outstanding": 30000.50,
    "collection_rate": 76.0,
    "avg_bill_value": 2500.01,
    "total_bills": 50,
    "paid_bills": 38,
    "pending_bills": 12
  },
  
  "operations": {
    "gate_passes_processed": 45,
    "total_items_received": 5420,
    "total_items_delivered": 4950,
    "pending_items": 470,
    "avg_turnaround_hours": 48.5,
    "delivery_fulfillment_rate": 91.3,
    "inventory_turnover": 10.53
  },
  
  "quality": {
    "mismatch_count": 23,
    "mismatch_rate": 0.42,
    "total_items_checked": 5420
  },
  
  "clients": {
    "active_clients": 18,
    "total_clients": 25,
    "client_retention_rate": 85.2,
    "new_clients_this_period": 3
  },
  
  "trends": {
    "revenue_by_date": [
      {"date": "2026-07-15", "revenue": 12500.00},
      {"date": "2026-07-16", "revenue": 15200.50}
    ],
    "top_clients": [
      {"client_name": "Hotel Paradise", "revenue": 45000.00},
      {"client_name": "Restaurant Elite", "revenue": 32000.00}
    ]
  },
  
  "alerts": {
    "count": 2,
    "items": [
      {
        "type": "high_outstanding",
        "severity": "high",
        "message": "Outstanding amount (30000.50) exceeds 30% of revenue",
        "value": 30000.50
      }
    ]
  }
}
```

## Key Performance Indicators (KPIs)

### Financial Metrics

#### 1. **Total Revenue**
- **Definition**: Sum of all bill grand totals in the period
- **Purpose**: Track overall sales performance
- **Industry Benchmark**: Month-over-month growth of 5-10%

#### 2. **Collection Rate**
- **Formula**: (Total Paid / Total Revenue) × 100
- **Purpose**: Measure cash flow efficiency
- **Industry Benchmark**: >70% (Excellent: >85%)
- **Alert Threshold**: <70% triggers high-severity alert

#### 3. **Average Bill Value (ABV)**
- **Formula**: Total Revenue / Total Bills
- **Purpose**: Track pricing effectiveness and upselling
- **Use Case**: Identify opportunities to increase revenue per transaction

#### 4. **Outstanding Amount**
- **Definition**: Sum of unpaid balances
- **Purpose**: Monitor accounts receivable health
- **Alert Threshold**: >30% of revenue triggers alert

### Operational Metrics

#### 5. **Turnaround Time**
- **Definition**: Average hours from receiving to first delivery
- **Purpose**: Measure operational efficiency
- **Industry Benchmark**: 24-72 hours for commercial laundry
- **Formula**: Average of (First Delivery Date - Receiving Date)

#### 6. **Delivery Fulfillment Rate**
- **Formula**: (Items Delivered / Items Received) × 100
- **Purpose**: Track inventory processing efficiency
- **Industry Benchmark**: >95% (commercial laundry standard)

#### 7. **Inventory Turnover**
- **Formula**: Items Delivered / Pending Items
- **Purpose**: Measure how quickly inventory cycles through
- **Industry Benchmark**: Higher is better (10+ is excellent)
- **Alert Threshold**: <3 indicates slow processing

### Quality Metrics

#### 8. **Mismatch Rate**
- **Formula**: (Mismatch Count / Total Items Checked) × 100
- **Purpose**: Track quality and accuracy
- **Industry Benchmark**: <5% (Excellent: <2%)
- **Alert Threshold**: >5% triggers medium-severity alert

### Client Metrics

#### 9. **Client Retention Rate**
- **Formula**: (Active Clients Last 90 Days / Total Clients) × 100
- **Purpose**: Measure customer loyalty and satisfaction
- **Industry Benchmark**: >80% for B2B services
- **Research**: Critical for subscription-based laundry models

#### 10. **Active Clients**
- **Definition**: Unique clients with gate passes in period
- **Purpose**: Track business reach and growth

## Alert System

### Alert Types & Severity

| Alert Type | Severity | Threshold | Action Required |
|------------|----------|-----------|-----------------|
| `high_outstanding` | High | Outstanding >30% of revenue | Review collection process, contact clients |
| `low_collection` | High | Collection rate <70% | Improve payment terms, follow up on invoices |
| `high_mismatch` | Medium | Mismatch rate >5% | Review quality control, train receiving staff |
| `high_inventory` | Medium | Pending items >40% | Expedite deliveries, check processing bottlenecks |

### Alert Response Structure

```json
{
  "type": "alert_type_name",
  "severity": "high|medium|low",
  "message": "Human-readable description",
  "value": 12345.67
}
```

## Trends & Charts Data

### Revenue by Date
- **Use**: Line chart showing daily revenue trend
- **Format**: Array of `{date, revenue}` objects
- **Sorted**: Chronologically (oldest to newest)

### Top Clients
- **Use**: Bar chart or table showing top 5 revenue contributors
- **Format**: Array of `{client_name, revenue}` objects
- **Sorted**: By revenue (highest to lowest)
- **Limit**: Top 5 clients

## Usage Examples

### 1. Monthly Business Review
```bash
GET /dashboard/overview?period=month
```
Use for monthly performance reviews, board meetings, and strategic planning.

### 2. Weekly Operations Check
```bash
GET /dashboard/overview?period=week
```
Use for weekly operations meetings and team performance tracking.

### 3. Daily Performance Monitor
```bash
GET /dashboard/overview?period=day
```
Use for daily stand-ups and real-time operational decisions.

### 4. Quarterly Financial Analysis
```bash
GET /dashboard/overview?period=quarter
```
Use for quarterly business reviews and investor reports.

## Frontend Integration Guide

### Dashboard Layout Recommendations

Based on research from ClearPoint Strategy and Intuit ERP:

1. **Limit to 5-9 Key Metrics Per View**
   - Cognitive load research shows people process 5-9 items at once
   - Display critical metrics prominently, hide detailed data in drill-downs

2. **Visual Hierarchy**
   ```
   Priority 1 (Top): Financial (Revenue, Collection Rate, Outstanding)
   Priority 2 (Middle): Operational (Turnaround, Fulfillment Rate)
   Priority 3 (Bottom): Quality & Clients
   ```

3. **Color Coding**
   - Green: Metrics meeting or exceeding benchmarks
   - Yellow: Metrics within acceptable range
   - Red: Metrics triggering alerts or below threshold

4. **Chart Types**
   - **Revenue Trend**: Line chart
   - **Top Clients**: Horizontal bar chart
   - **KPI Cards**: Large number with trend indicator (↑ ↓)
   - **Alerts**: Notification badges with severity colors

### Sample React Component Structure

```typescript
interface DashboardData {
  period: string;
  generated_at: string;
  financial: FinancialMetrics;
  operations: OperationalMetrics;
  quality: QualityMetrics;
  clients: ClientMetrics;
  trends: TrendsData;
  alerts: AlertsData;
}

// KPI Card Component
<KPICard
  title="Total Revenue"
  value={data.financial.total_revenue}
  format="currency"
  trend={calculateTrend(currentPeriod, previousPeriod)}
  benchmark={null}
/>

// Alert Banner
<AlertBanner
  alerts={data.alerts.items}
  severity="high"
  onDismiss={handleDismiss}
/>

// Revenue Chart
<LineChart
  data={data.trends.revenue_by_date}
  xKey="date"
  yKey="revenue"
  title="Revenue Trend"
/>
```

## Performance Considerations

### Database Queries
- **Optimized Indexes**: Ensure indexes on `receiving_date`, `bill_date`, `payment_status`, `client_name_search`
- **Query Complexity**: ~5-8 MongoDB queries per request
- **Expected Response Time**: 500ms-2s (depending on data volume)

### Caching Strategy
- **Cache Duration**: 5-15 minutes for dashboard overview
- **Cache Key**: `dashboard:overview:{period}:{date}`
- **Invalidation**: On any bill/gatepass/delivery creation or update

### Optimization Tips
1. Add indexes on frequently queried date fields
2. Implement Redis caching for dashboard data
3. Use aggregation pipelines for complex calculations
4. Consider pre-calculating daily metrics via background job

## Research References

### Content Rephrased for Compliance

Based on industry research, effective laundry business dashboards track these critical areas:

1. **Financial Health** - Revenue trends, payment collection efficiency, and cash flow status provide immediate insight into business viability.

2. **Operational Performance** - Turnaround times, processing efficiency, and delivery fulfillment rates directly impact customer satisfaction and operational costs.

3. **Quality Control** - Error rates and mismatch tracking identify training needs and process improvements.

4. **Customer Metrics** - Client retention and lifetime value are critical for subscription-based or B2B laundry services.

5. **Alert-Driven Decision Making** - Real-time alerts for critical thresholds enable proactive management rather than reactive problem-solving.

**Sources:**
- [Commercial Laundry KPIs](https://www.spindlelive.com/commercial-laundry-kpis)
- [Laundry Services KPIs Dashboard](https://www.modeliks.com/industries/professional-services/laundry-services-kpis-dashboard)
- [KPI Dashboard Best Practices](https://www.clearpointstrategy.com/blog/kpi-dashboard-best-practices)
- [Business Performance Dashboard](https://erp.intuit.com/blog/financials/business-performance-dashboard/)

## Changelog

### Version 1.0 - August 2026
- Initial release with comprehensive business metrics
- Added financial, operational, quality, and client KPIs
- Implemented alert system with severity levels
- Added revenue trends and top clients analysis
- Based on industry best practices research

