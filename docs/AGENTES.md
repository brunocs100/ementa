# Agentes

Catálogo dos agentes e das ferramentas da Ementa: o que cada um faz, como o grafo é montado, que modelo usa, que guardrails passam por ele e como é avaliado.

**Status:** Documentado (código a partir da F1) · **Ver também:** [Arquitetura](ARQUITETURA.md), [Plano](PLANO.md), [ADR 0002](adr/0002-langgraph-como-orquestrador.md), [Segurança e governança](SEGURANCA-E-GOVERNANCA.md)

## Visão geral

| Agente | id | Padrão | Fase | Status |
|---|---|---|---|---|
| Pesquisa jurídica | `legal_research` | RAG agêntico com reflexão | F1, F2, F5 | Documentado |
| Assistente | `assistant` | Supervisor que roteia | F3 | Implementado |
| Análise de contratos | `contract_review` | Map-reduce por cláusula | F4 | Implementado |
| Redação de peças | `brief_drafting` | Plan-and-execute com crítico e HITL | F4 | Documentado |
| Prazos e processos | `case_monitor` | Workflow agendado | F5 | Documentado |
| Triagem e conflito | `intake` | Extração estruturada e verificação em grafo | F5 | Documentado |
| BI do escritório | `firm_analytics` | Texto para SQL com guarda | F6 | Documentado |
| Compliance LGPD | `lgpd_compliance` | Agente Semantic Kernel consumindo o MCP | F8 | Documentado |
| Revisor de PR | `pr_reviewer` | Workflow no GitHub Actions | F8 | Documentado |

Ferramenta isolada que existe desde a F1: `calculate_deadline`, exposta no MCP e reaproveitada pelo `case_monitor` na F5.

Os agentes seguem três regras que valem para todos:

- O modelo nunca calcula: prazo, valor e contagem saem de ferramenta determinística testada.
- Toda resposta com citação passa pelo verificador antes de sair.
- Ação que sai do escritório (e-mail ao cliente, peça final, comentário público) para num `interrupt()` e espera aprovação humana.

## O que todo agente recebe do `AgentFactory`

O `AgentFactory` (padrão Builder, no `ementa_kit`) monta cada agente a partir de uma especificação curta. Quem escreve um agente novo declara o grafo, as ferramentas e o papel do modelo; o resto vem pronto.

| Recurso | O que o factory faz | Onde |
|---|---|---|
| Modelo por papel | Resolve `reasoning`, `default`, `fast`, `judge` ou `local` pelo registro de modelos, já com effort, timeout e retry | `ementa_kit.llm` |
| Política antes da chamada | Consulta o motor de políticas: sigilo do processo pode trocar a rota para `local` | `ementa_kit.policy` |
| Ferramentas com guardrails | Embrulha cada ferramenta com schema estrito, lista permitida por papel, nível de risco e auditoria | `ementa_kit.guardrails` |
| Contexto | Monta a visão que vai ao modelo, com orçamento por seção e histórico só de acréscimo | `ementa_kit.context` |
| Memória | Liga checkpointer e store do LangGraph no Postgres, com namespaces e permissão herdada do processo | `ementa_kit.memory` |
| Telemetria | Abre spans `invoke_agent`, `chat`, `execute_tool`, `retrieval` e `guardrail` com atributos `gen_ai.*` e `ementa.*` | `ementa_kit.telemetry` |
| Auditoria | Grava execução, ferramentas chamadas e decisões de guardrail na trilha com hash encadeado | `ementa_kit.audit` |
| Orçamento | Soma o custo por execução e para tudo quando o teto do dia ou do mês estoura | `ementa_kit.llm` |

```python
# Esboço da especificação de um agente (F1); nomes finais definidos no código.
spec = AgentSpec(
    name="legal_research",
    model_role="default",
    tools=[search_legal_corpus, get_statute_article],
    output_guardrails=["citations", "draft_disclaimer"],
    memory=None,  # entra na F3
)
graph = AgentFactory(settings).build(spec, build_graph=legal_research_graph)
```

## Pesquisa jurídica (`legal_research`)

**Objetivo.** Responder a perguntas de direito com base no corpus público, citando só fontes que existem e que estavam no contexto da resposta.

**Gatilho e canal.** Pergunta vinda do Copilot ou do Claude pelo `ementa-mcp` (ferramenta `answer_legal_question`, F1), da rota `POST /v1/legal-research` (F2) ou do `assistant` (F3).

**Padrão e por quê.** RAG agêntico com reflexão. A busca é feita por código, não pelo modelo escolhendo ferramenta a cada passo: o caminho é previsível, barato e fácil de testar. A reflexão é um laço curto: se o verificador reprova a resposta, o grafo reescreve as consultas com o motivo da falha e tenta mais uma vez.

### Grafo

```mermaid
flowchart TD
    A["parse_question"] -->|"citação explícita"| B["direct_lookup"]
    A -->|"pergunta aberta"| C["rewrite_queries"]
    C --> D["retrieve"]
    D --> E["assemble_context"]
    B --> E
    E --> F["generate_answer"]
    F --> G["verify_citations"]
    G -->|"falhou e attempts menor que 2"| C
    G -->|"aprovado ou sem tentativas"| H["finalize"]
```

