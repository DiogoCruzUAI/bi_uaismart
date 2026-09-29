# Frontend — a construir

**Next.js** (App Router) + TypeScript + Recharts.

A escolha não é preferência: o AlumiPremium já tem um dashboard Next.js + Recharts
consumindo uma camada semântica por HTTP com JWT. É o mesmo formato de payload e o
mesmo padrão de autenticação que este projeto vai precisar, então há código e
aprendizado para reaproveitar em vez de redescobrir.

Não foi gerado ainda de propósito: `create-next-app` deve rodar quando houver uma API
com contrato estável para consumir. Scaffold gerado antes do contrato vira retrabalho.

## O que precisa existir

| Módulo | Responsabilidade |
|---|---|
| `chat/` | conversa com streaming da resposta |
| `viz/` | renderização do gráfico a partir da spec devolvida pela API |
| `catalogo/` | revisão e aprovação humana do dicionário gerado pela IA |
| `dashboard/` | painéis persistentes |

`catalogo/` é o que costuma ser esquecido e é o que sustenta a confiança no número: o
dicionário é gerado pela IA e **aprovado por uma pessoa do tenant**. Sem essa tela, a
aprovação não acontece e a plataforma fica dependendo do palpite do modelo.

## Exigências de interface

- Todo número exibido mostra a procedência sob demanda: medida, filtros, base de
  cálculo e o SQL que rodou. Um BI que esconde o SQL está pedindo confiança cega.
- Resposta do caminho exploratório é **visivelmente rotulada** como tal.
- Célula suprimida por base insuficiente aparece como suprimida, com a contagem —
  nunca some em silêncio, nunca vira zero.
- Dicionário não revisado por humano é sinalizado na resposta que dependeu dele.
