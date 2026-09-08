"""
DATA PREPARATION SCRIPT
Run this ONCE on your laptop before uploading data to S3.
It reads the raw Kaggle twcs.csv and outputs a clean faq_data.csv
with only the 200 best support responses.
"""
import csv

INPUT_FILE = "twcs.csv"       # the raw Kaggle download
OUTPUT_FILE = "faq_data.csv"  # the clean file you'll upload to S3
MAX_ROWS = 200                 # keep this low to save Bedrock costs

rows_written = 0

with open(INPUT_FILE, encoding="utf-8") as infile, \
     open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as outfile:

    reader = csv.DictReader(infile)
    # Write only the 'text' column to the output (that's what we embed)
    writer = csv.DictWriter(outfile, fieldnames=["text"])
    writer.writeheader()

    for row in reader:
        text = row.get("text", "").strip()

        # Skip rows that are empty or just a URL (not useful for FAQ search)
        if not text or text.startswith("http"):
            continue

        # Skip very short rows (less than 30 chars = greeting, not an answer)
        if len(text) < 30:
            continue

        writer.writerow({"text": text})
        rows_written += 1

        # Stop once we have enough rows
        if rows_written >= MAX_ROWS:
            break

print(f"✅ Done! Saved {rows_written} clean rows to '{OUTPUT_FILE}'")
print(f"📤 Next step: Upload '{OUTPUT_FILE}' to your S3 bucket.")
