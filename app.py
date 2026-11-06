"""Plataforma de Inteligência de Conteúdo — versão 1.
Execute:  streamlit run app.py
Coleta: importação de CSV (sem scraping). Histórico preservado em SQLite (inteligencia.db).
Interação = curtidas + comentários. Campos vazios ficam como 'indisponível'."""
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import unicodedata
import urllib.request
from html import escape
from contextlib import closing
from datetime import datetime

import pandas as pd
import streamlit as st

DB = "inteligencia.db"
FORMATOS = {"reel": "Reels", "reels": "Reels", "video": "Reels", "vídeo": "Reels",
            "carrossel": "Carrossel", "carousel": "Carrossel",
            "imagem": "Imagem", "image": "Imagem", "foto": "Imagem"}
COLUNAS_CSV = ["usuario", "link", "publicado_em", "formato", "tema", "legenda",
               "curtidas", "comentarios", "compartilhamentos", "visualizacoes"]
DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
PAGINAS = ["Painel", "Perfis", "Importar publicações", "Análise por perfil",
           "Comparação", "Tendências", "Audiência e comentários", "Laboratório de ideias", "Banco de inteligência"]


# ---------- banco de dados ----------
def agora():
    return datetime.now().isoformat(timespec="minutes")


def q(sql, params=()):
    with closing(sqlite3.connect(DB)) as c:
        return pd.read_sql_query(sql, c, params=params)


def execute(sql, params=()):
    with closing(sqlite3.connect(DB)) as c, c:
        c.execute(sql, params)


def init_db():
    with closing(sqlite3.connect(DB)) as c, c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS perfis (
            id INTEGER PRIMARY KEY, usuario TEXT UNIQUE NOT NULL, url TEXT,
            categoria TEXT, nicho TEXT, posicionamento TEXT, criado_em TEXT);
        CREATE TABLE IF NOT EXISTS snapshots_perfil (
            id INTEGER PRIMARY KEY, perfil_id INTEGER NOT NULL, coletado_em TEXT NOT NULL,
            biografia TEXT, seguidores INTEGER, seguindo INTEGER, publicacoes INTEGER);
        CREATE TABLE IF NOT EXISTS publicacoes (
            id INTEGER PRIMARY KEY, perfil_id INTEGER NOT NULL, link TEXT NOT NULL,
            publicado_em TEXT, formato TEXT, tema TEXT, legenda TEXT, hashtags TEXT,
            curtidas REAL, comentarios REAL, compartilhamentos REAL, visualizacoes REAL,
            coletado_em TEXT NOT NULL, fonte TEXT);
        CREATE TABLE IF NOT EXISTS comentarios (
            id INTEGER PRIMARY KEY, link_publicacao TEXT NOT NULL, texto TEXT NOT NULL,
            autor_anonimo TEXT, curtidas REAL, publicado_em TEXT,
            coletado_em TEXT NOT NULL, fonte TEXT);
        CREATE TABLE IF NOT EXISTS ideias (
            id INTEGER PRIMARY KEY, gerado_em TEXT NOT NULL, tema TEXT, tipo TEXT, problema TEXT,
            justificativa TEXT, formato TEXT, abordagem TEXT, titulo TEXT, gancho TEXT,
            desenvolvimento TEXT, origem TEXT);
        """)
        cols = [r[1] for r in c.execute("PRAGMA table_info(ideias)")]
        if "revisao_de" not in cols:  # bancos criados antes desta versão
            c.execute("ALTER TABLE ideias ADD COLUMN revisao_de INTEGER")


# ---------- utilitários ----------
def num(v):
    try:
        x = float(v)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


def norm_usuario(s):
    s = str(s).strip().rstrip("/")
    if "instagram.com/" in s:
        s = s.split("instagram.com/")[1].split("/")[0].split("?")[0]
    return s.lstrip("@").lower()


def norm_formato(v):
    v = str(v).strip()
    return FORMATOS.get(v.lower(), v or None)


def mostrar(x, casas=0):
    return "indisponível" if pd.isna(x) else f"{x:,.{casas}f}"


def txt(v):
    return "indisponível" if pd.isna(v) or not str(v).strip() else str(v)


def garantir_perfil(usuario):
    execute("INSERT OR IGNORE INTO perfis (usuario, url, criado_em) VALUES (?,?,?)",
            (usuario, f"https://www.instagram.com/{usuario}/", agora()))
    return int(q("SELECT id FROM perfis WHERE usuario = ?", (usuario,))["id"].iloc[0])


# ---------- gravação ----------
def salvar_perfil(ref, categoria, nicho, posicionamento, bio, seguidores, seguindo, publicacoes):
    usuario = norm_usuario(ref)
    pid = garantir_perfil(usuario)
    execute("UPDATE perfis SET categoria=?, nicho=?, posicionamento=? WHERE id=?",
            (categoria or None, nicho or None, posicionamento or None, pid))
    # Cada salvamento gera um novo snapshot: o histórico de seguidores é preservado.
    execute("""INSERT INTO snapshots_perfil (perfil_id, coletado_em, biografia, seguidores, seguindo, publicacoes)
               VALUES (?,?,?,?,?,?)""",
            (pid, agora(), bio or None, num(seguidores), num(seguindo), num(publicacoes)))
    return usuario


def importar(df, fonte):
    ok, erros = 0, []
    for i, r in df.iterrows():
        usuario, link = norm_usuario(r.get("usuario", "")), str(r.get("link", "")).strip()
        if not usuario or not link:
            erros.append(f"Linha {i + 2}: 'usuario' e 'link' são obrigatórios.")
            continue
        data = pd.to_datetime(str(r.get("publicado_em", "")), dayfirst=True, errors="coerce")
        legenda = str(r.get("legenda", "")).strip() or None
        execute("""INSERT INTO publicacoes (perfil_id, link, publicado_em, formato, tema, legenda, hashtags,
                   curtidas, comentarios, compartilhamentos, visualizacoes, coletado_em, fonte)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (garantir_perfil(usuario), link, None if pd.isna(data) else data.isoformat(),
                 norm_formato(r.get("formato", "")), str(r.get("tema", "")).strip() or None, legenda,
                 " ".join(re.findall(r"#\w+", legenda or "")) or None,
                 num(r.get("curtidas")), num(r.get("comentarios")), num(r.get("compartilhamentos")),
                 num(r.get("visualizacoes")), agora(), fonte))
        ok += 1
    return ok, erros


