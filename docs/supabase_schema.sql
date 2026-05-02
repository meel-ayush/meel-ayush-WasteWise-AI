-- WasteWise AI — Supabase Schema v2.0
-- Run in Supabase SQL Editor

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Regions
CREATE TABLE IF NOT EXISTS regions (
    name TEXT PRIMARY KEY,
    type TEXT NOT NULL DEFAULT 'General Area',
    foot_traffic_baseline INTEGER NOT NULL DEFAULT 500,
    weekend_multiplier NUMERIC(4,2) NOT NULL DEFAULT 1.10,
    holiday_multiplier NUMERIC(4,2) NOT NULL DEFAULT 1.00,
    rain_impact NUMERIC(4,2) NOT NULL DEFAULT -0.20,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Chains
CREATE TABLE IF NOT EXISTS chains (
    chain_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'franchise',
    owner_email TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Restaurants
CREATE TABLE IF NOT EXISTS restaurants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    region TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'hawker',
    owner_name TEXT NOT NULL DEFAULT 'Owner',
    telegram_chat_id BIGINT,
    telegram_username TEXT,
    email TEXT,
    chain_id TEXT REFERENCES chains(chain_id),
    privacy_accepted BOOLEAN NOT NULL DEFAULT TRUE,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    specialty_weather TEXT NOT NULL DEFAULT 'neutral',
    closing_time TEXT NOT NULL DEFAULT '21:00',
    discount_pct INTEGER NOT NULL DEFAULT 30,
    marketplace_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    preferred_language TEXT NOT NULL DEFAULT 'english',
    latitude NUMERIC(10,6),
    longitude NUMERIC(10,6),
    state TEXT,
    bom JSONB NOT NULL DEFAULT '{}',
    recent_feedback_memory JSONB NOT NULL DEFAULT '[]',
    q_tables JSONB NOT NULL DEFAULT '{}',
    sustainability_waste_prevented_kg NUMERIC(10,3) NOT NULL DEFAULT 0,
    sustainability_co2_saved_kg NUMERIC(10,3) NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Menu Items
CREATE TABLE IF NOT EXISTS restaurant_menu (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    restaurant_id TEXT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    item TEXT NOT NULL,
    base_daily_demand INTEGER NOT NULL DEFAULT 50,
    profit_margin_rm NUMERIC(8,2) NOT NULL DEFAULT 2.50,
    price_rm NUMERIC(8,2) NOT NULL DEFAULT 5.00,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(restaurant_id, item)
);

-- Daily Records
CREATE TABLE IF NOT EXISTS daily_records (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    restaurant_id TEXT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    date DATE NOT NULL,
    total_revenue_rm NUMERIC(10,2) NOT NULL DEFAULT 0,
    total_waste_qty INTEGER NOT NULL DEFAULT 0,
    weather TEXT,
    foot_traffic TEXT,
    forecast_text TEXT,
    forecast_generated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(restaurant_id, date)
);

-- Items sold per day
CREATE TABLE IF NOT EXISTS daily_items_sold (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    restaurant_id TEXT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    date DATE NOT NULL,
    item TEXT NOT NULL,
    qty_sold INTEGER NOT NULL DEFAULT 0,
    revenue_rm NUMERIC(8,2),
    UNIQUE(restaurant_id, date, item)
);

-- Accounts
CREATE TABLE IF NOT EXISTS accounts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email TEXT UNIQUE NOT NULL,
    restaurant_id TEXT REFERENCES restaurants(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Sessions
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    chat_id BIGINT,
    telegram_username TEXT,
    label TEXT,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);

-- Pending OTPs
CREATE TABLE IF NOT EXISTS pending_otps (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email TEXT,
    purpose TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

-- Pending Registrations
CREATE TABLE IF NOT EXISTS pending_registrations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email TEXT NOT NULL,
    telegram_username TEXT NOT NULL,
    restaurant_data JSONB NOT NULL DEFAULT '{}',
    code_hash TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

-- Pending Approvals
CREATE TABLE IF NOT EXISTS pending_approvals (
    approval_id TEXT PRIMARY KEY,
    primary_chat_id BIGINT NOT NULL,
    requesting_chat_id BIGINT NOT NULL,
    requesting_username TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Active Events
CREATE TABLE IF NOT EXISTS active_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    restaurant_id TEXT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    headcount INTEGER NOT NULL DEFAULT 0,
    days INTEGER NOT NULL DEFAULT 1,
    event_date DATE NOT NULL,
    expires_at DATE NOT NULL
);

-- Closing Stock
CREATE TABLE IF NOT EXISTS closing_stock (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    restaurant_id TEXT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    stock_date DATE NOT NULL,
    stock_time TEXT,
    item TEXT NOT NULL,
    qty_available INTEGER NOT NULL DEFAULT 0,
    original_price_rm NUMERIC(8,2) NOT NULL,
    discounted_price_rm NUMERIC(8,2) NOT NULL,
    discount_pct INTEGER NOT NULL DEFAULT 30,
    UNIQUE(restaurant_id, stock_date, item)
);

-- Marketplace Orders
CREATE TABLE IF NOT EXISTS marketplace_orders (
    order_id TEXT PRIMARY KEY,
    restaurant_id TEXT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    order_date DATE NOT NULL,
    customer_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    items JSONB NOT NULL DEFAULT '[]',
    total_rm NUMERIC(8,2) NOT NULL DEFAULT 0,
    shopkeeper_earnings_rm NUMERIC(8,2) NOT NULL DEFAULT 0,
    platform_fee_rm NUMERIC(8,2) NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Audit Log
CREATE TABLE IF NOT EXISTS audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    ts TIMESTAMPTZ NOT NULL,
    actor_email TEXT,
    restaurant_id TEXT,
    action TEXT NOT NULL,
    endpoint TEXT,
    ip_address TEXT,
    success BOOLEAN NOT NULL,
    detail TEXT
);

-- AI Action Log
CREATE TABLE IF NOT EXISTS ai_action_log (
    action_id TEXT PRIMARY KEY,
    restaurant_id TEXT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    action_type TEXT NOT NULL,
    item TEXT,
    old_value NUMERIC(8,2),
    new_value NUMERIC(8,2),
    reason TEXT,
    undone BOOLEAN NOT NULL DEFAULT FALSE,
    undone_at TIMESTAMPTZ,
    expires_undo_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Federated Model
CREATE TABLE IF NOT EXISTS federated_model (
    id INTEGER PRIMARY KEY DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 0,
    weights JSONB NOT NULL DEFAULT '[0,0,0,0,0,0]',
    participants INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO federated_model (id, version, weights) VALUES (1, 0, '[0,0,0,0,0,0]') ON CONFLICT (id) DO NOTHING;

-- Gamification
CREATE TABLE IF NOT EXISTS gamification_stats (
    restaurant_id TEXT PRIMARY KEY REFERENCES restaurants(id) ON DELETE CASCADE,
    current_streak INTEGER NOT NULL DEFAULT 0,
    longest_streak INTEGER NOT NULL DEFAULT 0,
    last_log_date DATE,
    total_logs INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_daily_restaurant_date ON daily_records(restaurant_id, date);
CREATE INDEX IF NOT EXISTS idx_items_sold_rest_date ON daily_items_sold(restaurant_id, date);
CREATE INDEX IF NOT EXISTS idx_sessions_chat ON sessions(chat_id);
CREATE INDEX IF NOT EXISTS idx_stock_rest_date ON closing_stock(restaurant_id, stock_date);
CREATE INDEX IF NOT EXISTS idx_orders_restaurant ON marketplace_orders(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_events_restaurant ON active_events(restaurant_id);

-- RLS: service role bypasses all
ALTER TABLE restaurants ENABLE ROW LEVEL SECURITY;
ALTER TABLE restaurant_menu ENABLE ROW LEVEL SECURITY;
ALTER TABLE daily_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE daily_items_sold ENABLE ROW LEVEL SECURITY;
ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE marketplace_orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE closing_stock ENABLE ROW LEVEL SECURITY;
ALTER TABLE pending_otps ENABLE ROW LEVEL SECURITY;
ALTER TABLE pending_registrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE pending_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_action_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE active_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE gamification_stats ENABLE ROW LEVEL SECURITY;
ALTER TABLE chains ENABLE ROW LEVEL SECURITY;
ALTER TABLE federated_model ENABLE ROW LEVEL SECURITY;
ALTER TABLE regions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "svc_all" ON restaurants FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON restaurant_menu FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON daily_records FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON daily_items_sold FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON accounts FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON sessions FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON marketplace_orders FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON closing_stock FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON pending_otps FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON pending_registrations FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON pending_approvals FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON audit_log FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON ai_action_log FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON active_events FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON gamification_stats FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON chains FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON federated_model FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "svc_all" ON regions FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Public read for marketplace
CREATE POLICY "anon_read" ON closing_stock FOR SELECT TO anon USING (true);
CREATE POLICY "anon_read" ON restaurants FOR SELECT TO anon USING (marketplace_enabled = true);
CREATE POLICY "anon_read" ON restaurant_menu FOR SELECT TO anon USING (is_active = true);
CREATE POLICY "anon_insert" ON marketplace_orders FOR INSERT TO anon WITH CHECK (true);

-- Public read for regions (needed by registration flow) and federated model info
CREATE POLICY "anon_read" ON regions FOR SELECT TO anon USING (true);
CREATE POLICY "anon_read" ON federated_model FOR SELECT TO anon USING (true);

-- Seed regions
INSERT INTO regions (name, type, foot_traffic_baseline, weekend_multiplier, holiday_multiplier, rain_impact) VALUES
('Nilai, Negeri Sembilan','University Town',550,0.85,0.70,-0.22),
('Nilai INTI','University Campus',600,0.65,0.35,-0.20),
('Kuala Lumpur','City Centre',800,1.20,1.30,-0.18),
('Petaling Jaya','Urban Suburb',650,1.15,1.10,-0.20),
('Shah Alam','Industrial City',580,1.10,1.05,-0.20),
('Subang Jaya','Urban Suburb',620,1.15,1.10,-0.20),
('Bangsar KL','Trendy District',500,1.30,1.20,-0.15),
('Georgetown Penang','Tourist Hub',700,1.25,1.40,-0.15),
('Johor Bahru','Border City',680,1.20,1.35,-0.18),
('Ipoh','Heritage City',550,1.15,1.20,-0.20),
('General Area','General Area',500,1.10,1.00,-0.20)
ON CONFLICT (name) DO NOTHING;
