-- Run this in Supabase SQL editor to set up the pipeline tables.

-- One row per client you manage
create table if not exists clients (
  id            uuid primary key default gen_random_uuid(),
  name          text not null,
  youtube_channel_url text not null,
  active        boolean not null default true,
  created_at    timestamptz not null default now()
);

-- One row per YouTube video processed
create table if not exists videos (
  id                   uuid primary key default gen_random_uuid(),
  client_id            uuid not null references clients(id) on delete cascade,
  youtube_video_id     text not null,
  title                text,
  url                  text,
  duration_seconds     int,
  upload_date          text,       -- yt-dlp returns YYYYMMDD string
  view_count           bigint,
  thumbnail_url        text,
  description          text,
  like_count           bigint,
  channel_name         text,
  transcript_segments  jsonb,      -- [{start, end, text}, ...]
  transcript_text      text,       -- full plain text for Claude context
  status               text not null default 'transcribed',
  created_at           timestamptz not null default now(),

  unique(client_id, youtube_video_id)
);

-- Job queue — watcher writes 'clip' jobs, clipper picks them up
create table if not exists jobs (
  id          uuid primary key default gen_random_uuid(),
  client_id   uuid not null references clients(id) on delete cascade,
  video_id    uuid references videos(id) on delete cascade,
  job_type    text not null,        -- 'clip' | 'post_a' | 'post_b'
  status      text not null default 'pending',  -- pending | running | done | failed
  payload     jsonb,                -- extra data (clip paths, captions, etc.)
  error       text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- Index so clipper can efficiently poll for pending jobs
create index if not exists jobs_status_idx on jobs(status, job_type, created_at);

-- Per-platform upload tracking
create table if not exists posts (
  id          uuid primary key default gen_random_uuid(),
  clip_id     uuid not null references clips(id) on delete cascade,
  poster_slot text not null,   -- 'a' | 'b'
  platform    text not null,   -- 'tiktok' | 'youtube' | 'facebook'
  status      text not null default 'pending',  -- pending | posted | failed
  post_url    text,
  error       text,
  created_at  timestamptz not null default now()
);

-- One row per clip cut from a video
create table if not exists clips (
  id               uuid primary key default gen_random_uuid(),
  client_id        uuid not null references clients(id) on delete cascade,
  video_id         uuid not null references videos(id) on delete cascade,
  clip_index       int not null,          -- 1, 2, 3
  start_seconds    float not null,
  end_seconds      float not null,
  duration_seconds float not null,
  hook             text,                  -- why Claude picked this moment
  caption          text,                  -- AI-generated post caption
  storage_path     text,                  -- path inside Supabase Storage bucket
  status           text not null default 'ready',  -- ready | posted_a | posted_b | done
  created_at       timestamptz not null default now()
);