- `parse_question`: detecta citação explícita ("art. 49 do CDC") e normaliza para id canônico (`cdc:art49`).
- `direct_lookup`: busca o artigo pelo id, sem passar pelo modelo.
- `rewrite_queries`: gera consultas com sinônimos jurídicos ("CDC" e "Código de Defesa do Consumidor"); na segunda volta, usa o motivo da reprovação.
- `retrieve`: busca vetorial e textual (top 30 cada) com fusão RRF; rerank para top 8 a partir da F2; a partir da F5, `expand_citation_graph` entra aqui e traz súmulas e decisões ligadas ao artigo.
- `assemble_context`: monta o contexto com os ids de fonte, dentro do orçamento de tokens.
- `generate_answer`: responde em Structured Outputs, citando só ids de fonte do contexto.
- `verify_citations`: confere cada citação contra as fontes que foram para o contexto; citação fora delas sai, mesmo que exista no corpus.
- `finalize`: remove a citação não verificada, avisa o usuário e põe o aviso de rascunho.

### Estado

| Campo | Tipo | Conteúdo |
|---|---|---|
| `question` | `str` | Pergunta original |
| `user_role` | `str` | Papel de quem pergunta (filtra fontes por permissão) |
| `queries` | `list[str]` | Consultas geradas por `rewrite_queries` |
| `candidates` | `list[Chunk]` | Resultado bruto da busca, com pontuação |
| `sources` | `list[Source]` | Fontes que entraram no contexto, com id canônico |
| `draft` | `Answer` | Resposta estruturada do modelo |
| `citations` | `list[str]` | Ids citados no rascunho |
| `verification` | `Verification` | `verified` e `unverified` |
| `attempts` | `int` | Tentativas de geração |
| `final_answer` | `Answer` | Resposta entregue |
| `cost_usd` | `float` | Custo acumulado da execução |

### Ferramentas

| Ferramenta | Tipo | Guardrails |
|---|---|---|
| `search_legal_corpus` | Leitura | Schema estrito, classe de dado público, limite de resultados |
| `get_statute_article` | Leitura | Schema estrito, id canônico validado |
| `search_sumulas` (F2) | Leitura | Schema estrito, classe de dado público |
| `expand_citation_graph` (F5) | Leitura | Profundidade máxima, classe de dado público |

### Modelo, memória e HITL

- Papel `fast` (GPT-6 Luna) em `rewrite_queries`; papel `default` (GPT-6 Luna, effort medium, por custo) em `generate_answer`.
- Sem memória na F1. A partir da F3, lê preferências do advogado (formato de citação) no namespace dele.
- Sem HITL: a resposta é informativa e sempre marcada como rascunho.

### Exemplo de saída

<details><summary>Resposta estruturada (formato previsto)</summary>

```json
{
  "answer": "O consumidor pode desistir em 7 dias, contados da assinatura ou do recebimento, quando a compra é feita fora do estabelecimento comercial.",
  "citations": [{"id": "cdc:art49", "label": "CDC, art. 49", "verified": true}],
  "removed_citations": [],
  "disclaimer": "Rascunho gerado por IA. Confira antes de usar.",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"
}
```

</details>

### Métricas e avaliação

- Recall@5 e MRR da busca; precisão e recall de citação; taxa de citação removida; latência p95; custo por pergunta.
- F1: 10 perguntas de fumaça sobre o CDC, rodadas à mão. F2: 30 perguntas no CI, com gate contra a linha de base do `main`. Detalhes em [Avaliação](AVALIACAO.md).
- Meta: recall@5 ≥ 0,85 e 100% das citações exibidas verificadas.

**Custo estimado:** cerca de US$ 0,015 por pergunta (estimativa, conferir na F1). **Fase:** F1 (núcleo), F2 (rerank, súmulas, API), F5 (grafo). **Status:** Documentado.

## Ferramenta `calculate_deadline`

Ferramenta determinística, sem LLM. Existe para que nenhum modelo conte dias úteis. Exposta no MCP desde a F1 e usada pelo `case_monitor` na F5.

### Contrato

| Entrada | Tipo | Exemplo |
|---|---|---|
| `start_date` | data | `2026-12-03` (data da intimação) |
| `days` | inteiro | `15` |
| `count_mode` | `business_days` ou `calendar_days` | `business_days` |
| `court` | texto | `"TJRS"` |
| `state` | UF | `"RS"` |

| Saída | Conteúdo |
|---|---|
| `due_date` | Último dia do prazo |
| `skipped` | Dias pulados, com motivo (fim de semana, feriado, suspensão) |
| `rules_applied` | CPC art. 219 (dias úteis), art. 224 (exclui o dia do começo e inclui o do vencimento), art. 220 (suspensão de 20/12 a 20/01) |
| `assumptions` | Termo inicial informado pelo usuário; feriados municipais e portarias do tribunal fora do cálculo |
| `disclaimer` | Aviso de conferência pelo advogado |

### Caso fixo da demo

