# ADR 0007: MCP como padrão de integração com clientes de IA

**Status:** Aceito · **Data:** 2026-11-14 · **Fase:** F1 · **Ver também:** [Arquitetura](../ARQUITETURA.md), [Copilot corporativo](../COPILOT-CORPORATIVO.md)

## Contexto

As ferramentas da Ementa precisam chegar a vários clientes de IA:

- ao Copilot no VS Code, que o time de desenvolvimento usa;
- ao Claude Desktop e ao Claude Code;
- ao agente de compliance em Semantic Kernel (F8).

As ferramentas são pesquisar o corpus, buscar um artigo, calcular prazo e, mais tarde, consultar processo e checar conflito.

Fazer uma integração por cliente multiplica o trabalho e a superfície de segurança. O MCP (Model Context Protocol) é hoje o padrão aberto para isso. A revisão 2026-07-28 da especificação tornou o protocolo sem estado: não há handshake de inicialização nem sessão, e cada pedido carrega a versão e as capacidades (conferir na F1).

Há um risco que muda o desenho. O cliente MCP de terceiro manda o que a ferramenta devolve para o modelo de nuvem dele. Para a Ementa, ele é um processador de dados tão externo quanto a OpenAI.

## Decisão

**Servidor.** O servidor `ementa-mcp` usa o SDK oficial de MCP em Python e segue a spec 2026-07-28. Transporte stdio para uso local na F1; HTTP na F2, que escala atrás de um balanceador por ser sem estado.

**Ferramentas, recursos e prompts:**

- ferramentas: `search_legal_corpus`, `answer_legal_question`, `get_statute_article` e `calculate_deadline` na F1; `review_contract` na F4, como tarefa longa; `lookup_case` e `check_conflict` na F5; `query_firm_analytics` na F6;
- recursos: `ementa://statutes/{id}/articles/{n}`, `ementa://templates/{kind}` e `ementa://playbooks/contracts`;
- prompts: `review-contract` e `summarize-case`.

**Classe de dado por ferramenta.** Cada ferramenta declara a classe de dado que devolve: público, restrito ou sigiloso. Para cliente externo, a saída restrita é pseudonimizada e a sigilosa é negada com aviso (F2). Toda chamada passa por guardrails (ADR 0006), auditoria e telemetria.

**Autenticação.** Stdio local sem token, porque roda com o usuário da máquina. HTTP com JWT na F3 e OAuth na F6.

**Clientes declarados:**

- Copilot no VS Code, via `.vscode/mcp.json`;
- Claude Desktop e Claude Code;
- o agente `lgpd_compliance`.

## Alternativas consideradas

| Alternativa | A favor | Contra | Por que não agora |
|---|---|---|---|
| Só REST | Universal; a API já existe (F2) | Cada cliente de IA precisaria de um adaptador próprio para descobrir e chamar ferramentas | A REST continua para o console e sistemas; o MCP cobre os clientes de IA |
| Plugins proprietários por cliente | Integração mais profunda num produto | Um código por fornecedor; as extensões do Copilot feitas como GitHub App foram descontinuadas em favor do MCP (conferir na F8) | Aposta num formato em declínio |

## Consequências

**Positivas**

- Uma integração serve a todos os clientes MCP, e o Copilot no VS Code vira canal de demo sem código extra.
- Sem estado, o servidor HTTP escala horizontalmente como qualquer serviço web.
- A classificação de saída por ferramenta deixa explícito o que pode sair para fora, o que é exatamente o ponto de governança.

**Negativas e riscos**

- A especificação mudou bastante entre revisões. Mitigação: versão do SDK fixada no `uv.lock` e testes de contrato.
- O modelo do cliente vê o que o próprio usuário digita. A Ementa protege as chamadas dela e o que as ferramentas devolvem, não o prompt do usuário. Isso precisa ficar claro na demo e nos docs.
- A ferramenta longa (`review_contract`) depende do suporte do cliente a tarefas (conferir na F4).

## Como validar

- MCP Inspector lista as ferramentas, os recursos e os prompts, com os schemas certos.
- Testes com cliente em memória para cada ferramenta, incluindo a negação de saída sigilosa para cliente externo (F2).
- Demo do Marco 1: o Copilot do VS Code, em modo agente, responde sobre o art. 49 do CDC e calcula um prazo pelo `ementa-mcp`.
- Gatilho para revisitar: uma revisão da spec quebrar compatibilidade, ou um cliente-alvo deixar de suportar MCP.

## Referências

- Especificação do MCP, revisão 2026-07-28: <https://modelcontextprotocol.io/specification/2026-07-28/changelog>
- SDK oficial de MCP em Python: <https://github.com/modelcontextprotocol/python-sdk>
- MCP no VS Code: <https://code.visualstudio.com/docs/copilot/chat/mcp-servers> (conferir o caminho na F1)

---

[Índice de ADRs](README.md)
