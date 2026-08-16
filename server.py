import asyncio
import html as html_lib
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Optional

import httpx
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

# Cria o servidor MCP
mcp = FastMCP("G1-Noticias")


# --- Modelos -----------------------------------------------------------
# Estruturas simples de dados (imutáveis) que trafegam entre as classes
# abaixo. Mantê-las separadas da lógica de scraping é o que permite trocar
# a fonte de dados (ex: outro portal de notícias) sem mexer nas tools.


@dataclass(frozen=True)
class Editoria:
    """Uma editoria autorizada do G1: chave interna, nome de exibição e URL."""

    chave: str
    nome: str
    url: str


@dataclass(frozen=True)
class ItemNoticia:
    """Uma notícia encontrada na página de listagem de uma editoria."""

    titulo: str
    url: str
    chamada: str


@dataclass(frozen=True)
class MateriaG1:
    """O conteúdo extraído da página de uma matéria do G1."""

    titulo: str
    subtitulo: str
    paragrafos: list[str]


# --- Editorias permitidas ------------------------------------------------
# Lista fixa combinada com o Mário: só estas 16 editorias podem ser
# consultadas. Isso evita que a tool vire um scraper genérico do G1 (que
# tem seções não jornalísticas, como esportes ao vivo ou classificados).

EDITORIAS_PADRAO = [
    Editoria("agro", "Agro", "https://g1.globo.com/economia/agronegocios/"),
    Editoria("ciencia", "Ciência", "https://g1.globo.com/ciencia/"),
    Editoria("economia", "Economia", "https://g1.globo.com/economia/"),
    Editoria("educacao", "Educação", "https://g1.globo.com/educacao/"),
    Editoria("empreendedorismo", "Empreendedorismo", "https://g1.globo.com/empreendedorismo/"),
    Editoria("fato-ou-fake", "Fato ou Fake", "https://g1.globo.com/fato-ou-fake/"),
    Editoria("inovacao", "Inovação", "https://g1.globo.com/inovacao/"),
    Editoria("loterias", "Loterias", "https://g1.globo.com/loterias/"),
    Editoria("meio-ambiente", "Meio Ambiente", "https://g1.globo.com/meio-ambiente/"),
    Editoria("mundo", "Mundo", "https://g1.globo.com/mundo/"),
    Editoria("politica", "Política", "https://g1.globo.com/politica/"),
    Editoria("pop-arte", "Pop & Arte", "https://g1.globo.com/pop-arte/"),
    Editoria("saude", "Saúde", "https://g1.globo.com/saude/"),
    Editoria("tecnologia", "Tecnologia", "https://g1.globo.com/tecnologia/"),
    Editoria("trabalho-e-carreira", "Trabalho e Carreira", "https://g1.globo.com/trabalho-e-carreira/"),
    Editoria("turismo-e-viagem", "Turismo e Viagem", "https://g1.globo.com/turismo-e-viagem/"),
]


class RegistroEditorias:
    """Guarda as editorias permitidas e resolve o parâmetro `editoria` da tool."""

    def __init__(self, editorias: list[Editoria]) -> None:
        self._editorias = editorias
        self._por_chave = {editoria.chave: editoria for editoria in editorias}

    @property
    def todas(self) -> list[Editoria]:
        return list(self._editorias)

    def buscar(self, editoria_informada: str) -> Optional[Editoria]:
        return self._por_chave.get(self._normalizar_chave(editoria_informada))

    def formatar_lista(self) -> str:
        linhas = ["Editorias do G1 disponíveis para a tool noticias_g1:\n"]
        for editoria in self._editorias:
            linhas.append(f"- {editoria.nome} (chave: {editoria.chave}) — {editoria.url}")
        return "\n".join(linhas)

    @staticmethod
    def _normalizar_chave(texto: str) -> str:
        # Aceita variações como "Pop & Arte" ou "fato ou fake" e converte
        # para a mesma chave usada internamente (ex: "pop-arte").
        sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
        minusculo = sem_acento.strip().lower().replace(" ", "-")
        somente_validos = re.sub(r"[^a-z0-9-]", "", minusculo)
        return re.sub(r"-+", "-", somente_validos).strip("-")


# --- Acesso HTTP ----------------------------------------------------------


