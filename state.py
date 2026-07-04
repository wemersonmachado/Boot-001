import asyncio
import json
import os
from datetime import datetime
from database import save_setting, get_setting

# Defaults dos limites de sinais/hora — sobrescreva pelas ENVS nas Variables do
# Railway (SINAIS_MAX_HOUR_PUBLIC / SINAIS_MAX_HOUR_VIP). Motivo (2026-07-04):
# o SQLite do Railway ZERA a cada deploy (sem volume), então o valor salvo pelo
# dashboard voltava ao default 10/30 em todo restart — o usuário reajustava e a
# "nova diretriz" sumia sozinha. Com a env, o SEU padrão sobrevive ao deploy.
_DEF_MAX_HOUR_PUBLIC = os.getenv("SINAIS_MAX_HOUR_PUBLIC", "10")
_DEF_MAX_HOUR_VIP    = os.getenv("SINAIS_MAX_HOUR_VIP", "30")

# ── Estrutura fixa de modos/perfis — NÃO editar em runtime ───────────────────
# activate_mode() só pode TRANSITAR entre estes valores; qualquer string fora
# daqui é rejeitada antes de tocar em qualquer atributo de estado (atômico:
# ou a transição inteira é válida, ou nada muda). Isso trava a estrutura
# definida no código contra corrupção por um chamador que esqueça de validar
# (ex.: /config/approve chegava aqui sem checar o enum antes desta trava).
VALID_OPERATION_MODES = {"AUTONOMOUS", "SUPERVISED", "GRID", "SINAIS"}
VALID_RISK_PROFILES   = {"CONSERVATIVE", "NORMAL", "AGGRESSIVE"}


