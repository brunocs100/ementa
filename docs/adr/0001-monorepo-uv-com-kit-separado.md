# ADR 0001: Monorepo uv com kit separado do domínio

**Status:** Aceito · **Data:** 2026-11-14 · **Fase:** F0 · **Ver também:** [Arquitetura](../ARQUITETURA.md), [Plano](../PLANO.md)

## Contexto

A Ementa tem duas naturezas no mesmo código. Parte dela é infraestrutura de agentes que serviria a qualquer time: camada de modelos, guardrails, montador de contexto, memória, telemetria, avaliação, skills e helpers de MCP. A outra parte é jurídica: parser de lei, numeração CNJ, cálculo de prazo, agentes de pesquisa, contratos e peças.

Um dos objetivos do projeto é mostrar a criação de componentes reutilizáveis para agentes. Se as duas partes se misturam, o kit acaba importando conceito jurídico e deixa de ser reutilizável sem ninguém perceber. Ao mesmo tempo, é um projeto de uma pessoa, e manter vários repositórios com versões cruzadas custaria mais do que ajuda.

## Decisão

- Um monorepo com uv workspace e três camadas: `packages/ementa-kit` (genérico, importado como `ementa_kit`), `packages/ementa` (domínio jurídico, importado como `ementa`) e `apps/*` (entradas executáveis: `api`, `worker`, `mcp_server`, `compliance_sk`).
- Um único `uv.lock` na raiz, com versões fixadas para todo o workspace.
- Regras de dependência como contratos do import-linter, rodando no CI desde a F2:
  - `ementa_kit` não importa `ementa` nem `apps`;
  - `ementa.domain` não importa infraestrutura (banco, HTTP, SDK de modelo);
  - `apps` só usam a interface pública dos pacotes.
- O kit expõe portas (protocolos) que o domínio implementa ou consome: `VectorStore`, `ChatModelPort`, `Guardrail`, `PromptRegistry`, entre outras.

## Alternativas consideradas

| Alternativa | A favor | Contra | Por que não agora |
|---|---|---|---|
| Pacote único com subpacotes | Mais simples no começo; um `pyproject.toml` só | Nada impede o kit de importar o domínio; a fronteira vira convenção | O vazamento de domínio no kit é justamente o risco que esta decisão quer evitar |
| Vários repositórios (kit e domínio separados) | Fronteira física; versão própria do kit | Mudança que cruza os dois vira dois PRs e uma publicação no meio; CI duplicado | Custo alto para um time de uma pessoa; a separação física só faz sentido quando outro time consumir o kit |
| Monorepo com Poetry ou pip-tools | Ferramentas conhecidas | Suporte a workspace mais fraco; resolução mais lenta | O uv resolve o workspace inteiro, gerencia o Python e é rápido no CI |

## Consequências

**Positivas**

- A fronteira entre o genérico e o jurídico é verificada por máquina, não por revisão.
- Uma mudança que atravessa kit e domínio entra num commit só e é testada junto.
- O kit pode virar um wheel publicado na F10 sem refatoração, porque já nasce com dependências próprias.

**Negativas e riscos**

- Mais arquivos de configuração (um `pyproject.toml` por membro do workspace).
- Contrato de import mal escrito dá falsa segurança: os contratos precisam de um teste que falhe de propósito.
- Ferramentas que não entendem workspace (alguns plugins de IDE) podem precisar de ajuste no `PYTHONPATH`.

## Como validar

- O import-linter roda no CI e quebra o build quando alguém importa `ementa` dentro de `ementa_kit`. Um teste de fixture prova que o contrato pega a violação.
- Na F10, o template Copier gera um agente novo que depende só do `ementa-kit`, sem o domínio jurídico, e os testes desse agente passam no CI.
- Gatilho para revisitar: um segundo time ou produto passar a consumir o kit. Aí vale publicar o kit em repositório próprio, com versionamento semântico.

## Referências

- Documentação de workspaces do uv: <https://docs.astral.sh/uv/concepts/projects/workspaces/>
- Documentação do import-linter: <https://import-linter.readthedocs.io/>

---

[Índice de ADRs](README.md)
