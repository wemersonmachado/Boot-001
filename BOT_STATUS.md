# STATUS OPERACIONAL DO BOT — LEITURA OBRIGATÓRIA

> **Instrução para qualquer IA, desenvolvedor ou agente:** antes de analisar,
> editar, testar ou publicar este projeto, leia este arquivo inteiro. Ele é o
> registro de contexto operacional, segurança, versão e alterações conhecidas.

**Última atualização:** 2026-07-15  
**Projeto:** Boot-001  
**Repositório:** `wemersonmachado/Boot-001`

## Versão atual

A versão em revisão está na branch:

```text
feature/exit-statistics-shadow-hardening
commit bcfd242
```

Essa branch foi publicada no GitHub, mas não deve ser considerada produção até
passar por canário e aprovação explícita.

## Versão de produção protegida

A última versão estável de produção é:

```text
commit 10905ef
tag production-stable-2026-07-15
branch backup/production-stable-2026-07-15
```

O backup está publicado no GitHub e não deve ser alterado. Para rollback:

```bash
git checkout backup/production-stable-2026-07-15
```

Não usar `reset --hard` ou apagar a tag sem autorização explícita.

## O que já foi implementado

### Segurança de execução e sizing

- Alavancagem definida no Dashboard/Telegram é respeitada exatamente.
- Override explícito de alavancagem não é reduzido silenciosamente pelo perfil,
  exposição ou volatilidade.
- Autônomo, Pares e Grid usam o mesmo cálculo central de margem.
- Arbitragem de pares exige opt-in explícito.
- Execução real de timeframe abaixo de 5m permanece bloqueada por padrão.
- Altcoins em dinheiro real permanecem desativadas por padrão.
- O endpoint de versão e a identificação Railway usam configuração por env var.
- CORS foi restringido.

### Saídas e gerenciamento de trades — commit `bcfd242`

- PnL durante o ciclo desconta estimativa de taxas de ida e volta.
- O reconciliador da Binance continua sendo a fonte final do PnL realizado e
  pode substituir o valor estimado com taxas/funding reais.
- Break-even e trailing com trava de lucro só são permitidos após pelo menos
  `+1R`.
- O trailing antigo não pode mais mover o stop para lucro antes de `+1R`.
- O stop ATR continua ativo quando o trade possui ATR gravado.
- Stops estruturais continuam sendo gerados pelo `signal_engine` com suporte,
  resistência, ATR e pisos por timeframe.
- Time-stop agora usa limites por timeframe, mais agressivos em 1m/3m/5m.
- `EXIT_RULES_V2_ENABLED=0` desliga as novas regras de saída sem alterar o
  código estável.

### Aprendizado estatístico

- O ajuste histórico de score usa limite inferior de Wilson.
- Amostras pequenas deixam de produzir boosts excessivos.
- A calibração existente continua impedindo auto-tune quando o score está
  invertido ou sem evidência suficiente.

### Shadow Book

- Sinais bloqueados continuam sendo acompanhados sem executar dinheiro real.
- Motivos de bloqueio são normalizados em códigos (`LOW_SCORE`, `RISK_GATE`,
  `REGIME_NEUTRAL`, `PUMP_DUMP`, `DUPLICATE`, `TIMEFRAME_BLOCK`, `OTHER`).
- Foi adicionada migração idempotente da coluna `block_code`.
- `SHADOW_V2_ENABLED=0` desliga o registro aprimorado.

## O que já existia e foi preservado

- Roteamento por engines/regimes já existente no projeto.
- Stops ATR/estruturais e caps de TP por timeframe existentes no
  `signal_engine`.
- Auto-tune de score e memória por ativo/timeframe/hora/dia.
- Kill-switch, sizing centralizado e bloqueios de execução live.
- Fluxo de reconciliação de PnL da Binance.
- Banco SQLite com WAL, índices e migrações idempotentes.

## Flags importantes

```text
EXIT_RULES_V2_ENABLED=1   # novas regras de saída
SHADOW_V2_ENABLED=1       # Shadow Book normalizado
FEE_RATE_BPS=4.0          # estimativa padrão de taxa ida+volta
LIVE_MIN_TIMEFRAME=5m     # mínimo para execução real
LIVE_ALTCOINS_ENABLED=0   # altcoins reais desligadas por padrão
```

As flags devem ser alteradas primeiro em ambiente canário/paper. Nunca mudar
alavancagem, timeframe mínimo ou permissões live sem conferir Dashboard,
Telegram, Railway e código.

## Validações executadas

- `py_compile` de `config.py`, `risk_manager.py`, `database.py`, `main.py`,
  `models.py` e `signal_engine.py`: aprovado.
- `git diff --check`: aprovado.
- Migração do banco e gravação do Shadow Book: aprovada.
- Smoke test do break-even +1R: aprovado.
- Commit e push da branch de revisão: concluídos.

## Pendências conhecidas

1. Invalidação estrutural dinâmica baseada em candles atuais e confirmação
   multi-timeframe ainda precisa ser implementada com baixo custo e testes.
2. O banco ainda não separa permanentemente `gross_pnl_usdt`, `fees_usdt`,
   `funding_usdt` e `net_pnl_usdt`; hoje o ciclo usa estimativa e a Binance
   reconcilia o realizado.
3. Deve ser feito canário em paper/shadow antes de promover a branch para
   `main`/Railway.
4. Criar testes automatizados formais para cada engine, regime, modo e perfil.
5. Monitorar especialmente trades antigos abertos antes das migrações.

## Procedimento obrigatório para futuras alterações

1. Ler este arquivo e `CLAUDE.md`.
2. Confirmar branch, commit, tag estável e estado do Git.
3. Nunca editar a tag ou a branch de backup.
4. Criar branch nova a partir da versão correta.
5. Implementar flags de rollback e migrações idempotentes.
6. Auditar Dashboard, Telegram, Railway e GitHub em conjunto.
7. Rodar compilação, testes, `git diff --check` e smoke tests.
8. Revisar riscos de dinheiro real antes de publicar.
9. Só fazer deploy Railway após aprovação explícita e plano de rollback.
10. Atualizar este documento no mesmo commit da alteração relevante.

## Arquivo local não versionado

`monitor_dual.sh` é um arquivo local não rastreado. Não apagar, adicionar ou
alterar sem solicitação explícita; ele não faz parte do commit da feature.
