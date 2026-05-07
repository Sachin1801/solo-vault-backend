CREATE SCHEMA IF NOT EXISTS vault;
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS file_hash text;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS chunk_count bigint NOT NULL DEFAULT 0;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS embedding_model text;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS chunker_version text;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS index_error text;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS uploaded_at bigint;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS indexed_at bigint;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS owner_user_id text;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS organization_id text;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS project_id text;
UPDATE vault.entries SET owner_user_id = user_id WHERE owner_user_id IS NULL;

ALTER TABLE vault.chunks ADD COLUMN IF NOT EXISTS chunk_hash text;

CREATE INDEX IF NOT EXISTS idx_entries_user_hash       ON vault.entries(user_id, file_hash);
CREATE INDEX IF NOT EXISTS idx_entries_user_status     ON vault.entries(user_id, index_status);
CREATE INDEX IF NOT EXISTS idx_entries_owner           ON vault.entries(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_entries_organization    ON vault.entries(organization_id);
CREATE INDEX IF NOT EXISTS idx_entries_project         ON vault.entries(project_id);
CREATE INDEX IF NOT EXISTS idx_chunks_user_hash        ON vault.chunks(user_id, chunk_hash);

CREATE TABLE IF NOT EXISTS vault.organizations (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    created_at  bigint NOT NULL,
    updated_at  bigint NOT NULL
);

CREATE TABLE IF NOT EXISTS vault.organization_members (
    organization_id text NOT NULL REFERENCES vault.organizations(id) ON DELETE CASCADE,
    user_id         text NOT NULL,
    role            text NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    created_at      bigint NOT NULL,
    updated_at      bigint NOT NULL,
    PRIMARY KEY (organization_id, user_id)
);

CREATE TABLE IF NOT EXISTS vault.projects (
    id              text PRIMARY KEY,
    organization_id text NOT NULL REFERENCES vault.organizations(id) ON DELETE CASCADE,
    name            text NOT NULL,
    created_at      bigint NOT NULL,
    updated_at      bigint NOT NULL
);

CREATE TABLE IF NOT EXISTS vault.project_members (
    project_id text NOT NULL REFERENCES vault.projects(id) ON DELETE CASCADE,
    user_id    text NOT NULL,
    role       text NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    created_at bigint NOT NULL,
    updated_at bigint NOT NULL,
    PRIMARY KEY (project_id, user_id)
);

CREATE TABLE IF NOT EXISTS vault.entry_access (
    entry_id     text NOT NULL REFERENCES vault.entries(id) ON DELETE CASCADE,
    subject_type text NOT NULL CHECK (subject_type IN ('user', 'organization', 'project')),
    subject_id   text NOT NULL,
    role         text NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    created_at   bigint NOT NULL,
    updated_at   bigint NOT NULL,
    PRIMARY KEY (entry_id, subject_type, subject_id)
);

CREATE INDEX IF NOT EXISTS idx_org_members_user       ON vault.organization_members(user_id);
CREATE INDEX IF NOT EXISTS idx_project_members_user   ON vault.project_members(user_id);
CREATE INDEX IF NOT EXISTS idx_entry_access_subject   ON vault.entry_access(subject_type, subject_id);
