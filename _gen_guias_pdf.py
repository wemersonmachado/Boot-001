# -*- coding: utf-8 -*-
"""Gera 2 guias em PDF simples: atualização do bot e uso do painel Railway."""
import os
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                ListFlowable, ListItem, HRFlowable)

OUT = r"C:\Users\welli\OneDrive\Desktop\PDF"
os.makedirs(OUT, exist_ok=True)

ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Title"], fontSize=20, textColor=colors.HexColor("#1a1a2e"), spaceAfter=4)
SUB = ParagraphStyle("SUB", parent=ss["Normal"], fontSize=10, textColor=colors.HexColor("#666"), spaceAfter=14)
H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontSize=13, textColor=colors.HexColor("#2563eb"), spaceBefore=12, spaceAfter=6)
BODY = ParagraphStyle("BODY", parent=ss["Normal"], fontSize=10.5, leading=15, spaceAfter=6)
NOTE = ParagraphStyle("NOTE", parent=ss["Normal"], fontSize=9.5, leading=14,
                      textColor=colors.HexColor("#7a3b00"), backColor=colors.HexColor("#fff4e0"),
                      borderPadding=6, spaceBefore=6, spaceAfter=6)


def steps(items):
    return ListFlowable([ListItem(Paragraph(t, BODY), leftIndent=6) for t in items],
                        bulletType="1", bulletFontSize=10.5, leftIndent=16)


def bullets(items):
    return ListFlowable([ListItem(Paragraph(t, BODY), leftIndent=6) for t in items],
                        bulletType="bullet", start="•", leftIndent=16)


def hr():
    return HRFlowable(width="100%", thickness=0.7, color=colors.HexColor("#ddd"), spaceBefore=6, spaceAfter=6)


# ───────────────────────── PDF 1 — Atualizações ─────────────────────────
def pdf_atualizacoes():
    path = os.path.join(OUT, "Como_Atualizar_o_Bot_Railway.pdf")
    doc = SimpleDocTemplate(path, pagesize=A4, topMargin=18*mm, bottomMargin=16*mm,
                            leftMargin=18*mm, rightMargin=18*mm,
                            title="Como Atualizar o Trader Bot 001")
    e = []
    e.append(Paragraph("Como Atualizar o Trader Bot 001", H1))
    e.append(Paragraph("Guia rápido — deploy na Railway via GitHub · jun/2026", SUB))
    e.append(hr())

    e.append(Paragraph("Como funciona (visão geral)", H2))
    e.append(Paragraph(
        "O bot roda na <b>Railway</b> (serviço <b>fulfilling-wisdom</b>, região Singapore). "
        "A Railway está conectada ao repositório do GitHub <b>wemersonmachado/Boot-001</b> "
        "(branch <b>main</b>). <b>Todo push nessa branch dispara um redeploy automático</b> "
        "do bot em ~1 a 2 minutos. Você não mexe no servidor — só envia o código novo.", BODY))

    e.append(Paragraph("Passo a passo de uma atualização", H2))
    e.append(steps([
        "<b>Alterar o código</b> do bot (na pasta <i>trader_001</i> no PC, ou pedindo ao Claude).",
        "<b>Enviar a mudança para o GitHub</b> (repo Boot-001, branch main) com <i>git push</i>. "
        "Como o código no repo de deploy fica na raiz, a forma mais segura é pedir ao Claude "
        "“faça o deploy da atualização X” — ele sincroniza e dá o push certo.",
        "<b>A Railway detecta o push</b> e começa o build sozinha (aba Deployments mostra o progresso).",
        "<b>Aguardar ~1–2 min</b> até aparecer “Deployment successful”.",
        "<b>Verificar</b> no dashboard do bot e/ou nos Deploy Logs da Railway.",
    ]))

    e.append(Paragraph("Regras de ouro", H2))
    e.append(bullets([
        "<b>Nunca</b> suba o arquivo <b>.env</b> nem o banco <b>.db</b> ao GitHub (ficam protegidos pelo .gitignore).",
        "<b>Chaves/segredos</b> (Binance, Telegram) só na aba <b>Variables</b> da Railway — não no código.",
        "<b>Região</b> sempre <b>Singapore</b> (ou Europa). Nunca EUA — a Binance bloqueia (erro 451).",
        "Mudou variável na Railway? Ela <b>redeploya sozinha</b> — não precisa push.",
    ]))

    e.append(Paragraph("Se algo der errado (rollback)", H2))
    e.append(steps([
        "Na Railway: serviço → aba <b>Deployments</b>.",
        "Ache um deploy anterior que estava OK.",
        "Clique nos <b>⋮</b> → <b>Redeploy</b> (volta para aquela versão).",
    ]))
    e.append(Paragraph("Atalho: peça ao Claude “volte o bot para o deploy anterior”.", NOTE))

    doc.build(e)
    return path


