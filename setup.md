# Setup & Commands

## 1. Native Run (Stage A — No Docker)

To set your Hugging Face token (for the remote *Strong* tier) and/or API authorization token, set them in your terminal (or edit `.env`):

```bash
# PowerShell
$env:TRANSCRIPTOR_HF_TOKEN="hf_xxx"
$env:TRANSCRIPTOR_API_TOKEN="my_secret_token"

# Linux / macOS / Git Bash
export TRANSCRIPTOR_HF_TOKEN="hf_xxx"
export TRANSCRIPTOR_API_TOKEN="my_secret_token"
```

Start the app:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .
uvicorn app.main:app
```
*UI: http://127.0.0.1:8000*

---

## 2. Docker Compose (Stage B — Distributed)

Pass the Hugging Face and API tokens into the containers from your host shell:

```bash
# PowerShell
$env:TRANSCRIPTOR_HF_TOKEN="hf_xxx"; $env:TRANSCRIPTOR_API_TOKEN="my_secret_token"; docker compose up --build --scale worker=2

# Linux / macOS / Git Bash
TRANSCRIPTOR_HF_TOKEN="hf_xxx" TRANSCRIPTOR_API_TOKEN="my_secret_token" docker compose up --build --scale worker=2
```
*UI: http://127.0.0.1:8000*
