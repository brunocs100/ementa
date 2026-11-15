# ADR 0005: Camada de modelos agnóstica, com papéis e roteamento por sigilo

**Status:** Aceito · **Data:** 2026-11-14 · **Fase:** F1 · **Ver também:** [Arquitetura](../ARQUITETURA.md), [Segurança e governança](../SEGURANCA-E-GOVERNANCA.md)

## Contexto

A Ementa chama modelos de linguagem em rotas de exigência e custo muito diferentes:

- uma peça processual precisa do melhor raciocínio disponível;
- classificar uma pergunta ou extrair campos precisa de rapidez e preço baixo;
- o juiz das avaliações deve ser um modelo diferente do que responde, para reduzir o viés de autopreferência.

Processo em segredo de justiça não pode ir para modelo de nuvem nenhum.

Três forças mudam rápido: modelos, preços e provedores. Um escritório (ou uma empresa) também pode exigir trocar de provedor por contrato, e o custo de teste precisa de teto.

## Decisão

**Papéis no lugar de modelos.** O código pede um papel, e o `ModelRegistry` do kit resolve o modelo pela configuração.

| Papel | Modelo de referência |
|---|---|
| `reasoning` | GPT-6.1 Terra, effort high |
| `default` | GPT-6.1 Terra, effort medium |
| `fast` | GPT-6 Luna |
| `judge` | GPT-6.1 Sol, em amostra |
| `local` | Modelo aberto pequeno no Ollama |
| `embeddings` | Local (ADR 0010) |
| `rerank` | Local (ADR 0010) |

**Provedores.** OpenAI é a referência, Ollama serve a rota local e Anthropic entra só por configuração.

**Acesso.** O acesso aos modelos passa pelos chat models do LangChain (ChatOpenAI, ChatOllama, ChatAnthropic). Se um recurso do provedor não estiver exposto, a rota usa o SDK oficial direto, atrás da mesma porta.

**Política antes da chamada.** O motor de políticas do kit decide a rota antes de cada chamada. Processo sigiloso vai para `local`, sempre (F6).

**Orçamento.** O kit tem orçamento por ambiente e por área: US$ 2/dia e US$ 30/mês em dev e CI, com parada automática. Toda execução registra tokens e custo (cerca de US$ 0,015 por pergunta de pesquisa). O projeto da OpenAI tem chave própria e limite de gasto no painel.

**Histórico só de acréscimo.** Nada que o modelo já viu é editado:

- o prompt de sistema congela no início da conversa;
- contexto novo entra como mensagem nova;
- resultado de ferramenta é truncado antes de o modelo ver;
- compactar é começar outra conversa a partir de um resumo.

Na OpenAI, a Responses API roda com `store: false`, e o raciocínio criptografado é devolvido intacto a cada turno (nomes de parâmetro: conferir na F1). Conferido na F1: a instrumentação `opentelemetry-instrumentation-openai-v2` (2.4b0) cobre Chat Completions e embeddings, não a Responses API. Por isso a F1 usa Chat Completions, que não guarda a conversa no provedor por padrão; o `legal_research` da F1 tem uma pergunta e no máximo uma nova tentativa, então não precisa devolver raciocínio entre turnos. A troca para a Responses API fica para quando o agente tiver vários turnos (F3), com o span `chat` feito à mão se a instrumentação ainda não cobrir. Isso mantém o cache de prompt e deixa a troca de provedor segura: o Claude, por exemplo, recusa com erro 400 um histórico editado que carrega blocos de raciocínio (conferir na documentação da Anthropic).

Exemplo de configuração:

```yaml
# infra/models.yaml
roles:
  reasoning: {provider: openai, model: gpt-6.1-terra, effort: high}    # conferir id na F1
  default:   {provider: openai, model: gpt-6.1-terra, effort: medium}  # conferir id na F1
  fast:      {provider: openai, model: gpt-6-luna}                     # conferir id na F1
  judge:     {provider: openai, model: gpt-6.1-sol, effort: medium}    # conferir id na F1
  local:     {provider: ollama, model: "<modelo de 3 a 4B>"}           # escolhido na F6
budget:
  usd_per_day: 2
  usd_per_month: 30
  on_exceed: stop
```

A amostragem do juiz fica na configuração da avaliação, não aqui.

## Alternativas consideradas

| Alternativa | A favor | Contra | Por que não agora |
|---|---|---|---|
| SDK de um provedor só | Menos camadas; acesso imediato a todo recurso novo | Troca de provedor vira refatoração; sem rota local nem juiz independente | Contradiz o roteamento por sigilo |
| Proxy LiteLLM | Gateway pronto, com chaves virtuais, orçamentos e logs por time | Mais um serviço e um salto de rede; recursos específicos podem não passar pelo proxy | Bom candidato a gateway corporativo depois; hoje o registry no kit basta |
| Gateway gerenciado (Azure API Management, Portkey e similares) | Governança central, cotas e auditoria fora do código | Custo, dependência de nuvem, pouco a mostrar no código | Fora do orçamento de um portfólio |

## Consequências

**Positivas**

- Trocar o modelo de um papel é mudar uma linha de configuração, com a avaliação dizendo se a qualidade segurou.
- O roteamento por sigilo fica num lugar só e é testável sem chamar modelo nenhum.
- O custo fica visível por execução, por papel e por área.

**Negativas e riscos**

- Recurso específico de um provedor (raciocínio criptografado, cache com chave) exige adaptador. Se o LangChain atrasar, a rota precisa do SDK oficial.
- A regra do histórico só de acréscimo proíbe atalhos comuns, como apagar mensagens antigas ou reescrever o sistema a cada turno. Precisa de teste para não regredir.
- O modelo local em CPU é lento. A rota sigilosa terá latência maior, documentada e sem SLO até a F7.

## Como validar

- A mesma avaliação de pesquisa roda com dois provedores (OpenAI e um alternativo) só trocando a configuração. A diferença de qualidade e de custo vai para a tabela da avaliação.
- Teste de orçamento: com o teto baixo, a execução para com erro claro e registra o motivo.
- Teste do prefixo no CI, a partir da F2: compara pedidos consecutivos da mesma conversa e falha se o prefixo mudou.
- Teste de política: um processo marcado como sigiloso nunca gera chamada a provedor de nuvem.
- Gatilho para revisitar: dois ou mais times consumirem a plataforma. Aí um gateway (LiteLLM ou gerenciado) passa a valer o salto extra.

## Referências

- Preços e ids de modelo da OpenAI: <https://developers.openai.com/api/docs/pricing> (conferir na F1)
- Chat models do LangChain: <https://python.langchain.com/docs/integrations/chat/>
- Ollama: <https://ollama.com/>

---

[Índice de ADRs](README.md)
