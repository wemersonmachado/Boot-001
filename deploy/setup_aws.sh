#!/usr/bin/env bash
# ============================================================================
#  Trader Bot 001 — instalação na AWS EC2 (Ubuntu 22.04)
#  Uso:  bash setup_aws.sh
#  Rode DENTRO da pasta /home/ubuntu/trader_001 (onde está o main.py).
# ============================================================================
set -e

echo "==> [1/5] Teste de geo-block da Binance (Futures) ..."
CODE=$(curl -s -o /dev/null -w "%{http_code}" https://fapi.binance.com/fapi/v1/ping || echo "000")
echo "    HTTP $CODE"
if [ "$CODE" != "200" ]; then
  echo "    ❌ Binance retornou $CODE (451/403 = datacenter bloqueado)."
  echo "    NÃO continue: esse IP não fala com a Binance. Tente outra região/instância."
  exit 1
fi
echo "    ✅ Binance acessível deste IP."

echo "==> [2/5] Pacotes do sistema ..."
sudo apt update -y
sudo apt install -y python3-pip python3-venv git

echo "==> [3/5] Ambiente virtual + dependências ..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "==> [4/5] Checando .env ..."
if [ ! -f .env ]; then
  echo "    ⚠️  .env NÃO encontrado. Copiando do exemplo — PREENCHA antes de iniciar:"
  cp .env.example .env
  echo "    Edite com:  nano .env"
fi

echo "==> [5/5] Instalando serviço systemd (24/7, auto-restart) ..."
sudo cp deploy/trader.service /etc/systemd/system/trader.service
sudo systemctl daemon-reload
sudo systemctl enable trader.service

echo ""
echo "============================================================"
echo " PRONTO. Próximos passos:"
echo "   1) nano .env            # cole suas chaves e salve"
echo "   2) sudo systemctl start trader.service"
echo "   3) sudo systemctl status trader.service   # ver se subiu"
echo "   4) journalctl -u trader.service -f        # logs ao vivo"
echo "============================================================"
