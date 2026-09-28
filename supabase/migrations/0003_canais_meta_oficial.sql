-- Canais OFICIAIS da Meta: WhatsApp Cloud API e Instagram Messaging API.
--
-- Diferença crucial para o Render free tier (750h/mês, hiberna após ~15 min):
-- aqui o Meta faz POST no webhook (Edge Function do Supabase) sempre que chega
-- mensagem, e a Edge Function acorda o Render. Não existe polling nem
-- keepalive, então o app pode ficar 99% do tempo dormindo.
--
-- Os canais NÃO OFICIAIS (Evolution/Baileys e instagrapi) ficam em outros tipos
-- de canal e não usam nada deste arquivo.

ALTER TABLE canais DROP CONSTRAINT IF EXISTS canais_tipo_check;

ALTER TABLE canais
  ADD CONSTRAINT canais_tipo_check
  CHECK (tipo IN (
    'telegram',
    'whatsapp',            -- NÃO OFICIAL: Evolution API / Baileys
    'instagram',            -- NÃO OFICIAL: instagrapi (só sessionid)
    'webhook',
    'whatsapp_oficial',    -- OFICIAL: WhatsApp Cloud API
    'instagram_oficial'    -- OFICIAL: Instagram Messaging API
  ));

-- Roteamento dos webhooks oficiais: a Edge Function resolve o canal pelo
-- phone_number_id / ig_user_id que vem no evento, sem segredo na URL.
CREATE INDEX IF NOT EXISTS idx_canais_tipo_identificador
  ON canais (tipo, (config ->> 'phone_number_id'));

CREATE INDEX IF NOT EXISTS idx_canais_tipo_ig_user
  ON canais (tipo, (config ->> 'ig_user_id'));
