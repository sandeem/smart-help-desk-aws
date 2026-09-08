"""
DATA PREPARATION (PAIRED) — run ONCE on your laptop.
Reads the raw Kaggle twcs.csv and outputs faq_data_paired.csv where EACH ROW
bundles a customer's question with the agent's reply that answers it.

WHY: The original faq_data.csv stored every tweet as a separate row, so a
question like "drag an image onto a canvas no longer center snaps it" (the
customer's tweet) and "try resetting the Photoshop preferences" (the agent's
reply) were DISCONNECTED. Asking the intent question matched only the agent's
reply text, which echoed it back.

By pairing them into one row, the retrieved context contains BOTH the real
problem AND the answer — so retrieval lands on intent, not just phrasing.
"""
import csv
from collections import defaultdict

INPUT_FILE = "twcs.csv"          # the raw Kaggle download
OUTPUT_FILE = "faq_data_paired.csv"  # paired question+answer rows to upload
MAX_ROWS = 200                   # keep low to save Bedrock embedding costs

# ── 1. Load every tweet into memory, keyed by tweet_id ──────────────────────
rows = {}
with open(INPUT_FILE, encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        try:
            rows[int(row["tweet_id"])] = row
        except (ValueError, KeyError):
            continue

# ── 2. Build question→answer pairs ─────────────────────────────────────────
# An agent reply (inbound=False) has in_response_to_tweet_id = the customer's
# tweet_id. That customer tweet is the QUESTION; the agent reply is the ANSWER.
pairs = []
for tid, row in rows.items():
    if row["inbound"] != "False":
        continue  # only agent replies start a pair
    irtid = (row.get("in_response_to_tweet_id") or "").strip()
    if not irtid.isdigit():
        continue
    irtid = int(irtid)
    if irtid not in rows:
        continue
    customer = rows[irtid]
    if customer["inbound"] != "True":
        continue  # the referenced tweet must be a customer question

    q_text = (customer.get("text") or "").strip()
    a_text = (row.get("text") or "").strip()
    # Skip rows too short to be useful (greetings, URLs)
    if len(q_text) < 20 or len(a_text) < 20:
        continue
    if a_text.startswith("http") or q_text.startswith("http"):
        continue
    pairs.append((q_text, a_text))

# ── 3. Write paired rows ───────────────────────────────────────────────────
with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as out:
    writer = csv.DictWriter(out, fieldnames=["text"])
    writer.writeheader()
    for i, (q, a) in enumerate(pairs[:MAX_ROWS]):
        combined = f"Question: {q}\nAnswer: {a}"
        writer.writerow({"text": combined})

print(f"✅ Built {min(len(pairs), MAX_ROWS)} paired rows → '{OUTPUT_FILE}'")
print(f"   (total candidates available: {len(pairs)})")
print(f"📤 Next step: Upload '{OUTPUT_FILE}' to S3, then re-run ingestion.")
