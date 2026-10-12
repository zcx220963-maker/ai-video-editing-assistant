-- R2: materials.origin 扩容 + asset_library 建表
-- 幂等：可重复执行
BEGIN;
ALTER TABLE materials DROP CONSTRAINT IF EXISTS materials_origin_check;
ALTER TABLE materials ADD CONSTRAINT materials_origin_check
  CHECK (origin IN ('upload','library','bgm','url','render','art'));
COMMIT;

CREATE TABLE IF NOT EXISTS asset_library (
  id                   text PRIMARY KEY,
  sha256               text NOT NULL UNIQUE,
  bytes                bigint NOT NULL,
  mime                 text NOT NULL,
  ext                  text NOT NULL,
  width                integer, height  integer,
  object_key           text NOT NULL,
  source               text NOT NULL,
  src_id               text NOT NULL DEFAULT '',
  license              text NOT NULL,
  attribution_required boolean NOT NULL DEFAULT false,
  license_url          text NOT NULL DEFAULT '',
  subject              text NOT NULL DEFAULT '',
  style_family         text NOT NULL DEFAULT '',
  tags                 text NOT NULL DEFAULT '[]',
  caption              text NOT NULL DEFAULT '',
  era                  text NOT NULL DEFAULT '',
  enabled              boolean NOT NULL DEFAULT true,
  fetched_at           timestamptz NOT NULL DEFAULT now(),
  created_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS asset_subject_idx ON asset_library(subject, enabled);
CREATE INDEX IF NOT EXISTS asset_style_idx   ON asset_library(style_family, enabled);
CREATE INDEX IF NOT EXISTS asset_source_idx  ON asset_library(source, enabled);