Intimação na quinta, 03/12/2026, 15 dias úteis no TJRS. O vencimento é terça, 26/01/2027.

```mermaid
flowchart LR
    A["03/12 intimação"] --> B["04/12 a 18/12 dias 1 a 11"]
    B --> C["20/12 a 20/01 suspensão do art. 220"]
    C --> D["21/01 a 26/01 dias 12 a 15"]
    D --> E["26/01/2027 vencimento"]
```

Esse caso vira teste fixo na F1. Além dele, testes por propriedade com Hypothesis garantem que o vencimento nunca cai em fim de semana, feriado ou suspensão, e que aumentar `days` nunca antecipa o vencimento.

**Guardrails:** schema estrito; `days` entre 1 e 365; UF e tribunal de uma lista conhecida. **Classe de dado:** público. **Fase:** F1. **Status:** Documentado.

## Catálogo de ferramentas MCP

O que o `ementa-mcp` expõe a clientes externos (Copilot, Claude, agente SK). Cada ferramenta declara a classe de dado que devolve; a política de saída pseudonimiza o restrito e nega o sigiloso.

| Ferramenta | O que faz | Classe de dado | Agente ou módulo por trás | Fase |
|---|---|---|---|---|
| `search_legal_corpus` | Busca híbrida no corpus público | Público | RAG do `legal_research` | F1 |
| `answer_legal_question` | Resposta com citações verificadas | Público | `legal_research` | F1 |
| `get_statute_article` | Texto exato de um artigo | Público | RAG | F1 |
| `calculate_deadline` | Prazo processual com premissas | Público | Ferramenta determinística | F1 |
| `list_skills` | Nome e descrição das skills de runtime, sem as instruções | Público | `ementa_kit.skills` | F2 |
| `load_skill` | Instruções completas de uma skill, pelo nome | Público | `ementa_kit.skills` | F2 |
| `review_contract` | Análise de contrato, como tarefa longa | Restrito | `contract_review` | F4 |
| `lookup_case` | Metadados e movimentações recentes de um processo pelo número CNJ | Restrito; sigiloso negado | `DataJudClient` e `case_monitor` | F5 |
| `check_conflict` | Verifica conflito de interesses para um nome ou documento | Restrito | `check_conflict_graph` e `check_conflict_sql` do `intake` | F5 |
| `query_firm_analytics` | Pergunta de gestão em SQL guardado | Restrito | `firm_analytics` | F6 |

O `assistant` não é exposto por MCP; o cliente externo chama as ferramentas diretamente.

## Assistente (`assistant`)

**Objetivo.** Porta de entrada única de chat: entende o pedido e delega ao agente certo.

**Gatilho e canal.** Rota `POST /v1/chat` com SSE (F3) e, na F4, o chat do console. O histórico de uma conversa sai em `GET /v1/threads/{id}`, só para quem a abriu.

**Padrão e por quê.** Supervisor que roteia por chamada de ferramenta. A cada turno, o modelo `fast` recebe as ferramentas que o papel de quem pergunta pode usar e decide se chama o `legal_research`, o `calculate_deadline` ou responde direto (fora do escopo, pedido de dado que falta). O mesmo modelo escreve a resposta com o que a ferramenta devolveu. Cada subagente roda na própria conversa com o modelo: o `legal_research` monta o próprio contexto com as fontes e não grava nada na conversa do chat, o que isola contexto e mantém o histórico só de acréscimo em cada uma.

Na F3, o roteamento por function calling substituiu o nó de classificação com rota e confiança do desenho: é uma chamada a menos por turno e não precisa de um nó para compor a resposta. A troca é perder o número de confiança do roteamento; a acurácia fica para o conjunto rotulado. Os agentes das fases seguintes (`contract_review`, `brief_drafting`, `case_monitor`, `firm_analytics`) entram como ferramentas novas do mesmo supervisor.

```mermaid
flowchart TD
    A["mensagem do usuário"] -->|"PII vira marcador"| M["memórias relevantes, com os mesmos marcadores"]
    M -->|"na mensagem do turno, antes da pergunta"| B["supervisor, modelo fast"]
    B -->|"chamou ferramenta"| C["tools: legal_research ou calculate_deadline"]
    C -->|"resultado como mensagem nova"| B
    B -->|"respondeu"| D["finalize: confere as citações"]
    D --> E["evento done, com os valores reais"]
    E --> W["escritor de memória: o que o turno ensinou"]
```

**Estado** (no checkpointer, uma thread por conversa):

| Campo | Conteúdo |
|---|---|
| `messages` | Tudo o que o modelo viu, pseudonimizado e só de acréscimo, inclusive o que ficou antes de uma compactação; o prompt de sistema é a primeira mensagem e congela quando a conversa começa |
| `view_start` | Depois de uma compactação, a posição do último resumo em `messages`: a visão do modelo é o prompt de sistema e o que vem dali em diante |
| `pii_mapping` | Marcador e valor de cada dado pessoal da conversa, os das memórias inclusive; só cresce, e o mesmo valor mantém o marcador em todos os turnos |
| `owner_id`, `prompt` | Quem abriu a conversa e o id e a versão do prompt congelado |
| `allowed_tools`, `rounds` | Ferramentas do turno, pelo papel de quem pergunta, e chamadas ao modelo no turno |
| `question` | A mensagem do turno, pseudonimizada e sem as memórias que vieram com ela: é o que o histórico mostra |
| `sources` | Fontes verificadas pelo `legal_research` na conversa |
| `turns` | Cada turno terminado como o usuário vê, ainda com marcadores: reidratado na leitura |

