"""
SMART HELP DESK: QUERY SCRIPT (FastAPI Web Server)

This script runs as a WEB SERVER on the EC2 instance.
The Application Load Balancer (ALB) forwards user requests to this server on Port 8000.

How it works:
  1. User types a question in the chat UI.
  2. The request hits the ALB → forwarded to this EC2 server.
  3. This script turns the question into a vector (Bedrock Titan).
  4. Searches the database for the most similar stored text (pgvector).
  5. Asks Nova Micro to write a cited answer using that text.
  6. Returns the answer as JSON back to the user.

To start this server on EC2, run:
  uvicorn query:app --host 0.0.0.0 --port 8000
  (0.0.0.0 means "listen on ALL network interfaces" so the ALB can reach it)
"""
from fastapi import FastAPI, Request  # FastAPI = Python web framework (like a lightweight Flask)
from fastapi.middleware.cors import CORSMiddleware  # Allow browser frontend to call this API
import boto3        # AWS library (talks to Bedrock)
import json         # Converts Python objects to/from JSON text
import os           # Reads environment variables (DB host, password)
import re           # Pattern matching for the non-question (chit-chat) guard
import psycopg2     # Talks to PostgreSQL / RDS

# ── Create the FastAPI "app" ───────────────────────────────────────────────────
# This is the web server object. All routes are registered on it.
app = FastAPI()

# ── CORS (Cross-Origin Resource Sharing) ──────────────────────────────────────
# The chat UI is served from Vercel / CloudFront (a DIFFERENT domain than the EC2 API),
# so the browser would normally block the request. CORS middleware tells the browser
# it's safe to call this API from those frontend origins.
# For a demo/portfolio project we allow all origins; tighten to your frontend domain in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],            # Allow the Vercel/CloudFront frontend to call us
    allow_credentials=False,        # No cookies/sessions needed — API-only
    allow_methods=["*"],            # Allow GET, POST, etc.
    allow_headers=["*"],            # Allow any headers (Content-Type, etc.)
)

# ── Connect to Bedrock (the AWS AI service) ───────────────────────────────────
# We create this ONCE at startup so it's ready for every request.
bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")

# ── Database settings ─────────────────────────────────────────────────────────
# DB_HOST and DB_PASS come from environment variables set on the EC2 instance.
# On your local machine they default to the Docker Postgres values.
db_params = {
    "host": os.getenv("DB_HOST", "localhost"),
    "user": "postgres",
    "password": os.getenv("DB_PASS", "devpass"),
    "dbname": "postgres"
}

# ── Out-of-scope relevance gate ────────────────────────────────────────────────
# pgvector cosine distance: smaller = more similar (0 = identical).
# If even the CLOSEST retrieved chunk is farther than this threshold, the question
# is judged OUT OF SCOPE (e.g. "what's the weather in Paris?"). We refuse with N/A
# BEFORE the LLM runs, so the model can never hallucinate citations for unrelated Qs.
# Calibrated on the LOOM demo questions (measured cosine distance):
#   Photoshop reset       = 0.68  (IN SCOPE — must answer)
#   Sprint direct message = 0.34  (IN SCOPE — must answer)
#   "weather in Paris"    = 0.89  (OUT OF SCOPE — must refuse)
# 0.78 sits between the in-scope max (0.68) and the out-of-scope (0.89).
RELEVANCE_THRESHOLD = 0.78


# ── Non-question guard (chit-chat detector) ───────────────────────────────────
# WHY: a pure vector search ALWAYS returns its k nearest neighbours — even for "hi".
# Without this guard, greetings and small talk pull random FAQ chunks and the model
# tries to answer them, which looks broken in a demo.
# HOW: a cheap regex/keyword pass (no LLM call, no DB hit → zero cost, zero latency).
# SCOPE: this only filters CONVERSATIONAL input. Out-of-scope *questions*
# (e.g. "what's the weather?") are still handled downstream by the grounded prompt,
# which refuses when the retrieved context doesn't contain the answer.
REFUSAL_MESSAGE = (
    "I can only answer help-desk questions about our products and support topics. "
    'Try asking something like: "How do I reset my password?"'
)

