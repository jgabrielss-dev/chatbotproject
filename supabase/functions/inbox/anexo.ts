// Anexo e deduplicação da fila de entrada (item 15).
//
// O update do canal traz o arquivo, mas quem tem o token do bot (Telegram) ou a
// instância (Evolution) é o app do Render. Então a função não baixa nada: grava
// a REFERÊNCIA no `payload_json` e o worker baixa depois, exatamente como
// `app/main.py` faz nos webhooks que chegam nele. Sem isto, foto, áudio e
// documento do cliente sumiam — a fila só enfileirava quando vinha `texto`, e o
// caso mais comum (foto com legenda) era descartado em silêncio, com o
// Telegram recebendo um 200 que confirmava a entrega de nada.
//
// Fica num módulo só, sem nada de Deno e sem banco, porque é a parte que
// precisa de teste: `anexo_test.ts` roda com `deno test` e cobre a ordem dos
// campos, o maior tamanho da foto e a estabilidade da origem.

export const LIMITE_TEXTO = 8000;

//: Mesmo critério de `app/midia.py`: MIME exato, prefixo de MIME, e "arquivo"
//: quando não se sabe. O `tipo` precisa ser um dos quatro — `Midia.de_dict`
//: recusa qualquer outro e o anexo inteiro seria descartado no worker.
const MIME_EXATO: Record<string, string> = {
  "image/jpeg": "foto", "image/png": "foto", "image/webp": "foto",
  "image/heic": "foto", "image/gif": "foto",
  "audio/mpeg": "audio", "audio/mp3": "audio", "audio/ogg": "audio",
  "audio/wav": "audio", "audio/mp4": "audio", "audio/aac": "audio",
  "audio/flac": "audio", "audio/x-wav": "audio", "audio/amr": "audio",
  "video/mp4": "video", "video/webm": "video", "video/quicktime": "video",
  "video/3gpp": "video", "video/x-matroska": "video",
  "application/pdf": "arquivo", "text/plain": "arquivo", "text/csv": "arquivo",
  "text/markdown": "arquivo", "text/html": "arquivo",
  "application/json": "arquivo", "application/xml": "arquivo", "text/xml": "arquivo",
  "application/msword": "arquivo",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "arquivo",
  "application/vnd.ms-excel": "arquivo",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "arquivo",
};
const PREFIXO_MIME: [string, string][] = [
  ["image/", "foto"], ["audio/", "audio"], ["video/", "video"],
  ["application/pdf", "arquivo"], ["text/", "arquivo"],
];

/** Um dos quatro tipos do item 15, ou `arquivo` quando não dá para saber. */
export function classificar(mime: string): string {
  const m = (mime || "").split(";")[0].trim().toLowerCase();
  if (MIME_EXATO[m]) return MIME_EXATO[m];
  for (const [p, tipo] of PREFIXO_MIME) if (m.startsWith(p)) return tipo;
  return "arquivo";
}

//: Ordem importa: `video_note` e `voice` também podem vir com `video`/`audio` no
//: mesmo update, e o Telegram manda a foto em vários tamanhos do menor para o
//: maior (o último é o original). Mesma ordem de `app/channels/telegram.py`.
const CAMPO_TELEGRAM = ["video_note", "video", "audio", "voice", "document", "photo"];

function tipoDoTelegram(campo: string, mime: string): string {
  if (campo === "voice" || campo === "audio") return "audio";
  if (campo === "video" || campo === "video_note") return "video";
  if (campo === "photo") return "foto";
  return classificar(mime);
}

/** Anexo de um update do Telegram, na forma que `app/midia.py` entende. */
export function anexoTelegram(payload: any) {
  const msg = payload?.message ?? payload?.edited_message ?? {};
  for (const campo of CAMPO_TELEGRAM) {
    let bruto = msg[campo];
    if (!bruto) continue;
    if (campo === "photo") bruto = Array.isArray(bruto) ? bruto[bruto.length - 1] : bruto;
    if (typeof bruto !== "object" || bruto === null || !bruto.file_id) continue;
    const mime = bruto.mime_type || {
      photo: "image/jpeg", voice: "audio/ogg", video_note: "video/mp4",
    }[campo] || "";
    const nome = bruto.file_name ||
      `${campo}_${bruto.file_unique_id || String(bruto.file_id).slice(-12)}`;
    return {
      tipo: tipoDoTelegram(campo, mime),
      mime,
      nome,
      fonte: { ref: String(bruto.file_id) },
      legenda: typeof msg.caption === "string" ? msg.caption : "",
    };
  }
  return null;
}

//: Chave de cada tipo no corpo da Evolution -> tipo do item 15. `stickerMessage`
//: é foto (chega como webp) e o contato é texto, tratado antes daqui.
const CAMPO_EVOLUTION: [string, string][] = [
  ["imageMessage", "foto"],
  ["audioMessage", "audio"],
  ["videoMessage", "video"],
  ["documentMessage", "arquivo"],
  ["stickerMessage", "foto"],
];

/**
 * Anexo de um MESSAGES_UPSERT da Evolution. A legenda vai junto: é o texto que
 * o cliente escreveu com o arquivo ("isso aqui quebrou"), e sem ele sobra só
 * "[foto] IMG.jpg" — o agente responderia sobre a imagem em vez do problema.
 */
export function anexoEvolution(data: any) {
  const msg = (data ?? {}).message ?? {};
  for (const [campo, tipo] of CAMPO_EVOLUTION) {
    const corpo = msg[campo];
    if (typeof corpo !== "object" || corpo === null) continue;
    const mime = corpo.mimetype || "";
    const nome = corpo.fileName || corpo.fileEncSha256 || mime || campo;
    const base64 = corpo.media || corpo.base64 || "";
    const url = corpo.mediaUrl || corpo.url || corpo.link || "";
    const legenda = typeof corpo.caption === "string" && corpo.caption ? corpo.caption : "";
    if (base64) return { tipo, mime, nome, fonte: { base64: String(base64) }, legenda };
    if (url) return { tipo, mime, nome, fonte: { url: String(url) }, legenda };
  }
  return null;
}

/** A linha honesta que entra no histórico quando a pessoa mandou só anexo. */
export function descrever(anexo: Record<string, any> | null): string {
  return anexo ? `[${anexo.tipo}] ${anexo.nome} (${anexo.mime})` : "";
}

/**
 * `origem` de fallback quando o canal não manda id do evento.
 *
 * O índice único da fila é (canal_id, origem): uma origem degenerada ("wa:")
 * faria TODAS as mensagens seguintes do canal colidirem entre si e serem
 * descartadas em silêncio. Um hash do corpo é estável para o reenvio do mesmo
 * webhook (que é o que Telegram e Evolution fazem) e diferente para mensagens
 * diferentes. É o mesmo efeito de um id, sem depender do provedor.
 */
export async function origemFallback(prefixo: string, payload: unknown): Promise<string> {
  const bytes = new TextEncoder().encode(JSON.stringify(payload ?? {}));
  const mac = await crypto.subtle.digest("SHA-256", bytes);
  const hex = Array.from(new Uint8Array(mac).slice(0, 12))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  return `${prefixo}#${hex}`;
}
