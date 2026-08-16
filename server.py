import asyncio
import html as html_lib
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

# Cria o servidor MCP
mcp = FastMCP("G1-Noticias")

_DATA_MUITO_ANTIGA = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Guardrail de negócio, combinado com o Mário: esta tool nunca devolve mais
# de 3 notícias numa única chamada, mesmo que o parâmetro `quantidade`
# receba um valor maior. Ficou definido aqui, e não só na docstring, porque
# a docstring é só uma instrução para o cliente MCP respeitar — quem
# garante o limite de verdade é o código.
MAXIMO_NOTICIAS_POR_CHAMADA = 3


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

    def url_da_pagina(self, pagina: int) -> str:
        """Monta o URL de listagem da editoria; a página 1 é o URL normal,
        as seguintes usam a paginação "index/feed" do G1 (confirmada em
        teste manual: cada página traz 10 notícias mais antigas que a anterior)."""
        if pagina <= 1:
            return self.url
        return f"{self.url}index/feed/pagina-{pagina}.ghtml"

    def pertence(self, url_noticia: str) -> bool:
        """Confere se um link encontrado na listagem é mesmo desta editoria.

        As páginas de listagem do G1 também têm blocos de "veja também" e
        matérias relacionadas, com a mesma marcação HTML das notícias da
        editoria, mas apontando para outras seções do site. Sem esse filtro,
        a tool arrisca devolver uma notícia fora do escopo combinado.
        """
        return url_noticia.startswith(self.url)


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
    publicado_em: Optional[datetime]


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


class NormalizadorTexto:
    """Remove acentos e caixa alta, para comparar texto de forma tolerante
    a variação. Usado tanto para casar a chave de editoria quanto o tema."""

    @staticmethod
    def normalizar(texto: str) -> str:
        sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
        return sem_acento.lower()


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
        minusculo = NormalizadorTexto.normalizar(texto).strip().replace(" ", "-")
        somente_validos = re.sub(r"[^a-z0-9-]", "", minusculo)
        return re.sub(r"-+", "-", somente_validos).strip("-")


class GeradorPalavrasChave:
    """Amplia o tema de busca em até 10 variações (plural/singular e um
    radical curto de cada palavra), para não depender só do termo exato.

    Ex: tema "vacina" também casa com "vacinas", "vacinação", "vacinal" —
    porque o radical "vacin" fica entre as variações geradas.
    """

    _MAXIMO_VARIACOES = 10
    _TAMANHO_RADICAL = 5

    def gerar(self, tema: str) -> list[str]:
        normalizado = NormalizadorTexto.normalizar(tema)
        palavras = normalizado.split()
        significativas = [palavra for palavra in palavras if len(palavra) >= 3] or palavras

        variacoes: list[str] = []
        for palavra in significativas:
            self._adicionar(variacoes, palavra)
            self._adicionar(variacoes, self._variar_plural(palavra))
            self._adicionar(variacoes, palavra[: self._TAMANHO_RADICAL])
            if len(variacoes) >= self._MAXIMO_VARIACOES:
                break

        return variacoes[: self._MAXIMO_VARIACOES]

    @staticmethod
    def _adicionar(lista: list[str], palavra: str) -> None:
        if palavra and palavra not in lista:
            lista.append(palavra)

    @staticmethod
    def _variar_plural(palavra: str) -> str:
        if palavra.endswith("s") and len(palavra) > 4:
            return palavra[:-1]
        return palavra + "s"


