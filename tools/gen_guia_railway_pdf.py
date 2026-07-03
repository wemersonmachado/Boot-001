# -*- coding: utf-8 -*-
"""Gera o PDF definitivo 'Como Desligar e Religar o Trader Bot 001 na Railway'.
Ilustrações esquemáticas (mockups) da interface da Railway com passos numerados.
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Circle, Rectangle
from matplotlib.backends.backend_pdf import PdfPages

# ── Paleta (tema escuro estilo Railway) ──────────────────────────────────────
BG     = "#0D0B14"
CARD   = "#1A1726"
CARD2  = "#241F33"
PURPLE = "#8B5CF6"
GREEN  = "#22C55E"
RED    = "#EF4444"
AMBER  = "#F59E0B"
TXT    = "#ECECF1"
MUT    = "#9A93A8"
LINE   = "#332C45"

plt.rcParams["font.family"] = "DejaVu Sans"

def newpage(pdf):
    fig = plt.figure(figsize=(8.27, 11.69))            # A4 retrato
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, 100); ax.set_ylim(0, 100)
    ax.axis("off"); fig.patch.set_facecolor(BG); ax.set_facecolor(BG)
    return fig, ax

def panel(ax, x, y, w, h, color=CARD, ec=LINE, lw=1.2, rad=1.6):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={rad}",
                                fc=color, ec=ec, lw=lw, zorder=2))

def txt(ax, x, y, s, size=11, color=TXT, weight="normal", ha="left", va="center", style="normal", z=5):
    ax.text(x, y, s, fontsize=size, color=color, weight=weight, ha=ha, va=va,
            family="DejaVu Sans", style=style, zorder=z)

def badge(ax, x, y, n, color=RED, r=1.9):
    ax.add_patch(Circle((x, y), r, fc=color, ec="white", lw=1.4, zorder=8))
    ax.text(x, y, str(n), fontsize=12, color="white", weight="bold", ha="center", va="center", zorder=9)

def arrow(ax, x1, y1, x2, y2, color=AMBER, lw=2.6):
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle="-|>", color=color, lw=lw, shrinkA=0, shrinkB=0), zorder=8)

def pill(ax, x, y, w, h, s, fc, tc="white", size=9.5, weight="bold"):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={h/2}",
                                fc=fc, ec="none", zorder=4))
    txt(ax, x + w/2, y + h/2, s, size=size, color=tc, weight=weight, ha="center")

def tabs(ax, y, active="Deployments"):
    names = ["Deployments", "Variables", "Metrics", "Console", "Settings"]
    xs = 8
    for nm in names:
        w = 1.05 * len(nm) + 6
        col = TXT if nm == active else MUT
        txt(ax, xs, y, nm, size=10.5, color=col, weight="bold" if nm == active else "normal")
        if nm == active:
            ax.plot([xs - 1, xs + w - 7], [y - 2.0, y - 2.0], color=PURPLE, lw=2.6, zorder=6)
        xs += w
    ax.plot([6, 94], [y - 2.0, y - 2.0], color=LINE, lw=1.0, zorder=1)

def header(ax, title, sub=None):
    ax.add_patch(Circle((10, 95.2), 1.5, fc=PURPLE, ec="none", zorder=6))
    txt(ax, 13, 95.2, "Railway · Bot-001", size=10, color=MUT)
    txt(ax, 6, 90, title, size=17, color=TXT, weight="bold")
    if sub:
        txt(ax, 6, 86, sub, size=10.5, color=MUT)
    ax.plot([6, 94], [83.5, 83.5], color=LINE, lw=1.0)

def footer(ax, pg):
    ax.plot([6, 94], [5, 5], color=LINE, lw=0.8)
    txt(ax, 6, 3, "Trader Bot 001 — Guia Railway (ilustrações esquemáticas)", size=8, color=MUT)
    txt(ax, 94, 3, f"pág. {pg}", size=8, color=MUT, ha="right")

# Desenha um "card de deployment" com o menu de 3 pontinhos
def deploy_card(ax, x, y, w=82, h=14, status="ACTIVE", status_col=GREEN, title="Deploy atual", dots_hi=False):
    panel(ax, x, y, w, h, color=CARD)
    ax.add_patch(Circle((x + 5, y + h/2), 2.2, fc=CARD2, ec=LINE, zorder=3))
    pill(ax, x + 9, y + h/2 - 1.6, 13, 3.2, status, status_col if status != "FAILED" else RED)
    txt(ax, x + 25, y + h/2 + 1.2, title, size=10.5, color=TXT, weight="bold")
    txt(ax, x + 25, y + h/2 - 2.2, "via GitHub · push main", size=8.5, color=MUT)
    # botão View logs
    pill(ax, x + w - 26, y + h/2 - 1.7, 14, 3.4, "View logs", CARD2, TXT, size=9, weight="normal")
    # 3 pontinhos
    dotx = x + w - 6
    if dots_hi:
        ax.add_patch(Circle((dotx + 0.5, y + h/2), 3.2, fc="none", ec=AMBER, lw=2.4, zorder=7))
    for i in range(3):
        ax.add_patch(Circle((dotx, y + h/2 + 2 - i*2), 0.42, fc=TXT, ec="none", zorder=6))
    return dotx, y + h/2

# Menu dropdown dos 3 pontinhos
def dots_menu(ax, x, y, highlight=None, ring=None, ih=4.7, w=30):
    items = ["Restart", "Redeploy", "Remove", "View logs"]
    h = ih * len(items) + 2
    panel(ax, x, y - h, w, h, color=CARD2, ec=LINE)
    pos = {}
    for i, it in enumerate(items):
        iy = y - 3 - i * ih
        pos[it] = iy
        col = RED if it == "Remove" else TXT
        if highlight == it:
            panel(ax, x + 1.2, iy - ih/2 + 0.5, w - 2.4, ih - 1.0, color="#3A2D5A", ec=PURPLE, lw=1.6, rad=1.0)
        if ring == it:
            panel(ax, x + 1.2, iy - ih/2 + 0.5, w - 2.4, ih - 1.0, color="none", ec=RED, lw=1.8, rad=1.0)
        txt(ax, x + 4, iy, it, size=10.5, color=col, weight="bold" if highlight == it or ring == it else "normal")
    return w, h, pos

# ════════════════════════════════════════════════════════════════════════════
out = "C:/Users/welli/OneDrive/Desktop/PDF/Guia_Desligar_Religar_Railway.pdf"
with PdfPages(out) as pdf:

    # ── CAPA ──────────────────────────────────────────────────────────────────
    fig, ax = newpage(pdf)
    panel(ax, 8, 40, 84, 30, color=CARD)
    ax.add_patch(Circle((20, 62), 3.2, fc=PURPLE, ec="none"))
    txt(ax, 26, 62, "Railway", size=14, color=TXT, weight="bold")
    txt(ax, 50, 53, "GUIA DEFINITIVO", size=26, color=TXT, weight="bold", ha="center")
    txt(ax, 50, 47.5, "Como Desligar e Religar o Trader Bot 001 na Railway", size=12.5, color=PURPLE, ha="center", weight="bold")
    txt(ax, 50, 43, "Passo a passo, com ilustrações — versão 21/06/2026", size=10, color=MUT, ha="center")
    pill(ax, 33, 30, 34, 5, "3 métodos · verificação · segurança", PURPLE, "white", size=10)
    txt(ax, 50, 20, "As telas são ilustrações esquemáticas para mostrar onde clicar.", size=9, color=MUT, ha="center", style="italic")
    txt(ax, 50, 17, "A interface real da Railway pode variar levemente.", size=9, color=MUT, ha="center", style="italic")
    footer(ax, 1)
    pdf.savefig(fig, facecolor=BG); plt.close(fig)

    # ── VISÃO GERAL ───────────────────────────────────────────────────────────
    fig, ax = newpage(pdf)
    header(ax, "Visão geral — qual método usar", "Três formas de desligar/religar. Comece pela 1.")
    rows = [
        ("1", "RESTART (recomendado)", GREEN,
         "Desliga e religa sozinho, em 1 clique. Use no dia a dia para",
         "reiniciar o bot sem mexer em nada. Mais rápido e seguro."),
        ("2", "REMOVE + REDEPLOY", AMBER,
         "Remove o deploy ativo (desliga de verdade) e depois Redeploy",
         "para religar a MESMA versão. Use se o Restart não resolver."),
        ("3", "SETTINGS · PAUSE", RED,
         "Pausa o serviço inteiro pela aba Settings (Danger Zone).",
         "Use para deixar desligado por bastante tempo / economizar."),
    ]
    yy = 74
    for n, t, c, l1, l2 in rows:
        panel(ax, 8, yy - 12, 84, 13.5, color=CARD)
        badge(ax, 14, yy - 5.2, n, c)
        txt(ax, 20, yy - 2.5, t, size=12.5, color=c, weight="bold")
        txt(ax, 20, yy - 6.3, l1, size=9.6, color=TXT)
        txt(ax, 20, yy - 9.4, l2, size=9.6, color=MUT)
        yy -= 16.5
    panel(ax, 8, 14, 84, 9, color=CARD2, ec=PURPLE, lw=1.4)
    txt(ax, 12, 19.5, "Regra de ouro:", size=10.5, color=PURPLE, weight="bold")
    txt(ax, 12, 16, "Nunca rode o bot no PC e na Railway ao mesmo tempo (mesma conta = entradas duplicadas).", size=9.2, color=TXT)
    footer(ax, 2)
    pdf.savefig(fig, facecolor=BG); plt.close(fig)

    # ── MÉTODO 1 — RESTART ────────────────────────────────────────────────────
    fig, ax = newpage(pdf)
    header(ax, "Método 1 — Restart (desliga + religa)", "Aba Deployments · 1 clique. Recomendado.")
    # Passos (lista limpa, topo)
    steps = [
        (1, "Abra a aba  Deployments  e ache o deploy  Active (verde)."),
        (2, "Clique nos  3 pontinhos ( ··· )  no canto direito do deploy."),
        (3, "No menu, clique em  Restart.  O bot desliga e religa sozinho."),
    ]
    yy = 78
    for n, s in steps:
        badge(ax, 11, yy, n, RED); txt(ax, 16, yy, s, size=10.6, color=TXT)
        yy -= 6.6
    txt(ax, 8, 56, "Como fica na tela:", size=9.5, color=MUT, style="italic")
    # Mockup isolado (banda inferior)
    dotx, doty = deploy_card(ax, 8, 44, h=10, status="ACTIVE", status_col=GREEN, title="Deploy atual (Active)", dots_hi=True)
    arrow(ax, dotx - 1, doty - 1, 40, 38)
    dots_menu(ax, 10, 40, highlight="Restart")
    txt(ax, 42, 36.5, "Restart = ciclo completo", size=9.5, color=GREEN, weight="bold")
    txt(ax, 42, 33, "desligar → religar.", size=9.5, color=MUT)
    panel(ax, 8, 12, 84, 8.5, color=CARD2, ec=GREEN, lw=1.3)
    txt(ax, 12, 16.2, "Em ~10-30s o status volta para  Active  — não precisa fazer mais nada.", size=9.4, color=TXT)
    footer(ax, 3)
    pdf.savefig(fig, facecolor=BG); plt.close(fig)

    # ── MÉTODO 2 — REMOVE + REDEPLOY ─────────────────────────────────────────
    fig, ax = newpage(pdf)
    header(ax, "Método 2 — Remove (desliga) + Redeploy (religa)", "Quando o Restart não resolve.")
    steps = [
        (1, RED,   "DESLIGAR:  Deployments → 3 pontinhos ( ··· )  do deploy Active."),
        (2, RED,   "Clique em  Remove.  O serviço fica  Service offline."),
        (3, GREEN, "RELIGAR:  no mesmo deploy, abra os  3 pontinhos  de novo."),
        (4, GREEN, "Clique em  Redeploy.  Builda e volta para  Active."),
    ]
    yy = 78
    for n, c, s in steps:
        badge(ax, 11, yy, n, c); txt(ax, 16, yy, s, size=10.4, color=TXT)
        yy -= 6.8
    txt(ax, 8, 49, "Como fica na tela (o mesmo menu dos 3 pontinhos):", size=9.5, color=MUT, style="italic")
    # Um único menu mostrando Remove (anel vermelho) e Redeploy (realce)
    w, h, pos = dots_menu(ax, 14, 45, highlight="Redeploy", ring="Remove")
    txt(ax, 14 + w + 4, pos["Remove"], "← desliga", size=10, color=RED, weight="bold")
    txt(ax, 14 + w + 4, pos["Redeploy"], "← religa", size=10, color=GREEN, weight="bold")
    panel(ax, 8, 13, 84, 8.5, color=CARD2, ec=AMBER, lw=1.2)
    txt(ax, 12, 17.2, "Redeploy reconstrói a MESMA versão. O banco (DB) zera no processo.", size=9.3, color=TXT)
    footer(ax, 4)
    pdf.savefig(fig, facecolor=BG); plt.close(fig)

    # ── MÉTODO 3 — SETTINGS / PAUSE ──────────────────────────────────────────
    fig, ax = newpage(pdf)
    header(ax, "Método 3 — Settings · Pausar o serviço", "Para deixar desligado por bastante tempo.")
    tabs(ax, 80, "Settings")
    badge(ax, 9, 74, 1, RED); txt(ax, 13, 74, "Abra a aba  Settings  e desça até  Danger Zone.", size=10, color=TXT)
    # Danger zone panel
    panel(ax, 8, 40, 84, 28, color=CARD, ec=RED, lw=1.4)
    txt(ax, 12, 64, "Danger Zone", size=12, color=RED, weight="bold")
    ax.plot([12, 88], [61, 61], color=LINE, lw=0.8)
    txt(ax, 12, 57, "Pause Service", size=10.5, color=TXT, weight="bold")
    txt(ax, 12, 53.5, "Para o serviço (desliga). Pode religar depois.", size=9, color=MUT)
    pill(ax, 68, 54, 18, 4.6, "Pause", RED); badge(ax, 90, 56.2, 2, RED)
    arrow(ax, 87, 60, 84, 58)
    txt(ax, 12, 47, "Remove Service", size=10.5, color=TXT, weight="bold")
    txt(ax, 12, 43.5, "Remove o serviço do projeto. CUIDADO: não delete o projeto!", size=9, color=AMBER)
    pill(ax, 68, 44, 18, 4.6, "Remove", "#5A1F1F")
    badge(ax, 9, 33, 3, RED)
    txt(ax, 13, 33, "Para RELIGAR: volte em Deployments e clique em Redeploy", size=10, color=TXT)
    txt(ax, 13, 29.5, "(ou faça um novo push no GitHub — a Railway reconstrói sozinha).", size=9.4, color=MUT)
    panel(ax, 8, 14, 84, 10, color=CARD2, ec=AMBER, lw=1.3)
    txt(ax, 12, 20.5, "Atenção", size=10.5, color=AMBER, weight="bold")
    txt(ax, 12, 16.8, "Pausar não apaga suas Variables (chaves). Elas continuam salvas para quando religar.", size=9.2, color=TXT)
    footer(ax, 5)
    pdf.savefig(fig, facecolor=BG); plt.close(fig)

    # ── VERIFICAR QUE RELIGOU ────────────────────────────────────────────────
    fig, ax = newpage(pdf)
    header(ax, "Como confirmar que o bot voltou", "3 checagens rápidas depois de religar.")
    items = [
        ("1", "Status  Active  (verde)", GREEN,
         "Em Deployments, o deploy precisa estar  Active  e aparecer  Service online.",
         "Se ficar  FAILED  (vermelho), o build falhou — abra  View logs."),
        ("2", "Abrir o Dashboard", PURPLE,
         "Acesse a URL do bot no navegador. Deve carregar a tela com saldo e modos.",
         "fulfilling-wisdom-production-61f8.up.railway.app"),
        ("3", "Ver os Logs rodando", AMBER,
         "Deployments → View logs. Você deve ver as varreduras de moedas e mensagens",
         "do bot 'pensando' em tempo real."),
    ]
    yy = 76
    for n, t, c, l1, l2 in items:
        panel(ax, 8, yy - 13, 84, 14.5, color=CARD)
        badge(ax, 14, yy - 6, n, c)
        txt(ax, 20, yy - 3, t, size=12, color=c, weight="bold")
        txt(ax, 20, yy - 7, l1, size=9.3, color=TXT)
        txt(ax, 20, yy - 10.4, l2, size=9.0, color=MUT, style="italic")
        yy -= 17.5
    panel(ax, 8, 12, 84, 9.5, color=CARD2, ec=RED, lw=1.3)
    txt(ax, 12, 18, "Se aparecer FAILED:", size=10.5, color=RED, weight="bold")
    txt(ax, 12, 14.3, "Abra View logs, copie a linha vermelha e me envie — eu identifico a causa exata.", size=9.2, color=TXT)
    footer(ax, 6)
    pdf.savefig(fig, facecolor=BG); plt.close(fig)

    # ── SEGURANÇA + ATUALIZAÇÃO ──────────────────────────────────────────────
    fig, ax = newpage(pdf)
    header(ax, "Segurança e atualização do bot", "Leia antes de operar com a conta REAL.")
    checks = [
        ("Conta REAL protegida", "Ao voltar online, confira o MODO ativo e se está em PAPER ou REAL. Só opera real se você habilitar (ALLOW_REAL_TRADING)."),
        ("DB reseta no deploy", "Cada deploy/redeploy zera o banco da Railway: sessão e kill-switch -20% começam do zero."),
        ("Nunca 2 ao mesmo tempo", "PC + Railway juntos na mesma conta = entradas duplicadas e risco de ban. Só um ligado."),
        ("Variables ficam salvas", "Suas chaves (Binance/Telegram) vivem na aba Variables — não se perdem ao pausar/religar."),
    ]
    yy = 77
    for t, d in checks:
        panel(ax, 8, yy - 9.5, 84, 11, color=CARD)
        ax.add_patch(Circle((14, yy - 4), 2.0, fc=GREEN, ec="none"))
        txt(ax, 14, yy - 4, "✓", size=12, color="white", ha="center", weight="bold")
        txt(ax, 19, yy - 2.2, t, size=11, color=TXT, weight="bold")
        txt(ax, 19, yy - 6.2, d, size=9.0, color=MUT)
        yy -= 13
    panel(ax, 8, 16, 84, 12, color=CARD2, ec=PURPLE, lw=1.4)
    txt(ax, 12, 24.5, "Atualizar o bot ficou fácil (sem upload manual):", size=10.5, color=PURPLE, weight="bold")
    txt(ax, 12, 20.8, "Pasta  Desktop/Trade/Boot-001_repo  →  git add .  →  git commit -m \"...\"  →  git push", size=9.0, color=TXT)
    txt(ax, 12, 17.6, "A Railway detecta o push e reconstrói sozinha (~3-6 min).", size=9.0, color=MUT)
    footer(ax, 7)
    pdf.savefig(fig, facecolor=BG); plt.close(fig)

print("PDF gerado:", out)
