-- Split table for speed: text in one, numbers (vectors) in the other.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS support_articles (
    id SERIAL PRIMARY KEY,
    category TEXT,
    content TEXT NOT NULL,
    metadata JSONB -- stores chunk size and experiment info
);

CREATE TABLE IF NOT EXISTS embeddings (
    id SERIAL PRIMARY KEY,
    article_id INTEGER REFERENCES support_articles(id),
    embedding vector(1024) -- Titan V2 dimensions
);

-- Index for fast searching
CREATE INDEX ON embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
