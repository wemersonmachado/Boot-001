# -*- coding: utf-8 -*-
"""Guia ilustrado (setas) — Railway + Controle do Bot. Gera PNGs e monta o PDF."""
import os, tempfile
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Image as RLImage,
                                ListFlowable, ListItem, HRFlowable, PageBreak)

OUT = r"C:\Users\welli\OneDrive\Desktop\PDF"
os.makedirs(OUT, exist_ok=True)
IMGDIR = tempfile.mkdtemp(prefix="guia_img_")

# paleta (tema do dashboard)
BG=(10,10,20); CARD=(20,20,31); BORD=(45,45,62); TXT=(232,232,238); MUT=(150,150,165)
GOLD=(240,185,11); GREEN=(22,199,132); REDA=(255,70,70); BLUE=(59,130,246); PURP=(168,85,247)

def F(sz, bold=False):
    p = "C:/Windows/Fonts/" + ("arialbd.ttf" if bold else "arial.ttf")
    try: return ImageFont.truetype(p, sz)
    except: return ImageFont.load_default()

def rr(d, box, radius, fill=None, outline=None, w=2):
    d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=w)

def tw(d, s, font):
    return d.textlength(s, font=font)

def ctext(d, cx, y, s, font, fill):
    d.text((cx - tw(d,s,font)/2, y), s, font=font, fill=fill)

def arrow(d, p1, p2, color=REDA, w=6):
    import math
    d.line([p1, p2], fill=color, width=w)
    ang = math.atan2(p2[1]-p1[1], p2[0]-p1[0]); L=20
    for a in (ang-0.5, ang+0.5):
        d.line([p2, (p2[0]-L*math.cos(a), p2[1]-L*math.sin(a))], fill=color, width=w)

def callout(d, x, y, s, color=REDA):
    f=F(22, True); pad=10; wdt=tw(d,s,f)
    rr(d,[x,y,x+wdt+2*pad,y+40],8,fill=color)
    d.text((x+pad, y+7), s, font=f, fill=(255,255,255))

def save(img, name):
    p=os.path.join(IMGDIR,name); img.save(p); return p

# ─────────────── PANEL 1: LIGAR / DESLIGAR o bot ───────────────
def panel_modos():
    W,H=1100,560; im=Image.new("RGB",(W,H),BG); d=ImageDraw.Draw(im)
    rr(d,[30,30,W-30,H-30],16,fill=CARD,outline=BORD,w=2)
    d.text((55,55),"Modo de Operação",font=F(30,True),fill=TXT)
    d.text((55,100),"1 MODO POR VEZ — ativar um desliga o anterior",font=F(18),fill=MUT)
    # status badge
    rr(d,[W-300,52,W-55,92],8,fill=(16,40,30),outline=GREEN,w=2)
    d.ellipse([W-288,64,W-272,80],fill=GREEN); d.text((W-262,60),"ATIVO",font=F(20,True),fill=GREEN)
    # 4 botoes
    labels=[("Sinais",False),("Supervisionado",True),("Autônomo",False),("Grid",False)]
    bw=235; gap=18; x0=55; y=160; bh=90
    cx_active=None
    for i,(lb,act) in enumerate(labels):
        x=x0+i*(bw+gap)
        if act:
            rr(d,[x,y,x+bw,y+bh],12,fill=(40,33,0),outline=GOLD,w=3); col=GOLD; cx_active=x+bw/2
        else:
            rr(d,[x,y,x+bw,y+bh],12,fill=(28,28,40),outline=BORD,w=2); col=TXT
        ctext(d,x+bw/2,y+34,lb,F(22,True),col)
    # descricao
    rr(d,[55,270,W-55,320],10,fill=(28,28,40),outline=BORD,w=1)
    d.text((75,283),"Supervisionado — você aprova cada ordem no Telegram.",font=F(20),fill=MUT)
    # setas
    arrow(d,(180,470),(170,255)); callout(d,70,478,"① LIGAR: clique em um modo")
    arrow(d,(cx_active,470),(cx_active,255),color=BLUE)
    callout(d,cx_active-150,478,"② DESLIGAR: clique no modo ACESO",color=BLUE)
    return save(im,"modos.png")

