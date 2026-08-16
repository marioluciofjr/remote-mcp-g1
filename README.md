# mcp-g1

[![Made with Python](https://img.shields.io/badge/Python->=3.10-blue?logo=python&logoColor=white)](https://python.org "Ir para a página do Python")
![license - MIT](https://img.shields.io/badge/license-MIT-green)
![site - prazocerto.me](https://img.shields.io/badge/site-prazocerto.me-230023)
![linkedin - @marioluciofjr](https://img.shields.io/badge/linkedin-marioluciofjr-blue)

## Índice

* [Introdução](#introdução)
* [Sobre o G1](#sobre-o-g1)
* [Estrutura do projeto](#estrutura-do-projeto)
* [Tecnologias utilizadas](#tecnologias-utilizadas)
* [Requisitos](#requisitos)
* [Como instalar no Gemini Spark](#como-instalar-no-gemini-spark)
* [Como instalar no Claude Web](#como-instalar-no-claude-web)
* [Como instalar no ChatGPT](#como-instalar-no-chatgpt)
* [Links úteis](#links-úteis)
* [Contribuições](#contribuições)
* [Licença](#licença)
* [Contato](#contato)

## Introdução

O **mcp-g1** é um servidor remoto que implementa o Model Context Protocol (MCP). Ele busca notícias reais do portal G1 (g1.globo.com) a partir de um tema. Qualquer cliente MCP compatível chama essa busca em tempo real, pelo URL público do servidor.

Este MCP existe para um caso de uso específico: dar a uma IA generativa uma fonte de notícias confiável para pesquisar antes de escrever um texto. Em vez de a IA inventar ou citar uma fonte duvidosa, ela consulta o G1 ao vivo e recebe o resumo da notícia com o link da matéria original.

O servidor usa o transporte Streamable HTTP e roda na nuvem, na Vercel, no URL `https://remote-mcp-g1.vercel.app/mcp`.

> [!IMPORTANT]
> Esse URL só aceita pedidos `POST` e `DELETE`, no formato do protocolo MCP. Se você colar o URL no navegador, ele faz um pedido `GET` e mostra a mensagem "Method Not Allowed". Isso é esperado, não é um erro. Confirma só que o servidor está no ar. Use o URL dentro de um cliente MCP, não direto no navegador.

> [!IMPORTANT]
> Cada cliente MCP decide, por conta própria, quando chamar as tools deste servidor — essa decisão não é controlada por este projeto. Em teste real, o Claude reconheceu e chamou as tools do mcp-g1 mesmo sem citação direta ao app. Já o Gemini e o ChatGPT, às vezes, só chamam a tool se você citar o nome do app no prompt. Se a IA não usar o MCP mesmo com o app já conectado, cite o nome do app diretamente. Exemplo: `@G1 Quero fazer um texto sobre redações nota 1000 do Enem. Busque notícias no G1 sobre isso.`

> [!NOTE]
> Cada cliente formata a resposta da tool à sua própria maneira. Gemini, Claude e ChatGPT têm layouts diferentes para apresentar o mesmo conjunto de notícias — um pode usar tabela, outro lista, outro um resumo com mais contexto ao redor. Essa é uma reformatação do cliente em cima do texto que a tool devolveu, não uma diferença no conteúdo da busca. Em qualquer cliente, a tool `noticias_g1` nunca devolve mais de 3 notícias por chamada — esse limite é fixo no servidor, não depende do cliente respeitar um pedido de mais notícias.

## Sobre o G1

O G1 é o portal de notícias do Grupo Globo. O Mário Lúcio desenvolveu este MCP de forma independente, como projeto de teste técnico. Este projeto não tem afiliação com a Globo Comunicação e Participações S.A., nem é endossado por ela.

O servidor busca notícias só nas 16 editorias abaixo, escolhidas por não terem conteúdo esportivo nem de entretenimento leve:

Agro, Ciência, Economia, Educação, Empreendedorismo, Fato ou Fake, Inovação, Loterias, Meio Ambiente, Mundo, Política, Pop & Arte, Saúde, Tecnologia, Trabalho e Carreira, Turismo e Viagem.

## Estrutura do projeto

É um MCP-Server simples que utiliza o pacote [FastMCP](https://gofastmcp.com), seguindo também as orientações do repositório oficial do [Model Context Protocol](https://github.com/modelcontextprotocol/python-sdk), da Anthropic.

Este MCP-Server tem duas tools:

### `noticias_g1` (Tool)

Busca notícias do G1 relacionadas a um tema. A tool varre as editorias permitidas e devolve, para cada notícia encontrada, o título, a data de publicação, um resumo de até 150 palavras e o link da matéria original — da mais recente para a mais antiga.

* **Parâmetro obrigatório**: `tema`. Palavra ou frase a pesquisar nos títulos e nas chamadas das notícias (ex: "eleições", "inteligência artificial"). A tool amplia o tema em até 10 variações (plural, singular e radical de cada palavra), para não depender do termo exato.
* **Parâmetro opcional**: `editoria`. Restringe a busca a uma única editoria (ex: `tecnologia`, `saude`). Em branco, a tool busca nas 16 editorias permitidas.
* **Parâmetro opcional**: `quantidade`. Define quantas notícias retornar. O padrão e o máximo são 3 — esse limite é fixo no servidor. Mesmo que o cliente MCP peça um valor maior, a tool corta para 3 antes de buscar.
* **Busca em profundidade**: a tool sempre tenta entregar o número de notícias pedido. Se a primeira página de uma editoria não tiver candidatas suficientes, ela avança para as páginas seguintes (até 5 por editoria, ou até um limite de tempo de 25 segundos), em vez de parar cedo. Só devolve menos que o pedido se não existir cobertura suficiente sobre o tema.
* **Guardrail de editoria**: só entram no resultado links que pertencem mesmo à editoria de onde foram coletados — páginas de listagem do G1 também têm blocos de "veja também" apontando para outras seções, e esses são descartados.
* **Cache de 10 minutos**: a tool guarda cada página de cada editoria por 10 minutos, para não sobrecarregar o G1 a cada chamada.
* **Sem notícia encontrada**: a tool devolve uma mensagem explicando isso, em vez de inventar uma notícia.

### `g1_listar_editorias` (Tool)

Lista as 16 editorias permitidas, com nome, chave e URL. Essa tool não faz scraping — é uma lista fixa. Use-a para descobrir a chave certa para o parâmetro `editoria` de `noticias_g1`.

## Tecnologias utilizadas

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)
![FastMCP](https://img.shields.io/badge/FastMCP-servidor%20MCP-000000)
![Starlette](https://img.shields.io/badge/Starlette-ASGI-052F5F)
![Uvicorn](https://img.shields.io/badge/Uvicorn-servidor%20ASGI-2A6DB2)
![httpx](https://img.shields.io/badge/httpx-cliente%20HTTP%20ass%C3%ADncrono-0B6E4F)
![Vercel](https://img.shields.io/badge/Vercel-deploy-black?logo=vercel&logoColor=white)

* **Python** — linguagem do servidor.
* **FastMCP** — framework que implementa o protocolo MCP e expõe as tools via Streamable HTTP.
* **Starlette** — aplicação ASGI por baixo do FastMCP; aqui, acrescenta o CORS aberto para clientes remotos.
* **Uvicorn** — servidor ASGI usado para rodar o projeto localmente.
* **httpx** — busca as páginas do G1, em tempo real, a cada chamada da tool `noticias_g1`.
* **Vercel** — hospeda o servidor remoto e disponibiliza o URL público.

## Requisitos

Para **usar** o servidor a partir de um cliente MCP (Gemini Spark, Claude Web ou ChatGPT), você não precisa instalar nada. Basta um cliente que aceite um servidor MCP remoto via Streamable HTTP, e o URL público deste servidor.

Para **rodar o projeto localmente** (desenvolvimento ou testes), instale antes:

* [Python 3.10](https://www.python.org/downloads/) ou superior.
* As dependências do projeto: `pip install -r requirements.txt`.

## Como instalar no Gemini Spark

O Gemini Spark é o modo agêntico do Gemini App.

1. Acesse a barra lateral do Gemini Web e clique em "Spark".
2. Clique na aba "Apps Conectados".
3. Desça a barra de rolagem e clique no botão "Adicionar app personalizado".
4. Cole o link do MCP (`https://remote-mcp-g1.vercel.app/mcp`) no espaço "Adicione um link de app personalizado".
5. Clique no botão "Avançar".
6. Desça a barra de rolagem da nova tela e marque a caixa de seleção que tem a mensagem "Entendo e aceito os riscos de segurança e privacidade ao conectar este app personalizado".
7. Clique no botão "Conectar" e aguarde a próxima tela.
8. Aparecerá uma tela chamada "Salvar app personalizado". Você pode editar o nome do app.
9. Depois de conferir se está tudo certo e a tool estar listada, clique no botão "Conectar".

> Você saberá que está tudo certo se o MCP aparecer como um novo app em "Apps personalizados para o Spark".

## Como instalar no Claude Web

1. Na barra lateral do Claude Web, clique em "Personalizar".
2. Escolha a aba "Conectores".
3. Clique no botão "Adicionar" e escolha a opção "Adicionar conector personalizado".
4. Dê um nome para o conector.
5. Cole o link do MCP (`https://remote-mcp-g1.vercel.app/mcp`) no espaço abaixo do nome que escolheu na etapa 4.
6. Clique no botão "Adicionar".
7. Clique no botão "Vincular".
8. Clique no botão "Requer aprovação" e mude para "Sempre permitir".

## Como instalar no ChatGPT

1. Na barra lateral, clique em "Plugins".
2. Clique no botão "+", que fica do lado de "Pesquisar plugins".
3. Na tela "Novo plugin", dê um nome no espaço "Nome".
4. Em "Conexão", cole o link do MCP (`https://remote-mcp-g1.vercel.app/mcp`) e deixe a opção "URL do Servidor" habilitada.
5. Em "Autenticação", escolha a opção "Sem autenticação".
6. Clique na caixa de seleção "Entendi e quero continuar".
7. Clique no botão "Criar".
8. Na nova tela, clique no botão "Conectar".

## Links úteis

* [Documentação oficial do Model Context Protocol](https://modelcontextprotocol.io/introduction) - Todos os detalhes desse protocolo da Anthropic.
* [Documentação oficial do FastMCP](https://gofastmcp.com) - Framework usado para construir o servidor MCP deste projeto.
* [Documentação da Vercel para Python](https://vercel.com/docs/functions/runtimes/python) - Como a Vercel executa uma aplicação Python/ASGI.
* [Portal G1](https://g1.globo.com) - Fonte das notícias buscadas por este servidor.
* [Site oficial da Anthropic](https://www.anthropic.com/) - Novidades e estudos dos modelos Claude.

## Contribuições

Contribuições são bem-vindas! Se você tiver ideias para melhorar este projeto, sinta-se à vontade para abrir um fork do repositório.

## Licença

Este projeto está licenciado sob a licença MIT. Veja o arquivo [LICENSE](LICENSE) para mais detalhes.

## Contato

Mário Lúcio - Prazo Certo®
<div>
  <a href="https://www.linkedin.com/in/marioluciofjr" target="_blank"><img src="https://img.shields.io/badge/-LinkedIn-%230077B5?style=for-the-badge&logo=linkedin&logoColor=white"></a>
  <a href = "mailto:marioluciofjr@gmail.com" target="_blank"><img src="https://img.shields.io/badge/-Gmail-%23333?style=for-the-badge&logo=gmail&logoColor=white"></a>
  <a href="https://prazocerto.me/contato" target="_blank"><img src="https://img.shields.io/badge/prazocerto.me/contato-230023?style=for-the-badge&logo=wordpress&logoColor=white"></a>
</div>
