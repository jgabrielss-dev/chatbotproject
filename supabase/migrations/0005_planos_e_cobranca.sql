-- Chatbot Project SaaS - planos, assinatura e consumo
--
-- O que este bloco resolve (item 9 + 11 do pedido):
--   1. quantos agentes a conta pode ter;
--   2. quantos canais POR AGENTE;
--   3. quantas mensagens POR AGENTE por mes.
--   Mais volume = preco unitario menor, e quem paga o ano antecipado paga menos.
--   Troca de plano (upgrade/downgrade) e pedida pelo perfil e so vale no fim do
--   periodo JA PAGO: por isso `plano_proximo` e um campo, e nao um UPDATE em
--   `plano_id`.
--
-- Precos em REAL. A pesquisa de mercado que motivou os numeros esta comentada
-- em app/cobranca.py (CATALOGO_PADRAO), que e o codigo que faz o seed desta
-- tabela no boot. Aqui fica so o formato.

CREATE TABLE IF NOT EXISTS planos (
  id TEXT PRIMARY KEY,
  nome TEXT NOT NULL,
  descricao TEXT NOT NULL DEFAULT '',
  -- Preco do mes e do ano. O anual ja vem com o desconto embutido (20%), que e
  -- o mesmo desconto que Chatbase e Botpress anunciam na pagina de precos.
  preco_mensal NUMERIC(10, 2) NOT NULL DEFAULT 0,
  preco_anual NUMERIC(10, 2) NOT NULL DEFAULT 0,
  max_agentes INTEGER NOT NULL,
  max_canais_por_agente INTEGER NOT NULL,
  max_mensagens_por_agente_mes INTEGER NOT NULL,
  -- 7 para o plano de teste (item 11). 0 para os planos pagos.
  dias_gratis INTEGER NOT NULL DEFAULT 0,
  ordem INTEGER NOT NULL DEFAULT 0,
  ativo BOOLEAN NOT NULL DEFAULT TRUE
);