# ─────────────── PANEL 2: Capital / Banca ───────────────
def panel_capital():
    W,H=1100,360; im=Image.new("RGB",(W,H),BG); d=ImageDraw.Draw(im)
    rr(d,[30,30,W-30,H-30],16,fill=CARD,outline=BORD,w=2)
    d.text((55,55),"Capital Alocado",font=F(30,True),fill=TXT)
    vals=["$10","$30","$100","$500","$1k"]; bw=180; gap=15; x0=55; y=120; bh=70
    for i,v in enumerate(vals):
        x=x0+i*(bw+gap); rr(d,[x,y,x+bw,y+bh],10,fill=(28,28,40),outline=BORD,w=2)
        ctext(d,x+bw/2,y+22,v,F(22,True),TXT)
    rr(d,[55,210,W-230,270],10,fill=(20,20,30),outline=BORD,w=2)
    d.text((75,228),"Valor personalizado (USDT)...",font=F(20),fill=MUT)
    rr(d,[W-210,210,W-55,270],10,fill=GOLD); ctext(d,W-132,228,"OK",F(22,True),(20,20,20))
    arrow(d,(300,330),(300,195)); callout(d,60,5,"Defina a banca ANTES de Autônomo/Supervisionado")
    return save(im,"capital.png")

# ─────────────── PANEL 3: Railway abas + logs ───────────────
def panel_railway_logs():
    W,H=1100,520; im=Image.new("RGB",(W,H),BG); d=ImageDraw.Draw(im)
    rr(d,[30,30,W-30,H-30],16,fill=CARD,outline=BORD,w=2)
    d.text((55,55),"fulfilling-wisdom",font=F(28,True),fill=TXT)
    tabs=["Deployments","Variables","Metrics","Console","Settings"]; x=55; y=110
    xs=[]
    for i,t in enumerate(tabs):
        w=tw(d,t,F(20,True))+20; xs.append((x,x+w))
        col=GOLD if i==0 else MUT; d.text((x+10,y),t,font=F(20,True),fill=col)
        if i==0: d.line([(x,y+34),(x+w,y+34)],fill=GOLD,width=3)
        x+=w+18
    # linha do deploy
    rr(d,[55,180,W-55,300],10,fill=(28,28,40),outline=BORD,w=2)
    rr(d,[75,205,160,240],6,fill=(16,40,30),outline=GREEN,w=2); d.text((85,210),"ACTIVE",font=F(18,True),fill=GREEN)
    d.text((180,205),"Deploy do bot — successful",font=F(20),fill=TXT)
    d.text((180,238),"há poucos minutos via GitHub",font=F(16),fill=MUT)
    rr(d,[W-260,205,W-120,245],8,fill=(40,40,55),outline=BORD,w=1); d.text((W-245,212),"View logs",font=F(18,True),fill=TXT)
    d.text((W-95,205),"⋮",font=F(34,True),fill=TXT)
    # setas
    arrow(d,(120,430),(xs[0][0]+30,150)); callout(d,55,438,"① Aba Deployments")
    arrow(d,(W-190,430),(W-190,255)); callout(d,W-470,438,"② View logs = ver o bot rodando")
    arrow(d,(W-70,360),(W-78,255),color=BLUE); callout(d,W-360,366,"⋮ → Restart (reiniciar)",color=BLUE)
    return save(im,"rw_logs.png")