class BotState:
    def __init__(self):
        # Modo de operação e perfis
        self.operation_mode = "SINAIS"
        self.current_mode = "AGGRESSIVE"
        self.dual_mode_enabled = False
        self.sinais_enabled = True
        self.sinais_profile = "AGGRESSIVE"
        self.exec_mode = "SINAIS"
        
        # Claude Brain
        self.claude_brain_enabled = False
        self.sinais_claude_brain = False
        self.exec_claude_brain = False
        
        # Finanças e limites
        self.banca_usdt = 0.0
        self.exposure_pct = 10.0
        self.trades_per_session = 0
        self.daily_target_usdt = 0.0
        # Alavancagem FIXA definida pelo usuário para trades REAIS (AUTONOMOUS/
        # SUPERVISED/GRID). 0 = automático (a engine calcula por sinal, como
        # sempre fez). N>0 = trava exatamente Nx como base, respeitando ainda
        # o leverage_cap do perfil e as reduções de segurança (exposição/ATR)
        # como TETO — nunca sobe além do que o usuário pediu.
        self.leverage_override = 0

        # Limite de sinais por hora, por canal (0 = sem limite)
        self.sinais_max_hour_public = int(_DEF_MAX_HOUR_PUBLIC)
        self.sinais_max_hour_vip    = int(_DEF_MAX_HOUR_VIP)

        # % de cada nível de detalhe (1-4) do canal PÚBLICO — não afeta o VIP.
        self.public_tier_pct = {1: 65, 2: 20, 3: 10, 4: 5}

        # Simulação e fluxo
        self.paper_trading = False
        self.mode_started_at = datetime.utcnow().isoformat()

        # Grid + watchlists por modo (AUDITORIA 2026-07-04: estes 7 campos
        # nunca persistiam — /settings/grid e /settings/watchlist só mudavam
        # a variável em memória, igual ao bug da alavancagem. Corrigido.)
        self.grid_pairs = [
            "BTCUSDT", "ETHUSDT", "SOLUSDT",
            "HYPEUSDT", "XRPUSDT", "BNBUSDT",
            "SUIUSDT", "AVAXUSDT", "DOTUSDT",
        ]
        self.grid_profit_target_usdt = 0.0
        self.grid_leverage = 10
        self.grid_max_concurrent = 2
        self.supervised_watchlist = []
        self.autonomous_watchlist = []
        self.sinais_watchlist = []

    async def load_from_db(self):
        """Carrega todas as configurações salvas no SQLite para manter persistência pós-reboot."""
        try:
            self.operation_mode = await get_setting("operation_mode", "SINAIS")
            # Perfil de risco: chave canônica "current_mode" (fallback legado "trading_mode")
            self.current_mode = await get_setting("current_mode", await get_setting("trading_mode", "AGGRESSIVE"))
            self.dual_mode_enabled = (await get_setting("dual_mode_enabled", "False")) == "True"
            self.sinais_enabled = (await get_setting("sinais_enabled", "True")) == "True"
            self.sinais_profile = await get_setting("sinais_profile", "AGGRESSIVE")
            self.exec_mode = await get_setting("exec_mode", "SINAIS")
            
            # Brain
            self.claude_brain_enabled = (await get_setting("claude_brain_enabled", "False")) == "True"
            self.sinais_claude_brain = (await get_setting("sinais_brain_enabled", str(self.claude_brain_enabled))) == "True"
            self.exec_claude_brain = (await get_setting("exec_brain_enabled", str(self.claude_brain_enabled))) == "True"
            
            # Finanças
            self.banca_usdt = float(await get_setting("banca_usdt", "0.0"))
            self.exposure_pct = float(await get_setting("exposure_pct", "10.0"))
            self.trades_per_session = int(await get_setting("trades_per_session", "0"))
            self.daily_target_usdt = float(await get_setting("daily_target_usdt", "0.0"))
            self.leverage_override = int(await get_setting("leverage_override", "0"))
            self.sinais_max_hour_public = int(await get_setting("sinais_max_hour_public", _DEF_MAX_HOUR_PUBLIC))
            self.sinais_max_hour_vip = int(await get_setting("sinais_max_hour_vip", _DEF_MAX_HOUR_VIP))
            try:
                _raw_pct = await get_setting("public_tier_pct", json.dumps(self.public_tier_pct))
                self.public_tier_pct = {int(k): int(v) for k, v in json.loads(_raw_pct).items()}
            except Exception:
                pass  # mantém o default já definido em __init__ se o JSON salvo estiver corrompido
            self.paper_trading = (await get_setting("paper_trading", "False")) == "True"

            # Grid + watchlists (json — listas)
            for _attr, _key in (
                ("grid_pairs", "grid_pairs"),
                ("supervised_watchlist", "supervised_watchlist"),
                ("autonomous_watchlist", "autonomous_watchlist"),
                ("sinais_watchlist", "sinais_watchlist"),
            ):
                try:
                    _raw = await get_setting(_key, json.dumps(getattr(self, _attr)))
                    setattr(self, _attr, json.loads(_raw))
                except Exception:
                    pass  # mantém o default do __init__ se o JSON salvo estiver corrompido
            self.grid_profit_target_usdt = float(await get_setting("grid_profit_target_usdt", "0.0"))
            self.grid_leverage = int(await get_setting("grid_leverage", "10"))
            self.grid_max_concurrent = int(await get_setting("grid_max_concurrent", "2"))

            self.mode_started_at = await get_setting("mode_started_at", datetime.utcnow().isoformat())
            print(f"[STATE] Configurações carregadas do banco com sucesso. Modo atual: {self.operation_mode} | Brain: {self.claude_brain_enabled}")
        except Exception as e:
            print(f"[STATE] Erro ao carregar configurações do banco: {e}")

    async def save_key(self, key: str, value):
        """Atualiza a propriedade em memória e persiste no banco de dados SQLite."""
        setattr(self, key, value)
        try:
            await save_setting(key, str(value))
        except Exception as e:
            print(f"[STATE] Erro ao persistir setting {key}={value} no banco: {e}")

    async def save_key_json(self, key: str, value):
        """Como save_key, mas para listas/dicts: guarda o objeto NATIVO em
        memória (não a string) e persiste como JSON — evita o tipo divergir
        entre 'valor em memória' e 'valor recarregado do banco' (o que
        quebraria a trava de integridade de main.py:_audit_settings_sync)."""
        setattr(self, key, value)
        try:
            await save_setting(key, json.dumps(value))
        except Exception as e:
            print(f"[STATE] Erro ao persistir setting json {key}={value} no banco: {e}")

    async def activate_mode(self, mode: str, profile: str = "AGGRESSIVE", exec_mode: str = None,
                            sinais_brain: bool = False, exec_brain: bool = False):
        """
        Método atômico definitivo para alterar modos de operação.
        Garante que NENHUMA flag de estado fique órfã ou inconsistente.

        TRAVA DE SEGURANÇA: valida mode/exec_mode/profile contra a estrutura
        fixa (VALID_OPERATION_MODES/VALID_RISK_PROFILES) ANTES de mutar
        qualquer atributo — o bot pode TRANSITAR entre os valores definidos,
        nunca gravar um valor fora dessa estrutura. Levanta ValueError se
        algum valor for desconhecido; nada no estado é alterado nesse caso.
        """
        # Converte modos pt-BR se vierem de comandos do Telegram
        _alias = {
            "supervisao": "SUPERVISED", "supervisionado": "SUPERVISED",
            "autonomo": "AUTONOMOUS", "automatico": "AUTONOMOUS",
            "grid": "GRID",
            "sinais": "SINAIS", "signal": "SINAIS", "signals": "SINAIS",
        }

        mode = _alias.get(mode.lower(), mode.upper())
        if exec_mode is not None:
            exec_mode = _alias.get(exec_mode.lower(), exec_mode.upper())
        profile_norm = (profile or "").upper()

        if mode not in VALID_OPERATION_MODES:
            raise ValueError(f"Modo invalido: '{mode}'. Use um de {sorted(VALID_OPERATION_MODES)}.")
        if exec_mode is not None and exec_mode not in VALID_OPERATION_MODES:
            raise ValueError(f"Modo de execucao invalido: '{exec_mode}'. Use um de {sorted(VALID_OPERATION_MODES)}.")
        if profile_norm not in VALID_RISK_PROFILES:
            raise ValueError(f"Perfil invalido: '{profile}'. Use um de {sorted(VALID_RISK_PROFILES)}.")
        profile = profile_norm

        # Validação passou — só agora a transição começa a mutar estado.
        self.mode_started_at = datetime.utcnow().isoformat()

        # Configura as flags com base na ativação do modo dual ou unitário
        if exec_mode is not None:
            self.operation_mode = exec_mode
            self.exec_mode = exec_mode
            self.dual_mode_enabled = True
            self.sinais_enabled = True
            self.sinais_profile = profile.upper()
            self.current_mode = profile.upper()
            self.sinais_claude_brain = sinais_brain
            self.exec_claude_brain = exec_brain
        else:
            self.operation_mode = mode
            self.exec_mode = mode
            self.dual_mode_enabled = False
            self.current_mode = profile.upper()
            
            if mode == "SINAIS":
                self.sinais_enabled = True
                self.sinais_profile = profile.upper()
                self.sinais_claude_brain = self.claude_brain_enabled
                self.exec_claude_brain = False
            else:
                self.sinais_enabled = False
                self.sinais_claude_brain = False
                self.exec_claude_brain = self.claude_brain_enabled

        # CORREÇÃO DE SEGURANÇA: NÃO desligar paper_trading ao mudar de modo.
        # (Antes: self.paper_trading=False aqui causava abertura de ordem REAL ao
        # ativar Autônomo durante teste de simulação.) Paper só muda via /settings/paper_trading.

        # Salva o bloco inteiro de estados de uma só vez no banco de dados
        await self.save_key("operation_mode", self.operation_mode)
        await save_setting("current_mode", str(self.current_mode))  # chave canônica do perfil
        await self.save_key("dual_mode_enabled", self.dual_mode_enabled)
        await self.save_key("sinais_enabled", self.sinais_enabled)
        await self.save_key("sinais_profile", self.sinais_profile)
        await self.save_key("exec_mode", self.exec_mode)
        await self.save_key("sinais_brain_enabled", self.sinais_claude_brain)
        await self.save_key("exec_brain_enabled", self.exec_claude_brain)
        await self.save_key("paper_trading", self.paper_trading)
        await self.save_key("mode_started_at", self.mode_started_at)
        
        print(f"[STATE] Transição atômica executada: Modo={self.operation_mode} | Dual={self.dual_mode_enabled} | Sinais={self.sinais_enabled}")

# Instância única global exportada (Singleton)
state = BotState()
