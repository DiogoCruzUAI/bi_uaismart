"""Esquema inicial da plataforma

Tenants, usuários, conexões a bancos de clientes, catálogo semântico, chat e o
registro de procedência das consultas.

Gerada a partir de `Base.metadata` sem banco de dados: `alembic revision
--autogenerate` precisa de um Postgres de pé para comparar, e não havia um na
máquina onde isto foi escrito. As operações saíram de `CreateTableOp.from_table`
sobre `metadata.sorted_tables`, que já respeita a dependência de chave estrangeira.
Confira contra um banco real antes de considerar definitiva.

Revision ID: 0001_esquema_inicial
Revises:
Create Date: 2026-09-29
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision: str = "0001_esquema_inicial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('tenants',
    sa.Column('nome', sa.String(length=200), nullable=False),
    sa.Column('slug', sa.String(length=60), nullable=False),
    sa.Column('plano', sa.Enum('trial', 'basico', 'profissional', 'empresarial', name='planotenant', native_enum=False), nullable=False),
    sa.Column('ativo', sa.Boolean(), nullable=False),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('criado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('atualizado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('deletado_em', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('conexoes',
    sa.Column('nome', sa.String(length=120), nullable=False),
    sa.Column('tipo', sa.Enum('postgres', 'mysql', name='tipobanco', native_enum=False), nullable=False),
    sa.Column('host', sa.String(length=255), nullable=False),
    sa.Column('porta', sa.Integer(), nullable=False),
    sa.Column('banco', sa.String(length=120), nullable=False),
    sa.Column('usuario', sa.String(length=120), nullable=False),
    sa.Column('senha_cifrada', sa.Text(), nullable=False),
    sa.Column('usar_ssl', sa.Boolean(), nullable=False),
    sa.Column('status', sa.Enum('pendente', 'conectada', 'catalogada', 'erro', name='statusconexao', native_enum=False), nullable=False),
    sa.Column('ultimo_erro', sa.Text(), nullable=True),
    sa.Column('ultimo_teste_em', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ultimo_perfilamento_em', sa.DateTime(timezone=True), nullable=True),
    sa.Column('versao_dicionario', sa.Integer(), nullable=False),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('tenant_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('criado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('atualizado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('deletado_em', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'nome', name='uq_conexao_tenant_nome')
    )
    op.create_table('usuarios',
    sa.Column('tenant_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('nome', sa.String(length=200), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('senha_hash', sa.String(length=255), nullable=False),
    sa.Column('perfil', sa.Enum('admin', 'analista', 'leitor', name='perfilusuario', native_enum=False), nullable=False),
    sa.Column('ativo', sa.Boolean(), nullable=False),
    sa.Column('ultimo_acesso_em', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('criado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('atualizado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('deletado_em', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'email', name='uq_usuario_tenant_email')
    )
    op.create_table('cat_tabelas',
    sa.Column('conexao_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('esquema', sa.String(length=120), nullable=False),
    sa.Column('nome', sa.String(length=200), nullable=False),
    sa.Column('eh_view', sa.Boolean(), nullable=False),
    sa.Column('linhas_estimadas', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('bytes_estimados', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('papel', sa.Enum('fato', 'dimensao', 'ponte', 'desconhecido', name='papeltabela', native_enum=False), nullable=False),
    sa.Column('descricao', sa.Text(), nullable=True),
    sa.Column('descricao_negativa', sa.Text(), nullable=True),
    sa.Column('recorte_obrigatorio', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=True),
    sa.Column('confianca_ia', sa.Float(), nullable=True),
    sa.Column('revisada_em', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revisada_por_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('tenant_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('criado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('atualizado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('deletado_em', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['conexao_id'], ['conexoes.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['revisada_por_id'], ['usuarios.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('conexao_id', 'esquema', 'nome', name='uq_tabela_conexao_esquema_nome')
    )
    op.create_table('conversas',
    sa.Column('usuario_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('conexao_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('titulo', sa.String(length=200), nullable=False),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('tenant_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('criado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('atualizado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('deletado_em', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['conexao_id'], ['conexoes.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['usuario_id'], ['usuarios.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('cat_colunas',
    sa.Column('tabela_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('nome', sa.String(length=200), nullable=False),
    sa.Column('tipo_sql', sa.String(length=80), nullable=False),
    sa.Column('aceita_nulo', sa.Boolean(), nullable=False),
    sa.Column('eh_chave_primaria', sa.Boolean(), nullable=False),
    sa.Column('descricao', sa.Text(), nullable=True),
    sa.Column('descricao_negativa', sa.Text(), nullable=True),
    sa.Column('unidade', sa.Enum('monetaria', 'percentual', 'contagem', 'duracao', 'data', 'texto', 'identificador', 'codigo', 'desconhecida', name='unidadecoluna', native_enum=False), nullable=False),
    sa.Column('escala', sa.Float(), nullable=False),
    sa.Column('moeda', sa.String(length=3), nullable=True),
    sa.Column('dominio_tabela_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('cardinalidade_estimada', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('fracao_nula', sa.Float(), nullable=True),
    sa.Column('amostra', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=True),
    sa.Column('confianca_ia', sa.Float(), nullable=True),
    sa.Column('revisada_em', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revisada_por_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('tenant_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('criado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('atualizado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('deletado_em', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['dominio_tabela_id'], ['cat_tabelas.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['revisada_por_id'], ['usuarios.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tabela_id'], ['cat_tabelas.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tabela_id', 'nome', name='uq_coluna_tabela_nome')
    )
    op.create_table('cat_medidas',
    sa.Column('conexao_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('tabela_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('chave', sa.String(length=80), nullable=False),
    sa.Column('rotulo', sa.String(length=200), nullable=False),
    sa.Column('descricao', sa.Text(), nullable=True),
    sa.Column('expressao_sql', sa.Text(), nullable=False),
    sa.Column('filtros_implicitos', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=True),
    sa.Column('base_minima', sa.Integer(), nullable=False),
    sa.Column('confianca_ia', sa.Float(), nullable=True),
    sa.Column('revisada_em', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revisada_por_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('tenant_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('criado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('atualizado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('deletado_em', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['conexao_id'], ['conexoes.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['revisada_por_id'], ['usuarios.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tabela_id'], ['cat_tabelas.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('conexao_id', 'chave', name='uq_medida_conexao_chave')
    )
    op.create_table('mensagens',
    sa.Column('conversa_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('papel', sa.Enum('usuario', 'assistente', name='papelmensagem', native_enum=False), nullable=False),
    sa.Column('conteudo', sa.Text(), nullable=False),
    sa.Column('grafico', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=True),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('tenant_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('criado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('atualizado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('deletado_em', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['conversa_id'], ['conversas.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('cat_relacionamentos',
    sa.Column('conexao_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('coluna_origem_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('coluna_destino_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('origem', sa.Enum('fk_declarada', 'inferida', 'manual', name='origemrelacionamento', native_enum=False), nullable=False),
    sa.Column('confianca', sa.Float(), nullable=False),
    sa.Column('revisada_em', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('tenant_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('criado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('atualizado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('deletado_em', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['coluna_destino_id'], ['cat_colunas.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['coluna_origem_id'], ['cat_colunas.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['conexao_id'], ['conexoes.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('consultas',
    sa.Column('mensagem_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('conexao_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('pergunta', sa.Text(), nullable=False),
    sa.Column('modo', sa.Enum('semantico', 'exploratorio', name='modoconsulta', native_enum=False), nullable=False),
    sa.Column('status', sa.Enum('gerada', 'recusada', 'executada', 'erro', 'expirada', name='statusconsulta', native_enum=False), nullable=False),
    sa.Column('motivo_recusa', sa.Text(), nullable=True),
    sa.Column('spec', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=True),
    sa.Column('sql_gerado', sa.Text(), nullable=True),
    sa.Column('linhas_estimadas', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('custo_estimado', sa.Float(), nullable=True),
    sa.Column('linhas_retornadas', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=True),
    sa.Column('duracao_ms', sa.Integer(), nullable=True),
    sa.Column('houve_supressao', sa.Boolean(), nullable=False),
    sa.Column('versao_dicionario', sa.Integer(), nullable=False),
    sa.Column('modelo_llm', sa.String(length=60), nullable=True),
    sa.Column('tokens_entrada', sa.Integer(), nullable=True),
    sa.Column('tokens_cache_leitura', sa.Integer(), nullable=True),
    sa.Column('tokens_saida', sa.Integer(), nullable=True),
    sa.Column('veio_do_cache', sa.Boolean(), nullable=False),
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('tenant_id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), nullable=False),
    sa.Column('criado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('atualizado_em', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column('deletado_em', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['conexao_id'], ['conexoes.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['mensagem_id'], ['mensagens.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tenants_slug'), 'tenants', ['slug'], unique=True)
    op.create_index(op.f('ix_conexoes_tenant_id'), 'conexoes', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_usuarios_tenant_id'), 'usuarios', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_cat_tabelas_tenant_id'), 'cat_tabelas', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_cat_tabelas_conexao_id'), 'cat_tabelas', ['conexao_id'], unique=False)
    op.create_index(op.f('ix_conversas_tenant_id'), 'conversas', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_conversas_conexao_id'), 'conversas', ['conexao_id'], unique=False)
    op.create_index(op.f('ix_conversas_usuario_id'), 'conversas', ['usuario_id'], unique=False)
    op.create_index(op.f('ix_cat_colunas_tabela_id'), 'cat_colunas', ['tabela_id'], unique=False)
    op.create_index(op.f('ix_cat_colunas_tenant_id'), 'cat_colunas', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_cat_medidas_conexao_id'), 'cat_medidas', ['conexao_id'], unique=False)
    op.create_index(op.f('ix_cat_medidas_tenant_id'), 'cat_medidas', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_mensagens_tenant_id'), 'mensagens', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_mensagens_conversa_id'), 'mensagens', ['conversa_id'], unique=False)
    op.create_index(op.f('ix_cat_relacionamentos_conexao_id'), 'cat_relacionamentos', ['conexao_id'], unique=False)
    op.create_index(op.f('ix_cat_relacionamentos_tenant_id'), 'cat_relacionamentos', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_consultas_tenant_id'), 'consultas', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_consultas_mensagem_id'), 'consultas', ['mensagem_id'], unique=False)
    op.create_index(op.f('ix_consultas_conexao_id'), 'consultas', ['conexao_id'], unique=False)


def downgrade() -> None:
    op.drop_table('consultas')
    op.drop_table('cat_relacionamentos')
    op.drop_table('mensagens')
    op.drop_table('cat_medidas')
    op.drop_table('cat_colunas')
    op.drop_table('conversas')
    op.drop_table('cat_tabelas')
    op.drop_table('usuarios')
    op.drop_table('conexoes')
    op.drop_table('tenants')
