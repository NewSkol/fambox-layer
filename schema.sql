-- The family's own data, in its own schema inside the box's "builder" database.
-- Safe to run again: every statement checks before creating.
create schema if not exists family;

create table if not exists family.household (
  id int primary key default 1 check (id = 1),     -- one family per box
  name text not null,                              -- "Kask"
  slug text not null,                              -- "kask" (kask.fam.box)
  agent_name text not null default 'Pip',
  agent_line text,                                 -- Pip's line of the day
  tz text not null default 'Europe/Tallinn',
  lat numeric, lon numeric                         -- for the weather; none means no weather
);

create table if not exists family.people (
  id serial primary key,
  name text not null check (name <> ''),
  role text not null check (role in ('adult', 'child', 'pet')),
  colour text not null check (colour in ('teal', 'sky', 'plum', 'sage', 'rose', 'clay', 'lavender', 'olive')),
  sort int not null default 0
);

create table if not exists family.suggestions (
  id serial primary key,
  text text not null,
  items jsonb not null default '[]',               -- [{label, title, date, time?, end_date?}] or [{label, task, person}]
  accept_label text not null,
  quiet boolean not null default false,
  state text not null default 'pending' check (state in ('pending', 'accepted', 'dismissed')),
  decided_at timestamptz
);

create table if not exists family.events (
  id serial primary key,
  title text not null,
  starts_at timestamptz not null,
  ends_at timestamptz,
  all_day boolean not null default false,
  person_id int references family.people on delete set null,   -- none means everyone
  suggestion_id int references family.suggestions on delete set null
);
create index if not exists events_starts_at on family.events (starts_at);

create table if not exists family.tasks (
  id serial primary key,
  title text not null,
  person_id int references family.people on delete set null,
  done_at timestamptz,
  created_at timestamptz not null default now(),
  suggestion_id int references family.suggestions on delete set null
);

create table if not exists family.status (
  id serial primary key,
  sentence text not null,
  detail text,
  action_label text not null,
  person_id int references family.people on delete set null,
  done_at timestamptz
);

-- Prompt 06: moving in, sign-in by link, Pip on Telegram, a first dream.
-- "add column if not exists" keeps this file safe to run on a box that already has the tables above.
alter table family.household add column if not exists agent_character text not null default 'Pip';
alter table family.household add column if not exists ai_plan text not null default 'rules';  -- rules | claude | chatgpt
alter table family.household add column if not exists bot_token text;          -- the family's own bot; never shown
alter table family.household add column if not exists bot_username text;
-- Launch task 52: Telegram is outside the EU with no adequacy decision, so Pip on Telegram runs on explicit consent
-- (GDPR Art. 49(1)(a)): the adult who connects the bot ticks a box, and each family member says YES to Pip once.
alter table family.household add column if not exists telegram_consent_at timestamptz;
alter table family.household add column if not exists moved_in_at timestamptz;  -- onboarding finished
alter table family.household add column if not exists step int not null default 1;  -- furthest onboarding step reached
alter table family.people add column if not exists detail text;                -- "Mum", "9", "the dog"

create table if not exists family.keys (       -- sign-in links, sessions and Telegram join codes, stored hashed
  hash text primary key,
  kind text not null check (kind in ('link', 'session', 'join')),
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  used_at timestamptz
);

create table if not exists family.chats (      -- Telegram chats that may talk to Pip
  chat_id bigint primary key,
  name text,
  pending jsonb,                               -- Pip's open question, e.g. {"event": 12}
  linked_at timestamptz not null default now()
);
alter table family.chats add column if not exists consent_at timestamptz;

create table if not exists family.messages (   -- what was said to Pip and what Pip answered
  id serial primary key,
  via text not null check (via in ('telegram', 'web')),
  chat_id bigint,
  from_pip boolean not null,
  text text not null,
  at timestamptz not null default now()
);

create table if not exists family.dreams (
  id serial primary key,
  title text not null check (title <> ''),
  cover int not null default 0,                -- one of the four drawn covers
  sharers int[] not null default '{}',         -- people ids
  on_date date,                                -- none means someday
  saving boolean not null default false,
  created_at timestamptz not null default now()
);

-- Prompts 07 to 09: calendar feed, money, dreams, countdowns, routines, meals, notes, photos, layout, support.
alter table family.household add column if not exists feed_key text;               -- the private phone-calendar link
alter table family.household add column if not exists money_plan numeric(10, 2);   -- this month's plan, in euros
alter table family.events add column if not exists countdown boolean not null default false;  -- a tear-off countdown
alter table family.dreams add column if not exists note text;                      -- "5 nights", "Oskar is keen"
alter table family.dreams add column if not exists lived_on date;                  -- it came true
alter table family.dreams add column if not exists steps jsonb not null default '[]';  -- [{t, person, done}]
alter table family.dreams add column if not exists photo_id int;

