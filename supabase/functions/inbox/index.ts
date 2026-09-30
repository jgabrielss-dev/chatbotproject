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
//   /functions/v1/inbox/evolution/{canal_id}/{secret}
//   /functions/v1/inbox/cron                        (disparada pelo pg_cron do banco)
//   /functions/v1/inbox/meta/whatsapp               <- OFICIAL (Meta Cloud API)
//   /functions/v1/inbox/meta/instagram              <- OFICIAL (Instagram Messaging API)
//
// A rota /meta/* é o que resolve o problema do Render: o Meta chama a Edge
// Function direto, a fila é gravada e o Render é acordar sob demanda. Nenhum
// keepalive é necessário, então o app passa o mês hibernando. Nos canais NÃO
// OFICIAIS (evolution, instagrapi) o keepalive continua sendo obrigatório.
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

// Comparação em tempo constante: não vaza o segredo por diferença de tempo.
function segredoIgual(a: unknown, b: unknown): boolean {
  const x = String(a ?? "");
  const y = String(b ?? "");
  const n = Math.max(x.length, y.length);
  let diff = x.length ^ y.length;
  for (let i = 0; i < n; i++) {
    diff |= (x.charCodeAt(i) || 0) ^ (y.charCodeAt(i) || 0);
  }
  return diff === 0;
}

// Dispara (sem travar) o wake de um alvo. O EdgeRuntime.waitUntil segura o
// fetch depois que a resposta já foi devolvida para o Telegram/Evolution.
function wakeTarget(url: string) {
  if (!url) return;
  const p = fetch(url, { signal: AbortSignal.timeout(60_000) }).catch(() => {});
  const edge: any = (globalThis as any).EdgeRuntime;
  if (edge?.waitUntil) {
    try { edge.waitUntil(p); } catch { /* noop */ }
  } else {
    p.catch(() => {});
  }
}

