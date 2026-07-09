# TRADER 001 — Regras de trabalho neste projeto

**Bot de trading real (Binance Futures, dinheiro de verdade). Toda correção
precisa de trava de segurança — não só resolver o sintoma, mas tornar a
MESMA classe de bug impossível (ou autodetectável) de acontecer de novo.**

Este arquivo é carregado automaticamente em toda sessão futura neste
projeto. Segue o checklist abaixo em qualquer fix, não só nos "grandes".

---

## Checklist obrigatório em toda correção

### 1. Diagnosticar com números reais, não suposição
- [ ] Reproduzir o problema com dados concretos (endpoint real, log real, cálculo manual) antes de escrever qualquer fix.
- [ ] Se o cálculo envolve dinheiro (sizing, alavancagem, exposição, PnL), validar a fórmula com um exemplo numérico ANTES de aplicar — conferir se as unidades batem (ex.: nocional alavancado vs. banca crua já causou um bug real aqui — ver `_exposure_blocked()`).

### 2. A correção precisa de uma trava, não só do fix
Pergunte: **"Se isso quebrar de novo, alguém vai perceber, ou vai ficar em silêncio por horas?"** Se a resposta for silêncio, a correção está incompleta. Formas de trava, da mais simples à mais robusta:
- [ ] **Try/except com fallback**, nunca `except: pass` mudo. Se uma ação importante (ex.: notificar Telegram) pode falhar depois que a ação principal (ex.: abrir trade real) já aconteceu, o fallback tem que GARANTIR que o usuário saiba — nunca deixar a ação real acontecer "de graça" sem rastro (ver `send_trade_opened` com fallback em `send_alert`).
- [ ] **Reconciliação automática** para qualquer estado guardado em memória que representa "algo está aberto/pendente" (ex.: `_round_trade_ids`). Nunca confie só em "vou lembrar de liberar isso quando fechar" — sincronize periodicamente contra a fonte de verdade (`get_open_trades()`, banco). Um caminho de fechamento esquecido = trava presa para sempre.
- [ ] **Amostra mínima** para qualquer lógica adaptativa/estatística (auto-tune, win-rate, etc.) — com poucos dados, um único evento move o resultado demais. Definir explicitamente o `n` mínimo antes de agir.
- [ ] **Persistência correta** para toda configuração editável (dashboard ou Telegram): seguir o checklist já existente em `main.py` (`_SETTINGS_SYNC_REGISTRY`, comentário ~linha 297) — (1) campo em `BotState.__init__`, (2) em `load_from_db`, (3) em `sync_state_to_globals`, (4) em `save_global_state_to_db`, (5) registrar em `_SETTINGS_SYNC_REGISTRY`. Pular um desses = valor "obedece" na hora mas some no próximo deploy.
- [ ] **Migração de banco** (`ALTER TABLE ... ADD COLUMN`) sempre dentro de `try/except: pass` em `init_db()`, nunca assume que a tabela já tem a coluna nova — bancos existentes (local E Railway) não recriam do zero.

### 3. Tornar o estado interno inspecionável
- [ ] Se a correção introduz um gate/trava/contador que pode BLOQUEAR silenciosamente uma operação (ex.: circuit breaker, exposição, lote de trades), garantir que dá pra checar de fora sem depender do log do Railway — endpoint de debug (`GET /auto/debug_gates` é o modelo) ou pelo menos um campo em `/settings`.
- [ ] Todo bloqueio de sinal deve gerar rastro no shadow book (`_record_shadow`) ou equivalente — nunca só um `print()` que morre no console.

### 4. Nunca confiar que o deploy "deu certo" sem verificar
- [ ] Depois de `git push`, aguardar o Railway trocar de fato (checar `/health` ou um campo novo que só existe no código novo — não confiar em "parece que subiu").
- [ ] Confirmar em produção que a correção teve o efeito esperado com uma consulta real (endpoint, não achismo) antes de reportar como resolvido.
- [ ] Nunca reverter/mexer em posições reais abertas sem confirmação explícita do usuário.

### 5. Escrever o "porquê" no código, não só o "o quê"
- [ ] Comentário no código explicando a causa raiz do bug + por que a trava evita a recorrência (padrão já usado no projeto: `FIX AAAA-MM-DD: ...`). Isso é o que permite a próxima sessão (ou o próximo humano) entender a intenção sem precisar re-investigar do zero.

---

## Onde olhar primeiro (padrões já estabelecidos no projeto)
- `main.py` linha ~288: sistema de 3 camadas de trava de segurança já existente (sync memória↔banco, faixas válidas, integridade estrutural) — usar como modelo, não reinventar um novo padrão paralelo.
- `_record_shadow` / shadow book: registro de sinais bloqueados, usado para auditar se os filtros estão descartando oportunidades boas.
- `GET /auto/debug_gates`: modelo de endpoint de diagnóstico para estado interno que pode travar operação silenciosamente.
- `GET /selftest/settings`: auto-checagem sob demanda das 3 camadas de trava.

## Casos reais encontrados nesta base (para não repetir)
1. Sizing calculava nocional alavancado em vez de margem — banca alocada não batia com o que era executado.
2. Notificação Telegram podia falhar em silêncio DEPOIS do trade real já executado — sem fallback, usuário ficava com posição aberta sem saber.
3. Gate de exposição comparava nocional alavancado com banca crua — travava a 1ª entrada sempre que alavancagem > ~1.5×n.
4. Auto-tune de score sem amostra mínima e sem excluir trades de breakeven — ficava preso no aperto máximo por ruído estatístico, não por performance real.
5. Rastreamento de "vagas do lote" só liberava em 2 de vários caminhos de fechamento possíveis — uma vaga podia ficar presa para sempre e travar toda entrada nova em silêncio.
