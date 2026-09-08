"""
SMART HELP DESK: INGESTION PIPELINE
Triggered by an S3 upload event (when you drop faq_data.csv into the S3 bucket).
Reads the CSV, chunks each support response, embeds with Bedrock Titan, stores in RDS.

Flow:
  S3 Event → Lambda calls this handler → read CSV → chunk each row → embed → store in pgvector
"""
import boto3   # AWS library (talks to S3, Bedrock)
import json    # Converts Python objects to JSON for Bedrock
import os      # Reads environment variables (DB password, etc.)
import csv     # Reads the CSV from S3
import io      # Needed to read the S3 file as text (not bytes)
import psycopg2 # Talks to PostgreSQL/RDS
import socket   # Used to test raw TCP connectivity to RDS

# ── AWS Clients ──────────────────────────────────────────────────────────────
s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")

# ── Database settings ─────────────────────────────────────────────────────────
# DB_HOST comes from Lambda environment variable (set to your RDS endpoint).
# Defaults to "localhost" so you can test locally with Docker.
db_params = {
    "host": os.getenv("DB_HOST", "localhost"),
    "user": "postgres",
    "password": os.getenv("DB_PASS", "devpass"),
    "dbname": "postgres"
}

# ── Experiment Settings ───────────────────────────────────────────────────────
# CHANGE THIS VALUE to run your experiments (e.g., 200, 400, 600, 800, 1000)
CHUNK_SIZE = 600 

# We use 150 for large chunks (400+) to ensure context (like whole sentences) is preserved.
# For 200-char chunks, 150 would be 75% duplication, so we drop it to 75 to 
# keep a healthy balance of unique content vs. context in the database.
OVERLAP = 150 if CHUNK_SIZE >= 400 else 75


def chunk_text(text, size=CHUNK_SIZE, overlap=OVERLAP):
    """
    Splits long text into overlapping blocks.
    'size' = max characters per chunk (we tune this in the experiment).
    'overlap' = how many characters repeat between chunks (preserves sentence context).
    Example: "Hello world..." with size=10, overlap=3:
      Chunk 1: "Hello worl"
      Chunk 2: "orld..."
    """
    chunks = []
    i = 0
    while i < len(text):
        chunks.append(text[i:i + size])
        i += size - overlap
    return chunks


def get_embedding(text):
    """
    Calls Bedrock Titan Embeddings V2 to convert text into a 1024-number vector.
    The vector represents the MEANING of the text mathematically.
    """
    response = bedrock.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({"inputText": text})
    )
    # Parse the JSON response and pull out the "embedding" list
    return json.loads(response["body"].read())["embedding"]


def store_chunk(text, chunk_size, cur):
    """
    Saves one text chunk and its vector into the database.
    'chunk_size' is stored as metadata so we can filter by it in A/B experiments.
    """
    # 1. Get the vector from Bedrock
    vector = get_embedding(text)

    # 2. Save the text to support_articles table
    # %s = safe placeholder (prevents SQL injection attacks)
    cur.execute(
        "INSERT INTO support_articles (content, metadata) VALUES (%s, %s) RETURNING id",
        (text, json.dumps({"chunk_size": chunk_size}))
    )
    article_id = cur.fetchone()[0]  # get the auto-generated ID

    # 3. Save the vector to embeddings table, linked by article_id
    cur.execute(
        "INSERT INTO embeddings (article_id, embedding) VALUES (%s, %s)",
        (article_id, vector)
    )


def process_csv(csv_content, chunk_size=CHUNK_SIZE):
    """
    Reads each row of the CSV, extracts the 'text' column,
    chunks it, and stores each chunk in the database.
    """
    # Test raw TCP connectivity first (bypasses psycopg2)
    print("🔌 Testing raw TCP connection to RDS on port 5432...")
    try:
        sock = socket.create_connection(
            (os.getenv("DB_HOST", "localhost"), 5432), timeout=10
        )
        sock.close()
        print("✅ TCP connection to RDS succeeded!")
    except Exception as e:
        print(f"❌ TCP connection FAILED: {e}")
        raise

    print("🔌 Attempting psycopg2 connection to RDS...")
    conn = psycopg2.connect(
        **db_params,
        connect_timeout=15,
        sslmode="require",
        keepalives=1,
        keepalives_idle=5,
        keepalives_interval=2,
        keepalives_count=2
    )
    cur = conn.cursor()

    # csv_content is the raw text of the file — wrap in StringIO to read it
    reader = csv.DictReader(io.StringIO(csv_content))

    total_chunks = 0
    for row in reader:
        text = row.get("text", "").strip()

        # Skip empty or very short rows (not useful for search)
        if not text or len(text) < 30:
            continue

        # Chunk the text and store each chunk
        for chunk in chunk_text(text, size=chunk_size):
            store_chunk(chunk, chunk_size, cur)
            total_chunks += 1

    # Commit all inserts at once (faster than committing per row)
    conn.commit()
    cur.close()
    conn.close()
    print(f"✅ Ingestion complete: {total_chunks} chunks stored at size {chunk_size}.")
    return total_chunks


def lambda_handler(event, context):
    """
    Entry point: AWS Lambda calls this when a file is uploaded to S3.
    'event' contains the bucket name and file name.
    """
    # 1. Get the S3 bucket name and file key from the event
    bucket = event["Records"][0]["s3"]["bucket"]["name"]
    key = event["Records"][0]["s3"]["object"]["key"]
    print(f"Processing file: s3://{bucket}/{key}")

    # 2. Download the CSV from S3
    print("📥 Downloading CSV from S3...")
    response = s3.get_object(Bucket=bucket, Key=key)
    csv_content = response["Body"].read().decode("utf-8")
    print(f"✅ CSV downloaded ({len(csv_content)} bytes). Starting ingestion...")

    # 3. Process with current experiment's chunk size
    #    To run the A/B experiment, change the CHUNK_SIZE variable at the top.
    total = process_csv(csv_content, chunk_size=CHUNK_SIZE)
    return {"statusCode": 200, "body": f"Indexed {total} chunks."}


if __name__ == "__main__":
    """
    Local test: reads faq_data.csv from your local folder
    and ingests it into your local Docker Postgres.
    """
    print("Running local test ingestion...")
    with open("faq_data.csv", "r", encoding="utf-8") as f:
        content = f.read()
    process_csv(content, chunk_size=CHUNK_SIZE)
