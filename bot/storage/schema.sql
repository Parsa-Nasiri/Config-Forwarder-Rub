-- ---------------------------------------------------------------------------
-- Tele2Rubika :: Supabase / Postgres schema
--
-- Run this once in the Supabase SQL editor:
--   https://supabase.com/dashboard/project/<your-project>/sql
--
-- Everything is safe to re-run (all statements are IF NOT EXISTS / OR REPLACE).
-- ---------------------------------------------------------------------------

-- ---------------------------- users -----------------------------------------
create table if not exists public.users (
    chat_id         text primary key,
    user_id         text,
    first_name      text,
    last_name       text,
    username        text,
    language        text        not null default 'en',
    is_active       boolean     not null default true,
    is_paused       boolean     not null default false,
    paused_until    timestamptz,
    protocols       jsonb       not null default '[]'::jsonb,
    min_score       int         not null default 55,
    max_per_batch   int         not null default 5,
    live_mode       boolean     not null default false,
    affinity        jsonb       not null default '{}'::jsonb,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    last_seen_at    timestamptz not null default now()
);

create index if not exists users_active_idx
    on public.users (is_active, is_paused)
    where is_active = true;

-- --------------------------- configs ----------------------------------------
-- One row per *unique* proxy configuration (deduped by fingerprint).
create table if not exists public.configs (
    fingerprint     text primary key,
    protocol        text        not null,
    server          text,
    port            int,
    remark          text,
    raw             text        not null,
    score           numeric     not null default 0,
    geo             text,
    network         text,
    security        text,
    source_channel  text,
    source_message  text,
    first_seen_at   timestamptz not null default now(),
    last_seen_at    timestamptz not null default now(),
    seen_count      int         not null default 1,
    source_count    int         not null default 1,
    dead_reports    int         not null default 0,
    live_reports    int         not null default 0,
    copy_count      int         not null default 0,
    delivered_count int         not null default 0,
    health_ok       boolean,
    latency_ms      int,
    checked_at      timestamptz
);

create index if not exists configs_score_idx  on public.configs (score desc);
create index if not exists configs_seen_idx   on public.configs (first_seen_at desc);
create index if not exists configs_server_idx on public.configs (server);
create index if not exists configs_proto_idx  on public.configs (protocol);

-- -------------------------- deliveries --------------------------------------
-- One row per (chat, config) delivery attempt. Drives the outbound queue.
create table if not exists public.deliveries (
    id            bigserial primary key,
    chat_id       text        not null,
    fingerprint   text        not null,
    batch_id      text,
    ord           int         not null default 0,
    status        text        not null default 'queued', -- queued|sent|failed|skipped
    score         numeric     not null default 0,
    attempts      int         not null default 0,
    next_attempt  timestamptz not null default now(),
    message_id    text,
    error         text,
    copied_at     timestamptz,
    created_at    timestamptz not null default now(),
    sent_at       timestamptz,
    unique (chat_id, fingerprint)
);

create index if not exists deliveries_queue_idx
    on public.deliveries (status, next_attempt, score desc);
create index if not exists deliveries_chat_idx
    on public.deliveries (chat_id, created_at desc);
create index if not exists deliveries_batch_idx
    on public.deliveries (batch_id);

-- --------------------------- bot_state --------------------------------------
-- Generic key/value bag: Telegram session string, getUpdates offset, counters.
create table if not exists public.bot_state (
    key         text primary key,
    value       jsonb       not null default '{}'::jsonb,
    updated_at  timestamptz not null default now()
);

-- ------------------------- runner_locks -------------------------------------
-- Leader election so exactly one GitHub Actions runner does the work, even
-- while the old run and the new run briefly overlap during a handoff.
create table if not exists public.runner_locks (
    name        text primary key,
    instance_id text        not null,
    acquired_at timestamptz not null default now(),
    expires_at  timestamptz not null,
    meta        jsonb       not null default '{}'::jsonb
);

-- ------------------------- channel_stats ------------------------------------
-- Reputation of each monitored Telegram channel. Feeds the scoring engine.
create table if not exists public.channel_stats (
    channel     text primary key,
    total       int         not null default 0,
    dead        int         not null default 0,
    copies      int         not null default 0,
    updated_at  timestamptz not null default now()
);

-- --------------------------- sources ----------------------------------------
-- Optional runtime-managed list of Telegram channels (merged with the
-- TELEGRAM_CHANNELS environment variable).
create table if not exists public.sources (
    channel     text primary key,
    enabled     boolean     not null default true,
    added_by    text,
    created_at  timestamptz not null default now()
);

-- ===========================================================================
-- Atomic compare-and-swap leader election.
-- Returns true when *this* instance now owns the lock.
-- ===========================================================================
create or replace function public.acquire_runner_lock(
    p_name        text,
    p_instance    text,
    p_ttl_seconds int,
    p_meta        jsonb default '{}'::jsonb
) returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_now timestamptz := now();
    v_ttl interval    := (p_ttl_seconds || ' seconds')::interval;
begin
    insert into public.runner_locks (name, instance_id, acquired_at, expires_at, meta)
    values (p_name, p_instance, v_now, v_now + v_ttl, p_meta)
    on conflict (name) do nothing;

    -- Steal the lock only if we already own it or the lease has expired.
    update public.runner_locks
       set instance_id = p_instance,
           acquired_at = v_now,
           expires_at  = v_now + v_ttl,
           meta        = p_meta
     where name = p_name
       and (instance_id = p_instance or expires_at < v_now);

    return exists (
        select 1
          from public.runner_locks
         where name = p_name
           and instance_id = p_instance
           and expires_at >= v_now
    );
end;
$$;

create or replace function public.release_runner_lock(p_name text, p_instance text)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    delete from public.runner_locks
     where name = p_name and instance_id = p_instance;
end;
$$;

-- ===========================================================================
-- Row level security: locked down. Only the service_role key (which bypasses
-- RLS) can read or write. The anon key gets nothing even if it leaks.
-- ===========================================================================
alter table public.users         enable row level security;
alter table public.configs       enable row level security;
alter table public.deliveries    enable row level security;
alter table public.bot_state     enable row level security;
alter table public.runner_locks  enable row level security;
alter table public.channel_stats enable row level security;
alter table public.sources       enable row level security;
