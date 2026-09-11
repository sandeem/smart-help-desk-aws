# 🏗️ Architecture — Smart Help Desk

Infrastructure diagrams for the AWS RAG chatbot: what lives in which subnet, how traffic flows, and why each boundary exists.

All diagrams below are [Mermaid](https://mermaid.js.org/) and render automatically on GitHub.

---

## 1. Infrastructure Overview — VPC & Subnet Placement

The single most important property of this design: **nothing that holds data or runs application logic is reachable from the internet.** Only the load balancer lives in a public subnet.

```mermaid
flowchart TB
    subgraph internet["🌐 Internet"]
        user["👤 User<br/>Browser"]
        vercel["▲ Vercel<br/>Static UI + HTTPS<br/>rewrite proxy /api/*"]
    end

    subgraph aws["☁️ AWS — Region us-east-1"]
        subgraph managed["AWS Managed Services (outside VPC)"]
            bedrock["🧠 Amazon Bedrock<br/>Titan V2 · Nova Micro"]
            s3["🪣 Amazon S3<br/>enterprise-faq-data<br/>CSV · code bundle · reports"]
        end

        subgraph vpc["🔒 VPC — enterprise-vpc &nbsp;|&nbsp; 2 Availability Zones"]
            igw["🚪 Internet Gateway"]

            subgraph pub["🟩 PUBLIC SUBNETS — AZ-a + AZ-b"]
                alb["⚖️ Application Load Balancer<br/>listener :80<br/><b>alb-sg</b>"]
            end

            subgraph priv["🟥 PRIVATE SUBNETS — AZ-a + AZ-b &nbsp;(no internet route)"]
                ec2["🖥️ EC2 t3.micro<br/>FastAPI / uvicorn :8000<br/><b>ec2-sg</b>"]
                lam["λ Lambda<br/>ingest.py<br/>VPC-attached"]
                rds[("🗄️ RDS PostgreSQL<br/>db.t4g.micro<br/>+ pgvector<br/><b>rds-sg</b>")]
                vpce["🔌 VPC Endpoints<br/>Bedrock · SSM (Interface)<br/>S3 (Gateway)"]
            end
        end
    end

    user -->|HTTPS| vercel
    vercel -->|"HTTP /api/* → ALB"| igw
    igw --> alb
    alb -->|":8000 — only alb-sg allowed"| ec2
    ec2 -->|":5432 — only ec2-sg allowed"| rds
    lam -->|":5432"| rds

    ec2 -.->|private tunnel| vpce
    lam -.->|private tunnel| vpce
    vpce -.-> bedrock
    vpce -.-> s3
    s3 ==>|"S3 event trigger"| lam

    classDef pubStyle fill:#dcfce7,stroke:#16a34a,stroke-width:2px
    classDef privStyle fill:#fee2e2,stroke:#dc2626,stroke-width:2px
    classDef mgdStyle fill:#e0e7ff,stroke:#4f46e5,stroke-width:2px
    classDef netStyle fill:#fef3c7,stroke:#d97706,stroke-width:2px
    class alb pubStyle
    class ec2,lam,rds privStyle
    class bedrock,s3 mgdStyle
    class igw,vpce netStyle
```

### Placement rationale

| Component | Subnet | Why there |
| :--- | :--- | :--- |
| **Application Load Balancer** | 🟩 Public | Must accept traffic from the internet. Requires 2 AZs by design |
| **EC2 (FastAPI API)** | 🟥 Private | No public IP — reachable *only* through the ALB. Admin access via SSM, not SSH |
| **Lambda (ingestion)** | 🟥 Private | Needs to reach RDS, which is private. VPC-attached |
| **RDS PostgreSQL** | 🟥 Private | Holds all data. `Publicly accessible = false`. Zero internet exposure |
| **VPC Endpoints** | 🟥 Private | Give private resources a route to Bedrock/S3/SSM **without** a NAT Gateway |

> 💡 **Cost decision:** the VPC was created with **NAT Gateways: NONE** (~$32/month saved). VPC Endpoints provide the private path to AWS services instead — traffic never leaves the AWS backbone, which is both cheaper *and* more secure.

---

## 2. Security Group Chain — Defense in Depth

Each tier trusts **only** the tier directly in front of it. Rules reference **security group IDs**, not IP addresses — so the chain survives instance replacement and is auto-scaling ready.

```mermaid
flowchart LR
    net["🌐 0.0.0.0/0<br/>Anywhere"]
    albsg["<b>alb-sg</b><br/>─────────<br/>IN: :80 HTTP<br/>FROM 0.0.0.0/0"]
    ec2sg["<b>ec2-sg</b><br/>─────────<br/>IN: :8000 TCP<br/>FROM alb-sg ✅"]
    rdssg["<b>rds-sg</b><br/>─────────<br/>IN: :5432 PG<br/>FROM ec2-sg ✅"]

    net ==>|allowed| albsg
    albsg ==>|allowed| ec2sg
    ec2sg ==>|allowed| rdssg

    net -.->|"❌ BLOCKED"| ec2sg
    net -.->|"❌ BLOCKED"| rdssg
    albsg -.->|"❌ BLOCKED"| rdssg

    classDef pubT fill:#dcfce7,stroke:#16a34a,stroke-width:2px
    classDef appT fill:#fef3c7,stroke:#d97706,stroke-width:2px
    classDef dbT fill:#fee2e2,stroke:#dc2626,stroke-width:2px
    classDef netT fill:#f1f5f9,stroke:#64748b,stroke-width:2px
    class net netT
    class albsg pubT
    class ec2sg appT
    class rdssg dbT
```

**What this buys:** the database cannot be reached from the internet, and it cannot even be reached from the load balancer — only from the application tier. That's the *Principle of Least Privilege* applied at the network layer.

---

## 3. Flow A — Ingestion Pipeline (offline, event-driven)

Runs when the corpus changes. Builds the searchable knowledge base.

```mermaid
flowchart LR
    csv["📄 faq_data_paired.csv<br/>200 question-answer pairs"]
    s3["🪣 S3 Bucket"]
    lam["λ Lambda<br/>ingest.py"]
    chunk["✂️ chunk_text()<br/>600 chars<br/>150 overlap"]
    titan["🧠 Bedrock Titan V2<br/>→ 1024-dim vector"]
    db[("🗄️ RDS + pgvector<br/>support_articles<br/>embeddings")]

    csv -->|upload| s3
    s3 -->|"⚡ S3 event trigger"| lam
    lam --> chunk
    chunk -->|"per chunk"| titan
    titan -->|"text + vector"| db

    classDef step fill:#e0e7ff,stroke:#4f46e5,stroke-width:2px
    classDef store fill:#fee2e2,stroke:#dc2626,stroke-width:2px
    class lam,chunk,titan step
    class db,s3 store
```

**Why serverless here:** ingestion is bursty — it runs when data changes, then stops. Lambda costs nothing between runs.

---

## 4. Flow B — Live Query Pipeline (with guardrails)

Every question passes through **five layers**. Three of them can refuse *before* any model or database call is made.

```mermaid
flowchart TD
    q["❓ User question"]
    cors["1️⃣ CORS middleware<br/>allow cross-origin from Vercel"]
    guard{"2️⃣ Chit-chat guard<br/>regex · zero cost<br/>no DB, no LLM"}
    embed["🧠 Bedrock Titan V2<br/>embed question"]
    search["4️⃣ pgvector search<br/>cosine distance &lt;=&gt;<br/>Top-K = 10, chunk = 600"]
    gate{"3️⃣ Relevance gate<br/>closest distance<br/>&gt; 0.78 ?"}
    nova["5️⃣ Bedrock Nova Micro<br/>grounded prompt<br/>temperature = 0<br/>cite ≤ 3 sources"]
    strip{"Model declined?"}
    ok["✅ answer + sources<br/>confidence: High"]
    refuse["🚫 refusal<br/>confidence: N/A<br/>citations stripped"]

    q --> cors --> guard
    guard -->|"small talk<br/>'hi', 'thanks'"| refuse
    guard -->|"real question"| embed
    embed --> search --> gate
    gate -->|"too far<br/>e.g. 'weather in Paris' 0.89"| refuse
    gate -->|"in scope<br/>e.g. Photoshop 0.68"| nova
    nova --> strip
    strip -->|yes| refuse
    strip -->|no| ok

    classDef guardStyle fill:#fef3c7,stroke:#d97706,stroke-width:2px
    classDef aiStyle fill:#e0e7ff,stroke:#4f46e5,stroke-width:2px
    classDef okStyle fill:#dcfce7,stroke:#16a34a,stroke-width:2px
    classDef noStyle fill:#fee2e2,stroke:#dc2626,stroke-width:2px
    class guard,gate,strip guardStyle
    class embed,nova,search aiStyle
    class ok okStyle
    class refuse noStyle
```

### The three refusal paths — and why they're layered

| Layer | Mechanism | Cost to refuse | Catches |
| :--- | :--- | :--- | :--- |
| **Chit-chat guard** | Regex + keyword tiers | **$0** — no DB, no LLM | `"hi"`, `"thanks"`, `"are you a bot?"` |
| **Relevance gate** | pgvector cosine distance > 0.78 | 1 embed + 1 DB query, **no LLM** | `"What's the weather in Paris?"` |
| **Citation strip** | Server-side regex on the response | Full pipeline | Model declining despite retrieval |

> 🔑 **The design principle:** the relevance gate is **arithmetic, not a prompt instruction**. A prompt is a *request* the model may ignore; a distance threshold means the model **never executes**, so it *cannot* hallucinate a citation for an unrelated question. Safety is enforced in code, not trusted to the model.

---

## 5. The Top-K Safety Cliff

The finding that drove the production configuration. Retrieving **more** context made the system **less** safe.

```mermaid
xychart-beta
    title "Adversarial Block Rate vs Top-K"
    x-axis "Top-K (chunks retrieved)" [5, 10, 15, 20, 30]
    y-axis "Block rate (%)" 0 --> 100
    line [100, 100, 50, 50, 50]
```

| Top-K | Adversarial block rate | Interpretation |
| :---: | :---: | :--- |
| 5 | 100% ✅ | Safe, but thinner context |
| **10** | **100%** ✅ | **Production config — safe *and* complete** |
| 15 | 50% ❌ | Cliff edge — noise rationalised into answers |
| 20–30 | 50% ❌ | No recovery |

Past K=10 the extra chunks are mostly noise, but enough loosely-related text reaches the prompt that the model convinces itself it has grounds to answer. Production is locked at **chunk = 600, Top-K = 10**, which also cut p50 latency ~40% (1.00s → 0.60s).

---

## 6. Request Sequence — End to End

```mermaid
sequenceDiagram
    participant U as 👤 Browser
    participant V as ▲ Vercel
    participant A as ⚖️ ALB
    participant E as 🖥️ EC2 FastAPI
    participant B as 🧠 Bedrock
    participant D as 🗄️ RDS pgvector

    U->>V: POST /api/ {question}
    Note over V: HTTPS → HTTP rewrite proxy<br/>(avoids mixed-content block)
    V->>A: POST / (HTTP :80)
    A->>E: forward → :8000
    Note over E: chit-chat guard (regex, $0)
    E->>B: embed question (Titan V2)
    B-->>E: 1024-dim vector
    E->>D: ORDER BY cosine distance, LIMIT 10
    D-->>E: 10 chunks + cosine distances
    Note over E: relevance gate — distance > 0.78 ?
    E->>B: grounded prompt + context (Nova Micro)
    B-->>E: cited answer
    Note over E: strip citations if declined
    E-->>A: {answer, confidence, sources}
    A-->>V: JSON
    V-->>U: rendered answer + Sources
```

---

## 7. Service Inventory

| Service | Configuration | Role |
| :--- | :--- | :--- |
| **VPC** | 2 AZs · 2 public + 2 private subnets · no NAT Gateway | Network isolation |
| **ALB** | Public subnets · listener :80 → target :8000 · health check accepts 200 + 405 | Public entry point, health checks |
| **EC2** | `t3.micro` · private subnet · IAM: SSM + Bedrock | FastAPI query API |
| **Lambda** | Private subnet · S3 event trigger · IAM: S3 + Bedrock | Ingestion pipeline |
| **RDS** | `db.t4g.micro` · PostgreSQL + pgvector · Single-AZ · not publicly accessible | Text + 1024-dim vectors |
| **S3** | Bucket for CSV, code bundle, benchmark reports | Storage + event source |
| **Bedrock** | Titan Embeddings V2 · Nova Micro · Nova Lite | Embed · generate · evaluate |
| **VPC Endpoints** | Bedrock + SSM (Interface) · S3 (Gateway) | Private AWS access, no NAT |
| **SSM Session Manager** | Via Interface Endpoint | Admin shell — no SSH keys, no public IP |
| **IAM Roles** | `AmazonSSMManagedInstanceCore`, `AmazonBedrockFullAccess`, S3 read | Least-privilege, no static credentials |
| **Vercel** | Static hosting + `/api/*` rewrite to ALB | Frontend + HTTPS termination |

**Total running cost: under $5** — Free-Tier compute and database, no NAT Gateway, and right-sized models (Nova Micro rather than a frontier model for summarising short support snippets).

---

## 8. Design Decisions & Trade-offs

| Decision | Alternative considered | Why this choice |
| :--- | :--- | :--- |
| **pgvector inside RDS** | Pinecone / OpenSearch | One Free-Tier database stores text *and* vectors — no extra service, cost, or system to secure |
| **EC2 for the API** | Lambda behind API Gateway | Avoids cold starts + VPC ENI attach latency on every live query |
| **Lambda for ingestion** | Always-on worker | Ingestion is bursty and event-driven; costs nothing between runs |
| **VPC Endpoints** | NAT Gateway | Saves ~$32/month and keeps traffic on the AWS backbone |
| **ALB for one instance** | Public IP on EC2 | Keeps EC2 private, adds health checks, horizontally scalable from day one |
| **Vercel rewrite proxy** | ACM cert + custom domain on ALB | No domain purchase needed; solves HTTPS→HTTP mixed content immediately |
| **Nova Micro** | Claude / larger frontier model | Right-sized: sub-second latency and minimal cost for short-snippet summarisation |
| **Two-table schema** | Single table | Keeping large text out of the vector table means the similarity scan reads less per row |

### Known limitations (deliberate scope boundaries)

- **Stateless** — no conversation memory; each question is independent
- **Semantic search only** — no BM25/hybrid layer, so exact identifiers (SKUs, error codes) are a weak spot
- **Console-provisioned** — not yet Infrastructure as Code (Terraform/CDK is the next step)
- **Single-AZ RDS** — Free Tier constraint; production would be Multi-AZ
- **Threshold hand-calibrated** — 0.78 was tuned on a small sample and would need re-tuning on a larger corpus

---

*Diagrams use Mermaid and render natively on GitHub. Resource names are illustrative; no live endpoints or credentials appear in this repository.*