| Ferramenta | Tipo | Guardrails |
|---|---|---|
| `legal_research` | Subagente, leitura | Lista permitida por papel; a pergunta chega já pseudonimizada; citações verificadas dentro dele e de novo na resposta final |
| `calculate_deadline` | Leitura, determinística | Schema estrito; o vencimento vem do código, e o modelo nunca conta dias |

- **Modelo:** `fast` no roteamento e na resposta (GPT-6 Luna, effort low). Na F9, pode passar para o modelo local ajustado.
- **Prompt:** `assistant.supervisor` v1 em `prompts/assistant/`. Uma conversa aberta continua com o prompt da versão em que começou. As seções `memorias` e `resumo` chegam delimitadas por tags e o v1 não fala delas; uma v2 que as explique depende de rodar a avaliação.
- **Visão do modelo:** derivada do estado, sem editar nada: cada pedido começa com o pedido anterior inteiro (teste com `assert_append_only`, inclusive depois de reiniciar o processo). Resultado de ferramenta é truncado em 6.000 caracteres antes de entrar; o completo fica no `artifact` da mensagem, que não vai ao modelo.
- **Compactação:** quando a visão passa de `EMENTA_CONTEXT_BUDGET_TOKENS` (8.000 tokens aproximados, cerca de cinco turnos com pesquisa), o turno seguinte a resume antes de a mensagem entrar, com o `compact` do kit. O pedido de resumo é a visão inteira com o prompt `context.summarize` v1 no fim e as ferramentas do último turno com `tool_choice: none`, então reaproveita o prefixo em cache. O resumo entra em `messages` como mensagem nova e `view_start` aponta para ele: daí em diante a visão é o prompt de sistema, o resumo e o que vem depois, de novo só de acréscimo (teste também com reinício no Postgres). O estado guarda a conversa inteira para auditoria e o histórico mostra todos os turnos. O evento `ementa.context.compacted` vai para o `invoke_agent` do turno. Se o resumo falha, o erro fica no log, a visão segue inteira e o turno seguinte tenta de novo.
- **Limites:** até 3 chamadas com ferramentas por turno; a quarta vai com `tool_choice: none` e precisa responder, com as mesmas ferramentas na lista para o prefixo não mudar. Ferramenta que falha vira resultado de erro e o turno segue. Chamada de ferramenta deixada aberta por queda do processo ganha um resultado de interrupção no começo do turno seguinte, acrescentado e não editado, porque o provedor recusa histórico com chamada sem resposta.
- **Citações:** o `finalize` tira da resposta a frase que cita id fora das fontes verificadas na conversa e lista o que tirou em `removed`. Só os ids entre colchetes contam: as premissas do prazo citam "CPC, art. 219" em prosa.
- **Dados pessoais:** a mensagem passa pelo `pii_guard()` antes de entrar no grafo, com o mapeamento da conversa; as memórias lidas passam em seguida, com o mesmo mapeamento, e o valor que já tem marcador o mantém. O modelo e o `legal_research` só veem marcadores. Os tokens saem com os valores reais, palavra por palavra: o fim do texto fica retido enquanto pode virar marcador (`[CP` antes de `F_1]`), de modo que os tokens juntos dão exatamente a resposta reidratada (teste por propriedade com Hypothesis).
- **Memória:** curta no checkpointer do LangGraph no Postgres, schema `langgraph`; a conversa sobrevive a reinício do processo (teste de integração). Longa no store (`ementa.memory`): antes de o supervisor responder, o turno busca as memórias do advogado, do processo (quando quem chama passa `case_id`) e do escritório pela mensagem como foi digitada, no e5 local, e manda a seção `memorias` (até 600 tokens) na mensagem do próprio turno, antes da pergunta (`<pergunta>`), nunca numa mensagem anterior. Depois do `done`, ainda no mesmo fluxo, o `MemoryWriter` aprende com o turno (a mensagem e a resposta final) nos namespaces de `chat_targets` e com o mapeamento da conversa; `EMENTA_MEMORY_WRITE_ENABLED=false` desliga a escrita e mantém a leitura. Store ou escritor que falha fica no log e não derruba a conversa. A rota `POST /v1/chat` ainda não recebe `case_id`, porque falta conferir a permissão por processo (F6).
- **HITL:** herda dos subagentes.
- **Telemetria:** `invoke_agent assistant` com `gen_ai.conversation.id`; dentro dele, `retrieval memory`, `execute_tool legal_research` (com o `invoke_agent legal_research` embaixo) ou `execute_tool calculate_deadline`, `guardrail pii`, `guardrail citation_verifier` e, depois do `done`, `memory write`. O turno que compacta leva o evento `ementa.context.compacted`.
- **Métricas:** acurácia do roteamento num conjunto rotulado (a fazer); taxa de "fora do escopo"; custo por conversa.
- **Custo estimado:** um roteamento custa uma fração de centavo; o resto é o do subagente. A escrita de memória é mais uma chamada `fast` por turno; a compactação, uma chamada cada vez que a conversa passa do orçamento, quase toda no prefixo em cache. **Fase:** F3. **Status:** Implementado (supervisor, memória curta e longa, compactação e SSE).

