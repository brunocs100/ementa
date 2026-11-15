# ADR 0010: Embeddings e rerank locais, escolhidos por medição

**Status:** Aceito · **Data:** 2026-11-14 · **Fase:** F1 (embeddings) e F2 (rerank) · **Ver também:** [Arquitetura](../ARQUITETURA.md), [ML e fine-tuning](../ML-E-FINE-TUNING.md)

## Contexto

Para a busca vetorial (ADR 0003), todo chunk precisa virar um vetor, e cada pergunta também. No corpus público isso não é problema de privacidade. Mas, a partir da F4, entram documentos internos e documentos de processo, e mandar esse texto para uma API de embeddings é transferir dado pessoal para fora (LGPD, art. 33; conferir na F6).

O português jurídico também tem vocabulário próprio: "repetição do indébito", "vício redibitório", "tutela de urgência". Um modelo pode ir bem em benchmark geral e mal aqui.

A máquina de dev não tem GPU com CUDA e tem 15 GB de RAM, então o modelo precisa rodar bem em CPU.

## Decisão

**Embeddings locais e multilíngues.** Os candidatos iniciais são a família multilingual-e5 e o bge-m3. A F1 escolhe por uma medição rápida de recall@k nas 10 perguntas de fumaça, não por ranking público.

**Rerank local.** Um cross-encoder multilíngue local reordena os candidatos da fusão RRF, a partir da F2 (top 8).

**Execução em CPU com ONNX**, via fastembed, para poupar RAM e tempo. A alternativa é o sentence-transformers.

**Linhagem.** Cada chunk guarda o nome e a versão do modelo que gerou o vetor. Trocar o modelo exige reindexar, e a reindexação é um job idempotente do worker.

**Experimento registrado.** A F9 registra a escolha no MLflow e a reconfere com as 30 perguntas: modelo, recall@1, @5 e @10, MRR, tempo por mil chunks e RAM. Os embeddings da OpenAI entram como linha de comparação.

## Medição da F1 (15/11/2026)

As 10 perguntas de fumaça (`evals/smoke_cdc.jsonl`), só a busca híbrida com a pergunta original, sem reescrita e sem modelo. CPU do notebook de dev, 118 artigos do CDC. O bge-m3 não está na lista do fastembed 0.9 e ficou para a F9.

| Modelo | Dim | Top 1 | Top 5 | MRR@5 | Indexar o CDC | Por consulta |
|---|---|---|---|---|---|---|
| `intfloat/multilingual-e5-large` | 1024 | 8/10 | 9/10 | 0,83 | 142 s | 109 ms |
| `paraphrase-multilingual-mpnet-base-v2` | 768 | 5/10 | 10/10 | 0,69 | 43 s | 65 ms |
| `paraphrase-multilingual-MiniLM-L12-v2` | 384 | 6/10 | 9/10 | 0,68 | 7 s | 126 ms |

Escolha: `multilingual-e5-large` (licença MIT), que põe o artigo certo em primeiro em 8 das 10. O mpnet acha todos no top 5, mas ordena pior. A pergunta que o e5 perde sozinha (prazo de indenização por acidente, art. 27) entra no top 5 com a reescrita do agente: com o `legal_research` completo, são 10/10 no top 5 e 10/10 com a citação esperada na resposta. O custo é o download de 2,2 GB e cerca de 2,5 minutos para indexar em CPU, aceitável para um corpus que muda pouco. O MiniLM fica para o CI, se um teste precisar de embedding real.

## Medição do rerank (F2, 15/11/2026)

As mesmas 10 perguntas, só a busca, com a pergunta original. A fusão RRF entrega os 30 primeiros; o cross-encoder lê a pergunta com cada um (título, caminho e texto do artigo, como o embedding indexou) e reordena. Um modelo por vez na memória, já carregado, na mesma CPU (Ryzen 5 5600G, 6 núcleos). A latência é só a do rerank, mediana por consulta; a busca híbrida com o embedding da pergunta fica em torno de 100 ms.

O fastembed 0.9.1 lista três cross-encoders multilíngues: o `bge-reranker-v2-m3`, em fp32 e em int8, e o `jina-reranker-v2-base-multilingual`. Os outros da lista são só em inglês. O `mmarco-mMiniLMv2-L12-H384-v1`, treinado no mMARCO (que tem português), não está na lista: o kit o registra no fastembed a partir do ONNX do próprio repositório no Hugging Face.

| Rerank | Licença | Download | Top 1 | Top 5 | MRR@5 | Rerank por consulta |
|---|---|---|---|---|---|---|
| Sem rerank (fusão RRF, F1) | | | 8/10 | 9/10 | 0,83 | |
| `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` | Apache-2.0 | 0,47 GB | 10/10 | 10/10 | 1,00 | 1,8 s |
| `jinaai/jina-reranker-v2-base-multilingual` | CC BY-NC 4.0 | 1,11 GB | 10/10 | 10/10 | 1,00 | 8,9 s |
| `BAAI/bge-reranker-v2-m3-int8` | Apache-2.0 | 0,57 GB | 9/10 | 10/10 | 0,93 | 16 s |
| `BAAI/bge-reranker-v2-m3` | Apache-2.0 | 2,27 GB | 9/10 | 10/10 | 0,93 | 37 s |

Todos os cross-encoders trazem para o primeiro lugar as duas perguntas que a fusão ordenava mal: o prazo de indenização por acidente (art. 27, sexto na fusão) e a cláusula de desvantagem exagerada (art. 51, terceiro). Os dois bge descem a da negativação (art. 43) para terceiro, atrás dos artigos do superendividamento.