# ─────────────── PANEL 4: Railway variáveis ───────────────
def panel_railway_vars():
    W,H=1100,460; im=Image.new("RGB",(W,H),BG); d=ImageDraw.Draw(im)
    rr(d,[30,30,W-30,H-30],16,fill=CARD,outline=BORD,w=2)
    tabs=["Deployments","Variables","Metrics","Settings"]; x=55; y=70; xs=[]
    for i,t in enumerate(tabs):
        w=tw(d,t,F(20,True))+20; xs.append(x)
        col=GOLD if t=="Variables" else MUT; d.text((x+10,y),t,font=F(20,True),fill=col)
        if t=="Variables": d.line([(x,y+34),(x+w,y+34)],fill=GOLD,width=3)
        x+=w+18
    rr(d,[W-260,60,W-130,100],8,fill=(40,33,0),outline=GOLD,w=2); d.text((W-245,68),"Raw Editor",font=F(18,True),fill=GOLD)
    rows=["BINANCE_API_KEY      *******","BINANCE_SECRET_KEY   *******","TELEGRAM_TOKEN       *******","BINANCE_TESTNET      false"]
    yy=150
    for r in rows:
        rr(d,[55,yy,W-55,yy+50],8,fill=(28,28,40),outline=BORD,w=1); d.text((75,yy+13),r,font=F(20),fill=TXT); yy+=62
    arrow(d,(W-195,420),(W-195,105)); callout(d,W-560,426,"Raw Editor → colar chaves → Atualizar")
    return save(im,"rw_vars.png")

# ─────────────── PANEL 5: Railway uso/gasto ───────────────
def panel_railway_usage():
    W,H=1100,340; im=Image.new("RGB",(W,H),BG); d=ImageDraw.Draw(im)
    rr(d,[30,30,W-30,H-30],16,fill=CARD,outline=BORD,w=2)
    d.text((55,55),"Usage  (menu do workspace)",font=F(28,True),fill=TXT)
    rr(d,[55,120,360,250],12,fill=(28,28,40),outline=BORD,w=2)
    d.text((75,140),"Usado no ciclo",font=F(18),fill=MUT); d.text((75,175),"$0,67",font=F(40,True),fill=GOLD)
    rr(d,[390,120,695,250],12,fill=(28,28,40),outline=BORD,w=2)
    d.text((410,140),"Crédito grátis",font=F(18),fill=MUT); d.text((410,175),"$5,00",font=F(40,True),fill=GREEN)
    rr(d,[725,120,W-55,250],12,fill=(16,40,30),outline=GREEN,w=2)
    d.text((745,140),"A pagar agora",font=F(18),fill=MUT); d.text((745,175),"$0,00",font=F(40,True),fill=GREEN)
    arrow(d,(200,300),(200,255)); callout(d,55,5,"Workspace → Usage: aqui você vê o gasto")
    return save(im,"rw_usage.png")

imgs = {
    "modos":panel_modos(),"capital":panel_capital(),"logs":panel_railway_logs(),
    "vars":panel_railway_vars(),"usage":panel_railway_usage(),
}

# ─────────────── MONTA O PDF ───────────────
ss=getSampleStyleSheet()
H1=ParagraphStyle("H1",parent=ss["Title"],fontSize=21,textColor=colors.HexColor("#1a1a2e"),spaceAfter=2)
SUB=ParagraphStyle("SUB",parent=ss["Normal"],fontSize=10,textColor=colors.HexColor("#666"),spaceAfter=10)
H2=ParagraphStyle("H2",parent=ss["Heading2"],fontSize=14,textColor=colors.HexColor("#2563eb"),spaceBefore=10,spaceAfter=5)
BODY=ParagraphStyle("BODY",parent=ss["Normal"],fontSize=10.5,leading=15,spaceAfter=5)
NOTE=ParagraphStyle("NOTE",parent=ss["Normal"],fontSize=9.5,leading=14,textColor=colors.HexColor("#7a3b00"),
                    backColor=colors.HexColor("#fff4e0"),borderPadding=6,spaceBefore=4,spaceAfter=8)