Eventos do `POST /v1/chat`, um JSON por evento no `data` do SSE, com o tipo no campo `event`:

| `event` | Campos | Quando |
|---|---|---|
| `step` | `name` (`supervisor`, `legal_research`, `calculate_deadline`), `status` (`started`, `finished`, `denied`, `failed`) | Começo e fim de cada chamada ao modelo e de cada ferramenta |
| `token` | `text` | Pedaço da resposta, palavra inteira, com os valores reais |
| `citation` | `source_id`, `title`, `url` | Fonte verificada pelo `legal_research`, assim que a ferramenta volta |
| `done` | `thread_id`, `message`, `removed`, `notice` | Último evento; `message` é a resposta conferida e prevalece sobre os tokens juntos. O fluxo fecha depois, quando o escritor de memória termina |
| `error` | Campos de problema RFC 9457 (`type`, `title`, `status`, `detail`, `instance`) | Erro depois de aberto o fluxo; antes dele, a resposta é o problema com o status HTTP (404, 422, 503) |

```text
data: {"event":"step","name":"legal_research","status":"started"}

data: {"event":"citation","source_id":"cdc:art49","title":"Código de Defesa do Consumidor, art. 49","url":"https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm"}

data: {"event":"token","text":"Pode desistir "}

data: {"event":"done","thread_id":"7c2af06a-ef81-470c-b55a-5e2cad731fe9","message":"Pode desistir em 7 dias [cdc:art49].","removed":[],"notice":"Rascunho para conferência do advogado responsável. Não é aconselhamento jurídico."}
```

## Análise de contratos (`contract_review`)

**Objetivo.** Ler um contrato de consumo de serviços, apontar as cláusulas que o playbook do escritório condena, com o fundamento no CDC ou numa súmula do STJ do corpus, e propor a redação nova de cada uma.

**Gatilho e canal.** Hoje, `services.contract_review_agent().review(conteúdo, nome_do_arquivo)`. A rota `POST /v1/contracts/{id}/review`, a caixa de aprovações do console e a ferramenta MCP `review_contract` entram na integração da F4.

**Padrão e por quê.** Map-reduce por cláusula. Cada cláusula vai a um `analyze_clause` próprio com `Send` do LangGraph, em paralelo e com contexto pequeno: o modelo nunca vê o contrato inteiro. Dentro do map, as regras do playbook rodam primeiro, por código; os critérios que pedem julgamento vão depois ao modelo `fast`. A redução junta os achados numa linha por cláusula e regra.

```mermaid
flowchart TD
    A["parse_document: Markdown, texto ou PDF"] --> B["split_clauses: cláusulas e pseudonimização"]
    B -->|"Send, uma por cláusula"| C["analyze_clause: regras e depois critérios no fast"]
    C --> D["aggregate_findings: matriz de risco"]
    D --> E["verify_citations: fundamento só do corpus"]
    E --> F["draft_redlines: redação sugerida e relatório"]
    F --> G["human_review: ask_for_approval"]
    G -->|"approve ou edit"| H["publish_report"]
    G -->|"reject"| H
```

- `parse_document`: o mesmo `extract_text` dos documentos da F3 (pypdf para PDF); até 2 MiB. PDF sem camada de texto é recusado.
- `split_clauses`: corta nos títulos ("Cláusula 5ª – Do foro", "CLÁUSULA QUINTA", "## 5. Do foro", "5. DO FORO"); o preâmbulo e o fecho (local, data, assinaturas) ficam fora; sem título, cada parágrafo é uma cláusula. Pseudonimiza cada cláusula com o `pii_guard()` e um mapeamento só para o contrato, para o mesmo nome ter o mesmo marcador em todas.
- `analyze_clause`: as regras do playbook (`check_playbook_rule`, sem modelo) e, fora do modo só regras, os critérios (`classify_clause`): a cláusula pseudonimizada, os critérios e o que as regras já acharam vão ao `fast` com o prompt `contract_review.clause` v1, que devolve JSON com critério, trecho, motivo e sugestão. JSON inválido ganha uma nova tentativa, acrescentada à conversa. Critério desconhecido é descartado. O texto do modelo volta com os valores reais.
- `aggregate_findings`: uma linha por cláusula e regra, na ordem do contrato, a mais grave primeiro e as regras antes do modelo. Gravidade e fundamento vêm do playbook, nunca do modelo.
- `verify_citations`: o verificador da F1 sobre cada texto da linha, com o corpus inteiro (CDC e súmulas do STJ, sem os dispositivos vetados) como fontes permitidas. Fonte fora dele ou artigo citado sem id sai; linha que fica sem fundamento sai também; tudo vai para `removed`.
- `draft_redlines`: troca cada frase apontada pela redação do escritório (regra) ou pela sugestão do modelo (critério), com o número do item ("5.2") mantido; redação vazia tira a frase; trecho que não está na cláusula vira acréscimo. Depois monta o relatório em Markdown: resumo, avisos, matriz, redlines e o texto literal de cada fundamento.
- `human_review`: chama `ask_for_approval(ApprovalRequest(agent="contract_review", kind="contract_report", ...))` com o relatório como `draft` e a classe `restricted`. O grafo é compilado com checkpointer e para ali. Edição sem texto não decide: o pedido volta.
- `publish_report`: `approve` publica o rascunho; `edit` publica o texto do advogado; `reject` não publica nada e encerra. Mandar ao cliente fica com quem chamou.