class FiltroPorTema:
    """Decide se uma notícia da listagem tem relação com o tema pesquisado,
    usando as variações de palavra-chave geradas por GeradorPalavrasChave."""

    def __init__(self, tema: str, gerador_palavras_chave: GeradorPalavrasChave) -> None:
        self._palavras_chave = gerador_palavras_chave.gerar(tema)

    def combina(self, item: ItemNoticia) -> bool:
        texto = NormalizadorTexto.normalizar(f"{item.titulo} {item.chamada}")
        return any(palavra in texto for palavra in self._palavras_chave)


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
    """Extrai título, subtítulo, parágrafos e data de publicação de uma matéria do G1."""

    _TITULO_RE = re.compile(r'<h1 class="content-head__title"[^>]*>(.*?)</h1>', re.DOTALL)
    _SUBTITULO_RE = re.compile(r'<meta itemprop="alternateName" content="([^"]*)"')
    _PARAGRAFO_RE = re.compile(r'<p class="[^"]*content-text__container[^"]*"[^>]*>(.*?)</p>', re.DOTALL)
    _DATA_PUBLICACAO_RE = re.compile(r'<meta itemprop="datePublished" content="([^"]+)"')

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
            publicado_em=self._extrair_data(html_pagina),
        )

    def _extrair_data(self, html_pagina: str) -> Optional[datetime]:
        # A primeira ocorrência do meta datePublished é sempre a da matéria
        # em si; ocorrências seguintes podem pertencer a vídeos embutidos no
        # corpo do texto, com data própria diferente da publicação da matéria.
        data_match = self._DATA_PUBLICACAO_RE.search(html_pagina)
        if data_match is None:
            return None
        try:
            return datetime.fromisoformat(data_match.group(1))
        except ValueError:
            return None


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


class FormatadorData:
    """Formata a data de publicação para exibição, no padrão dd/mm/aaaa HHhMM."""

    def formatar(self, publicado_em: Optional[datetime]) -> str:
        if publicado_em is None:
            return "data não disponível"
        return publicado_em.strftime("%d/%m/%Y %Hh%M")