# ───────────────────────── PDF 2 — Painel Railway ─────────────────────────
def pdf_painel():
    path = os.path.join(OUT, "Como_Usar_Painel_Railway.pdf")
    doc = SimpleDocTemplate(path, pagesize=A4, topMargin=18*mm, bottomMargin=16*mm,
                            leftMargin=18*mm, rightMargin=18*mm,
                            title="Como Usar o Painel da Railway")
    e = []
    e.append(Paragraph("Como Usar o Painel da Railway", H1))
    e.append(Paragraph("Guia rápido do dashboard · Trader Bot 001 · jun/2026", SUB))
    e.append(hr())

    e.append(Paragraph("Hierarquia (não se perca)", H2))
    e.append(bullets([
        "<b>Workspace</b> (sua conta) → <b>Projeto</b> (feisty-miracle) → <b>Serviço</b> (fulfilling-wisdom).",
        "Clique no <b>card do serviço</b> para abrir as abas dele.",
    ]))

    e.append(Paragraph("As abas do serviço — o que faz cada uma", H2))
    e.append(bullets([
        "<b>Deployments</b> — histórico de deploys + <b>logs</b>. É aqui que você vê o bot rodando.",
        "<b>Variables</b> — suas chaves/segredos (.env). Use o <b>Raw Editor</b> para colar/editar tudo.",
        "<b>Metrics</b> — consumo de CPU e memória (RAM).",
        "<b>Console</b> — um terminal dentro do servidor (uso avançado).",
        "<b>Settings</b> — região, domínio público (Networking) e zona de perigo (deletar).",
    ]))

    e.append(Paragraph("Tarefas mais comuns", H2))
    e.append(steps([
        "<b>Ver o bot rodando (logs):</b> Deployments → <b>View logs</b> → aba <b>Deploy Logs</b>.",
        "<b>Reiniciar o bot:</b> Deployments → no deploy ativo, <b>⋮</b> → <b>Restart</b>.",
        "<b>Editar chaves:</b> Variables → <b>Raw Editor</b> → editar → <b>Atualizar variáveis</b> (redeploya só).",
        "<b>Ver/criar o link do dashboard:</b> Settings → <b>Networking</b> → domínio público.",
        "<b>Ver quanto gastou:</b> menu do workspace → <b>Usage</b> (Uso).",
    ]))

    e.append(Paragraph("Endereço do dashboard do BOT", H2))
    e.append(Paragraph(
        "O painel do <b>bot</b> (não confundir com o painel da Railway) fica em:<br/>"
        "<b>https://fulfilling-wisdom-production-61f8.up.railway.app/</b><br/>"
        "É lá que você liga/desliga modos e vê saldo e sinais.", BODY))

    e.append(Paragraph(
        "⚠️ Evite o botão <b>“Agent”</b> (IA da Railway). Ele consome seus créditos — "
        "já gastou ~US$0,62 dos US$5 grátis. Para mexer no projeto, peça ao Claude.", NOTE))

    doc.build(e)
    return path


if __name__ == "__main__":
    p1 = pdf_atualizacoes()
    p2 = pdf_painel()
    print("OK:", p1)
    print("OK:", p2)