class ClienteHttpG1:
    """Busca uma página do G1, devolvendo None em caso de erro de rede/HTTP.

    O G1 é um site de terceiro fora do nosso controle — por isso todo erro
    aqui é tratado como "página indisponível agora", nunca propagado como
    exceção para a tool. É a tool que decide se avisa a pessoa usuária ou
    tenta outro candidato.
    """

    def __init__(self, timeout_segundos: float = 10.0) -> None:
        self._timeout_segundos = timeout_segundos

    async def buscar_html(self, cliente: httpx.AsyncClient, url: str) -> Optional[str]:
        try:
            resposta = await cliente.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; mcp-g1/1.0)"},
                timeout=self._timeout_segundos,
            )
            resposta.raise_for_status()
        except httpx.HTTPError:
            return None
        return resposta.text


# --- Extração de HTML ------------------------------------------------------
# As expressões regulares abaixo foram conferidas contra o HTML real do G1
# (template "bastian", usado em todas as editorias do site). Cada item de
# notícia na listagem tem a chamada logo depois do título, então a janela
# de captura é limitada a poucos caracteres (`{0,300}`) para não vazar e
# acabar pegando a chamada da PRÓXIMA notícia da lista.


class LimpadorHtml:
    """Remove tags e desfaz entidades HTML, usado pelos dois extratores abaixo."""

    _TAG_RE = re.compile(r"<[^>]+>")

    def limpar(self, texto_html: str) -> str:
        sem_tags = self._TAG_RE.sub(" ", texto_html)
        return " ".join(html_lib.unescape(sem_tags).split())


class ExtratorListagemG1:
    """Extrai os itens de notícia (título, link, chamada) de uma página de editoria."""

    _ITEM_RE = re.compile(
        r'<a href="(https://g1\.globo\.com/[^"]+\.ghtml)" class="feed-post-link[^"]*">'
        r'<p[^>]*>(.*?)</p></a>'
        r'.{0,300}?<div class="feed-post-body-resumo"><p[^>]*>(.*?)</p></div>',
        re.DOTALL,
    )

    def __init__(self, limpador: LimpadorHtml) -> None:
        self._limpador = limpador

    def extrair(self, html_pagina: str) -> list[ItemNoticia]:
        itens = []
        for url, titulo_bruto, chamada_bruta in self._ITEM_RE.findall(html_pagina):
            titulo = self._limpador.limpar(titulo_bruto)
            if not titulo:
                continue
            itens.append(ItemNoticia(titulo=titulo, url=url, chamada=self._limpador.limpar(chamada_bruta)))
        return itens


class ExtratorMateriaG1:
    """Extrai título, subtítulo e parágrafos do corpo de uma matéria do G1."""

    _TITULO_RE = re.compile(r'<h1 class="content-head__title"[^>]*>(.*?)</h1>', re.DOTALL)
    _SUBTITULO_RE = re.compile(r'<meta itemprop="alternateName" content="([^"]*)"')
    _PARAGRAFO_RE = re.compile(r'<p class="[^"]*content-text__container[^"]*"[^>]*>(.*?)</p>', re.DOTALL)

    def __init__(self, limpador: LimpadorHtml) -> None:
        self._limpador = limpador

    def extrair(self, html_pagina: str) -> Optional[MateriaG1]:
        titulo_match = self._TITULO_RE.search(html_pagina)
        if titulo_match is None:
            return None
        subtitulo_match = self._SUBTITULO_RE.search(html_pagina)
        paragrafos = [self._limpador.limpar(p) for p in self._PARAGRAFO_RE.findall(html_pagina)]
        return MateriaG1(
            titulo=self._limpador.limpar(titulo_match.group(1)),
            subtitulo=self._limpador.limpar(subtitulo_match.group(1)) if subtitulo_match else "",
            paragrafos=[p for p in paragrafos if p],
        )


