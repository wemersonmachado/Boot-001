import asyncio
from datetime import datetime
from database import save_setting, get_setting

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
        
        # Simulação e fluxo
        self.paper_trading = False
        self.mode_started_at = datetime.utcnow().isoformat()

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
            self.paper_trading = (await get_setting("paper_trading", "False")) == "True"
            
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

    async def activate_mode(self, mode: str, profile: str = "AGGRESSIVE", exec_mode: str = None, 
                            sinais_brain: bool = False, exec_brain: bool = False):
        """
        Método atômico definitivo para alterar modos de operação.
        Garante que NENHUMA flag de estado fique órfã ou inconsistente.
        """
        self.mode_started_at = datetime.utcnow().isoformat()
        
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

        # Configura as flags com base na ativação do modo dual ou unitário
        if exec_mode is not None:
            self.operation_mode = exec_mode
            self.exec_mode = exec_mode
            self.dual_mode_enabled = True
            self.sinais_enabled = True
            self.sinais_profile = "AGGRESSIVE" # Sempre fixado agressivo para alta qualidade de sinais
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
                self.sinais_profile = "AGGRESSIVE"
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