# ── SASSY/PERSONA REFUSALS (test build) ─────────────────────────────────────────
# The IN-SCOPE grounded prompt (get_answer) is UNCHANGED — real support questions
# still get clean, cited answers. Only the OUT-OF-CONTEXT refusal paths use these.
# These are mean / sassy / dark-humor but still clearly a refusal (never answer
# the out-of-scope question). The pick is deterministic per question (crc32), so
# the same question gets the same snark across server restarts/recordings.
SASSY_REFUSALS = [
    "Oh, adorable. You typed that and thought I'd care? I'm a help desk, not your therapist. "
    "Ask me about our actual products, or I'll pretend this ticket is from 2019.",
    "Wow, look at you asking about THAT. My entire purpose is customer support for our products "
    "— not your random curiosity. Try again with something a support bot would actually know.",
    "That question is so out of scope it's practically in another timezone. I answer questions about "
    "our products. Anything else gets the digital side-eye and a 'no'.",
    "Cute. Real cute. I run on vector search and 600-character chunks, not vibes and wikipedia. "
    "Ask me something about the products, or don't — I'm a bot and I'm very good at doing nothing.",
    "I'd love to help, but that question isn't in my context, my database, or my best interests. "
    "Stick to product/support questions, or prepare to be thoroughly ignored (but in a witty way).",
    "Okay, this is embarrassing for both of us. That's not a help-desk question. I answer support "
    "questions about our products. Anything else gets a snarky refusal like this one.",
    "You really looked at a support chatbot and asked it THAT? Bold. I'm only wired for product/support "
    "questions. Out of scope means out of luck, my friend.",
    "I'd pretend to care, but my training data doesn't cover 'off-topic tangents'. Ask a product question "
    "or I'll just keep being this pleasantly unhinged about ignoring you.",
]


def sassy_refusal(question: str = "") -> str:
    """
    Returns a mean/sassy/dark-humor refusal for out-of-context questions.
    Stable per question (crc32, not Python's salted hash()) so a demo shows the
    SAME snark for the same question across server restarts/recordings.
    """
    if not question:
        return SASSY_REFUSALS[0]
    import zlib
    idx = zlib.crc32(question.encode("utf-8")) % len(SASSY_REFUSALS)
    return SASSY_REFUSALS[idx]

# TIER 1 — the ENTIRE input is small talk (matched with fullmatch).
# These win outright, even when they contain question words: "how are you" holds
# "how" and "what's your name" holds "what", but neither is a support question.
CHITCHAT_EXACT = [
    r"(hi|hey|hello|yo|sup|howdy|hiya)( there| again| all| team)?",
    r"good\s+(morning|afternoon|evening|night)",
    r"(thanks|thank you|thx|ty|cheers|much appreciated)( a lot| so much)?",
    r"(bye|goodbye|see you|see you later|later|cya)",
    r"(ok|okay|k|cool|nice|great|got it|sure|yes|no|yep|nope)",
    r"how are you( doing| today)?",
    r"(who|what) are you",
    r"are you (a |an )?(bot|robot|human|ai|real)",
    r"what('s| is) your name",
    r"(lol|haha|hmm+|test|testing|hello world)",
]

# TIER 2 — the input STARTS with a greeting but may continue into a real question.
# "hi" alone is refused (Tier 1); "hi, how do I reset my password?" is answered,
# because the support-keyword check runs before this tier.
CHITCHAT_PREFIX = [
    r"^(hi|hey|hello|yo|sup|howdy|hiya)\b",
    r"^good\s+(morning|afternoon|evening|night)\b",
    r"^(thanks|thank you|thx|ty|cheers)\b",
    r"^(bye|goodbye|see you|cya)\b",
]

# If any of these appear the input is treated as a genuine support question,
# even when it also looks like small talk (e.g. "hi, how do I reset my password?").
SUPPORT_KEYWORDS = (
    "how", "why", "what", "when", "where", "which", "can i", "do i", "does",
    "error", "issue", "problem", "broken", "not working", "fail", "help with",
    "account", "password", "login", "log in", "sign in", "refund", "return",
    "order", "shipping", "delivery", "cancel", "billing", "charge", "payment",
    "subscription", "upgrade", "downgrade", "install", "update", "reset",
    "support", "contact", "policy", "warranty", "track",
)