create table if not exists family.spending (
  id serial primary key,
  amount numeric(10, 2) not null check (amount > 0),
  note text not null default '',
  spent_on date not null default current_date,
  bill_id int
);

create table if not exists family.bills (       -- monthly: due on the same day each month
  id serial primary key,
  title text not null check (title <> ''),
  amount numeric(10, 2) not null check (amount > 0),
  due_day int not null check (due_day between 1 and 31),
  paid_month date                               -- first day of the month it was last paid for
);

create table if not exists family.goals (       -- saving goals, often for a dream
  id serial primary key,
  title text not null check (title <> ''),
  target numeric(10, 2) not null check (target > 0),
  saved numeric(10, 2) not null default 0,
  by_date date,
  dream_id int references family.dreams on delete set null
);

create table if not exists family.routines (    -- picture steps a child ticks
  id serial primary key,
  person_id int references family.people on delete cascade,
  part text not null default 'morning' check (part in ('morning', 'evening')),
  steps jsonb not null default '[]',            -- [{name, icon}]
  sort int not null default 0
);

create table if not exists family.routine_done (
  routine_id int references family.routines on delete cascade,
  step int not null,
  on_day date not null,
  at timestamptz not null default now(),
  primary key (routine_id, step, on_day)
);

create table if not exists family.meals (
  id serial primary key,
  on_date date not null,
  title text not null check (title <> '')
);

create table if not exists family.shopping (
  id serial primary key,
  title text not null check (title <> ''),
  done_at timestamptz
);

create table if not exists family.notes (
  id serial primary key,
  text text not null check (text <> ''),
  created_at timestamptz not null default now()
);

create table if not exists family.photos (      -- the family's own pictures: dreams they lived, the wall frame
  id serial primary key,
  data bytea not null,
  mime text not null,
  caption text not null default '',
  created_at timestamptz not null default now()
);

create table if not exists family.blocks (      -- the family's own arrangement of the home screen, on every device
  kind text primary key,
  size text not null check (size in ('S', 'M', 'L')),
  sort int not null,
  shown boolean not null default true
);

create table if not exists family.support (     -- "Something not right?": what the family told us
  id serial primary key,
  text text not null check (text <> ''),
  page text,
  at timestamptz not null default now()
);

-- Phases B and C (2026-11-07): every euro has a pot. A saving goal is a pot; it says where its money physically is.
alter table family.goals add column if not exists place text;                       -- "Cash box at home", "Revolut Pocket", "Bank"
alter table family.household add column if not exists payday int check (payday between 1 and 31);  -- Pip asks "save first?"

create table if not exists family.pot_moves (    -- money into (+) or out of (-) a pot; goals.saved is the running total
  id serial primary key,
  goal_id int not null references family.goals on delete cascade,
  amount numeric(10, 2) not null check (amount <> 0),
  note text not null default '',
  on_date date not null default current_date
);

create table if not exists family.income (       -- what came in: "salary 3200"
  id serial primary key,
  amount numeric(10, 2) not null check (amount > 0),
  note text not null default '',
  on_date date not null default current_date
);