def steps(items,bt="1"):
    return ListFlowable([ListItem(Paragraph(t,BODY),leftIndent=6) for t in items],
                        bulletType=bt,start="•" if bt=="bullet" else None,leftIndent=16,bulletFontSize=10.5)

def img(key,wmm=170):
    ip=imgs[key]; iw,ih=Image.open(ip).size; w=wmm*mm; h=w*ih/iw
    return RLImage(ip,width=w,height=h)

path=os.path.join(OUT,"Guia_Railway_e_Controle_do_Bot.pdf")
doc=SimpleDocTemplate(path,pagesize=A4,topMargin=16*mm,bottomMargin=14*mm,leftMargin=16*mm,rightMargin=16*mm,
                      title="Guia Railway e Controle do Bot")
e=[]
e.append(Paragraph("Guia Ilustrado — Railway &amp; Controle do Bot",H1))
e.append(Paragraph("Trader Bot 001 · passo a passo com setas · jun/2026",SUB))
e.append(HRFlowable(width="100%",thickness=0.7,color=colors.HexColor("#ddd"),spaceAfter=6))

e.append(Paragraph("Parte 1 — Como LIGAR e DESLIGAR o bot (dashboard do bot)",H2))
e.append(Paragraph("Dashboard do bot: <b>https://fulfilling-wisdom-production-61f8.up.railway.app/</b>",BODY))
e.append(img("modos"))
e.append(steps([
    "<b>LIGAR:</b> clique em um dos 4 modos. Ativar um <b>desliga</b> o anterior (1 modo por vez).",
    "<b>DESLIGAR:</b> clique no modo que está <b>aceso</b> (dourado) — ele apaga e o status volta para AGUARDANDO.",
]))
e.append(Paragraph("O que cada modo faz", BODY))
e.append(steps([
    "<b>Sinais</b> — só alertas no Telegram, NÃO abre ordens (mais seguro).",
    "<b>Supervisionado</b> — manda cada sinal com botões Aprovar/Rejeitar.",
    "<b>Autônomo</b> — abre e fecha ordens REAIS sozinho.",
    "<b>Grid</b> — grid trading automático.",
],bt="bullet"))

e.append(Spacer(1,6))
e.append(Paragraph("Defina o capital antes de operar (Autônomo/Supervisionado)",H2))
e.append(img("capital",150))

e.append(PageBreak())
e.append(Paragraph("Parte 2 — Painel da RAILWAY (onde o bot está hospedado)",H2))
e.append(Paragraph("Caminho: Workspace &#8594; Projeto <b>feisty-miracle</b> &#8594; Serviço <b>fulfilling-wisdom</b>.",BODY))

e.append(Paragraph("2.1 Ver o bot rodando e reiniciar",H2))
e.append(img("logs"))
e.append(steps([
    "Abra a aba <b>Deployments</b>.",
    "Clique em <b>View logs</b> &#8594; aba <b>Deploy Logs</b> para ver o bot ao vivo.",
    "Para reiniciar: no deploy ativo, <b>⋮</b> &#8594; <b>Restart</b>.",
]))

e.append(Paragraph("2.2 Editar chaves / variáveis (.env)",H2))
e.append(img("vars"))
e.append(steps([
    "Aba <b>Variables</b> &#8594; botão <b>Raw Editor</b>.",
    "Cole/edite no formato CHAVE=valor (uma por linha) &#8594; <b>Atualizar variáveis</b>.",
    "Ao salvar, a Railway redeploya sozinha (não precisa push).",
]))
e.append(Paragraph("⚠️ NUNCA use o botão “Agent” da Railway — consome seus créditos. Para mudanças, peça ao Claude.",NOTE))

e.append(Paragraph("2.3 Quanto você já gastou",H2))
e.append(img("usage",150))
e.append(steps([
    "Menu do <b>workspace</b> &#8594; <b>Usage</b>.",
    "“A pagar agora” mostra o valor real (hoje: US$ 0,00 — dentro do crédito grátis).",
]))

doc.build(e)
print("OK:", path)