-- Uma assinatura por conta. `plano_id` e o que vale AGORA; `plano_proximo` e o
-- que o cliente pediu e que entra no fim do periodo pago. `fim_periodo` e a
-- data que o app le para decidir a virada (nao ha cron: a virada e avaliada
-- na leitura, o que faz o sistema funcionar sem nenhum agendador).
CREATE TABLE IF NOT EXISTS assinaturas (
  usuario_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  plano_id TEXT NOT NULL REFERENCES planos(id),
  status TEXT NOT NULL DEFAULT 'ativo'
    CHECK (status IN ('teste', 'ativo', 'expirado', 'cancelado')),
  ciclo TEXT NOT NULL DEFAULT 'mensal' CHECK (ciclo IN ('mensal', 'anual')),
  inicio_periodo TIMESTAMPTZ NOT NULL DEFAULT now(),
  fim_periodo TIMESTAMPTZ NOT NULL,
  plano_proximo TEXT REFERENCES planos(id),
  ciclo_proximo TEXT CHECK (ciclo_proximo IN ('mensal', 'anual')),
  -- Cancelamento tambem e agendado: o cliente paga ate o fim e o acesso acaba
  -- junto. Guardar a data evita Depends-on: um "cancela em" ambiguo.
  cancela_em TIMESTAMPTZ,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
  atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_assinaturas_plano ON assinaturas (plano_id);
CREATE INDEX IF NOT EXISTS idx_assinaturas_fim ON assinaturas (fim_periodo);

-- Consumo de mensagens por AGENTE e por mes. A chave e (conta, agente, mes):
-- o limite do item 9 e POR AGENTE, e nao por conta -- uma conta com 3 agentes
-- nao pode gastar o saldo de um so.
--
-- Contador e nao COUNT(*) em `mensagens`: a checagem acontece no caminho quente
-- (toda mensagem que entra passa por aqui) e um COUNT com JOIN de tres tabelas
-- por mensagem custaria mais do que o envio inteiro. A tabela e autoritativa
-- para efeito de cota; `mensagens` continua sendo o historico.
CREATE TABLE IF NOT EXISTS consumo_mensagens (
  usuario_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  agente_id INTEGER NOT NULL REFERENCES agentes(id) ON DELETE CASCADE,
  -- 'YYYY-MM' em UTC. Texto e nao date porque o agrupamento e por string e
  -- `'2026-01' < '2026-02'` ja ordena.
  mes TEXT NOT NULL,
  mensagens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (usuario_id, agente_id, mes)
);

CREATE INDEX IF NOT EXISTS idx_consumo_mes ON consumo_mensagens (mes);

-- Livro de pagamentos. A integração de cobrança (gateway) ainda NÃO existe --
-- o pedido é para não travar o trabalho esperando ela -- mas o livro já fica,
-- porque duas coisas dependem dele: nada renova sozinho (um plano pago que
-- virasse "ativo" sem pagamento seria serviço grátis), e o agente de suporte
-- precisa poder lançar um pagamento confirmado a pedido do cliente.
CREATE TABLE IF NOT EXISTS pagamentos (
  id BIGSERIAL PRIMARY KEY,
  usuario_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  plano_id TEXT NOT NULL REFERENCES planos(id),
  ciclo TEXT NOT NULL CHECK (ciclo IN ('mensal', 'anual')),
  valor NUMERIC(10, 2) NOT NULL,
  status TEXT NOT NULL DEFAULT 'pendente'
    CHECK (status IN ('pendente', 'pago', 'falhou', 'cancelado')),
  -- 'gateway' quando o cobrador real marcar; 'suporte' quando foi lançado à mão
  -- pelo agente de suporte. A distinção existe para o admin saber de onde veio
  -- a receita, não para dar crédito em cima de crédito.
  metodo TEXT NOT NULL DEFAULT '',
  referencia TEXT NOT NULL DEFAULT '',
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
  pago_em TIMESTAMPTZ,
  -- Quando o pagamento estendeu o período da assinatura. Um pagamento pode
  -- estar 'pago' e ainda não aplicado (pago adiantado, período que só começa no
  -- fim do que já foi pago); este campo separa os dois casos sem precisar
  -- inferir por data.
  aplicado_em TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_pagamentos_usuario ON pagamentos (usuario_id, criado_em DESC);
CREATE INDEX IF NOT EXISTS idx_pagamentos_pendente ON pagamentos (status) WHERE status = 'pendente';

ALTER TABLE pagamentos ENABLE ROW LEVEL SECURITY;
CREATE POLICY pagamentos_own ON pagamentos FOR ALL
  USING (auth.uid() = usuario_id)
  WITH CHECK (auth.uid() = usuario_id);

-- --------------------------------------------------------------------------
-- Nome do usuário (item 5: "o cadastro pede o nome")
--
-- `perfis` só tinha e-mail, e o e-mail é identificador de login — usá-lo como
-- nome na interface daria "olá, cliente@exemplo.com.br" em toda tela interna.
-- A coluna é preenchida pelo cadastro e editável na tela de conta; quando
-- vier vazia (conta antiga) a interface mostra o e-mail, que é o que já
-- funcionava.
-- --------------------------------------------------------------------------
ALTER TABLE perfis ADD COLUMN IF NOT EXISTS nome TEXT;

-- RLS: a assinatura e o consumo de cada conta nao sao visiveis a ninguem alem
-- da propria conta. O app le com service_role (ignora RLS), entao isto e a
-- segunda camada, igual as policies de agentes/canais.
ALTER TABLE planos ENABLE ROW LEVEL SECURITY;
-- `planos` e leitura publica: a home mostra a tabela de precos para quem nem
-- esta logado ainda, e nao ha nada sensivel na tabela.
CREATE POLICY planos_publico ON planos FOR SELECT USING (ativo);

ALTER TABLE assinaturas ENABLE ROW LEVEL SECURITY;
CREATE POLICY assinaturas_own ON assinaturas FOR ALL
  USING (auth.uid() = usuario_id)
  WITH CHECK (auth.uid() = usuario_id);

ALTER TABLE consumo_mensagens ENABLE ROW LEVEL SECURITY;
CREATE POLICY consumo_own ON consumo_mensagens FOR ALL
  USING (auth.uid() = usuario_id)
  WITH CHECK (auth.uid() = usuario_id);

-- Contas criadas antes desta migration ja entram como admin: sem assinatura
-- elas cairiam no teste de 7 dias e perderiam o acesso ao que ja usavam.
-- O seed do app (app.cobranca.garantir_catalogo) faz a parte que depende do
-- papel; aqui so fica o texto de quem o operador precisa rodar uma vez.
