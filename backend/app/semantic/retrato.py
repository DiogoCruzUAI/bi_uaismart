"""O retrato de um banco de cliente: tudo que a plataforma observou, antes de julgar.

O retrato é a entrada do gerador de dicionário. Ele carrega **fatos e sinais**, nunca
interpretação: `salario` é `bigint`, tem 4,2 milhões de valores distintos e chega a
magnitude 10. Que isso significa centavos é conclusão do LLM, não deste módulo.

`para_prompt()` existe porque despejar o retrato inteiro no contexto é caro e é
exatamente o que o mantra proíbe. A versão compacta descarta o que não ajuda a
decidir — e o que sobra é o que o modelo precisa para julgar unidade, escala e
semântica.
"""

from dataclasses import dataclass, field
from datetime import datetime

from app.semantic.estrutura import LigacaoInferida
from app.semantic.sinais import SinaisColuna


@dataclass(slots=True)
class RetratoColuna:
    nome: str
    tipo_sql: str
    aceita_nulo: bool
    eh_chave_primaria: bool
    sinais: SinaisColuna
    cardinalidade: int | None = None
    fracao_nula: float | None = None
    valores_comuns: list | None = None
    minimo: object = None
    maximo: object = None

    def para_prompt(self) -> dict:
        """Forma compacta para o prompt do dicionário.

        Campo nulo é omitido: em um banco com 15 mil colunas, `"minimo": null`
        repetido milhares de vezes é custo puro e não informa nada.
        """
        d: dict = {"nome": self.nome, "tipo": self.tipo_sql}
        if self.eh_chave_primaria:
            d["chave_primaria"] = True
        if not self.aceita_nulo:
            d["obrigatoria"] = True

        s = self.sinais
        marcas = [
            marca
            for marca, ligada in (
                ("identificador", s.parece_identificador),
                ("categorica", s.parece_categorico),
                ("constante", s.parece_constante),
                ("quase_sempre_nula", s.quase_sempre_nulo),
                ("sem_estatistica", s.sem_estatistica),
            )
            if ligada
        ]
        if marcas:
            d["sinais"] = marcas
        if self.cardinalidade is not None:
            d["valores_distintos"] = self.cardinalidade
        if s.magnitude_maxima is not None:
            # A informação que permite julgar escala. Um salário com magnitude 10
            # não está em reais — mas quem conclui isso é o modelo.
            d["magnitude"] = s.magnitude_maxima
        if self.minimo is not None or self.maximo is not None:
            d["faixa"] = [self.minimo, self.maximo]
        if self.valores_comuns:
            d["exemplos"] = self.valores_comuns[:8]
        return d


@dataclass(slots=True)
class RetratoTabela:
    esquema: str
    nome: str
    eh_view: bool
    papel: str
    linhas_estimadas: int | None
    bytes_estimados: int | None
    colunas: list[RetratoColuna] = field(default_factory=list)

    @property
    def qualificado(self) -> str:
        return f"{self.esquema}.{self.nome}"

    def para_prompt(self) -> dict:
        d: dict = {
            "tabela": self.qualificado,
            "papel": self.papel,
            "colunas": [c.para_prompt() for c in self.colunas],
        }
        if self.eh_view:
            d["view"] = True
        if self.linhas_estimadas is not None:
            d["linhas_estimadas"] = self.linhas_estimadas
        return d


@dataclass(slots=True)
class LigacaoDeclarada:
    esquema_origem: str
    tabela_origem: str
    coluna_origem: str
    esquema_destino: str
    tabela_destino: str
    coluna_destino: str

    def para_prompt(self) -> dict:
        return {
            "de": f"{self.esquema_origem}.{self.tabela_origem}.{self.coluna_origem}",
            "para": f"{self.esquema_destino}.{self.tabela_destino}.{self.coluna_destino}",
            "confianca": 1.0,
            "origem": "declarada",
        }


@dataclass(slots=True)
class RetratoBanco:
    coletado_em: datetime
    tabelas: list[RetratoTabela] = field(default_factory=list)
    ligacoes_declaradas: list[LigacaoDeclarada] = field(default_factory=list)
    ligacoes_inferidas: list[LigacaoInferida] = field(default_factory=list)
    # Tabelas que existem no banco mas ficaram de fora do retrato, por limite.
    tabelas_nao_perfiladas: int = 0

    @property
    def total_colunas(self) -> int:
        return sum(len(t.colunas) for t in self.tabelas)

    def tabela(self, qualificado: str) -> RetratoTabela | None:
        alvo = qualificado.lower()
        return next((t for t in self.tabelas if t.qualificado.lower() == alvo), None)

    def para_prompt(self, tabelas: list[str] | None = None) -> dict:
        """Retrato compacto, opcionalmente restrito a algumas tabelas.

        O parâmetro `tabelas` é o que torna a recuperação de contexto possível: para
        responder "quantas empresas abriram em MG", o modelo não precisa das outras
        quatro mil tabelas do banco. Mandar tudo seria força bruta paga por token.
        """
        escolhidas = self.tabelas
        if tabelas is not None:
            alvo = {t.lower() for t in tabelas}
            escolhidas = [t for t in self.tabelas if t.qualificado.lower() in alvo]

        nomes = {t.qualificado.lower() for t in escolhidas}

        def relevante(de: str, para: str) -> bool:
            return de.lower() in nomes or para.lower() in nomes

        declaradas = [
            lig.para_prompt()
            for lig in self.ligacoes_declaradas
            if relevante(
                f"{lig.esquema_origem}.{lig.tabela_origem}",
                f"{lig.esquema_destino}.{lig.tabela_destino}",
            )
        ]
        inferidas = [
            {
                "de": f"{lig.esquema_origem}.{lig.tabela_origem}.{lig.coluna_origem}",
                "para": f"{lig.esquema_destino}.{lig.tabela_destino}.{lig.coluna_destino}",
                "confianca": lig.confianca,
                "origem": "inferida",
                "motivo": lig.motivo,
            }
            for lig in self.ligacoes_inferidas
            if relevante(
                f"{lig.esquema_origem}.{lig.tabela_origem}",
                f"{lig.esquema_destino}.{lig.tabela_destino}",
            )
        ]

        return {
            "tabelas": [t.para_prompt() for t in escolhidas],
            "ligacoes": declaradas + inferidas,
        }
