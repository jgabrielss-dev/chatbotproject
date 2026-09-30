-- Chatbot Project SaaS - schema inicial (sem cobrança)

CREATE TABLE IF NOT EXISTS agentes (
  id SERIAL PRIMARY KEY,
  nome TEXT NOT NULL,
  system_prompt TEXT NOT NULL DEFAULT '',
  ativo BOOLEAN NOT NULL DEFAULT TRUE,
  -- 'produto', 'suporte' ou 'prompt': agente interno da plataforma (itens 7,
  -- 12 e 13). NULL em todo agente de cliente. A identidade dele — o prompt —
  -- vem de app/agentes_internos.py, e não desta coluna; ela existe para a
  -- linha não entrar na lista editável do painel. Ver
  -- supabase/migrations/0007_agentes_internos.sql.
  interno TEXT,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agentes_interno
  ON agentes (interno) WHERE interno IS NOT NULL;

CREATE TABLE IF NOT EXISTS canais (
  id SERIAL PRIMARY KEY,
  agente_id INTEGER NOT NULL REFERENCES agentes(id) ON DELETE CASCADE,
  -- 'whatsapp' e 'instagram' são integrações NÃO OFICIAIS (Evolution/Baileys e
  -- instagrapi). As oficiais da Meta ficam em 'whatsapp_oficial' e
  -- 'instagram_oficial'. Ver supabase/migrations/0003_canais_meta_oficial.sql.
  -- 'site' e o canal dos chats internos da plataforma (itens 7, 12, 13). Ver
  -- supabase/migrations/0007_agentes_internos.sql.
  tipo TEXT NOT NULL CHECK (tipo IN ('telegram', 'whatsapp', 'instagram', 'webhook', 'whatsapp_oficial', 'instagram_oficial', 'site')),
  nome TEXT NOT NULL,
  config JSONB NOT NULL DEFAULT '{}'::jsonb,
  ativo BOOLEAN NOT NULL DEFAULT TRUE,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_canais_agente ON canais (agente_id);
CREATE INDEX IF NOT EXISTS idx_canais_tipo ON canais (tipo);
-- Um canal 'site' por agente interno (itens 7, 12, 13). UNIQUE para o UPSERT
-- do boot ser idempotente. Ver supabase/migrations/0007_agentes_internos.sql.
CREATE UNIQUE INDEX IF NOT EXISTS idx_canais_site
  ON canais (agente_id) WHERE tipo = 'site';
-- Roteamento dos webhooks oficiais da Meta (o canal é resolvido pelo
-- phone_number_id / ig_user_id do evento, sem segredo na URL).
CREATE INDEX IF NOT EXISTS idx_canais_tipo_identificador
  ON canais (tipo, (config ->> 'phone_number_id'));
CREATE INDEX IF NOT EXISTS idx_canais_tipo_ig_user
  ON canais (tipo, (config ->> 'ig_user_id'));

-- Colunas abertas: "memoria" guarda o que o system prompt mandar a IA extrair do usuário
CREATE TABLE IF NOT EXISTS sessoes (
  id SERIAL PRIMARY KEY,
  agente_id INTEGER NOT NULL REFERENCES agentes(id) ON DELETE CASCADE,
  canal_id INTEGER NOT NULL REFERENCES canais(id) ON DELETE CASCADE,
  usuario_externo TEXT NOT NULL,
  memoria JSONB NOT NULL DEFAULT '{}'::jsonb,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
  atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (canal_id, usuario_externo)
);

CREATE INDEX IF NOT EXISTS idx_sessoes_agente ON sessoes (agente_id);

CREATE TABLE IF NOT EXISTS mensagens (
  id BIGSERIAL PRIMARY KEY,
  sessao_id INTEGER NOT NULL REFERENCES sessoes(id) ON DELETE CASCADE,
  de_ia BOOLEAN NOT NULL,
  texto TEXT NOT NULL,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Item da fila que gerou a linha ('caixa:<id>', 'caixa:<id>:ia'). O worker
  -- devolve a mensagem para a fila depois de uma falha no Gemini ou no envio;
  -- sem esta chave, cada tentativa gravava a pergunta de novo no histórico e
  -- contava 1 na cota do dono de novo (migration 0009).
  chave TEXT
);

CREATE INDEX IF NOT EXISTS idx_mensagens_sessao ON mensagens (sessao_id, criado_em);

-- Unico e PARCIAL de proposito: a fila deduplica pelo item, mas o chat do
-- navegador nao vem da fila e tem `chave` nula — vários NULL convivem sem
-- colidir, então repetir a mesma frase no chat continua gravando.
CREATE UNIQUE INDEX IF NOT EXISTS mensagens_chave_uniq
  ON mensagens (chave) WHERE chave IS NOT NULL;

-- Caixa de entrada durável: nenhuma mensagem recebida é ignorada.
-- O webhook apenas persiste a mensagem e um worker processa com retry (backoff).
CREATE TABLE IF NOT EXISTS caixa_entrada (
  id BIGSERIAL PRIMARY KEY,
  canal_id INTEGER NOT NULL REFERENCES canais(id) ON DELETE CASCADE,
  remetente TEXT NOT NULL,
  texto TEXT NOT NULL,
  -- Id único do evento (tg:update_id, wa:key.id, ig:thread:msg, web:uuid).
  -- NUNCA vazio: ver uq_caixa_origem.
  origem TEXT NOT NULL,
  payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL DEFAULT 'pendente'
    CHECK (status IN ('pendente', 'processando', 'respondido', 'sem_resposta', 'erro')),
  tentativas INTEGER NOT NULL DEFAULT 0,
  proxima_tentativa TIMESTAMPTZ NOT NULL DEFAULT now(),
  ultimo_erro TEXT,
  resposta TEXT,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Quando a resposta foi entregue.
  processado_em TIMESTAMPTZ,
  -- Quando a mensagem entrou em 'processando'. Sem isto, reenfileirar o que
  -- ficou preso usaria criado_em e devolveria à fila um item que estava sendo
  -- processado AGORA, respondendo a mesma mensagem duas vezes.
  processando_em TIMESTAMPTZ
);

-- Índice NÃO parcial: o PostgREST (upsert da edge function, onConflict
-- "canal_id,origem") não consegue provar o predicado de um índice parcial e a
-- inserção falharia. Consequência: 'origem' precisa ser única e não-vazia em
-- todas as mensagens, o que o app garante gerando id por evento.
CREATE UNIQUE INDEX IF NOT EXISTS uq_caixa_origem ON caixa_entrada (canal_id, origem);
CREATE INDEX IF NOT EXISTS idx_caixa_fila ON caixa_entrada (status, proxima_tentativa);
CREATE INDEX IF NOT EXISTS idx_caixa_canal ON caixa_entrada (canal_id, criado_em);

-- Sessões do Instagram guardadas no Postgres (grátis, junto do resto).
-- O disco do Render é efêmero: sem isto, todo cold start relogaria a conta e
-- a conta cairia no limite de logins/bloqueio do Instagram.
-- A chave é 'sessionid:<sha1[:8]>' (nunca o sessionid cru, que é segredo).
CREATE TABLE IF NOT EXISTS instagram_sessoes (
  username TEXT PRIMARY KEY,
  dados JSONB NOT NULL DEFAULT '{}'::jsonb,
  atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- RLS ligado e sem policy: o app acessa com service_role (que ignora RLS) e o
-- anon/authenticated do PostgREST não enxerga nada. Sem isto, qualquer pessoa
-- com a public anon key leria os cookies de sessão do Instagram pela API.
ALTER TABLE instagram_sessoes ENABLE ROW LEVEL SECURITY;

-- ==========================================================================
-- Contas e multi-tenant
--
-- Este bloco e o mesmo da migration 0004_contas_e_multitenant.sql, mantido
-- aqui porque scripts/init_db.py aplica ESTE arquivo (nao as migrations) numa
-- base nova. Os dois precisam ficar em sincronia: mudar a migration sem
-- mudar isto faz o banco de um dev ficar diferente do de producao.
-- ==========================================================================

-- Configuracao interna. Sem policy de RLS: so o service_role (nosso backend)
-- le e escreve. O papel de admin vem daqui, nao de um campo no navegador.
CREATE TABLE IF NOT EXISTS app_config (
  chave TEXT PRIMARY KEY,
  valor TEXT NOT NULL DEFAULT '',
  atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE app_config ENABLE ROW LEVEL SECURITY;

-- Um perfil por conta do Supabase Auth. `id` e o mesmo uuid de auth.users.
CREATE TABLE IF NOT EXISTS perfis (
  id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  nome TEXT,
  email TEXT,
  role TEXT NOT NULL DEFAULT 'usuario' CHECK (role IN ('admin', 'usuario')),
  bloqueado BOOLEAN NOT NULL DEFAULT false,
  -- A fonte da verdade do 2FA e o Supabase Auth (auth.mfa_factors). Isto aqui
  -- diz se a NOSSA API exige o segundo fator: sem a coluna, um token `aal1`
  -- continuaria valendo depois de a pessoa ativar o 2FA.
  mfa_ativo BOOLEAN NOT NULL DEFAULT false,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_perfis_role ON perfis (role);
ALTER TABLE perfis ENABLE ROW LEVEL SECURITY;

-- Cada cliente so le o proprio perfil. Quem adminstra papel usa a API, que
-- conecta com service_role e ignora RLS de proposito.
CREATE POLICY perfis_own_select ON perfis
  FOR SELECT USING (auth.uid() = id);

-- Trigger de cadastro: toda conta nova ganha um perfil na hora, sem depender
-- de ninguem lembrar de criar. O papel vem de app_config.admin_emails, que e
-- uma lista de e-mails separada por virgula.
CREATE OR REPLACE FUNCTION public.criar_perfil() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  v_email TEXT := lower(coalesce(NEW.email, ''));
  v_admins TEXT := '';
  v_role TEXT := 'usuario';
BEGIN
  SELECT valor INTO v_admins FROM app_config WHERE chave = 'admin_emails';
  -- Casamento por IGUALDADE de item da lista, e nao por `LIKE '%' || email ||
  -- '%'`. O `LIKE` casava por SUBSTRING: com 'maria@exemplo.com.br' na lista,
  -- 'maria@exemplo.com' e '@exemplo.com.br' viravam admin — e, pior, um
  -- cadastro sem e-mail (vazio) casava com '%%' e promoteia qualquer conta a
  -- admin. Aqui os dois lados sao comparados como lista separada por virgula,
  -- que e a mesma forma da migration 0004 e da funcao ja aplicada no banco.
  IF v_email <> ''
     AND position(',' || v_email || ','
                  in ',' || lower(replace(coalesce(v_admins, ''), ' ', '')) || ',') > 0
  THEN
    v_role := 'admin';
  END IF;

  INSERT INTO perfis (id, email, role) VALUES (NEW.id, v_email, v_role)
  ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.criar_perfil();

-- Contas que ja existiam antes desta migration entram como 'usuario'. O
-- primeiro admin precisa ser promovido na mao (ver comentarios da migration).
INSERT INTO perfis (id, email, role)
SELECT id, lower(email), 'usuario' FROM auth.users
ON CONFLICT (id) DO NOTHING;

-- Dono do agente. NULL = agente legado, sem dono; o admin logando reivindica
-- (app/main.py -> /api/eu). ON DELETE CASCADE: deletar a conta leva o agente,
-- e nao deixa um bot orfao respondendo no WhatsApp de um ex-cliente.
ALTER TABLE agentes ADD COLUMN IF NOT EXISTS dono_id UUID REFERENCES auth.users(id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS idx_agentes_dono ON agentes (dono_id);

-- RLS por tenant. O app hoje fala com o banco pela service_role e passa o
-- `dono_id` na propria query, entao estas policies sao a segunda camada: se
-- alguem plugar o PostgREST com a anon key, `authenticated` so enxerga a si
-- mesmo. Vale mais como rede de seguranca do que como mecanismo principal.
ALTER TABLE agentes ENABLE ROW LEVEL SECURITY;
CREATE POLICY agentes_own ON agentes FOR ALL
  USING (dono_id = auth.uid())
  WITH CHECK (dono_id = auth.uid());

ALTER TABLE canais ENABLE ROW LEVEL SECURITY;
CREATE POLICY canais_own ON canais FOR ALL
  USING (EXISTS (SELECT 1 FROM agentes a WHERE a.id = canais.agente_id AND a.dono_id = auth.uid()))
  WITH CHECK (EXISTS (SELECT 1 FROM agentes a WHERE a.id = canais.agente_id AND a.dono_id = auth.uid()));

ALTER TABLE sessoes ENABLE ROW LEVEL SECURITY;
CREATE POLICY sessoes_own ON sessoes FOR ALL
  USING (EXISTS (SELECT 1 FROM agentes a WHERE a.id = sessoes.agente_id AND a.dono_id = auth.uid()))
  WITH CHECK (EXISTS (SELECT 1 FROM agentes a WHERE a.id = sessoes.agente_id AND a.dono_id = auth.uid()));

ALTER TABLE mensagens ENABLE ROW LEVEL SECURITY;
CREATE POLICY mensagens_own ON mensagens FOR ALL
  USING (EXISTS (
    SELECT 1 FROM sessoes s JOIN agentes a ON a.id = s.agente_id
    WHERE s.id = mensagens.sessao_id AND a.dono_id = auth.uid()))
  WITH CHECK (EXISTS (
    SELECT 1 FROM sessoes s JOIN agentes a ON a.id = s.agente_id
    WHERE s.id = mensagens.sessao_id AND a.dono_id = auth.uid()));

ALTER TABLE caixa_entrada ENABLE ROW LEVEL SECURITY;
CREATE POLICY caixa_own ON caixa_entrada FOR ALL
  USING (EXISTS (
    SELECT 1 FROM canais c JOIN agentes a ON a.id = c.agente_id
    WHERE c.id = caixa_entrada.canal_id AND a.dono_id = auth.uid()))
  WITH CHECK (EXISTS (
    SELECT 1 FROM canais c JOIN agentes a ON a.id = c.agente_id
    WHERE c.id = caixa_entrada.canal_id AND a.dono_id = auth.uid()));

-- Grants do schema public. Um `CREATE SCHEMA public` (recuperacao de banco,
-- restore de backup) recria o schema SEM os grants que o Supabase instala, e a
-- edge function passa a falhar com "permission denied for schema public".
GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA PUBLIC GRANT ALL ON SEQUENCES TO service_role;

-- O SELECT para anon era o buraco: qualquer pessoa com a public anon key lia
-- canais.config (token do Telegram, sessionid do Instagram, access_token e
-- app_secret da Meta) direto pelo PostgREST, sem passar pela nossa API. Agora
-- `anon` nao tem nada, e `authenticated` fica so com leitura — a escrita e a
-- checagem de posse continuam sendo do backend.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA PUBLIC REVOKE ALL ON TABLES FROM anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA PUBLIC REVOKE ALL ON SEQUENCES FROM anon;

-- Leitura para quem tem sessao. As policies acima filtram pelo dono, entao
-- isto nao abre nada: apenas permite que o RLS possa ser avaliado.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA PUBLIC GRANT SELECT ON TABLES TO authenticated;

-- ==========================================================================
-- Planos, assinatura, consumo e pagamentos
--
-- Mesmo conteudo da migration 0005_planos_e_cobranca.sql, mantido aqui pelo
-- mesmo motivo do bloco acima: scripts/init_db.py aplica ESTE arquivo numa
-- base nova. Preco e regra de cota vivem em app/cobranca.py (fonte da verdade,
-- sem I/O, testavel); a tabela `planos` e o espelho que o app sincroniza no
-- boot, e `assinaturas`/`consumo_mensagens` sao o estado de cada conta.
--
-- A virada de periodo NAO roda por cron: e avaliada na leitura
-- (app.repos_cobranca.atualizar_periodo). Um plano pago nunca se renova
-- sozinho -- renovar sem pagamento seria serviço de graca.
-- ==========================================================================

CREATE TABLE IF NOT EXISTS planos (
  id TEXT PRIMARY KEY,
  nome TEXT NOT NULL,
  descricao TEXT NOT NULL DEFAULT '',
  preco_mensal NUMERIC(10, 2) NOT NULL DEFAULT 0,
  preco_anual NUMERIC(10, 2) NOT NULL DEFAULT 0,
  max_agentes INTEGER NOT NULL,
  max_canais_por_agente INTEGER NOT NULL,
  max_mensagens_por_agente_mes INTEGER NOT NULL,
  dias_gratis INTEGER NOT NULL DEFAULT 0,
  ordem INTEGER NOT NULL DEFAULT 0,
  ativo BOOLEAN NOT NULL DEFAULT TRUE
);

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
  -- true quando `plano_proximo` JA FOI PAGO (ver migration 0008). false é o
  -- pedido sem pagamento, que só vale se houver pagamento. Sem distinguir os
  -- dois, vencer o período ou dava serviço de graça ou jogava fora o que a
  -- pessoa tinha pago.
  proximo_pago BOOLEAN NOT NULL DEFAULT false,
  cancela_em TIMESTAMPTZ,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
  atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_assinaturas_plano ON assinaturas (plano_id);
CREATE INDEX IF NOT EXISTS idx_assinaturas_fim ON assinaturas (fim_periodo);

-- Limite POR AGENTE (item 9): a chave inclui o agente de proposito, para que
-- um cliente com 3 agentes nao esgote a cota de um so.
CREATE TABLE IF NOT EXISTS consumo_mensagens (
  usuario_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  agente_id INTEGER NOT NULL REFERENCES agentes(id) ON DELETE CASCADE,
  mes TEXT NOT NULL,
  mensagens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (usuario_id, agente_id, mes)
);
CREATE INDEX IF NOT EXISTS idx_consumo_mes ON consumo_mensagens (mes);

CREATE TABLE IF NOT EXISTS pagamentos (
  id BIGSERIAL PRIMARY KEY,
  usuario_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  plano_id TEXT NOT NULL REFERENCES planos(id),
  ciclo TEXT NOT NULL CHECK (ciclo IN ('mensal', 'anual')),
  valor NUMERIC(10, 2) NOT NULL,
  status TEXT NOT NULL DEFAULT 'pendente'
    CHECK (status IN ('pendente', 'pago', 'falhou', 'cancelado')),
  metodo TEXT NOT NULL DEFAULT '',
  referencia TEXT NOT NULL DEFAULT '',
  qr_code TEXT NOT NULL DEFAULT '',
  expira_em TIMESTAMPTZ,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
  pago_em TIMESTAMPTZ,
  aplicado_em TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_pagamentos_usuario ON pagamentos (usuario_id, criado_em DESC);
CREATE INDEX IF NOT EXISTS idx_pagamentos_pendente ON pagamentos (status) WHERE status = 'pendente';
CREATE INDEX IF NOT EXISTS idx_pagamentos_referencia ON pagamentos (referencia);

-- Migração de bases que já existem: `init_db.py` aplica ESTE arquivo, e o
-- CREATE TABLE IF NOT EXISTS acima não altera tabela criada antes. As colunas
-- do checkout PIX (item "cobrança real") chegam por ALTER idempotente.
ALTER TABLE pagamentos ADD COLUMN IF NOT EXISTS qr_code TEXT NOT NULL DEFAULT '';
ALTER TABLE pagamentos ADD COLUMN IF NOT EXISTS expira_em TIMESTAMPTZ;

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

ALTER TABLE pagamentos ENABLE ROW LEVEL SECURITY;
CREATE POLICY pagamentos_own ON pagamentos FOR ALL
  USING (auth.uid() = usuario_id)
  WITH CHECK (auth.uid() = usuario_id);

-- As tabelas novas entram no grant da service_role e perdem o acesso do anon,
-- que o bloco acima ja aplicava a "todas". Repetido aqui para quem le este
-- arquivo de cima para baixo sem ligar: o padrao do Postgres NAO e o mesmo
-- grant que o Supabase instala.
GRANT ALL ON planos, assinaturas, consumo_mensagens, pagamentos TO service_role;
REVOKE ALL ON planos, assinaturas, consumo_mensagens, pagamentos FROM anon;