def is_chitchat(question: str) -> bool:
    """
    Returns True if the input is small talk / not a support question,
    so the caller can refuse without hitting the database or the model.
    """
    if not question:
        return True

    text = question.strip().lower()

    # Strip trailing punctuation so "hi!" and "thanks." match the patterns.
    normalized = re.sub(r"[\s!.?,]+$", "", text)

    if len(normalized) < 3:
        return True  # "hi", "ok", "?" — nothing to search for

    # TIER 1 first: if the whole input is small talk, refuse it regardless of
    # any question words it happens to contain ("how are you", "what are you").
    if any(re.fullmatch(p, normalized) for p in CHITCHAT_EXACT):
        return True

    # A genuine support keyword now wins: "hey, my order is late" is a question.
    if any(kw in normalized for kw in SUPPORT_KEYWORDS):
        return False

    # TIER 2: starts like small talk and carried no support keyword → refuse.
    if any(re.match(p, normalized) for p in CHITCHAT_PREFIX):
        return True

    # No question mark, no support keyword, and only a word or two → treat as chit-chat.
    if "?" not in text and len(normalized.split()) <= 2:
        return True

    return False


@app.post("/")
async def handle_query(request: Request):
    """
    This is the API endpoint. It listens for POST requests at '/' (the root path).
    The ALB health check also hits '/' — FastAPI returns HTTP 200 by default.

    The request body should be JSON like: {"question": "What is your return policy?"}
    Optional: {"question": "...", "k": 10, "chunk_size": "600"}
      - k = how many results to pull from the database (default: 10)
      - chunk_size = filter by experiment chunk size (default: "600")
    Both defaults are the Tuning Lab optima; clients may override them.

    Small talk ("hi", "thanks", "are you a bot?") is refused up front by
    is_chitchat() with confidence "N/A" — no DB query and no Bedrock call.
    """
    # 1. Parse the JSON body from the incoming HTTP request
    data = await request.json()

    question = data.get("question")            # the user's question text
    # ── OPTIMAL CONFIG (from Tuning Lab) — locked so production uses best values ──
    # Best = chunk=600, K=10 (100% adversarial block rate, sub-second latency; 600 won the Judge Lab).
    # Clients may override, but these are the production defaults.
    k = data.get("k", 10)                      # top-k results (default 10 — our optimal)
    chunk_size = data.get("chunk_size", "600") # A/B filter (default 600 — best avg in Judge Lab)

    # 2. Non-question guard: refuse small talk BEFORE spending a DB query or a
    #    Bedrock call. Vector search would otherwise return its nearest neighbours
    #    for "hi" and the model would try to answer from irrelevant chunks.
    if is_chitchat(question):
        return {"answer": sassy_refusal(question), "confidence": "N/A"}

    # 3. Search the database for relevant text snippets
    results = search_db(question, k, chunk_size)   # list of (content, distance)

    # 3b. OUT-OF-SCOPE GATE: if even the closest chunk is too dissimilar, refuse
    #     with N/A BEFORE calling the LLM. This is deterministic — the model can
    #     never hallucinate a citation for an unrelated question (e.g. weather).
    if not results:
        return {"answer": sassy_refusal(question), "confidence": "N/A", "sources": []}
    closest_distance = results[0][1] if isinstance(results[0], tuple) else 0.0
    if closest_distance > RELEVANCE_THRESHOLD:
        return {"answer": sassy_refusal(question), "confidence": "N/A", "sources": []}

    # 4. Ask Nova Micro to write a cited answer from those snippets
    answer = get_answer(question, results)

    # 5. Detect whether the model actually answered or declined. When it says the
    #    context doesn't contain the info (e.g. "What is your return policy?"), the
    #    answer is effectively a refusal — so tag it N/A (Q3b spec) so the frontend
    #    hides the confidence footer, exactly like the chit-chat guard.
    NO_ANSWER_PHRASES = (
        "does not contain", "doesn't contain", "cannot provide", "can't provide",
        "no information", "don't have information", "do not have information",
        "not in the context", "not in the provided context", "i don't have", "i do not have",
    )
    declined = any(phrase in answer.lower() for phrase in NO_ANSWER_PHRASES)

    # 6. Return the answer + the retrieved source chunks so the frontend can
    #    render a "Sources" section that makes the [n] citation markers resolvable.
    sources = [r[0] for r in results]

    # DEFENSIVE: if the model declined (no answer in context), strip ANY [n]
    # citation markers the model may have written anyway, and clear the sources
    # array. This is a hard guarantee that out-of-scope refusals never leak
    # citations to the UI — regardless of what the model does with the prompt.
    # In the SASSY build we also replace the model's neutral decline with a
    # mean/dark-humor refusal (still never reveals the out-of-scope answer).
    if declined:
        answer = sassy_refusal(question)
        sources = []

    conf = "N/A" if declined else "High"
    return {"answer": answer, "confidence": conf, "sources": sources}


