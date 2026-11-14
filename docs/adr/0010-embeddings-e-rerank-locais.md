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
