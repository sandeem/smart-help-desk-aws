#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# EC2 SETUP SCRIPT — Smart Help Desk API Server
# ─────────────────────────────────────────────────────────────────────────────
# PURPOSE: Installs Python, the required libraries, and starts the FastAPI
#          web server on port 8000 so the Load Balancer can forward traffic to it.
#
# HOW TO USE THIS ON EC2:
#   Option A (User Data): Paste this into the "User Data" box when launching
#                         the EC2 instance. AWS will run it automatically at boot.
#   Option B (SSM): Connect via AWS Systems Manager Session Manager and run:
#                   sudo bash setup_ec2.sh
# ─────────────────────────────────────────────────────────────────────────────

# Step 1: Update the package manager (always do this first on a fresh EC2)
# 'yum' is the package manager for Amazon Linux (like 'apt' on Ubuntu)
sudo yum update -y

# Step 2: Install Python 3 and pip (the Python package installer)
sudo yum install python3 python3-pip -y

# Step 3: Install the Python libraries your app needs
# - boto3:           AWS library (talks to Bedrock, S3, DynamoDB)
# - psycopg2-binary: PostgreSQL/RDS database library
# - fastapi:         The web framework that handles HTTP requests
# - uvicorn:         The web server that RUNS FastAPI (like Apache/Nginx but for Python)
pip3 install boto3 psycopg2-binary fastapi uvicorn

# Step 4: Create the app directory and copy your code there
mkdir -p /home/ec2-user/app

# Step 5: Copy your scripts to the app directory
# (Run these manually after connecting via SSM, or use AWS CodeDeploy in production)
# cp query.py /home/ec2-user/app/
# cp ingest.py /home/ec2-user/app/

# Step 6: Set your database environment variables
# Replace the values below with your actual RDS endpoint and password.
# In production, you would use AWS Secrets Manager instead.
export DB_HOST="YOUR_RDS_ENDPOINT_HERE"    # e.g. helpdesk-db.cq3kwo6sigw5.us-east-1.rds.amazonaws.com
export DB_PASS="password1234"

# Step 7: START THE WEB SERVER
# uvicorn = the server runner
# query:app = "in the query.py file, run the 'app' object"
# --host 0.0.0.0 = listen on ALL network interfaces (required for ALB to reach it)
# --port 8000 = the port the ALB Target Group is configured to forward to
# --reload = (REMOVE in production) auto-restarts when code changes
cd /home/ec2-user/app
uvicorn query:app --host 0.0.0.0 --port 8000

# ─────────────────────────────────────────────────────────────────────────────
# VERIFICATION: After running, test the server is alive by running this in SSM:
#   curl http://localhost:8000/
# You should see: {"detail":"Method Not Allowed"} or similar JSON (means it's working!)
# Then check your ALB → Target Groups → Targets tab — status should show "healthy"
# ─────────────────────────────────────────────────────────────────────────────