**Estado** (no checkpointer, uma thread por revisão; só registros simples, sem tipos próprios, que o serializador do LangGraph vai recusar):

| Campo | Conteúdo |
|---|---|
| `filename`, `media_type`, `content`, `document_id`, `requested_by` | O arquivo como chegou e quem pediu |
| `rules_only`, `playbook`, `prompt`, `model` | O modo e as versões, fixados no começo da revisão |
| `clauses`, `masked`, `pii_mapping` | As cláusulas, o texto que o modelo vê e o mapeamento dos marcadores |
| `findings`, `unreviewed` | O que cada `analyze_clause` achou e as cláusulas que o modelo não avaliou (redutor de soma, um lote por cláusula) |
| `matrix`, `removed`, `redlines`, `draft` | Matriz de risco, o que o verificador tirou, redações sugeridas e o relatório |
| `decision`, `status`, `final` | A decisão do advogado (`ApprovalDecision` em JSON), `approved`, `edited` ou `rejected`, e o texto publicado |

| Ferramenta (span) | Tipo | Guardrails |
|---|---|---|
| `parse_document` | Leitura | Tipo e tamanho do arquivo (2 MiB); detecção de texto oculto fica para a F6 |
| `check_playbook_rule` | Leitura, determinística | Regras do playbook, sem modelo |
| `classify_clause` | Leitura, modelo `fast` | Cláusula pseudonimizada, JSON validado, critério do playbook |