# ---------- comentários (módulo 3) ----------
# Classificação por palavras-chave: triagem heurística, sujeita a erros e a revisão humana.
CATEGORIAS = {
    "dúvida": r"\?|\bcomo\b|\bqual\b|\bquais\b|\bonde\b|\bquando\b|alguem sabe|pode explicar",
    "elogio": r"parabens|\bamei\b|\badorei\b|\blindo\b|\blinda\b|excelente|perfeito|incrivel|maravilhos|sensacional|\btop\b",
    "crítica": r"\bruim\b|pessimo|horrivel|absurdo|vergonha|decepcion|mentira|nojent|\bodeio\b",
    "concordância": r"\bconcordo\b|\bexato\b|isso mesmo|\bverdade\b",
    "discordância": r"\bdiscordo\b|nao concordo|nao e bem assim|\berrado\b",
    "humor": r"k{3,}|(?:ha){2,}|(?:rs){2,}|rss+|😂|🤣|😅|😆",
    "pedido/interesse": r"\bquero\b|\bqueria\b|\bgostaria\b|\btutorial\b|parte 2|\bcontinua\b|faz(?:e)? (?:um|uma)\b",
}
STOP = set("""para pelo pela pelos pelas mais muito isso esse essa este esta esses essas como quando
onde qual quais mesmo mesma ainda depois antes sempre nunca tudo todo toda todos todas voce voces
ficou ficar sera seria estou estao tambem porque entao aqui ali meu minha seu sua nosso nossa dela dele
""".split())


