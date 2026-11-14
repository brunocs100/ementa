# ADR 0011: Fine-tuning de modelo pequeno para a triagem, servido local

**Status:** Aceito · **Data:** 2026-11-14 · **Fase:** F9 · **Ver também:** [ML e fine-tuning](../ML-E-FINE-TUNING.md), [Agentes](../AGENTES.md)

## Contexto

O `intake` (F5) recebe o relato do cliente em texto livre e precisa devolver um JSON com área, subárea, urgência e documentos necessários. É uma tarefa estreita e repetitiva, de alto volume, e o relato traz dado pessoal e muitas vezes sensível (saúde, família, dívidas).

No desenho, ela roda no papel `fast` (GPT-6 Luna) com prompt. Isso tem três custos:

- o relato sai do escritório para a nuvem, ainda que pseudonimizado;
- cada triagem custa uma chamada;
- a qualidade depende do prompt e da versão do modelo do provedor.

Um modelo pequeno ajustado para essa tarefa pode rodar no próprio escritório. Também é a demonstração concreta de fine-tuning e de MLOps.

A máquina de dev não tem GPU com CUDA. O treino precisa ir para uma GPU emprestada; a inferência, para CPU.

## Decisão

**Treino.** QLoRA (adaptadores LoRA sobre o modelo base quantizado em 4 bits) num modelo aberto de 1 a 4B parâmetros. A escolha do modelo base sai da licença de uso e do desempenho em português (conferir candidatos na F9). O treino roda no Colab ou no Kaggle, com GPU gratuita.

**Dados:**

- relatos sintéticos gerados por destilação, com controle de diversidade (área, tom, escolaridade, região);
- o rótulo vem da especificação de geração;
- uma amostra é revisada à mão;
- o conjunto de teste é separado e inclui exemplos escritos à mão.

**Servir.** O modelo exportado em GGUF roda no Ollama, em CPU, na rota de triagem do papel `local` (o papel aponta para modelos por rota: o geral do sigilo e este).

**Cascata por confiança.** O modelo local responde primeiro. Abaixo do limiar de confiança, ou com JSON inválido, a triagem sobe para o Luna com o relato pseudonimizado. Processo sigiloso nunca sobe: abaixo do limiar, vai para revisão humana.

**MLOps.** O MLflow registra parâmetros, métricas e artefatos, com os aliases `champion` e `challenger`. A promoção depende de a avaliação passar.

**Melhoria contínua.** As correções humanas da triagem viram dado de monitoramento de drift e de retreino.

## Alternativas consideradas

| Alternativa | A favor | Contra | Por que não agora |
|---|---|---|---|
| Só prompt no Luna ou no Terra | Nada para treinar nem operar | O relato sai do escritório; custo por chamada; muda quando o provedor muda | Fica como linha de base e como fallback da cascata |
| Fine-tuning via API da OpenAI | Simples; sem GPU; modelo forte | Dado de treino e de inferência sai; custo de treino e de uso; modelo preso ao provedor | Contradiz o objetivo de manter o relato local |
| Classificador BERTimbau | Ótimo para classificar texto em português; leve | Não gera o JSON estruturado com subárea e lista de documentos; exige vários classificadores | Bom para um campo só; a tarefa pede saída estruturada |

## Consequências

**Positivas**

- A triagem roda sem enviar o relato para fora e com custo marginal perto de zero.
- O ciclo completo de MLOps fica demonstrado: dados, treino, avaliação, registro, servir, cascata e drift.
- A cascata mantém a qualidade quando o modelo pequeno não tem certeza.

**Negativas e riscos**

- Dado sintético tem viés de quem gerou. Mitigação: amostra revisada à mão e teste com exemplos humanos.
- Latência em CPU é maior que na nuvem. Mitigação: modelo pequeno, quantização e cascata. A rota local não tem SLO; a latência é medida.
- GPU gratuita tem limite de tempo e de sessão. Mitigação: treino curto, checkpoints e script reprodutível.

## Como validar

- F1 macro do modelo ajustado até 2 pontos abaixo da linha de base com prompt, no mesmo conjunto de teste.
- JSON válido em pelo menos 99% das saídas.
- Custo marginal perto de zero e latência aceitável em CPU, medidos e registrados no MLflow.
- Taxa de subida na cascata medida; se passar de 30%, o modelo não está pagando o que custa.
- Gatilho para revisitar: a linha de base com prompt ficar muito à frente depois de duas rodadas de treino, ou o custo de operar o modelo local superar a economia.

## Referências

- QLoRA (artigo original): <https://arxiv.org/abs/2305.14314>
- Hugging Face PEFT: <https://huggingface.co/docs/peft/>
- Ollama, importação de GGUF: <https://github.com/ollama/ollama/blob/main/docs/import.md> (conferir o caminho na F9)
- MLflow Model Registry: <https://mlflow.org/docs/latest/>

---

[Índice de ADRs](README.md)
