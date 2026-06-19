# Configuração de deploy da Square Cloud — https://docs.squarecloud.app
# App do tipo "website" (FastAPI/uvicorn). A Square injeta a env var PORT;
# o bot já lê PORT/HOST de config.py (HOST=0.0.0.0).

DISPLAY_NAME=Trader Bot 001
DESCRIPTION=Bot de trading FastAPI + Binance + APScheduler
MAIN=main.py
MEMORY=1024
VERSION=recommended
AUTORESTART=true

# Website: expõe o dashboard. Troque se o subdomínio já estiver em uso.
SUBDOMAIN=trader-bot-001