class CacheListagem:
    """Cache em memória, por editoria e página, com TTL — evita varrer as
    páginas do G1 de novo a cada chamada. 10 minutos é o mesmo padrão usado
    no mcp-sofiasa."""

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
    """Orquestra a busca: escolhe as editorias, pagina até achar candidatos
    suficientes, busca cada matéria candidata, ordena por data de publicação
    real e monta o texto final devolvido pela tool.

    A busca sempre tenta entregar `quantidade` notícias (padrão e mínimo
    esperado: 3). Para isso, ela não para na primeira página de cada
    editoria: se os candidatos encontrados na página 1 não forem
    suficientes, ela busca a página 2, depois a 3, e assim por diante, até
    `max_paginas` ou até o orçamento de tempo se esgotar. Isso cobre temas
    cuja notícia mais recente não está no topo da editoria (ex: um assunto
    de nicho, como turismo ou loterias, que publica com menos frequência).
    """

    def __init__(
        self,
        registro_editorias: RegistroEditorias,
        cliente_http: ClienteHttpG1,
        extrator_listagem: ExtratorListagemG1,
        extrator_materia: ExtratorMateriaG1,
        montador_resumo: MontadorResumo,
        formatador_data: FormatadorData,
        cache: CacheListagem,
        max_paginas: int = 5,
        orcamento_segundos: float = 25.0,
        concorrencia_maxima: int = 8,
    ) -> None:
        self._registro_editorias = registro_editorias
        self._cliente_http = cliente_http
        self._extrator_listagem = extrator_listagem
        self._extrator_materia = extrator_materia
        self._montador_resumo = montador_resumo
        self._formatador_data = formatador_data
        self._cache = cache
        self._max_paginas = max_paginas
        self._orcamento_segundos = orcamento_segundos
        self._concorrencia_maxima = concorrencia_maxima

    async def buscar(self, tema: str, editoria: str = "", quantidade: int = 3) -> str:
        tema = tema.strip()
        if not tema:
            return "Informe um tema para buscar notícias do G1 (ex: 'eleições', 'saúde mental')."
        # Guardrail rígido: esta tool nunca devolve mais de MAXIMO_NOTICIAS_POR_CHAMADA
        # notícias numa única chamada, mesmo que o cliente MCP peça um valor maior.
        quantidade = max(1, min(quantidade, MAXIMO_NOTICIAS_POR_CHAMADA))

        if editoria.strip():
            alvo = self._registro_editorias.buscar(editoria)
            if alvo is None:
                chaves = ", ".join(e.chave for e in self._registro_editorias.todas)
                return f"Editoria '{editoria}' não é permitida. Editorias válidas: {chaves}."
            editorias_alvo = [alvo]
        else:
            editorias_alvo = self._registro_editorias.todas

        filtro = FiltroPorTema(tema, GeradorPalavrasChave())

        async with httpx.AsyncClient() as cliente:
            resolvidos, houve_candidato = await self._buscar_resolvidos(cliente, editorias_alvo, filtro, quantidade)

        if not resolvidos:
            if houve_candidato:
                return (
                    f"Notícias sobre '{tema}' foram encontradas no G1, mas não foi possível "
                    "acessar o conteúdo completo agora. Tente novamente em instantes."
                )
            return (
                f"Não foram encontradas notícias sobre '{tema}' nas editorias do G1 "
                "permitidas para busca, mesmo vasculhando várias páginas de cada "
                "editoria. Tente um tema mais amplo, ou use a tool g1_listar_editorias "
                "para ver as editorias disponíveis."
            )

        resolvidos.sort(key=lambda resolvido: resolvido[1].publicado_em or _DATA_MUITO_ANTIGA, reverse=True)
        selecionados = resolvidos[:quantidade]
        return self._formatar_resposta(tema, quantidade, selecionados)

    async def _buscar_resolvidos(
        self,
        cliente: httpx.AsyncClient,
        editorias_alvo: list[Editoria],
        filtro: FiltroPorTema,
        quantidade: int,
    ) -> tuple[list[tuple[ItemNoticia, MateriaG1, str]], bool]:
        """Varre página a página, resolvendo cada candidata encontrada, até
        ter `quantidade` notícias resolvidas de verdade — não apenas
        candidatas brutas. Continuar paginando enquanto faltar é o que
        garante a entrega das 3 notícias mesmo quando algumas candidatas
        falham ao carregar (o resultado final é sempre baseado no que
        realmente carregou, nunca numa contagem otimista de candidatas)."""
        resolvidos: list[tuple[ItemNoticia, MateriaG1, str]] = []
        urls_vistas: set[str] = set()
        houve_candidato = False
        inicio = time.monotonic()

        for pagina in range(1, self._max_paginas + 1):
            listas = await asyncio.gather(
                *(self._itens_da_pagina(cliente, editoria_alvo, pagina) for editoria_alvo in editorias_alvo)
            )
            novos = []
            for lista in listas:
                for item in lista:
                    if item.url not in urls_vistas and filtro.combina(item):
                        urls_vistas.add(item.url)
                        novos.append(item)

            if novos:
                houve_candidato = True
                # Só resolve o necessário para cobrir o que falta (com uma
                # folga de 3x, para compensar falhas de carregamento) — não
                # gasta requisição resolvendo candidatas além do preciso.
                faltam = max(quantidade - len(resolvidos), 0)
                necessarios = max(faltam * 3, 3)
                resolvidos.extend(await self._resolver_materias(cliente, novos[:necessarios]))

            tempo_esgotado = (time.monotonic() - inicio) >= self._orcamento_segundos
            if len(resolvidos) >= quantidade or tempo_esgotado:
                break

        return resolvidos, houve_candidato

    async def _resolver_materias(
        self, cliente: httpx.AsyncClient, candidatos: list[ItemNoticia]
    ) -> list[tuple[ItemNoticia, MateriaG1, str]]:
        """Busca cada matéria candidata em paralelo (com limite de
        concorrência, para não sobrecarregar o G1) e descarta as que
        falharam ao carregar ou ficaram sem texto para resumir."""
        semaforo = asyncio.Semaphore(self._concorrencia_maxima)
        brutos = await asyncio.gather(
            *(self._materia_e_resumo_limitada(cliente, semaforo, item.url) for item in candidatos)
        )
        return [
            (item, materia, resumo)
            for item, (materia, resumo) in zip(candidatos, brutos)
            if materia is not None and resumo
        ]

    def _formatar_resposta(
        self,
        tema: str,
        quantidade: int,
        selecionados: list[tuple[ItemNoticia, MateriaG1, str]],
    ) -> str:
        blocos = [f"Notícias do G1 sobre '{tema}', da mais recente para a mais antiga:\n"]
        for indice, (item, materia, resumo) in enumerate(selecionados, start=1):
            data_formatada = self._formatador_data.formatar(materia.publicado_em)
            blocos.append(
                f"{indice}. {item.titulo}\n"
                f"Publicado em: {data_formatada}\n"
                f"Resumo: {resumo}\n"
                f"Link: {item.url}\n"
            )
        if len(selecionados) < quantidade:
            blocos.append(
                f"(Encontrei {len(selecionados)} notícia(s) sobre '{tema}' nas editorias "
                "permitidas — não há mais cobertura disponível agora, mesmo depois de "
                "vasculhar várias páginas.)"
            )
        return "\n".join(blocos)

    async def _itens_da_pagina(
        self, cliente: httpx.AsyncClient, editoria: Editoria, pagina: int
    ) -> list[ItemNoticia]:
        chave_cache = f"{editoria.chave}:{pagina}"
        em_cache = self._cache.obter(chave_cache)
        if em_cache is not None:
            return em_cache
        html_pagina = await self._cliente_http.buscar_html(cliente, editoria.url_da_pagina(pagina))
        itens_brutos = self._extrator_listagem.extrair(html_pagina) if html_pagina else []
        itens = [item for item in itens_brutos if editoria.pertence(item.url)]
        self._cache.guardar(chave_cache, itens)
        return itens

    async def _materia_e_resumo_limitada(
        self, cliente: httpx.AsyncClient, semaforo: asyncio.Semaphore, url: str
    ) -> tuple[Optional[MateriaG1], str]:
        async with semaforo:
            return await self._materia_e_resumo(cliente, url)

    async def _materia_e_resumo(self, cliente: httpx.AsyncClient, url: str) -> tuple[Optional[MateriaG1], str]:
        html_pagina = await self._cliente_http.buscar_html(cliente, url)
        if not html_pagina:
            return None, ""
        materia = self._extrator_materia.extrair(html_pagina)
        if materia is None:
            return None, ""
        return materia, self._montador_resumo.montar(materia)