class MontadorResumo:
    """Monta um resumo de tamanho alvo (em palavras) a partir de uma matéria."""

    def __init__(self, limite_palavras: int = 150) -> None:
        self._limite_palavras = limite_palavras

    def montar(self, materia: MateriaG1) -> str:
        # Começa pelo subtítulo (já é um resumo editorial curto do G1) e
        # completa com os parágrafos do corpo até atingir o limite de
        # palavras. Se a matéria for mais curta que o limite, devolve só o
        # que existe — nunca inventa texto para completar 150 palavras.
        texto_completo = " ".join(parte for parte in [materia.subtitulo, *materia.paragrafos] if parte)
        palavras = texto_completo.split()
        if not palavras:
            return ""
        resumo = " ".join(palavras[: self._limite_palavras])
        if len(palavras) > self._limite_palavras:
            resumo += "…"
        return resumo


class FiltroPorTema:
    """Decide se uma notícia da listagem tem relação com o tema pesquisado."""

    def __init__(self, tema: str) -> None:
        todas_as_palavras = self._normalizar(tema).split()
        significativas = [palavra for palavra in todas_as_palavras if len(palavra) >= 3]
        self._palavras_chave = significativas or todas_as_palavras

    def combina(self, item: ItemNoticia) -> bool:
        texto = self._normalizar(f"{item.titulo} {item.chamada}")
        return any(palavra in texto for palavra in self._palavras_chave)

    @staticmethod
    def _normalizar(texto: str) -> str:
        sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
        return sem_acento.lower()


class CacheListagem:
    """Cache em memória, por editoria, com TTL — evita varrer as 16 páginas
    do G1 a cada chamada. 10 minutos é o mesmo padrão usado no mcp-sofiasa."""

    def __init__(self, ttl_segundos: int = 600) -> None:
        self._ttl_segundos = ttl_segundos
        self._dados: dict[str, tuple[float, list[ItemNoticia]]] = {}

    def obter(self, chave: str) -> Optional[list[ItemNoticia]]:
        registro = self._dados.get(chave)
        if registro is None:
            return None
        buscado_em, itens = registro
        if (time.monotonic() - buscado_em) >= self._ttl_segundos:
            return None
        return itens

    def guardar(self, chave: str, itens: list[ItemNoticia]) -> None:
        self._dados[chave] = (time.monotonic(), itens)


class BuscadorNoticiasG1:
    """Orquestra a busca: escolhe as editorias, filtra pelo tema, busca cada
    matéria candidata e monta o texto final devolvido pela tool."""

    def __init__(
        self,
        registro_editorias: RegistroEditorias,
        cliente_http: ClienteHttpG1,
        extrator_listagem: ExtratorListagemG1,
        extrator_materia: ExtratorMateriaG1,
        montador_resumo: MontadorResumo,
        cache: CacheListagem,
    ) -> None:
        self._registro_editorias = registro_editorias
        self._cliente_http = cliente_http
        self._extrator_listagem = extrator_listagem
        self._extrator_materia = extrator_materia
        self._montador_resumo = montador_resumo
        self._cache = cache

    async def buscar(self, tema: str, editoria: str = "", quantidade: int = 3) -> str:
        tema = tema.strip()
        if not tema:
            return "Informe um tema para buscar notícias do G1 (ex: 'eleições', 'saúde mental')."
        quantidade = max(1, min(quantidade, 10))

        if editoria.strip():
            alvo = self._registro_editorias.buscar(editoria)
            if alvo is None:
                chaves = ", ".join(e.chave for e in self._registro_editorias.todas)
                return f"Editoria '{editoria}' não é permitida. Editorias válidas: {chaves}."
            editorias_alvo = [alvo]
        else:
            editorias_alvo = self._registro_editorias.todas

        async with httpx.AsyncClient() as cliente:
            listas = await asyncio.gather(
                *(self._itens_da_editoria(cliente, editoria_alvo) for editoria_alvo in editorias_alvo)
            )
            candidatos = [item for lista in listas for item in lista]

            filtro = FiltroPorTema(tema)
            correspondentes = [item for item in candidatos if filtro.combina(item)]

            if not correspondentes:
                return (
                    f"Não foram encontradas notícias sobre '{tema}' nas editorias do G1 "
                    "permitidas para busca. Tente um tema mais amplo, ou use a tool "
                    "g1_listar_editorias para ver as editorias disponíveis."
                )

            resultados: list[tuple[ItemNoticia, str]] = []
            for item in correspondentes:
                if len(resultados) >= quantidade:
                    break
                resumo = await self._resumo_da_materia(cliente, item.url)
                if resumo:
                    resultados.append((item, resumo))

        if not resultados:
            return (
                f"Notícias sobre '{tema}' foram encontradas no G1, mas não foi possível "
                "acessar o conteúdo completo agora. Tente novamente em instantes."
            )

        blocos = [f"Notícias do G1 sobre '{tema}':\n"]
        for indice, (item, resumo) in enumerate(resultados, start=1):
            blocos.append(f"{indice}. {item.titulo}\nResumo: {resumo}\nLink: {item.url}\n")
        return "\n".join(blocos)

    async def _itens_da_editoria(self, cliente: httpx.AsyncClient, editoria: Editoria) -> list[ItemNoticia]:
        em_cache = self._cache.obter(editoria.chave)
        if em_cache is not None:
            return em_cache
        html_pagina = await self._cliente_http.buscar_html(cliente, editoria.url)
        itens = self._extrator_listagem.extrair(html_pagina) if html_pagina else []
        self._cache.guardar(editoria.chave, itens)
        return itens

    async def _resumo_da_materia(self, cliente: httpx.AsyncClient, url: str) -> str:
        html_pagina = await self._cliente_http.buscar_html(cliente, url)
        if not html_pagina:
            return ""
        materia = self._extrator_materia.extrair(html_pagina)
        if materia is None:
            return ""
        return self._montador_resumo.montar(materia)


