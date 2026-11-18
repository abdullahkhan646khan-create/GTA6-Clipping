-- Seed watchlist (Tier 1 English YouTube streamers). Handles are best-known; the Scout flags any that don't resolve.
-- permission_status starts 'not_asked' — publishing is blocked until you set 'granted' (Streamers page).
insert into streamers (name, channel_url, handle, priority) values
  ('IShowSpeed',      'https://www.youtube.com/@IShowSpeed',     '@IShowSpeed',     95),
  ('Caylus',          'https://www.youtube.com/@Caylus',         '@Caylus',         90),
  ('RDCWorld',        'https://www.youtube.com/@RDCworld1',      '@RDCworld1',      85),
  ('gameranx',        'https://www.youtube.com/@gameranxTV',     '@gameranxTV',     80),
  ('Typical Gamer',   'https://www.youtube.com/@TypicalGamer',   '@TypicalGamer',   85),
  ('Valkyrae',        'https://www.youtube.com/@Valkyrae',       '@Valkyrae',       80),
  ('Sykkuno',         'https://www.youtube.com/@Sykkuno',        '@Sykkuno',        80),
  ('Fuslie',          'https://www.youtube.com/@fuslie',         '@fuslie',         70),
  ('Ludwig',          'https://www.youtube.com/@ludwig',         '@ludwig',         80),
  ('TimTheTatman',    'https://www.youtube.com/@timthetatman',   '@timthetatman',   80),
  ('Dr Disrespect',   'https://www.youtube.com/@DrDisrespect',   '@DrDisrespect',   75),
  ('Jacksepticeye',   'https://www.youtube.com/@jacksepticeye',  '@jacksepticeye',  75),
  ('LazarBeam',       'https://www.youtube.com/@LazarBeam',      '@LazarBeam',      75),
  ('Ali-A',           'https://www.youtube.com/@AliA',           '@AliA',           70),
  ('VanossGaming',    'https://www.youtube.com/@VanossGaming',   '@VanossGaming',   70),
  ('MrBossFTW',       'https://www.youtube.com/@MrBossFTW',      '@MrBossFTW',      65),
  ('Zerkaa',          'https://www.youtube.com/@Zerkaa',         '@Zerkaa',         65)
on conflict (channel_url) do nothing;

insert into rules (rules, note)
select '{}'::jsonb, 'empty = use worker/rules/clip_rules.json defaults; dashboard overrides merge on top'
where not exists (select 1 from rules);
