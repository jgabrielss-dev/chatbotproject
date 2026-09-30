-- Item 14 (os tres agentes internos nunca podem ser desviados) e os canais
-- que eles usam.

-- 1) 'site': o canal dos chats internos (itens 7, 12 e 13).
--
-- O chat da home, o gerador de prompt e o suporte NAO sao um bot do cliente: sao
-- agentes da propria plataforma, conversando pelo navegador. Eles reaproveitam
-- `sessoes`, `mensagens` e `processar_mensagem` (e por isso o item 15 vale
-- neles sem codigo extra), mas nao sao Telegram nem WhatsApp.
--
-- Por que um tipo novo e nao reaproveitar 'webhook': 'webhook' e o canal de
-- INTEGRACAO, o que o cliente consome. Um chat da plataforma marcado como
-- webhook apareceria na lista de canais do cliente, e ai o agente interno vira
-- algo que o cliente pode apontar para a URL dele - que e literalmente ser
-- desviado. O tipo separado mantem a separacao visivel no schema.
-- O DROP antes do ADD é para o caso da tabela já existir com a versão antiga
-- da constraint em algum ambiente: `ADD CONSTRAINT` falharia por nome repetido.
ALTER TABLE canais DROP CONSTRAINT IF EXISTS canais_tipo_check;
ALTER TABLE canais ADD CONSTRAINT canais_tipo_check
  CHECK (tipo IN ('telegram', 'whatsapp', 'instagram', 'webhook',
                  'whatsapp_oficial', 'instagram_oficial', 'site'));

-- 2) `agentes.interno`: qual agente interno e este.
--
-- Vale a coluna em vez de confiar no nome: o nome o cliente edita, a coluna nao
-- (a API recusa mexer em agente interno). A lista de agentes do cliente filtra
-- `interno IS NULL`, entao os tres nunca aparecem la nem podem ser editados nem
-- apagados - e o prompt deles nem sai desta tabela: vem de
-- `app/agentes_internos.py`, de forma que editar a linha nao muda o agente.
ALTER TABLE agentes ADD COLUMN IF NOT EXISTS interno TEXT;

-- Consulta de "este agente interno ja foi criado?" no boot. UNIQUE, e nao um
-- indice comum, porque e o que faz o UPSERT do boot ser idempotente: sem ele,
-- um `ON CONFLICT (interno)` nao tem conflito para casar e cada deploy criaria
-- um agente interno novo, com o historico órfão. Parcial porque a coluna e
-- NULL em todos os agentes de cliente, e NULL nao concorre com NULL.
CREATE UNIQUE INDEX IF NOT EXISTS idx_agentes_interno
  ON agentes (interno) WHERE interno IS NOT NULL;

-- Um canal 'site' por agente interno, pelo mesmo motivo. `idx_canais_agente` ja
-- existe para o SELECT, este e para o UPSERT.
CREATE UNIQUE INDEX IF NOT EXISTS idx_canais_site
  ON canais (agente_id) WHERE tipo = 'site';

-- 3) Um canal 'site' por agente interno.
--
-- `config` guarda o slug e a origem da sessao. Guardar aqui e nao em
-- `app_config` porque a sessao de um chat anônimo (o da home) precisa sobreviver
-- a reinicio do servidor sem perder o historico que a pessoa leu.
--
-- `dono_id` fica NULL de proposito: sao agentes da plataforma, nao de ninguem.
-- Como `_enviar_resposta` nao conhece 'site', nenhum worker tenta responder
-- por este canal - a resposta volta na propria requisicao HTTP.

-- 4) O consumo dos agentes internos nao conta para ninguem.
--
-- `consumo_mensagens` tem `dono_id NOT NULL`, e o `_cota_do_dono` ja pula
-- quem nao tem dono. Nao ha nada a fazer aqui alem do comentario: e o motivo de
-- `dono_id` continuar NULL, e nao de uma regra nova.

-- 5) Onde a sessao do chat vivo mora.
--
-- `sessoes` ja tem UNIQUE (canal_id, usuario_externo), que e exatamente a
-- identidade "esta pessoa neste chat". Para o da home, `usuario_externo` e um
-- token aleatorio que fica no navegador de quem chegou pelo link; para o de
-- suporte e o gerador, e o uuid da conta. Um nunca colide com o outro porque
-- sao canais diferentes.