def norm_txt(s):
    s = unicodedata.normalize("NFKD", str(s).lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def norm_link(s):
    return str(s).strip().split("?")[0].rstrip("/").lower()


def anonimizar(t):
    t = re.sub(r"https?://\S+", "[link]", t)
    t = re.sub(r"@\w+", "@[perfil]", t)
    return t if len(t) <= 140 else t[:140] + "…"


def importar_comentarios(df, fonte):
    ok, erros = 0, []
    publicacoes = {norm_link(l): l for l in q("SELECT DISTINCT link FROM publicacoes")["link"]}
    for i, r in df.iterrows():
        link = publicacoes.get(norm_link(r.get("link", "")))
        texto = str(r.get("texto", "")).strip()
        if not link or not texto:
            erros.append(f"Linha {i + 2}: publicação não encontrada (importe as publicações antes) ou 'texto' vazio.")
            continue
        autor = str(r.get("autor", "")).strip().lower()
        data = pd.to_datetime(str(r.get("publicado_em", "")), dayfirst=True, errors="coerce")
        execute("""INSERT INTO comentarios (link_publicacao, texto, autor_anonimo, curtidas,
                   publicado_em, coletado_em, fonte) VALUES (?,?,?,?,?,?,?)""",
                (link, texto, hashlib.sha256(autor.encode()).hexdigest()[:12] if autor else None,
                 num(r.get("curtidas")), None if pd.isna(data) else data.isoformat(), agora(), fonte))
        ok += 1
    return ok, erros


def carregar_comentarios():
    c = q("""SELECT c.link_publicacao, pf.usuario, c.texto, c.autor_anonimo, c.curtidas, c.coletado_em
             FROM comentarios c JOIN (SELECT DISTINCT link, perfil_id FROM publicacoes) p
             ON p.link = c.link_publicacao JOIN perfis pf ON pf.id = p.perfil_id""")
    c["curtidas"] = pd.to_numeric(c["curtidas"], errors="coerce")
    return c.sort_values("coletado_em").drop_duplicates(
        ["link_publicacao", "texto", "autor_anonimo"], keep="last").reset_index(drop=True)


def classificar(df):
    n = df.texto.map(norm_txt)
    return pd.DataFrame({cat: n.str.contains(pat, regex=True) for cat, pat in CATEGORIAS.items()},
                        index=df.index)


def termos(df, n=15):
    palavras = [w for t in df.texto.map(norm_txt) for w in re.findall(r"[a-z]{4,}", t) if w not in STOP]
    return pd.Series(palavras, dtype=object).value_counts().head(n) if palavras else pd.Series(dtype=int)


# ---------- consultas e análises ----------
def perfis_atuais():
    return q("""SELECT pf.usuario, pf.categoria, pf.nicho, pf.posicionamento, s.biografia,
                s.seguidores, s.seguindo, s.publicacoes, s.coletado_em AS ultima_coleta
                FROM perfis pf LEFT JOIN snapshots_perfil s ON s.id = (
                  SELECT id FROM snapshots_perfil WHERE perfil_id = pf.id
                  ORDER BY coletado_em DESC, id DESC LIMIT 1)
                ORDER BY pf.usuario""")


def carregar(ultimos=True):
    df = q("""SELECT pf.usuario, p.link, p.publicado_em, p.formato, p.tema, p.legenda, p.hashtags,
              p.curtidas, p.comentarios, p.compartilhamentos, p.visualizacoes, p.coletado_em
              FROM publicacoes p JOIN perfis pf ON pf.id = p.perfil_id""")
    df["publicado_em"] = pd.to_datetime(df["publicado_em"], errors="coerce")
    for c in ["curtidas", "comentarios", "compartilhamentos", "visualizacoes"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # O banco guarda todas as coletas; a análise usa a versão mais recente de cada publicação.
    df = df.sort_values("coletado_em").drop_duplicates(["usuario", "link"], keep="last")
    df["interacao"] = df[["curtidas", "comentarios"]].sum(axis=1, min_count=1)
    if ultimos:  # 20 publicações mais recentes por perfil
        df = df.sort_values("publicado_em", na_position="first").groupby("usuario", group_keys=False).tail(20)
    return df.reset_index(drop=True)


def resumo_perfis(df):
    g = df.groupby("usuario")
    r = pd.DataFrame({
        "publicacoes": g.size(),
        "media_curtidas": g.curtidas.mean(),
        "mediana_curtidas": g.curtidas.median(),
        "media_comentarios": g.comentarios.mean(),
        "mediana_comentarios": g.comentarios.median(),
        "media_interacao": g.interacao.mean(),
        "mediana_interacao": g.interacao.median(),
        "compartilhamentos_total": g.compartilhamentos.sum(min_count=1),
        "primeira": g.publicado_em.min(),
        "ultima": g.publicado_em.max(),
    })
    semanas = (r["ultima"] - r["primeira"]).dt.days / 7
    r["posts_por_semana"] = (r["publicacoes"] / semanas).where(semanas >= 1)
    seg = pd.to_numeric(perfis_atuais().set_index("usuario")["seguidores"], errors="coerce")
    r["seguidores"] = seg.reindex(r.index)
    r["taxa_por_seguidor_%"] = (r["media_interacao"] / r["seguidores"] * 100).where(r["seguidores"] > 0)
    return r.drop(columns=["primeira", "ultima"]).round(2)


def por_formato(df):
    return df.assign(formato=df.formato.fillna("não informado")).groupby("formato").agg(
        publicacoes=("link", "count"), media_interacao=("interacao", "mean"),
        mediana_interacao=("interacao", "median")).round(1).sort_values("media_interacao", ascending=False)


def por_tema(df):
    tema = df.tema.fillna("sem tema")
    t = df.assign(tema=tema).groupby("tema").agg(
        publicacoes=("link", "count"), perfis=("usuario", "nunique"),
        media_interacao=("interacao", "mean"), mediana_interacao=("interacao", "median"),
        media_compartilhamentos=("compartilhamentos", "mean"))
    datas = df.publicado_em.dropna().sort_values()
    if len(datas) >= 2:  # comparação entre a primeira e a segunda metade do período
        meio = datas.iloc[len(datas) // 2]
        t["antes"] = tema[df.publicado_em < meio].value_counts().reindex(t.index).fillna(0).astype(int)
        t["depois"] = tema[df.publicado_em >= meio].value_counts().reindex(t.index).fillna(0).astype(int)
    t["amostra"] = t.publicacoes.map(lambda n: "fraca (<3)" if n < 3 else "ok")
    return t.sort_values("publicacoes", ascending=False).round(1)


def por_dia(df):
    d = df.dropna(subset=["publicado_em"])
    dia = d.publicado_em.dt.dayofweek.map(lambda i: DIAS[i])
    return d.groupby(dia).agg(publicacoes=("link", "count"), mediana_interacao=("interacao", "median")) \
        .reindex(DIAS).dropna(how="all")


# ---------- laboratório de ideias (módulo 7) ----------
# Regras transparentes sobre os dados. Os campos criativos podem ser redigidos por IA, se configurada.
MODELO_IA = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
ABORDAGEM = {
    "dúvida": "Conteúdo de resposta: responder às dúvidas mais recorrentes com exemplos concretos.",
    "pedido/interesse": "Série ou continuação: convidar a audiência a escolher o próximo tópico.",
    "crítica": "Apresentar o tema com dados e fontes verificáveis e abrir espaço para contraponto explícito.",
    "discordância": "Apresentar o tema com dados e fontes verificáveis e abrir espaço para contraponto explícito.",
    "elogio": "Relato ou bastidores que aprofundem a experiência que a audiência já valoriza.",
    "concordância": "Relato ou bastidores que aprofundem a experiência que a audiência já valoriza.",
    "humor": "Formato leve e bem-humorado, sem ridicularizar grupos.",
}


def recomendar_formato(base, tema):
    d = base[base.tema.fillna("sem tema") == tema]
    fm = d.assign(formato=d.formato.fillna("não informado")).groupby("formato").agg(
        n=("link", "count"), mediana=("interacao", "median"))
    fm = fm[(fm.n >= 2) & fm.mediana.notna()]
    if not fm.empty:
        f = fm.mediana.idxmax()
        return f"{f}: mediana de interação {fm.loc[f, 'mediana']:,.0f} em {int(fm.loc[f, 'n'])} publicações do tema"
    f = por_formato(base).index[0]
    return f"{f} (melhor formato da amostra geral; o tema não tem publicações suficientes por formato)"


def gerar_oportunidades(base, com):
    if base.empty:
        return []
    geral = base.interacao.median()
    total_perfis = base.usuario.nunique()
    com = com.assign(tema=com.link_publicacao.map(dict(zip(base.link, base.tema.fillna("sem tema")))))
    t = por_tema(base)
    ops = []
    for tema, row in t.iterrows():
        n, perfis = int(row.publicacoes), int(row.perfis)
        c_t = com[com.tema == tema]
        fl = classificar(c_t) if len(c_t) else None
        interesse = float((fl["dúvida"] | fl["pedido/interesse"]).mean()) if len(c_t) else 0.0
        dominante = fl.sum().idxmax() if len(c_t) and fl.sum().max() > 0 else None
        saturado = total_perfis >= 3 and perfis / total_perfis >= 0.75
        tipo = None
        if n >= 3 and pd.notna(row.mediana_interacao) and row.mediana_interacao >= 1.2 * geral and not saturado:
            tipo = "desempenho"
        elif len(c_t) >= 3 and interesse >= 0.3 and n <= 2:
            tipo = "lacuna"
        elif n >= 4 and row.get("depois", 0) >= 2 and row.get("depois", 0) > row.get("antes", 0):
            tipo = "crescimento"
        if not tipo:
            continue
        if tipo == "lacuna":
            problema = (f"{interesse:.0%} dos {len(c_t)} comentários deste tema são dúvidas ou pedidos, "
                        f"mas o tema aparece em apenas {n} publicação(ões) analisadas.")
        elif tipo == "desempenho":
            problema = ("A audiência reage ao tema acima da média geral, e o tema não aparece "
                        "em quase todos os perfis analisados.")
        else:
            problema = (f"O tema aparece mais na segunda metade do período ({int(row.antes)} publicações "
                        f"na primeira metade e {int(row.depois)} na segunda).")
        ops.append({
            "tema": tema, "tipo": tipo, "problema": problema,
            "justificativa": (f"Mediana de interação do tema: {row.mediana_interacao:,.0f} (geral: {geral:,.0f}); "
                              f"{n} publicação(ões) em {perfis} de {total_perfis} perfis; "
                              f"{len(c_t)} comentário(s) analisado(s)."),
            "formato": recomendar_formato(base, tema),
            "abordagem": ABORDAGEM.get(dominante, "Abordagem informativa com ponto de vista explícito."),
            "termos": termos(c_t, 5).index.tolist() if len(c_t) else [],
        })
    ordem = {"lacuna": 0, "desempenho": 1, "crescimento": 2}
    return sorted(ops, key=lambda o: ordem[o["tipo"]])


def titulo_modelo(op):
    return {"lacuna": f"{op['tema'].capitalize()}: o que a audiência mais pergunta",
            "desempenho": f"{op['tema'].capitalize()} na prática",
            "crescimento": f"Por que {op['tema']} está ganhando espaço"}[op["tipo"]]


def rascunho_ia(op):
    chave = os.environ.get("ANTHROPIC_API_KEY")
    if not chave:
        return None, "Configure ANTHROPIC_API_KEY para gerar gancho e desenvolvimento com IA."
    evidencias = (f"Tema: {op['tema']}. Tipo: {op['tipo']}. Problema ou interesse: {op['problema']} "
                  f"Justificativa: {op['justificativa']} "
                  f"Termos recorrentes nos comentários: {', '.join(op['termos']) or 'nenhum'}. "
                  f"Formato recomendado: {op['formato']}. Abordagem: {op['abordagem']}")
    pedido = ("Você é pesquisador de comunicação digital. Proponha uma ideia de conteúdo original com base "
              "somente nas evidências abaixo. Não reproduza nem copie conteúdos de perfis, não invente números "
              "e trate a ideia como hipótese a ser testada. Responda somente com JSON, com as chaves "
              "\"titulo\", \"gancho\" e \"desenvolvimento\" (este último com até 120 palavras).\n\n"
              f"Evidências: {evidencias}")
    corpo = json.dumps({"model": MODELO_IA, "max_tokens": 800,
                        "messages": [{"role": "user", "content": pedido}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=corpo, method="POST",
                                 headers={"x-api-key": chave, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            dados = json.loads(resp.read())
        texto = "".join(b.get("text", "") for b in dados["content"] if b.get("type") == "text")
        return json.loads(texto[texto.index("{"): texto.rindex("}") + 1]), None
    except Exception as e:
        return None, f"Falha ao gerar rascunho com IA: {e}"


# ---------- histórico e relatório PDF ----------
def salvar_ideias(linhas):
    momento = agora()  # cada salvamento gera novos registros; nada é sobrescrito
    for l in linhas:
        execute("""INSERT INTO ideias (gerado_em, tema, tipo, problema, justificativa, formato, abordagem,
                   titulo, gancho, desenvolvimento, origem) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (momento, l["tema"], l["tipo"], l["problema"], l["justificativa"], l["formato"],
                 l["abordagem"], l["titulo"], l["gancho"], l["desenvolvimento"], l["origem"]))
    return len(linhas)


def resumo_dados(base):
    datas = base.publicado_em.dropna()
    periodo = f"{datas.min():%d/%m/%Y} a {datas.max():%d/%m/%Y}" if not datas.empty else "indisponível"
    ultima = q("SELECT MAX(coletado_em) AS m FROM publicacoes")["m"].iloc[0]
    return (f"Perfis: {base.usuario.nunique()} · Publicações analisadas (20 mais recentes por perfil): {len(base)} · "
            f"Período: {periodo} · Última coleta: {ultima or 'indisponível'}. "
            "As ideias são hipóteses derivadas dos dados, não garantias de resultado.")


def gerar_pdf(linhas, resumo):
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title="Relatório de ideias de conteúdo",
                            leftMargin=50, rightMargin=50, topMargin=50, bottomMargin=50)
    estilos = getSampleStyleSheet()
    historia = [Paragraph("Relatório de ideias de conteúdo", estilos["Title"]),
                Paragraph(escape(resumo), estilos["Normal"]), Spacer(1, 12)]
    campos = [("problema", "Problema ou interesse"), ("justificativa", "Justificativa"),
              ("formato", "Formato recomendado"), ("abordagem", "Abordagem editorial"),
              ("titulo", "Possível título"), ("gancho", "Gancho inicial"),
              ("desenvolvimento", "Desenvolvimento"), ("origem", "Origem do texto criativo")]
    for i, l in enumerate(linhas, 1):
        historia.append(Paragraph(f"{i}. {escape(str(l['tema']))} ({escape(str(l['tipo']))})", estilos["Heading2"]))
        for chave, rotulo in campos:
            historia.append(Paragraph(f"<b>{rotulo}:</b> {escape(str(l[chave]))}", estilos["Normal"]))
        historia.append(Spacer(1, 10))
    doc.build(historia)
    return buf.getvalue()


def salvar_versao(ideia_id, titulo, gancho, desenvolvimento):
    # Não sobrescreve: a versão anterior permanece, e a nova aponta para ela em revisao_de.
    o = q("SELECT * FROM ideias WHERE id = ?", (ideia_id,)).iloc[0]
    execute("""INSERT INTO ideias (gerado_em, tema, tipo, problema, justificativa, formato, abordagem,
               titulo, gancho, desenvolvimento, origem, revisao_de) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (agora(), o["tema"], o["tipo"], o["problema"], o["justificativa"], o["formato"], o["abordagem"],
             titulo, gancho, desenvolvimento, "editado manualmente", ideia_id))


def _grafico(titulo, serie, est):
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.shapes import Drawing
    from reportlab.platypus import Paragraph
    valores = [0.0 if pd.isna(v) else float(v) for v in serie.values]
    if not valores or max(valores) <= 0:
        return [Paragraph(f"<b>{escape(titulo)}:</b> sem dados.", est["Normal"])]
    d = Drawing(480, 190)
    ch = VerticalBarChart()
    ch.x, ch.y, ch.width, ch.height = 45, 50, 420, 115
    ch.data = [valores]
    ch.categoryAxis.categoryNames = [str(i)[:16] for i in serie.index]
    ch.categoryAxis.labels.fontSize = 7
    ch.categoryAxis.labels.angle = 30
    ch.categoryAxis.labels.boxAnchor = "ne"
    ch.valueAxis.valueMin = 0
    ch.valueAxis.labels.fontSize = 7
    d.add(ch)
    return [Paragraph(f"<b>{escape(titulo)}</b>", est["Normal"]), d]


def gerar_pdf_painel(df, base, resumo):
    # df: publicações com os filtros do painel; base: publicações para a comparação entre perfis.
    from io import BytesIO
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title="Relatório de painel e comparação",
                            leftMargin=50, rightMargin=50, topMargin=50, bottomMargin=50)
    est = getSampleStyleSheet()

    def tab(d):
        dados = [[str(c) for c in d.columns]]
        for linha in d.itertuples(index=False):
            dados.append(["indisponível" if pd.isna(v) else (f"{v:,.1f}" if isinstance(v, float) else str(v))
                          for v in linha])
        t = Table(dados, repeatRows=1)
        t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 7), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                               ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
        return t

    res = resumo_perfis(base)
    historia = [Paragraph("Relatório de painel e comparação", est["Title"]),
                Paragraph(escape(resumo), est["Normal"]), Spacer(1, 10),
                Paragraph("<b>Indicadores (filtros do painel)</b>", est["Heading2"]),
                tab(pd.DataFrame([{"Publicações": len(df), "Perfis": df.usuario.nunique(),
                                   "Média de interação": df.interacao.mean(),
                                   "Mediana de interação": df.interacao.median()}])),
                Spacer(1, 10)]
    historia += _grafico("Formatos predominantes", df.formato.fillna("não informado").value_counts(), est)
    historia += _grafico("Temas mais frequentes (top 10)", df.tema.fillna("sem tema").value_counts().head(10), est)
    historia += [Paragraph("<b>Publicações de melhor desempenho (top 5 por interação)</b>", est["Heading2"]),
                 tab(df.nlargest(5, "interacao")[["usuario", "formato", "tema", "interacao"]]),
                 Paragraph("<b>Comparação entre perfis</b>", est["Heading2"]),
                 tab(res[["publicacoes", "mediana_curtidas", "mediana_comentarios", "mediana_interacao",
                          "posts_por_semana", "seguidores", "taxa_por_seguidor_%"]].reset_index()),
                 Paragraph("Seguidores não indicam desempenho. Compare medianas e taxas, observando o tamanho da amostra.",
                           est["Normal"])]
    historia += _grafico("Mediana de interação por perfil", res["mediana_interacao"], est)
    doc.build(historia)
    return buf.getvalue()


# ---------- interface ----------
def sem_dados():
    st.info("Nenhuma publicação importada ainda. Use a página 'Importar publicações'.")
    st.stop()


def filtros(df):
    st.sidebar.header("Filtros")
    perfis = st.sidebar.multiselect("Perfis", sorted(df.usuario.unique()))
    formatos = st.sidebar.multiselect("Formatos", sorted(df.formato.dropna().unique()))
    temas = st.sidebar.multiselect("Temas", sorted(df.tema.dropna().unique()))
    datas = df.publicado_em.dropna()
    if not datas.empty:
        periodo = st.sidebar.date_input("Período", (datas.min().date(), datas.max().date()))
        if isinstance(periodo, tuple) and len(periodo) == 2:
            ini, fim = pd.Timestamp(periodo[0]), pd.Timestamp(periodo[1]) + pd.Timedelta(days=1)
            df = df[df.publicado_em.between(ini, fim, inclusive="left")]
    if perfis:
        df = df[df.usuario.isin(perfis)]
    if formatos:
        df = df[df.formato.isin(formatos)]
    if temas:
        df = df[df.tema.isin(temas)]
    return df


st.set_page_config(page_title="Inteligência de Conteúdo", layout="wide")
init_db()
pagina = st.sidebar.radio("Navegação", PAGINAS)

if pagina == "Painel":
    st.title("Painel principal")
    base = carregar()
    if base.empty:
        sem_dados()
    df = filtros(base)
    atualizacao = q("SELECT MAX(coletado_em) AS m FROM publicacoes")["m"].iloc[0]
    c = st.columns(5)
    c[0].metric("Perfis monitorados", int(q("SELECT COUNT(*) AS n FROM perfis")["n"].iloc[0]))
    c[1].metric("Publicações analisadas", len(df))
    c[2].metric("Média de interação", mostrar(df.interacao.mean()))
    c[3].metric("Mediana de interação", mostrar(df.interacao.median()))
    c[4].metric("Última atualização", atualizacao or "—")
    col1, col2 = st.columns(2)
    col1.subheader("Formatos predominantes")
    col1.bar_chart(df.formato.fillna("não informado").value_counts())
    col2.subheader("Temas mais frequentes")
    col2.bar_chart(df.tema.fillna("sem tema").value_counts().head(10))
    st.subheader("Publicações de melhor desempenho (top 5 por interação)")
    st.dataframe(df.nlargest(5, "interacao")[["usuario", "formato", "tema", "interacao", "link"]],
                 hide_index=True)
    st.download_button("Baixar publicações filtradas (CSV)", df.to_csv(index=False).encode("utf-8-sig"),
                       "publicacoes_filtradas.csv", "text/csv")
    st.download_button("Baixar relatório de painel e comparação (PDF)",
                       gerar_pdf_painel(df, base, resumo_dados(base)),
                       "relatorio_painel.pdf", "application/pdf")

elif pagina == "Perfis":
    st.title("Perfis monitorados")
    with st.form("novo_perfil"):
        ref = st.text_input("Link ou @ do perfil", placeholder="https://www.instagram.com/usuario/ ou @usuario")
        c1, c2, c3 = st.columns(3)
        categoria = c1.text_input("Categoria")
        nicho = c2.text_input("Nicho")
        posicionamento = c3.text_input("Posicionamento")
        bio = st.text_area("Biografia")
        d1, d2, d3 = st.columns(3)
        seguidores = d1.text_input("Seguidores (vazio = indisponível)")
        seguindo = d2.text_input("Seguindo")
        publicacoes = d3.text_input("Quantidade de publicações")
        if st.form_submit_button("Salvar perfil"):
            if not norm_usuario(ref):
                st.error("Informe o link ou o @ do perfil.")
            else:
                u = salvar_perfil(ref, categoria, nicho, posicionamento, bio, seguidores, seguindo, publicacoes)
                st.success(f"Perfil @{u} salvo. Os dados anteriores foram preservados como histórico.")
    st.subheader("Situação atual")
    st.dataframe(perfis_atuais(), hide_index=True)

elif pagina == "Importar publicações":
    st.title("Importar publicações")
    st.write("Colunas: " + ", ".join(COLUNAS_CSV) + ". Salve o arquivo em UTF-8. "
             "Datas no formato dd/mm/aaaa ou aaaa-mm-dd. Números sem separador de milhar.")
    st.download_button("Baixar modelo CSV", ",".join(COLUNAS_CSV) + "\n",
                       "modelo_publicacoes.csv", "text/csv")
    arq = st.file_uploader("Arquivo CSV", type="csv")
    if arq and st.button("Importar"):
        df = pd.read_csv(io.BytesIO(arq.getvalue()), dtype=str, sep=None, engine="python",
                         encoding="utf-8-sig").fillna("")
        if not {"usuario", "link"} <= set(df.columns):
            st.error("O arquivo precisa ter, no mínimo, as colunas 'usuario' e 'link'.")
        else:
            ok, erros = importar(df, arq.name)
            st.success(f"{ok} publicações importadas em {agora()}.")
            for e in erros:
                st.warning(e)
    st.divider()
    st.subheader("Importar comentários")
    st.write("Colunas: link (da publicação), texto, autor (opcional; é pseudonimizado), publicado_em, curtidas. "
             "O link precisa corresponder a uma publicação já importada.")
    arq_c = st.file_uploader("Arquivo CSV de comentários", type="csv", key="comentarios")
    if arq_c and st.button("Importar comentários"):
        dfc = pd.read_csv(io.BytesIO(arq_c.getvalue()), dtype=str, sep=None, engine="python",
                          encoding="utf-8-sig").fillna("")
        if not {"link", "texto"} <= set(dfc.columns):
            st.error("O arquivo precisa ter, no mínimo, as colunas 'link' e 'texto'.")
        else:
            ok_c, erros_c = importar_comentarios(dfc, arq_c.name)
            st.success(f"{ok_c} comentários importados em {agora()}.")
            for e in erros_c:
                st.warning(e)

elif pagina == "Audiência e comentários":
    st.title("Audiência e comentários")
    st.caption("Classificação por palavras-chave: triagem automática, sujeita a erros e a revisão humana. "
               "Os comentários não representam toda a audiência. Autores são pseudonimizados e exemplos são anonimizados.")
    base = carregar()
    com = carregar_comentarios()
    com = com[com.link_publicacao.isin(base.link)]
    if com.empty:
        st.info("Nenhum comentário importado para as publicações analisadas.")
        st.stop()
    perfis = st.multiselect("Perfis", sorted(com.usuario.unique()), default=sorted(com.usuario.unique()))
    com = com[com.usuario.isin(perfis)]
    if com.empty:
        st.info("Selecione ao menos um perfil.")
        st.stop()
    flags = classificar(com)
    c = st.columns(3)
    c[0].metric("Comentários", len(com))
    c[1].metric("Autores distintos (pseudônimos)", int(com.autor_anonimo.nunique()))
    c[2].metric("Sem categoria identificada", int((~flags.any(axis=1)).sum()))
    st.subheader("Categorias temáticas")
    st.dataframe(pd.DataFrame({"comentários": flags.sum(), "% do total": (flags.mean() * 100).round(1)})
                 .sort_values("comentários", ascending=False))
    st.subheader("Exemplos anonimizados")
    for cat in CATEGORIAS:
        ex = com.loc[flags[cat], "texto"].head(2)
        if not ex.empty:
            with st.expander(f"{cat} ({int(flags[cat].sum())})"):
                for t in ex:
                    st.write(f"- {anonimizar(t)}")
    st.subheader("Termos mais usados")
    st.bar_chart(termos(com))
    st.subheader("Comentários por publicação")
    st.dataframe(com.groupby("link_publicacao").agg(
        comentarios=("texto", "count"), autores_distintos=("autor_anonimo", "nunique"))
        .sort_values("comentarios", ascending=False))

elif pagina == "Análise por perfil":
    base = carregar()
    if base.empty:
        sem_dados()
    usuario = st.selectbox("Perfil", sorted(base.usuario.unique()))
    d = base[base.usuario == usuario]
    p = perfis_atuais().set_index("usuario").loc[usuario]
    r = resumo_perfis(d).loc[usuario]
    st.title(f"@{usuario}")
    st.write(f"**Categoria:** {txt(p.categoria)} · **Nicho:** {txt(p.nicho)} · "
             f"**Posicionamento:** {txt(p.posicionamento)}")
    if not pd.isna(p.biografia):
        st.write(f"**Biografia:** {p.biografia}")
    c = st.columns(5)
    c[0].metric("Seguidores (última coleta)", mostrar(p.seguidores))
    c[1].metric("Publicações analisadas", int(r["publicacoes"]))
    c[2].metric("Mediana de curtidas", mostrar(r["mediana_curtidas"]))
    c[3].metric("Mediana de comentários", mostrar(r["mediana_comentarios"]))
    c[4].metric("Frequência (posts/semana)", mostrar(r["posts_por_semana"], 1))
    st.subheader("Publicações")
    st.dataframe(d.sort_values("publicado_em", ascending=False)[
        ["publicado_em", "formato", "tema", "curtidas", "comentarios",
         "compartilhamentos", "visualizacoes", "link"]], hide_index=True)
    st.subheader("Desempenho por formato")
    st.dataframe(por_formato(d))

elif pagina == "Comparação":
    st.title("Comparação entre perfis")
    st.caption("Seguidores não indicam desempenho. Compare medianas e taxas por seguidor, "
               "e observe o número de publicações de cada amostra.")
    base = carregar()
    if base.empty:
        sem_dados()
    todos = sorted(base.usuario.unique())
    escolhidos = st.multiselect("Perfis", todos, default=todos[:3])
    d = base[base.usuario.isin(escolhidos)]
    if d.empty:
        st.info("Selecione ao menos um perfil.")
        st.stop()
    res = resumo_perfis(d)
    st.dataframe(res)
    st.subheader("Mediana de interação por perfil")
    st.bar_chart(res["mediana_interacao"])
    st.subheader("Média de interação por formato e perfil")
    st.dataframe(d.assign(formato=d.formato.fillna("não informado"))
                 .pivot_table(index="usuario", columns="formato", values="interacao", aggfunc="mean").round(1))

elif pagina == "Tendências":
    st.title("Tendências")
    st.caption("Indicativos da amostra importada. Temas com menos de 3 publicações têm sinal fraco. "
               "Correlação não é causalidade.")
    base = carregar()
    if base.empty:
        sem_dados()
    df = filtros(base)
    t = por_tema(df)
    st.subheader("Temas: frequência, interação e variação entre as metades do período")
    st.dataframe(t)
    st.subheader("Temas com maior interação (mínimo de 3 publicações)")
    st.dataframe(t[t.publicacoes >= 3].sort_values("media_interacao", ascending=False))
    st.subheader("Mediana de interação por dia da semana")
    st.bar_chart(por_dia(df)["mediana_interacao"])
    st.subheader("Formatos")
    st.dataframe(por_formato(df))

elif pagina == "Laboratório de ideias":
    st.title("Laboratório de ideias")
    st.caption("Hipóteses derivadas dos dados importados. Critérios mínimos: 3 publicações por tema "
               "(ou 3 comentários, para lacunas). Não copie conteúdos dos perfis analisados.")
    base = carregar()
    if base.empty:
        sem_dados()
    com = carregar_comentarios()
    com = com[com.link_publicacao.isin(base.link)]
    ops = gerar_oportunidades(base, com)
    if not ops:
        st.info("Nenhuma oportunidade atende aos critérios mínimos. Importe mais publicações e comentários.")
        st.stop()
    usar_ia = st.checkbox("Gerar gancho e desenvolvimento com IA", value=bool(os.environ.get("ANTHROPIC_API_KEY")))
    linhas = []
    for op in ops:
        chave = f"ia|{op['tema']}|{op['tipo']}"
        rascunho, erro = None, None
        if usar_ia:
            if chave not in st.session_state:
                st.session_state[chave] = rascunho_ia(op)
            rascunho, erro = st.session_state[chave]
        rascunho = rascunho or {}
        titulo = rascunho.get("titulo") or titulo_modelo(op)
        gancho = rascunho.get("gancho") or "indisponível sem IA"
        desenvolvimento = rascunho.get("desenvolvimento") or "indisponível sem IA"
        with st.expander(f"{op['tipo'].capitalize()} · {op['tema']}", expanded=True):
            st.markdown(f"**Problema ou interesse identificado:** {op['problema']}")
            st.markdown(f"**Justificativa:** {op['justificativa']}")
            st.markdown(f"**Formato recomendado:** {op['formato']}")
            st.markdown(f"**Abordagem editorial:** {op['abordagem']}")
            st.markdown(f"**Possível título:** {titulo}")
            st.markdown(f"**Gancho inicial:** {gancho}")
            st.markdown(f"**Desenvolvimento:** {desenvolvimento}")
            if erro:
                st.warning(erro)
        linhas.append({"tema": op["tema"], "tipo": op["tipo"], "problema": op["problema"],
                       "justificativa": op["justificativa"], "formato": op["formato"],
                       "abordagem": op["abordagem"], "titulo": titulo, "gancho": gancho,
                       "desenvolvimento": desenvolvimento,
                       "origem": "IA" if rascunho else "sem IA"})
    c1, c2 = st.columns(2)
    if c1.button("Salvar ideias no banco"):
        c1.success(f"{salvar_ideias(linhas)} ideias salvas em {agora()}. O histórico anterior foi preservado.")
    c2.download_button("Baixar relatório (PDF)", gerar_pdf(linhas, resumo_dados(base)),
                       "relatorio_ideias.pdf", "application/pdf")
    st.download_button("Baixar ideias (CSV)", pd.DataFrame(linhas).to_csv(index=False).encode("utf-8-sig"),
                       "ideias.csv", "text/csv")

elif pagina == "Banco de inteligência":
    st.title("Banco de inteligência")
    st.caption("Histórico completo de coletas. Cada importação e cada salvamento de perfil gera novos registros.")
    hist = q("""SELECT pf.usuario, p.coletado_em, p.fonte, p.link, p.publicado_em, p.formato, p.tema,
                p.curtidas, p.comentarios, p.compartilhamentos, p.visualizacoes, p.legenda
                FROM publicacoes p JOIN perfis pf ON pf.id = p.perfil_id ORDER BY p.coletado_em DESC""")
    snap = q("""SELECT pf.usuario, s.coletado_em, s.seguidores, s.seguindo, s.publicacoes, s.biografia
                FROM snapshots_perfil s JOIN perfis pf ON pf.id = s.perfil_id ORDER BY s.coletado_em DESC""")
    st.subheader(f"Publicações ({len(hist)} registros)")
    st.dataframe(hist, hide_index=True)
    st.download_button("Exportar publicações (CSV)", hist.to_csv(index=False).encode("utf-8-sig"),
                       "banco_publicacoes.csv", "text/csv")
    st.subheader(f"Histórico de perfis ({len(snap)} registros)")
    st.dataframe(snap, hide_index=True)
    st.download_button("Exportar histórico de perfis (CSV)", snap.to_csv(index=False).encode("utf-8-sig"),
                       "banco_perfis.csv", "text/csv")
    st.subheader("Ideias: versões atuais")
    if "msg" in st.session_state:
        st.success(st.session_state.pop("msg"))
    atuais = q("""SELECT id, gerado_em, tema, tipo, titulo, gancho, desenvolvimento, origem FROM ideias i
                  WHERE i.id NOT IN (SELECT revisao_de FROM ideias WHERE revisao_de IS NOT NULL)
                  ORDER BY id DESC""")
    st.dataframe(atuais, hide_index=True)
    if not atuais.empty:
        rotulos = {int(r.id): f"#{int(r.id)} · {r.tema} · {r.titulo}" for r in atuais.itertuples()}
        escolha = st.selectbox("Editar ideia", list(rotulos), format_func=lambda i: rotulos[i])
        atual = atuais[atuais.id == escolha].iloc[0]
        sem_nulo = lambda v: "" if pd.isna(v) else str(v)
        novo_t = st.text_input("Possível título", sem_nulo(atual.titulo), key=f"t{escolha}")
        novo_g = st.text_input("Gancho inicial", sem_nulo(atual.gancho), key=f"g{escolha}")
        novo_d = st.text_area("Desenvolvimento", sem_nulo(atual.desenvolvimento), key=f"d{escolha}")
        if st.button("Salvar nova versão"):
            salvar_versao(int(escolha), novo_t, novo_g, novo_d)
            st.session_state["msg"] = f"Nova versão da ideia #{escolha} salva. A versão anterior foi preservada."
            st.rerun()
    st.subheader("Histórico completo de ideias")
    hist_ide = q("SELECT * FROM ideias ORDER BY id DESC")
    st.dataframe(hist_ide, hide_index=True)
    st.download_button("Exportar histórico de ideias (CSV)", hist_ide.to_csv(index=False).encode("utf-8-sig"),
                       "banco_ideias.csv", "text/csv")
