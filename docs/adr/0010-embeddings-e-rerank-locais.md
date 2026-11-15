# ADR 0010: Embeddings e rerank locais, escolhidos por medição

**Status:** Aceito · **Data:** 2026-11-14 · **Fase:** F1 · **Ver também:** [Arquitetura](../ARQUITETURA.md), [ML e fine-tuning](../ML-E-FINE-TUNING.md)

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

## Alternativas consideradas

| Alternativa | A favor | Contra | Por que não agora |
|---|---|---|---|
| Embeddings da OpenAI | Qualidade alta; nada para operar; mesma conta do provedor | Texto sai do escritório; custo por reindexação; dependência de rede no teste | Entram como linha de base no experimento, não como padrão |
| Voyage | Modelos com boa fama em domínio específico | Mais um fornecedor, um contrato e um envio de dado para fora | Mesmo motivo de privacidade, com um fornecedor a mais |
| Sem rerank | Menos latência e menos código | A fusão RRF sozinha mistura bem, mas ordena pior os primeiros | O rerank é barato em CPU para 30 candidatos; a medição decide se fica |

## Consequências

**Positivas**

- Nenhum texto do escritório sai para gerar vetor, nem do corpus nem dos documentos de processo.
- Testes e CI rodam sem rede e sem custo de API para indexar.
- A escolha é defensável com números do próprio domínio.

**Negativas e riscos**

- Modelo local tem limite de contexto e qualidade menor que os maiores da nuvem. Mitigação: chunk por artigo, que é curto, e o rerank.
- Baixar o modelo pesa no primeiro uso e na imagem Docker. Mitigação: cache do modelo em volume e download no build.
- Trocar de modelo exige reindexar tudo. A linhagem diz exatamente o que reindexar.

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
