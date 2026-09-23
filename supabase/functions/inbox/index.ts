// Edge Function "inbox" — fila de entrada sempre disponível.
//
// Recbe os webhooks do Telegram e da Evolution API mesmo quando o app no
// Render está dormindo (free tier), grava a mensagem na caixa_entrada
// (queue durável no Postgres/Supabase) e SO ENTÃO acorda o Render para
// processar. Assim o Render fica 99% do tempo dormindo, economizando as
// 750h/mês do free tier.
//
// Rotas (POST):
//   /functions/v1/inbox/telegram/{canal_id}/{secret}
//   /functions/v1/inbox/evolution/{canal_id}
import { createClient } from "jsr:@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SERVICE_ROLE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
// URLs que o app (node) tem acesso; disparam o spin-up do Render/Evolution
const RENDER_HEALTH_URL = Deno.env.get("RENDER_HEALTH_URL") ?? "";
const EVOLUTION_URL = Deno.env.get("EVOLUTION_URL") ?? "";

const supabase = createClient(SUPABASE_URL, SERVICE_ROLE, {
  auth: { persistSession: false },
});

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

function json(body: Record<string, unknown>, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });
}

// Dispara (sem travar) o wake. O EdgeRuntime.waitUntil segura o fetch
// depois que a resposta já foi devolvida para o Telegram/Evolution.
function wake() {
  const targets: Promise<unknown>[] = [];
  if (RENDER_HEALTH_URL) {
    targets.push(
      fetch(RENDER_HEALTH_URL, { signal: AbortSignal.timeout(60_000) }).catch(() => {}),
    );
  }
  if (EVOLUTION_URL) {
    targets.push(
      fetch(EVOLUTION_URL, { signal: AbortSignal.timeout(60_000) }).catch(() => {}),
    );
  }
  const p = Promise.allSettled(targets);
  const edge: any = (globalThis as any).EdgeRuntime;
  if (edge?.waitUntil) {
    try { edge.waitUntil(p); } catch { /* noop */ }
  } else {
    p.catch(() => {});
  }
}

async function patchConfig(
  canalId: number,
  configAtual: Record<string, any>,
  patch: Record<string, any>,
) {
  const { data: fresh } = await supabase
    .from("canais").select("config").eq("id", canalId).maybeSingle();
  const base = (fresh?.config ?? configAtual) as Record<string, any>;
  await supabase
    .from("canais").update({ config: { ...base, ...patch } }).eq("id", canalId);
}

async function enfileirar(linha: Record<string, unknown>) {
  const r = await supabase
    .from("caixa_entrada")
    .upsert(linha, { onConflict: "canal_id,origem", ignoreDuplicates: true });
  return r;
}

async function handleTelegram(canalId: string, secret: string, payload: any) {
  const { data: canal, error } = await supabase
    .from("canais").select("id, tipo, config").eq("id", canalId).maybeSingle();
  if (error) return json({ ok: false }, 500);
  if (!canal || canal.tipo !== "telegram" || canal.config?.secret !== secret) {
    return json({ ok: false }, 404);
  }
  const msg = payload?.message ?? {};
  const chat = msg.chat ?? {};
  const texto = typeof msg.text === "string" ? msg.text : null;
  const chatId = chat.id != null ? String(chat.id) : null;
  if (texto && chatId) {
    const r = await enfileirar({
      canal_id: canal.id,
      remetente: chatId,
      texto,
      origem: `tg:${payload?.update_id ?? ""}`,
      payload_json: payload ?? {},
      status: "pendente",
    });
    console.log("inbox", JSON.stringify({ rota: "telegram", canalId, origem: `tg:${payload?.update_id ?? ""}`, insert: r.error ? { err: r.error.message, code: r.error.code } : { status: r.status } }));
    if (r.error) return json({ ok: false, error: r.error.message }, 500);
    wake();
  }
  return json({ ok: true });
}

async function handleEvolution(canalId: string, payload: any) {
  const { data: canal, error } = await supabase
    .from("canais").select("id, tipo, config").eq("id", canalId).maybeSingle();
  if (error) return json({ ok: false }, 500);
  if (!canal || canal.tipo !== "whatsapp") return json({ ok: false }, 404);

  const cfg = (canal.config ?? {}) as Record<string, any>;
  const instanciaEvento = payload?.instance;
  if (instanciaEvento && cfg.instance_name && instanciaEvento !== cfg.instance_name) {
    return json({ ok: true });
  }

  const evento = String(payload?.event ?? "").toUpperCase().replace(/\./g, "_");
  const data = payload?.data ?? {};

  if (evento === "QRCODE_UPDATED") {
    const qrDados = data.qrcode ?? data;
    const qr = String(qrDados.base64 ?? qrDados.code ?? "").replace(/^data:image\/png;base64,/, "");
    const patch: Record<string, any> = { status: "scanning" };
    if (qr) patch.qr = qr;
    await patchConfig(canal.id, cfg, patch);
    return json({ ok: true });
  }

  if (evento === "CONNECTION_UPDATE") {
    const estado = data?.state;
    const patch: Record<string, any> = {};
    if (estado) patch.status = estado;
    if (estado === "open") patch.qr = "";
    if (Object.keys(patch).length) await patchConfig(canal.id, cfg, patch);
    return json({ ok: true });
  }

  if (evento === "MESSAGES_UPSERT") {
    const key = data?.key ?? {};
    if (key.fromMe) return json({ ok: true });
    const msg = data?.message ?? {};
    const ext = msg.extendedTextMessage ?? {};
    const texto = msg.conversation ?? ext.text;
    let remote = key.remoteJid ?? null;
    if (remote) remote = String(remote).split("@")[0];
    if (texto && remote) {
      const keyId = String(key.id ?? "");
      const r = await enfileirar({
        canal_id: canal.id,
        remetente: remote,
        texto,
        origem: `wa:${keyId}`,
        payload_json: data ?? {},
        status: "pendente",
      });
      console.log("inbox", JSON.stringify({ rota: "evolution", canalId, origem: `wa:${keyId}`, insert: r.error ? { err: r.error.message, code: r.error.code } : { status: r.status } }));
      if (r.error) return json({ ok: false, error: r.error.message }, 500);
      wake();
    }
    return json({ ok: true });
  }

  return json({ ok: true });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return json({ ok: true });
  if (req.method !== "POST") return json({ ok: false }, 405);
  const url = new URL(req.url);
  const seg = url.pathname.split("/").filter(Boolean);
  // O Supabase pode expor o pathname com ou sem o prefixo functions/v1/inbox
  const idx = seg.indexOf("inbox");
  const rest = idx >= 0 ? seg.slice(idx + 1) : seg;
  try {
    const payload = await req.json().catch(() => ({}));
    if (rest[0] === "telegram" && rest[1] && rest[2]) {
      return await handleTelegram(rest[1], rest[2], payload);
    }
    if (rest[0] === "evolution" && rest[1]) {
      return await handleEvolution(rest[1], payload);
    }
    return json({ ok: false, error: "rota inválida", path: url.pathname }, 404);
  } catch (e) {
    return json({ ok: false, error: String((e as Error)?.message ?? e) }, 500);
  }
});