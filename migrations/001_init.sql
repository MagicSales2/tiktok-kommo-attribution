-- ============================================================
--  TikTok <-> Kommo Attribution - esquema inicial
-- ============================================================

CREATE TABLE IF NOT EXISTS attribution_clicks (
    code            TEXT PRIMARY KEY,
    ttclid          TEXT,
    ttp             TEXT,
    utm_source      TEXT,
    utm_medium      TEXT,
    utm_campaign    TEXT,
    utm_content     TEXT,
    utm_term        TEXT,
    campaign_id     TEXT,
    adgroup_id      TEXT,
    ad_id           TEXT,
    referrer        TEXT,
    landing_url     TEXT,
    user_agent      TEXT,
    ip              TEXT,
    lead_id         BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_attribution_clicks_lead
    ON attribution_clicks (lead_id);

CREATE TABLE IF NOT EXISTS leads (
    lead_id             BIGINT PRIMARY KEY,
    contact_id          BIGINT,
    pipeline_id         BIGINT,
    status_id           BIGINT,
    last_status_id      BIGINT,
    max_rank            INTEGER NOT NULL DEFAULT 0,
    stage_key           TEXT,
    name                TEXT,
    phone               TEXT,
    email               TEXT,
    ref_code            TEXT,
    ttclid              TEXT,
    ttp                 TEXT,
    attribution_source  TEXT,
    utm_source          TEXT,
    utm_medium          TEXT,
    utm_campaign        TEXT,
    utm_content         TEXT,
    utm_term            TEXT,
    value               NUMERIC(14, 2) NOT NULL DEFAULT 0,
    currency            TEXT NOT NULL DEFAULT 'COP',
    consent             BOOLEAN NOT NULL DEFAULT TRUE,
    raw                 JSONB,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_event_at       TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_leads_status ON leads (status_id);

CREATE TABLE IF NOT EXISTS lead_jobs (
    id              BIGSERIAL PRIMARY KEY,
    lead_id         BIGINT NOT NULL,
    reason          TEXT NOT NULL DEFAULT 'webhook',
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at       TIMESTAMPTZ,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_lead_jobs_pending
    ON lead_jobs (lead_id) WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS ix_lead_jobs_pick
    ON lead_jobs (status, next_attempt_at);

CREATE TABLE IF NOT EXISTS events_outbox (
    id              BIGSERIAL PRIMARY KEY,
    dedupe_key      TEXT NOT NULL UNIQUE,
    event_id        TEXT NOT NULL,
    lead_id         BIGINT NOT NULL,
    event_name      TEXT NOT NULL,
    stage_key       TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at       TIMESTAMPTZ,
    payload         JSONB NOT NULL,
    response        JSONB,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_events_outbox_pick
    ON events_outbox (status, next_attempt_at);

CREATE INDEX IF NOT EXISTS ix_events_outbox_lead
    ON events_outbox (lead_id, id DESC);

CREATE TABLE IF NOT EXISTS webhook_log (
    id          BIGSERIAL PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source      TEXT NOT NULL DEFAULT 'kommo',
    event_type  TEXT,
    lead_id     BIGINT,
    payload     JSONB,
    processed   BOOLEAN NOT NULL DEFAULT FALSE,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS ix_webhook_log_received
    ON webhook_log (received_at DESC);
