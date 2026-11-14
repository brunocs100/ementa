# ADR 0012: Console em FastAPI, Jinja2 e HTMX

**Status:** Aceito · **Data:** 2026-11-14 · **Fase:** F4 · **Ver também:** [Arquitetura](../ARQUITETURA.md), [Agentes](../AGENTES.md)

## Contexto

A partir da F4, o advogado precisa de uma tela para:

- ver a caixa de aprovações (HITL) dos rascunhos de peça e dos relatórios de contrato;
- editar e aprovar ou devolver cada um;
- acompanhar uma execução em andamento.

A sócia decide a triagem e o DPO consulta a auditoria.

O núcleo do console é a aprovação humana. Ela precisa de estado confiável: quem aprovou, o quê, com qual edição. O resultado volta para a thread do LangGraph certa.

O projeto é em Python, mantido por uma pessoa. Uma segunda stack (Node, bundler, framework de front) dobraria o que manter e testar. O console também precisa de aparência de produto, porque os advogados usam a caixa de aprovações no dia a dia.

## Decisão

- **FastAPI com Jinja2.** O console é renderizado no servidor, dentro do app `api`, com rotas sob `/console`.
- **HTMX nas interações.** Aprovar, devolver, editar trecho e paginar a caixa de entrada sem recarregar a página.
- **Progresso ao vivo por SSE**, o mesmo canal que a API já oferece.
- **Sem build de JavaScript.** O HTMX e o CSS são arquivos estáticos versionados, e o CSS tem tokens próprios, sem tema genérico.
- **A aprovação chama a mesma API REST** (`/v1/approvals`, F4) que um sistema externo usaria. O console não tem regra de negócio própria.
- **Autenticação e permissão iguais às da API** (JWT e RBAC da F3). A aprovação respeita o papel: só o advogado responsável ou a sócia aprova uma peça.

## Alternativas consideradas

| Alternativa | A favor | Contra | Por que não agora |
|---|---|---|---|
| Streamlit | Muito rápido para montar | Cara de demo; o modelo de reexecução do script complica estado de aprovação e permissão por usuário | O núcleo é aprovação com estado e permissão |
| Gradio | Pronto para chat e demo de modelo | Feito para demonstrar modelo de ML, não para fluxo de trabalho com papéis | Mesmo motivo do Streamlit |
| React ou Next.js | Melhor experiência rica; ecossistema grande | Segunda stack, build e testes de front; mais tempo do que o roadmap tem | Vale se o console virar produto; hoje o custo não compensa |

## Consequências

**Positivas**

- Uma stack só. O console vive no mesmo processo e nos mesmos testes da API.
- A aprovação passa pela API pública, então o fluxo de HITL é o mesmo pelo console, por sistema externo ou por teste.
- HTML renderizado no servidor é simples de proteger: CSRF, cabeçalhos de segurança e sem token no navegador além do cookie de sessão.

**Negativas e riscos**

- Interação muito rica (edição colaborativa, arrastar e soltar) fica difícil com HTMX. Fora do escopo.
- Sem framework de front, o acabamento visual depende de CSS bem feito.
- Templates Jinja2 sem teste quebram calados. Mitigação: testes de rota que conferem o HTML principal.

## Como validar

- Teste ponta a ponta do fluxo de aprovação: um `brief_drafting` chega ao `interrupt()`, aparece na caixa de entrada, o advogado edita e aprova pelo console, e a thread retoma e finaliza a peça com a edição.
- Teste de permissão: o estagiário vê o rascunho, mas o botão de aprovar não aparece e a API recusa com 403.
- Gatilho para revisitar: o console passar a ter usuários de verdade pedindo interações que o HTMX não sustenta.

## Referências

- FastAPI, templates: <https://fastapi.tiangolo.com/advanced/templates/>
- HTMX: <https://htmx.org/docs/>

---

[Índice de ADRs](README.md)