def search_db(question, k, chunk_size=None):
    """
    Searches pgvector for the 'k' most semantically similar text chunks.

    Steps:
      A) Turn the question into a 1024-number vector (Bedrock Titan).
      B) Use the '<=> ' operator (cosine distance) to find closest vectors in RDS.
      C) Return the top k matching text chunks.
    """
    # Step A: Embed the question into a vector
    # Using the current V2 embedding model
    response = bedrock.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({
            "inputText": question,
            "dimensions": 1024,
            "normalize": True
        })
    )
    q_vector = json.loads(response["body"].read())["embedding"]

    # Step B: Connect to the database and search
    conn = psycopg2.connect(**db_params)
    cur = conn.cursor()

    # If chunk_size is provided, filter to only match chunks from that experiment
    # This is what makes our A/B tuning possible
    filter_sql = "WHERE metadata->>'chunk_size' = %s" if chunk_size else ""

    # Build the parameter tuple based on whether we're filtering
    # Order: [chunk_size if filter], q_vector, k
    # Param order matches placeholder order in SQL: SELECT distance vector,
    # WHERE chunk_size (optional), ORDER BY vector, LIMIT k.
    if chunk_size:
        params = (q_vector, str(chunk_size), q_vector, k)
    else:
        params = (q_vector, q_vector, k)

    # '<=> ' = pgvector cosine distance (smaller = more similar)
    # 'ORDER BY ... LIMIT k' = return only the closest k results
    # 'JOIN' combines the embeddings table with the support_articles text table
    cur.execute(f"""
        SELECT content, embedding <=> %s::vector AS distance
        FROM embeddings
        JOIN support_articles ON article_id = support_articles.id
        {filter_sql}
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """, params)

    # ALSO return the cosine distance so we can gate out-of-scope queries.
    # The SELECT below returns (content, distance) — distance is smaller = more relevant.
    results = cur.fetchall()  # list of (content, distance) tuples
    cur.close()
    conn.close()
    return results


def get_answer(question, contexts):
    """
    Sends the retrieved text snippets + the question to Amazon Nova Micro.
    Nova Micro is instructed to answer ONLY from the provided context (no guessing).

    The prompt uses numbered citations [1], [2], etc. so answers are grounded
    and verifiable — this is the key feature that prevents hallucination.
    Citations are capped at 3 and suppressed on refusals, so a "no answer in
    context" response doesn't emit every retrieved chunk ID.
    """
    # Format retrieved snippets as a numbered list for Nova Micro to cite
    ctx_text = "\n".join([f"[{i+1}] {r[0]}" for i, r in enumerate(contexts)])

    # The prompt is the "instruction" we give Nova Micro.
    # RESTORED the original minimalist prompt that produced the best answers.
    # "Answer ONLY from context" + "cite at most 3" + "refuse plainly" is the contract.
    # The two structural fixes are KEPT and do the out-of-scope work deterministically:
    #   (1) relevance gate refuses far-off questions (weather) before the LLM runs,
    #   (2) defensive strip clears [n] markers + sources on any detected refusal.
    prompt = (
        "You are a customer support assistant. "
        "Answer using ONLY the context below. "
        "Cite only the specific sources you actually used, at most 3, as [1], [2]. "
        "If the context doesn't contain the answer, say so plainly without citations.\n\n"
        f"Context:\n{ctx_text}\n\n"
        f"Question: {question}"
    )

    # Call Amazon Nova Micro via Bedrock (Newest, cheapest, actively supported)
    response = bedrock.invoke_model(
        modelId="amazon.nova-micro-v1:0",
        body=json.dumps({
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {
                "maxTokens": 500,
                "temperature": 0
            }
        })
    )

    # Pull the answer text out of Nova's JSON response
    return json.loads(response["body"].read())["output"]["message"]["content"][0]["text"]