# --- Composição (instâncias únicas usadas pelas tools) ---------------------

_limpador_html = LimpadorHtml()
_registro_editorias = RegistroEditorias(EDITORIAS_PADRAO)
_buscador_noticias = BuscadorNoticiasG1(
    registro_editorias=_registro_editorias,
    cliente_http=ClienteHttpG1(),
    extrator_listagem=ExtratorListagemG1(_limpador_html),
    extrator_materia=ExtratorMateriaG1(_limpador_html),
    montador_resumo=MontadorResumo(limite_palavras=150),
    formatador_data=FormatadorData(),
    cache=CacheListagem(ttl_segundos=600),
    max_paginas=5,
    orcamento_segundos=25.0,
    concorrencia_maxima=8,
)


@mcp.tool(
    name="noticias_g1",
    annotations=ToolAnnotations(
        title="Buscar as notícias mais recentes do G1 por tema",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def noticias_g1(tema: str, editoria: str = "", quantidade: int = 3) -> str:
    """Busca notícias reais e recentes do G1, como fonte confiável para pesquisa de fontes.

    Use esta tool sempre que precisar de notícias verdadeiras e verificáveis
    do G1 sobre um assunto — por exemplo, antes de escrever um texto, para
    citar uma fonte jornalística confiável em vez de arriscar uma informação
    inventada ou desatualizada. Não use busca genérica na internet para esse
    fim: esta tool já garante que o link devolvido é uma matéria real do G1.

    A tool varre as editorias autorizadas do G1 (ver a tool
    g1_listar_editorias), avançando página por página quando necessário até
    reunir notícias suficientes, e ordena os resultados pela data de
    publicação real — a mais recente primeiro. Ela sempre tenta devolver o
    número de notícias pedido em `quantidade`; só devolve menos se não
    existir cobertura suficiente sobre o tema nas editorias permitidas.

    Limite rígido: esta tool NUNCA devolve mais de 3 notícias numa única
    chamada, mesmo que `quantidade` peça um valor maior — o servidor corta
    o valor para 3 antes de buscar. Se o pedido da pessoa usuária precisar
    de mais de 3 notícias, chame esta tool mais de uma vez (por exemplo,
    uma vez por editoria ou por sub-tema) em vez de esperar um `quantidade`
    maior.

    Args:
        tema: Assunto a pesquisar (ex: "eleições", "inteligência artificial").
        editoria: Chave de uma editoria específica para restringir a busca
            (ex: "tecnologia", "saude"). Deixe em branco para buscar nas 16
            editorias permitidas.
        quantidade: Quantas notícias retornar (padrão e máximo 3).

    Returns:
        Texto com, para cada notícia, da mais recente para a mais antiga:
        título, data de publicação, resumo de até 150 palavras e o link
        obrigatório da matéria original, para a pessoa usuária conferir a
        fonte na íntegra. Se nada for encontrado, explica isso em vez de
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
