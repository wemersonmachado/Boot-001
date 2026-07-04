"""
Auditoria estática — configuração que muda em memória mas nunca persiste.

Motivo (2026-07-04): esse dia inteiro foi gasto achando, um por um, comandos/
endpoints que mudavam uma variável global (ex.: CURRENT_MODE, GRID_LEVERAGE,
PAPER_TRADING) sem nunca chamar save_global_state_to_db() — o dashboard/
Telegram pareciam obedecer na hora, mas o valor sumia no próximo deploy sem
nenhum aviso. Rodar este script varre TODAS as funções de main.py em busca da
mesma classe de bug, de uma vez, em segundos — em vez de depender de alguém
notar meses depois.

Uso:
    python tools/audit_settings_wiring.py

Saída esperada: "NENHUMA" (0 funções suspeitas). Se aparecer algum nome de
função, ela muda uma variável da lista REGISTRY_VARS sem persistir — adicione
`await save_global_state_to_db()` (ou save_key/save_key_json/activate_mode)
antes do return.

Complementa (não substitui) a checagem em runtime:
  - main.py:_audit_settings_sync()      — compara memória vs. banco ao vivo
  - main.py:job_settings_integrity_watch — roda a cada 10min, alerta no Telegram
  - GET /selftest/settings               — mesma checagem, sob demanda
"""
import ast
import os
import sys

REGISTRY_VARS = {
    "OPERATION_MODE", "SINAIS_ENABLED", "SINAIS_PROFILE", "EXEC_MODE",
    "BANCA_USDT", "EXPOSURE_PCT", "TRADES_PER_SESSION", "DAILY_TARGET_USDT",
    "PAPER_TRADING", "LEVERAGE_OVERRIDE", "GRID_PAIRS", "GRID_PROFIT_TARGET_USDT",
    "GRID_LEVERAGE", "GRID_MAX_CONCURRENT", "SUPERVISED_WATCHLIST",
    "AUTONOMOUS_WATCHLIST", "SINAIS_WATCHLIST", "CURRENT_MODE",
}

# Funções que legitimamente escrevem estas variáveis SEM persistir de novo —
# são elas próprias a fonte de verdade da persistência (chamá-las aqui dentro
# seria recursão/redundância).
EXEMPT_FUNCS = {"sync_state_to_globals", "save_global_state_to_db", "_audit_settings_sync"}

PERSIST_CALL_NAMES = {"save_global_state_to_db", "activate_mode", "save_key", "save_key_json"}


def audit(main_py_path: str) -> list[str]:
    src = open(main_py_path, encoding="utf-8").read()
    tree = ast.parse(src)

    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in EXEMPT_FUNCS:
            continue

        assigns_registry_var = False
        calls_save = False
        for n in ast.walk(node):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id in REGISTRY_VARS:
                        assigns_registry_var = True
            if isinstance(n, ast.Call):
                fname = n.func.id if isinstance(n.func, ast.Name) else (
                    n.func.attr if isinstance(n.func, ast.Attribute) else None
                )
                if fname in PERSIST_CALL_NAMES:
                    calls_save = True

        if assigns_registry_var and not calls_save:
            problems.append(node.name)

    return problems


if __name__ == "__main__":
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    main_py = os.path.join(base, "main.py")
    result = audit(main_py)
    if result:
        print(f"[AUDIT] {len(result)} funcao(oes) suspeita(s) — mudam config sem persistir:")
        for name in result:
            print(f"  - {name}")
        sys.exit(1)
    print("[AUDIT] NENHUMA — todas as funcoes que mudam configuracao registrada persistem corretamente.")
    sys.exit(0)