create table if not exists family.bank_lines (   -- lines from a statement file or the bank, until the family accepts them
  id serial primary key,
  on_date date not null,
  amount numeric(10, 2) not null,                -- money out is negative
  text text not null,                            -- who and what; long numbers (cards) are cut to their last four
  source text not null default '',               -- "LHV", "Revolut", "bank"
  hint text,                                     -- "own" (moving the family's own money)
  proposal jsonb,                                -- {"do": "spend" | "bill" | "pot_in" | "pot_out" | "income" | "skip", "id": n}
  state text not null default 'new' check (state in ('new', 'done', 'skipped')),
  created_at timestamptz not null default now(),
  unique (on_date, amount, text)                 -- the same line from a second file is not taken twice
);

create table if not exists family.kakeibo (      -- the four questions at the month's end, with the month's numbers
  month date primary key,                        -- the first day of the month
  facts jsonb not null default '{}',
  answer text,
  asked_at timestamptz not null default now(),
  answered_at timestamptz
);

create table if not exists family.journal (      -- a dream's small diary: "found cheap flights"
  id serial primary key,
  dream_id int not null references family.dreams on delete cascade,
  text text not null check (text <> ''),
  photo_id int references family.photos on delete set null,
  on_date date not null default current_date
);

-- Chores earn points toward a child's own dream.
alter table family.tasks add column if not exists points int check (points between 1 and 1000);
alter table family.routines add column if not exists points int check (points between 1 and 100);  -- for each step
alter table family.dreams add column if not exists points_for int references family.people on delete set null;
alter table family.dreams add column if not exists points_target int check (points_target between 1 and 100000);
create table if not exists family.points (
  id serial primary key,
  person_id int not null references family.people on delete cascade,
  amount int not null,
  why text not null default '',
  task_id int references family.tasks on delete set null,      -- removing a chore later keeps what it earned
  routine_id int references family.routines on delete set null,
  step int,
  on_day date,
  at timestamptz not null default now()
);

-- Phase E (2026-11-07): Pip in the family's Telegram group, Pip with AI, letter photos, a morning message per person.
alter table family.chats add column if not exists kind text not null default 'private';  -- private | group
alter table family.messages add column if not exists sender bigint;   -- the Telegram person who wrote (or was answered)
create table if not exists family.members (      -- one Telegram person: their own YES, and their morning message
  tg_user bigint primary key,                    -- Telegram's user id (a private chat has the same id)
  name text,
  person_id int references family.people on delete set null,   -- who they are at the table, matched by first name
  consent_at timestamptz,                        -- none: seen in the family group, has not said YES yet
  morning time default '07:00',                  -- none: no morning message
  morning_sent date,                             -- never twice a day
  created_at timestamptz not null default now()
);
-- each private chat that already said YES is a person who said YES
insert into family.members (tg_user, name, consent_at)
  select chat_id, name, consent_at from family.chats where chat_id > 0 and consent_at is not null on conflict do nothing;
create table if not exists family.pending (      -- Pip's open question, per chat and per person in it
  chat_id bigint not null,                       -- 0: the home screen
  sender bigint not null,
  data jsonb not null,
  primary key (chat_id, sender)
);
with moved as (update family.chats set pending = null where pending is not null returning chat_id, pending)
  insert into family.pending (chat_id, sender, data) select chat_id, chat_id, pending from moved on conflict do nothing;
alter table family.household add column if not exists ai_mode text not null default 'fambox';  -- fambox | own | rules
alter table family.household add column if not exists ai_provider text;   -- own key: anthropic | openai | openrouter | gemini | mistral
alter table family.household add column if not exists ai_key text;        -- own key; never shown again, never logged
alter table family.household add column if not exists ai_state text;      -- the AI's last answer: ok | cap_reached | ai_not_set_up | ...
alter table family.household add column if not exists ai_said_on date;    -- "no AI right now" is said once a day at most

-- Phase D: the new home screen. Households that existed when this column arrived keep the first home ('old', offered
-- the new one once); every household made after it starts on the new one. old | kept (said "not now") | new | tried
alter table family.household add column if not exists home text not null default 'old';
alter table family.household alter column home set default 'new';

-- Two-way calendars (2026-11, calsync.py): one iCloud and/or one Google calendar kept in step with the table.
alter table family.events add column if not exists updated_at timestamptz not null default now();
create or replace function family.touch_event() returns trigger language plpgsql as $$
begin
  if (new.title, new.starts_at, new.ends_at, new.all_day, new.person_id)
     is distinct from (old.title, old.starts_at, old.ends_at, old.all_day, old.person_id) then
    new.updated_at = clock_timestamp();
  end if;
  return new;
end $$;
create or replace trigger touch before update on family.events for each row execute function family.touch_event();

create table if not exists family.cal_accounts (
  id serial primary key,
  kind text not null unique check (kind in ('icloud', 'google')),
  login text not null default '',     -- the Apple ID or Google address, shown on the Calendar page
  secret text,                        -- iCloud app-specific password or Google refresh token: never shown, never logged
  calendar text,                      -- the CalDAV calendar's address or the Google calendar id; none until chosen
  calendar_name text,
  choices jsonb not null default '[]',  -- the calendars to choose from: [{url, name}]
  sync_token text,
  access_token text,                  -- Google's short-lived key
  access_until timestamptz,
  problem text,                       -- none when fine; signin | gone | offline
  problem_since timestamptz,
  synced_at timestamptz,
  created_at timestamptz not null default now()
);
create table if not exists family.cal_links (   -- a table event and its copy in one connected calendar
  account_id int not null references family.cal_accounts on delete cascade,
  remote_id text not null,            -- iCloud: the event's address (+ "#" + the start, one of a repeating event); Google: its id
  event_id int references family.events on delete set null,  -- none: removed on the table, to be removed there
  etag text,
  seen timestamptz,                   -- the table event's updated_at when both sides were last the same
  came_from_there boolean not null default false,
  primary key (account_id, remote_id)
);
create index if not exists cal_links_event on family.cal_links (event_id);

-- Any change, from the dashboard or from Pip, tells every open screen to refresh.
create or replace function family.announce() returns trigger language plpgsql as $$
begin
  perform pg_notify('family_changed', tg_table_name);
  return null;
end $$;

do $$
declare t text;
begin
  foreach t in array array['household', 'people', 'suggestions', 'events', 'tasks', 'status', 'messages', 'dreams',
                       'spending', 'bills', 'goals', 'routines', 'routine_done', 'meals', 'shopping', 'notes',
                       'photos', 'blocks', 'pot_moves', 'income', 'bank_lines', 'kakeibo', 'journal', 'points'] loop
    execute format('create or replace trigger announce after insert or update or delete on family.%I
                    for each statement execute function family.announce()', t);
  end loop;
end $$;