# --- Composição (instâncias únicas usadas pelas tools) ---------------------

_limpador_html = LimpadorHtml()
_registro_editorias = RegistroEditorias(EDITORIAS_PADRAO)
_buscador_noticias = BuscadorNoticiasG1(
    registro_editorias=_registro_editorias,
    cliente_http=ClienteHttpG1(),
    extrator_listagem=ExtratorListagemG1(_limpador_html),
    extrator_materia=ExtratorMateriaG1(_limpador_html),
    montador_resumo=MontadorResumo(limite_palavras=150),
    cache=CacheListagem(ttl_segundos=600),
)


@mcp.tool(
    name="noticias_g1",
    annotations=ToolAnnotations(
        title="Buscar notícias do G1 por tema",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def noticias_g1(tema: str, editoria: str = "", quantidade: int = 3) -> str:
    """Busca notícias reais do G1 relacionadas a um tema, como fonte confiável para textos.

    Varre as editorias autorizadas do G1 (ver a tool g1_listar_editorias)
    procurando notícias cujo título ou chamada tenha relação com o tema
    informado. Para cada notícia encontrada, acessa a matéria original e
    monta um resumo de até 150 palavras.

    Args:
        tema: Assunto a pesquisar (ex: "eleições", "inteligência artificial").
        editoria: Chave de uma editoria específica para restringir a busca
            (ex: "tecnologia", "saude"). Deixe em branco para buscar nas 16
            editorias permitidas.
        quantidade: Quantas notícias retornar (padrão 3, máximo 10).

    Returns:
        Texto com, para cada notícia: título, resumo de até 150 palavras e o
        link obrigatório da matéria original, para a pessoa usuária conferir
        a fonte na íntegra. Se nada for encontrado, explica isso em vez de
        inventar uma notícia.
    """
    return await _buscador_noticias.buscar(tema=tema, editoria=editoria, quantidade=quantidade)


@mcp.tool(
    name="g1_listar_editorias",
    annotations=ToolAnnotations(
        title="Listar editorias do G1 permitidas",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def g1_listar_editorias() -> str:
    """Lista as editorias do G1 permitidas para a tool noticias_g1.

    Não faz scraping — é uma lista fixa e estática, útil para a IA descobrir
    a chave de editoria a usar no parâmetro `editoria` de noticias_g1.

    Returns:
        Texto com o nome, a chave e a URL de cada uma das 16 editorias.
    """
    return _registro_editorias.formatar_lista()


# Cria a aplicação ASGI em Streamable HTTP, no caminho /mcp, com CORS
# liberado para clientes remotos e modo stateless para rodar em serverless
app = mcp.http_app(
    path="/mcp",
    stateless_http=True,
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["mcp-session-id"],
        )
    ],
)

if __name__ == "__main__":
    import os
    import uvicorn

    porta = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=porta)