// `comEvolution`=false nos canais oficiais da Meta: acordamos só o Render que
// responde. A Evolution é do canal NÃO oficial e não precisa (nem deve) ser
// acordada por aqui, porque assim o evento oficial não gasta nada dela.
function wake(comEvolution = true) {
  wakeTarget(RENDER_HEALTH_URL);
  if (comEvolution) wakeTarget(EVOLUTION_URL);
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
  if (!canal || canal.tipo !== "telegram" || !segredoIgual(canal.config?.secret, secret)) {
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

async function handleEvolution(canalId: string, secret: string, payload: any) {
  const { data: canal, error } = await supabase
    .from("canais").select("id, tipo, config").eq("id", canalId).maybeSingle();
  if (error) return json({ ok: false }, 500);
  if (!canal || canal.tipo !== "whatsapp") return json({ ok: false }, 404);
  // O segredo na URL fecha o endpoint: sem ele, qualquer um que adivinhasse o
  // id do canal (sequencial) injetaria mensagens e gastaria cota do Gemini.
  if (!segredoIgual(canal.config?.secret, secret)) return json({ ok: false }, 404);

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

// ---------------------------------------------------------------------------
// Canais OFICIAIS da Meta
// ---------------------------------------------------------------------------
//
// these two helpers are used by both WhatsApp Cloud API and Instagram
// Messaging API: the event identifies the account, and we resolve the channel
// by that id (no secret in the URL).

type Familia = "whatsapp" | "instagram";

const CAMPO_IDENTIFICADOR: Record<Familia, "phone_number_id" | "ig_user_id"> = {
  whatsapp: "phone_number_id",
  instagram: "ig_user_id",
};

// Resolve o canal pelo id da conta que veio no evento. Se DOIS canais
// declararem o mesmo phone_number_id / ig_user_id, o roteamento fica ambíguo e
// a assinatura passa a ser conferida contra o app_secret errado — por isso a
// duplicata é rejeitada em vez de escolher o primeiro registro.
async function resolverCanal(familia: Familia, identificador: string) {
  const campo = CAMPO_IDENTIFICADOR[familia];
  const tipo = familia === "whatsapp" ? "whatsapp_oficial" : "instagram_oficial";
  const { data } = await supabase
    .from("canais")
    .select("id, tipo, config, ativo")
    .eq("tipo", tipo)
    .eq("config->>" + campo, identificador)
    .limit(2);
  const lista = (data ?? []) as any[];
  if (lista.length === 0) return null;
  if (lista.length > 1) {
    console.error(JSON.stringify({
      rota: `meta/${familia}`,
      alerta: "identificador duplicado entre canais; o roteamento fica ambíguo",
      campo, identificador, canais: lista.map((c) => c.id),
    }));
    return null;
  }
  return lista[0];
}

async function conferirAssinatura(req: Request, corpo: string, cfg: Record<string, any>) {
  const segredo = String(cfg?.app_secret ?? "");
  if (!segredo) {
    // Sem app_secret não dá para validar X-Hub-Signature-256. Registrado no log
    // porque isso significa que qualquer um pode forjar um evento.
    console.warn(
      JSON.stringify({ rota: "meta", alerta: "app_secret ausente: assinatura do webhook não validada" }),
    );
    return true;
  }
  const cabecalho = req.headers.get("x-hub-signature-256") ?? "";
  const chave = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(segredo),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = await crypto.subtle.sign("HMAC", chave, new TextEncoder().encode(corpo));
  const esperado = "sha256=" + Array.from(new Uint8Array(mac))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  return cabecalho === esperado;
}

// Verificação de webhook que o Meta faz via GET (subscribe).
async function handleVerificacao(url: URL, familia: Familia) {
  const modo = url.searchParams.get("hub.mode") ?? "";
  const token = url.searchParams.get("hub.verify_token") ?? "";
  const desafio = url.searchParams.get("hub.challenge") ?? "";

  // O handshake do Meta NÃO manda phone_number_id/ig_user_id, então o canal só
  // pode ser resolvido pelo verify_token — que precisa ser único entre os
  // canais. Por isso a duplicata é rejeitada em vez de escolher o primeiro.
  const tipo = familia === "whatsapp" ? "whatsapp_oficial" : "instagram_oficial";
  if (!token) return json({ ok: false, error: "verify_token ausente" }, 400);
  const { data: candidatos } = await supabase
    .from("canais")
    .select("id, tipo, config")
    .eq("tipo", tipo)
    .eq("config->>verify_token", token)
    .limit(2);
  const lista = (candidatos ?? []) as any[];

  if (lista.length === 0) {
    return json({ ok: false, error: "verify_token não corresponde a nenhum canal" }, 403);
  }
  if (lista.length > 1) {
    console.error(JSON.stringify({
      rota: `meta/${familia}`,
      alerta: "verify_token duplicado entre canais; use um valor diferente em cada canal",
      canais: lista.map((c) => c.id),
    }));
    return json({ ok: false, error: "verify_token duplicado entre canais" }, 409);
  }
  const canal = lista[0];
  if (modo === "subscribe") {
    // Resposta exigida pelo Meta: o hub.challenge em text/plain puro.
    return new Response(desafio, { status: 200, headers: { "Content-Type": "text/plain" } });
  }
  return json({ ok: false, error: "hub.mode inválido" }, 403);
}

// Eventos de mensagem.
//
// A Meta usa DOIS formatos diferentes e eles NAO se misturam:
//  - WhatsApp Cloud API: entry[].changes[].value.messages[], e a conta vem em
//    value.metadata.phone_number_id. A mensagem traz `from` e `text.body`.
//  - Instagram Messaging API: entry[].messaging[] (sem `changes`), a conta vem
//    em recipient.id / entry[].id, e a mensagem traz sender.id, message.mid e
//    message.text. Ver https://developers.facebook.com/docs/instagram-messaging/webhooks
type EventoNormalizado = { identificador: string; mensagem: any };

// Achata o payload nos dois formatos para uma lista so.
function normalizarEventos(familia: Familia, parsed: any): EventoNormalizado[] {
  const saida: EventoNormalizado[] = [];
  for (const entry of parsed?.entry ?? []) {
    if (familia === "instagram") {
      // Instagram: entry[].messaging[]. O recipient e a conta profissional que
      // recebeu; o sender e o IGSID do cliente (e pra ele que a gente responde).
      for (const ev of entry?.messaging ?? []) {
        const identificador = String(ev?.recipient?.id ?? entry?.id ?? "");
        if (identificador) saida.push({ identificador, mensagem: ev });
      }
      continue;
    }
    for (const change of entry?.changes ?? []) {
      const value = change?.value ?? {};
      const identificador = String(value?.metadata?.phone_number_id ?? "");
      for (const mensagem of value?.messages ?? []) {
        if (identificador) saida.push({ identificador, mensagem });
      }
    }
  }
  return saida;
}

async function handleMetaMensagem(familia: Familia, req: Request) {
  const corpo = await req.text();
  let parsed: any = {};
  try { parsed = JSON.parse(corpo); } catch { parsed = {}; }

  const eventos = normalizarEventos(familia, parsed);
  if (eventos.length === 0) {
    console.warn(JSON.stringify({ rota: `meta/${familia}`, alerta: "payload sem mensagens reconheciveis" }));
    return json({ ok: true });
  }

  // A assinatura e conferida uma vez por conta resolvida, ANTES de gravar
  // qualquer coisa, para nao enfileirar evento forjado.
  const conferidos = new Map<number, boolean>();
  let acordou = false;

  for (const { identificador, mensagem } of eventos) {
    const canal = await resolverCanal(familia, identificador);
    if (!canal) {
      // 200 de proposito: canal pausado, removido ou de outra conta. A Meta
      // reenviaria o mesmo evento indefinidamente e encheria a fila de lixo.
      console.warn(JSON.stringify({ rota: `meta/${familia}`, alerta: "canal nao encontrado", identificador }));
      continue;
    }
    if (!conferidos.has(canal.id)) {
      const ok = await conferirAssinatura(req, corpo, (canal.config ?? {}) as Record<string, any>);
      conferidos.set(canal.id, ok);
      if (!ok) {
        console.warn(JSON.stringify({ rota: `meta/${familia}`, alerta: "assinatura invalida", canalId: canal.id }));
      }
    }
    if (!conferidos.get(canal.id)) return json({ ok: false, error: "assinatura invalida" }, 401);
    if (canal.ativo === false) {
      console.warn(JSON.stringify({ rota: `meta/${familia}`, alerta: "canal inativo; evento ignorado", canalId: canal.id }));
      continue;
    }

    // O Instagram devolve as PROPRIAS mensagens com is_echo=true. Sem este filtro
    // o bot responderia a si mesmo em laco.
    if (mensagem?.message?.is_echo === true || mensagem?.is_echo === true) continue;
    if (mensagem?.message?.is_deleted === true) continue;
    if (mensagem?.message?.is_unsupported === true) continue;

    const texto = familia === "whatsapp" ? mensagem?.text?.body : mensagem?.message?.text;
    const de = familia === "whatsapp" ? String(mensagem?.from ?? "") : String(mensagem?.sender?.id ?? "");
    const origemId = familia === "whatsapp" ? String(mensagem?.id ?? "") : String(mensagem?.message?.mid ?? "");
    if (!texto || !de || !origemId) continue;

    const r = await enfileirar({
      canal_id: canal.id,
      remetente: de,
      texto: String(texto).slice(0, 4000),
      origem: `${familia === "whatsapp" ? "wamo" : "igmo"}:${origemId}`,
      payload_json: mensagem,
      status: "pendente",
    });
    console.log("inbox", JSON.stringify({
      rota: `meta/${familia}`, canalId: canal.id, origem: origemId,
      insert: r.error ? { err: r.error.message, code: r.error.code } : { status: r.status },
    }));
    if (r.error) return json({ ok: false, error: r.error.message }, 500);
    // So o Render; nada de Evolution no canal oficial. Uma unica wake por request.
    if (!acordou) { wake(false); acordou = true; }
  }
  return json({ ok: true });
}

async function handleMeta(familia: Familia, req: Request, url: URL) {
  if (req.method === "GET") return await handleVerificacao(url, familia);
  if (req.method !== "POST") return json({ ok: false }, 405);
  return await handleMetaMensagem(familia, req);
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return json({ ok: true });
  const url = new URL(req.url);
  const seg = url.pathname.split("/").filter(Boolean);
  // O Supabase pode expor o pathname com ou sem o prefixo functions/v1/inbox
  const idx = seg.indexOf("inbox");
  const rest = idx >= 0 ? seg.slice(idx + 1) : seg;
  try {
    if (rest[0] === "meta" && (rest[1] === "whatsapp" || rest[1] === "instagram")) {
      return await handleMeta(rest[1], req, url);
    }
    if (req.method !== "POST") return json({ ok: false }, 405);
    const payload = await req.json().catch(() => ({}));
    if (rest[0] === "telegram" && rest[1] && rest[2]) {
      return await handleTelegram(rest[1], rest[2], payload);
    }
    if (rest[0] === "evolution" && rest[1] && rest[2]) {
      return await handleEvolution(rest[1], rest[2], payload);
    }
    // Rota usada pelo pg_cron do banco (despertador periódico): acorda
    // SOMENTE a Evolution (a sessão Baileys reconecta e o WhatsApp entrega
    // o backlog de mensagens dormidas). NÃO desperta o app (economiza horas).
    if (rest[0] === "cron") {
      wakeTarget(EVOLUTION_URL);
      return json({ ok: true });
    }
    return json({ ok: false, error: "rota inválida", path: url.pathname }, 404);
  } catch (e) {
    return json({ ok: false, error: String((e as Error)?.message ?? e) }, 500);
  }
});