- **Playbook:** `data/firm/playbooks/consumer-services.yaml`, fictício: 16 regras e 4 critérios, cada um com id, gravidade, fundamento (ids do corpus), motivo citando o fundamento e redação sugerida. Regra é padrão sobre o texto sem acento (`all`, `any`, `none`) e, em duas, um número acima do limite perto de uma palavra na mesma frase (multa acima de 2%; carência acima de 24 horas perto de "urgência"). O teste confere que todo fundamento existe no corpus e que o motivo cita exatamente o fundamento.
- **Modelo:** só o `fast`, e só nos critérios. As redlines saem da redação do playbook, e não do `reasoning` previsto no desenho: é a posição já aprovada do escritório, não custa chamada e o teste a confere palavra por palavra. A troca é a redação genérica; o advogado ajusta na aprovação. Nos critérios, a sugestão é do modelo.
- **Modo só regras:** `review(..., rules_only=True)` não chama modelo nem carrega o guardrail de PII; o relatório avisa que os critérios não foram avaliados. Sem chave, a revisão completa falha antes de começar (`ModelUnavailableError`).
- **Falhas:** cláusula em que o modelo falha ou não devolve JSON depois da nova tentativa fica com os achados das regras e entra em `unreviewed`, que o relatório e o resumo da aprovação mostram. Orçamento estourado para a revisão.
- **Memória:** nenhuma ainda. A memória episódica das aprovações é a outra parte da F4.
- **HITL:** obrigatória. `resume(thread_id, decisão)` confere que há aprovação pendente e que a edição traz texto; a API de aprovações pode também retomar direto com `Command(resume=decisão.model_dump())`, de outro processo.
- **Telemetria:** `invoke_agent contract_review` na revisão e na retomada, com `gen_ai.conversation.id` (a thread), playbook, prompt e as contagens; dentro, `execute_tool parse_document`, um `execute_tool check_playbook_rule` e um `execute_tool classify_clause` por cláusula (o evento `ementa.context.section_tokens` vai neste), `guardrail pii` por cláusula e `guardrail citation_verifier`. Nenhum texto do contrato vai para span ou log.
- **Dados de avaliação:** 10 contratos fictícios em `data/firm/contracts/` (academia, pilates, curso online, idiomas, internet, streaming, plano de saúde, odontológico, faxina, estacionamento), em Markdown e em texto, com 97 cláusulas: 49 plantadas com problema e 48 limpas. O gabarito é `evals/contracts_planted.jsonl`. Os números estão em [Avaliação](AVALIACAO.md#cláusulas-plantadas-f4).
- **Métricas:** precisão, recall e F1 por par (cláusula, regra) e por cláusula; taxa de cláusula não avaliada; custo por contrato.
- **Custo estimado:** uma chamada `fast` por cláusula, com cerca de 700 tokens de entrada (contados nos 10 contratos à razão de 4 caracteres por token, com modelo falso): por volta de US$ 0,001 por contrato de 10 cláusulas no GPT-6 Luna, e de US$ 0,01 pela avaliação inteira. É estimativa pelos preços de `prices.toml`, sem chamada real. **Fase:** F4. **Status:** Implementado (grafo, playbook, aprovação e avaliação das regras); a rota, o console e a ferramenta MCP ficam para a integração.

## Redação de peças (`brief_drafting`)

**Objetivo.** Produzir o rascunho de uma peça (ex.: petição inicial de consumo) a partir dos fatos, com teses pesquisadas e citações verificadas.

**Gatilho e canal.** Pedido do advogado no console (F4).

**Padrão e por quê.** Plan-and-execute com crítico e verificação. O plano separa as teses; cada tese é pesquisada pelo subgrafo do `legal_research`; o crítico revisa a coerência; o verificador bloqueia peça com citação não confirmada. Peça é o produto mais arriscado do escritório, por isso a aprovação humana é obrigatória.

```mermaid
stateDiagram-v2
    [*] --> plan_brief
    plan_brief --> research_theses
    research_theses --> write_draft
    write_draft --> critique
    critique --> write_draft: ajustes
    critique --> verify_citations: ok
    verify_citations --> write_draft: citação falhou
    verify_citations --> human_review: verificado
    human_review --> write_draft: pedido de ajuste
    human_review --> export_docx: aprovado
    export_docx --> [*]
```

**Estado:** `case_id`, `facts`, `plan` (teses), `research` (fontes por tese), `draft`, `critique_notes`, `verification`, `review_decision`, `lawyer_edits`, `cost_usd`.

| Ferramenta | Tipo | Guardrails |
|---|---|---|
| Subgrafo `legal_research` | Leitura | Os da pesquisa |
| `render_template` | Leitura | Só modelos aprovados do escritório |
| `verify_citations` | Leitura | Bloqueia em vez de só remover |
| `export_docx` | Escrita | Só depois da aprovação; auditado |

- **Modelo:** `reasoning` (effort high) para plano, rascunho e crítica.
- **Memória:** ficha do processo; memória episódica com rascunhos aprovados e a edição do advogado, usada como exemplo few-shot e, na F9, como dado de treino.
- **HITL:** `interrupt()` antes de exportar; o advogado aprova, edita ou devolve.
- **Métricas:** taxa de aprovação sem edição; tamanho da edição do advogado; citações bloqueadas; custo por peça.
- **Custo estimado:** ordem de dezenas de centavos de dólar por peça (estimativa, conferir na F4). **Fase:** F4. **Status:** Documentado.

## Prazos e processos (`case_monitor`)

**Objetivo.** Acompanhar processos no DataJud, detectar movimentações novas, calcular prazos e avisar o responsável.

**Gatilho e canal.** Agendador no worker (F5). Notificação por e-mail (Mailpit no dev) e webhook de Teams ou Slack.

**Padrão e por quê.** Workflow agendado, determinístico, com LLM só onde ajuda: classificar a movimentação e resumir. Busca, comparação e cálculo são código.

```mermaid
flowchart TD
    A["load_tracked_cases"] --> B["datajud_search"]
    B --> C["diff_movements"]
    C -->|"nada novo"| H["done"]
    C -->|"movimentação nova"| D["classify_movement"]
    D -->|"abre prazo"| E["calculate_deadline"]
    D -->|"informativa"| F["summarize"]
    E --> F
    F --> G["notify"]
    G --> H
```

**Estado:** `case_ids`, `movements_new`, `classifications`, `deadlines`, `notifications`, `errors`.

| Ferramenta | Tipo | Guardrails |
|---|---|---|
| `datajud_search` | Leitura externa | Chave por variável de ambiente, guarda de egress, limite de taxa, só metadados |
| `classify_movement` | Leitura | Saída estruturada |
| `calculate_deadline` | Leitura | Os da ferramenta |
| `notify` | Externa | Só para usuário interno; sem dado sigiloso no corpo do e-mail |

- **Modelo:** `fast` para classificar e resumir; rota `local` quando o processo é sigiloso (F6).
- **Memória:** movimentações já vistas no Postgres.
- **HITL:** não age fora do escritório; só alerta.
- **Métricas:** prazos detectados contra um conjunto de teste; atraso entre movimentação e alerta; falhas do DataJud.
- **Custo estimado:** fração de centavo por movimentação nova (estimativa). **Fase:** F5. **Status:** Documentado.

## Triagem e conflito (`intake`)

**Objetivo.** Transformar o relato do potencial cliente em ficha estruturada e verificar conflito de interesses antes de aceitar o caso.

**Gatilho e canal.** Formulário ou chat no console (F5).

**Padrão e por quê.** Extração estruturada seguida de verificação em grafo. O conflito é uma pergunta de relacionamento (essa parte contrária já foi cliente? é sócia de um cliente?), que o grafo responde melhor que SQL.

```mermaid
flowchart TD
    A["extract_intake"] --> B["normalize_parties"]
    B --> C["check_conflict_graph"]
    B --> D["check_conflict_sql"]
    C --> E["assess"]
    D --> E
    E --> F["partner_decision"]
    F -->|"aceita"| G["open_case"]
    F -->|"recusa"| H["record_refusal"]
```

**Estado:** `narrative`, `intake` (área, subárea, urgência, partes, documentos necessários), `conflicts`, `assessment`, `decision`.

| Ferramenta | Tipo | Guardrails |
|---|---|---|
| `extract_intake` | Leitura | Pseudonimização antes da nuvem; saída estruturada |
| `check_conflict_graph` | Leitura | Pessoas só por hash com sal no Neo4j |
| `check_conflict_sql` | Leitura | RLS; só o necessário para o conflito |
| `open_case` | Escrita | Só depois da decisão da sócia; auditado |

- **Modelo:** `fast` na extração (candidato ao modelo ajustado da F9).
- **HITL:** a sócia decide.
- **Métricas:** acurácia de área e urgência; conflitos encontrados no conjunto plantado.
- **Custo estimado:** fração de centavo por triagem (estimativa). **Fase:** F5. **Status:** Documentado.

## BI do escritório (`firm_analytics`)

**Objetivo.** Responder perguntas sobre os números do escritório (prazos vencendo, horas por área, custo de IA) em linguagem natural.

**Gatilho e canal.** Pergunta no console ou ferramenta MCP `query_firm_analytics` (F6).

**Padrão e por quê.** Texto para SQL com guarda. O modelo escreve SQL; o código decide se ele roda.

```mermaid
flowchart TD
    A["load_schema_context"] --> B["generate_sql"]
    B --> C["run_guarded_sql"]
    C -->|"recusado"| B
    C -->|"ok"| D["explain_result"]
```

**Estado:** `question`, `schema_context` (views do schema `analytics` e descrições), `sql`, `guard_result`, `rows`, `answer`, `attempts`.

| Ferramenta | Tipo | Guardrails |
|---|---|---|
| `run_guarded_sql` | Leitura | sqlglot: só SELECT em views permitidas, LIMIT obrigatório; papel `bi_reader`; `statement_timeout`; RLS com `app.user_id` |

- **Modelo:** `default`.
- **HITL:** não; só leitura.
- **Métricas:** acerto de resultado num conjunto de perguntas com SQL de gabarito; consultas recusadas pela guarda.
- **Custo estimado:** cerca de um centavo por pergunta (estimativa). **Fase:** F6. **Status:** Documentado.

## Compliance LGPD (`lgpd_compliance`)

**Objetivo.** Revisar um contrato ou fluxo de dados contra um checklist de LGPD e apontar lacunas.

**Gatilho e canal.** Pedido no console ou rotina agendada (F8).

**Padrão e por quê.** Agente Semantic Kernel consumindo as ferramentas do `ementa-mcp`. Mostra que o MCP é o contrato de integração: um agente de outro framework, em outro processo, usa as mesmas ferramentas com os mesmos guardrails. O porte para o Microsoft Agent Framework 1.x está no [ADR 0009](adr/0009-semantic-kernel-e-agent-framework.md).

```mermaid
flowchart LR
    A["agente SK"] -->|"MCP"| B["ementa-mcp"]
    B --> C["search_legal_corpus"]
    B --> D["get_statute_article"]
    A --> E["skill lgpd-checklist"]
    A --> F["relatório de lacunas"]
```

| Ferramenta | Tipo | Guardrails |
|---|---|---|
| Ferramentas do `ementa-mcp` | Leitura | As do servidor MCP, incluindo a política de saída para cliente externo |

- **Modelo:** `default`, configurado no próprio SK.
- **HITL:** relatório é rascunho.
- **Métricas:** lacunas encontradas em documentos fictícios com falhas plantadas.
- **Fase:** F8. **Status:** Documentado.

## Revisor de PR (`pr_reviewer`)

**Objetivo.** Revisar pull requests do próprio repositório contra as regras do `AGENTS.md` e dos ADRs.

**Gatilho e canal.** Workflow `ai-review.yml` no GitHub Actions, a cada pull request (F8).

**Padrão e por quê.** Workflow simples: lê o diff, separa por área e confere regras concretas (ferramenta nova sem teste, guardrail não registrado, prompt mudado sem avaliação, dependência do kit para o domínio). É o agente que liga a Ementa ao ciclo de desenvolvimento.

```mermaid
flowchart TD
    A["load_diff"] --> B["load_rules"]
    B --> C["review_by_area"]
    C --> D["dedupe_findings"]
    D --> E["post_review_comment"]
```

| Ferramenta | Tipo | Guardrails |
|---|---|---|
| Leitura do diff e dos ADRs | Leitura | Só o repositório |
| Comentário no PR | Externa | Comentário apenas; o merge é humano |

- **Modelo:** `default`; orçamento por PR no CI.
- **Métricas:** achados aceitos contra descartados.
- **Fase:** F8. **Status:** Documentado.

---

[Voltar ao README](../README.md)