A montagem do lote muda a latência em duas vezes e meia. O fastembed preenche cada lote até o par mais longo (até 790 tokens com o art. 51), e os artigos do CDC têm mediana de 140 tokens. Num lote só com os 30, o mMiniLM leva 4,7 s, o jina 22 s e o bge int8 42 s. No mMiniLM, um par por vez é o mais rápido (1,8 s, contra cerca de 2,5 s em lotes de 8 ordenados por tamanho), cerca de 60 ms por candidato, e é como o kit roda. Os modelos maiores aparecem na tabela com lotes de 8 ordenados por tamanho, o melhor medido para eles.

Escolha: `mmarco-mMiniLMv2-L12-H384-v1` (Apache-2.0), ligado por padrão. Empata com o jina no topo, mas o jina tem licença não comercial e é quase cinco vezes mais lento; os bge ordenam pior e levam de 16 a 37 s por consulta nesta CPU. O mMiniLM lê até 512 tokens, pergunta incluída: os seis artigos mais longos do CDC (51, 104-A, 18, 39, 6 e 54-G) chegam ao rerank com o fim cortado, o que não muda a busca. Sem o título e o caminho no texto do rerank, a qualidade foi a mesma e o rerank ficou 15% mais rápido; o texto fica igual ao do embedding até as 30 perguntas dizerem o contrário. As 10 perguntas são fáceis para os cross-encoders e não separam o mMiniLM do jina: a comparação se repete com as 30 perguntas e na F9.

Configuração: `EMENTA_RERANK_ENABLED` (padrão `true`), `EMENTA_RERANK_MODEL`, `EMENTA_RERANK_CANDIDATES` (30, os candidatos da fusão que o cross-encoder lê) e `EMENTA_RERANK_TOP_K` (8, as fontes que vão para o contexto do agente). Com várias consultas, o cross-encoder compara os candidatos com a pergunta como foi feita, não com as reescritas. Desligado, a busca é a da F1: fusão RRF e 5 fontes no contexto. O span `retrieval rerank` grava o modelo e quantos candidatos entraram e ficaram; a duração do span é a latência. Para reproduzir a qualidade: `uv run ementa smoke --retrieval-only`, com `EMENTA_RERANK_ENABLED=false` para a linha de base e `EMENTA_RERANK_MODEL` para trocar o modelo.

## Corte por nota do rerank (15/11/2026)

Com o corpus inteiro, os 8 trechos mantidos traziam ruído: numa pergunta sobre arrependimento, só o `cdc:art49` tinha a ver, e os outros 7 eram artigos do CPC. O rerank agora descarta o que fica mais de `EMENTA_RERANK_MARGIN` (5,0) abaixo da melhor nota. Nas 30 perguntas, a fonte esperada nunca ficou mais de 3,4 abaixo da melhor; a busca segue com recall@5 de 1,00 e o contexto cai de 8 para 4,7 trechos em média. Medição só da busca, sem chamada de modelo.

| Margem | Recall@5 | Trechos no contexto (média) |
|---|---|---|
| 3 | 29/30 | 2,8 |
| 4 | 30/30 | 3,7 |
| 5 (padrão) | 30/30 | 4,7 |
| sem corte | 30/30 | 8,0 |

## Alternativas consideradas

| Alternativa | A favor | Contra | Por que não agora |
|---|---|---|---|
| Embeddings da OpenAI | Qualidade alta; nada para operar; mesma conta do provedor | Texto sai do escritório; custo por reindexação; dependência de rede no teste | Entram como linha de base no experimento, não como padrão |
| Voyage | Modelos com boa fama em domínio específico | Mais um fornecedor, um contrato e um envio de dado para fora | Mesmo motivo de privacidade, com um fornecedor a mais |
| Sem rerank | Menos latência e menos código | A fusão RRF sozinha mistura bem, mas ordena pior os primeiros | Medido na F2: o mMiniLM leva o MRR@5 de 0,83 a 1,00 por cerca de 1,8 s em CPU; o rerank fica, e desligá-lo é configuração |

## Consequências

**Positivas**

- Nenhum texto do escritório sai para gerar vetor, nem do corpus nem dos documentos de processo.
- Testes e CI rodam sem rede e sem custo de API para indexar.
- A escolha é defensável com números do próprio domínio.

**Negativas e riscos**

- Modelo local tem limite de contexto e qualidade menor que os maiores da nuvem. Mitigação: chunk por artigo, que é curto, e o rerank.
- Baixar o modelo pesa no primeiro uso e na imagem Docker. Mitigação: cache do modelo em volume e download no build.
- Trocar de modelo exige reindexar tudo. A linhagem diz exatamente o que reindexar.
- O rerank soma cerca de 1,8 s por busca em CPU, e o tempo cresce com os candidatos, cerca de 60 ms cada. Mitigação: menos candidatos em `EMENTA_RERANK_CANDIDATES` ou o rerank desligado; trocar o cross-encoder não exige reindexar.

## Como validar

- Recall@5 ≥ 0,85 no conjunto dourado com o modelo escolhido (ver [Avaliação](../AVALIACAO.md)).
- Tabela do experimento no MLflow (F9), comparando os candidatos locais com os embeddings da OpenAI em recall, latência e RAM.
- Com rerank contra sem rerank: o rerank só fica se melhorar o MRR sem estourar o SLO da rota.
- Gatilho para revisitar: a diferença para a OpenAI passar de 10 pontos de recall@5, ou o corpus crescer a ponto de a indexação em CPU virar gargalo.

## Referências

- fastembed: <https://github.com/qdrant/fastembed>
- sentence-transformers: <https://www.sbert.net/>
- MLflow Tracking: <https://mlflow.org/docs/latest/>

---

[Índice de ADRs](README.md)
