-- Pagamento antecipado: o plano novo só vira regra quando o período pago vence.
--
-- O problema que esta coluna resolve: `_abrir_periodo_pago` recebia o pagamento
-- do plano novo e escrevia `plano_id` na mesma hora, mesmo com `inicio_periodo`
-- no futuro. A regra do item 9 ("preço e regras mudam só no fim do período já
-- pago") ficava furada justamente no caso em que ela existe — o downgrade
-- antecipado cortava a cota que o cliente ainda tinha pago, e o upgrade dava
-- o plano grande antes da hora.
--
-- `proximo_pago` distingue as duas coisas que `plano_proximo` guardava misturadas:
--   * false — o cliente PEDIU a troca (button "Contratar"), nada foi pago.
--     Ela só vale se houver pagamento; sem isso, vencer o período transformava
--     um pedido em serviço de graça.
--   * true  — o pagamento JÁ foi confirmado e aplicado. Vencendo o período,
--     `atualizar_periodo` promove o plano e abre o período novo.
--
-- Sem a coluna, as duas situações eram indistinguíveis e o código tinha de
-- escolher: promover (dá serviço grátis) ou expirar (joga fora o que foi pago).

ALTER TABLE assinaturas
  ADD COLUMN IF NOT EXISTS proximo_pago BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN assinaturas.proximo_pago IS
  'true quando plano_proximo já foi pago: vence o período e o plano entra em vigor';