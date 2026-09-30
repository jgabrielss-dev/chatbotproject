// Testes da parte pura da Edge Function: `deno test supabase/functions/inbox/`.
//
// Sem dependência externa (nada de `@std/assert`) para o teste rodar offline e
// num `deno test` de um comando. O que é verificado aqui é o que o worker do
// Render entende: o formato do `anexo` em `payload_json`, a ordem dos campos do
// Telegram, e a origem de fallback — a fila deduplica por (canal_id, origem), e
// um erro aqui significa mensagem do cliente descartada em silêncio.

import {
  anexoEvolution,
  anexoTelegram,
  classificar,
  descrever,
  origemFallback,
} from "./anexo.ts";

function igual(recebido: unknown, esperado: unknown, oque: string) {
  const a = JSON.stringify(recebido);
  const b = JSON.stringify(esperado);
  if (a !== b) throw new Error(`${oque}\n  esperado: ${b}\n  recebido:  ${a}`);
}

function verdadeiro(valor: unknown, oque: string) {
  if (!valor) throw new Error(`${oque} (recebi ${JSON.stringify(valor)})`);
}

// --------------------------------------------------------------------------
// classificar
// --------------------------------------------------------------------------

Deno.test("classificar: MIME exato", () => {
  igual(classificar("image/png"), "foto", "png é foto");
  igual(classificar("audio/ogg"), "audio", "ogg é audio");
  igual(classificar("video/mp4"), "video", "mp4 é video");
  igual(classificar("application/pdf"), "arquivo", "pdf é arquivo");
});

Deno.test("classificar: prefixo e normalização", () => {
  igual(classificar("image/tiff"), "foto", "prefixo image/");
  igual(classificar("audio/x-wav; codecs=1"), "audio", "parametro ; é ignorado");
  igual(classificar("  VIDEO/MP4  "), "video", "maiusculo e espaco");
});

Deno.test("classificar: o que não se sabe é arquivo", () => {
  // `application/octet-stream` NÃO entra nas tabelas de propósito: um arquivo
  // sem tipo é melhor como "não sei o que é isso" do que inventar.
  igual(classificar("application/octet-stream"), "arquivo", "sem tipo");
  igual(classificar(""), "arquivo", "mime vazio");
  igual(classificar(undefined as unknown as string), "arquivo", "mime ausente");
});

// --------------------------------------------------------------------------
// anexoTelegram
// --------------------------------------------------------------------------

Deno.test("anexoTelegram: update só com texto não tem anexo", () => {
  igual(
    anexoTelegram({ update_id: 1, message: { chat: { id: 5 }, text: "oi" } }),
    null,
    "texto não é anexo",
  );
});

Deno.test("anexoTelegram: pega a foto no maior tamanho", () => {
  const a = anexoTelegram({
    update_id: 1,
    message: {
      chat: { id: 5 },
      caption: "isso aqui quebrou",
      photo: [
        { file_id: "pequeno", file_unique_id: "u1", width: 90 },
        { file_id: "grande", file_unique_id: "u2", width: 1280 },
      ],
    },
  });
  igual(a?.tipo, "foto", "foto");
  igual(a?.mime, "image/jpeg", "mime padrao do Telegram para foto");
  igual(a?.fonte, { ref: "grande" }, "o ultimo da lista e o original");
  igual(a?.legenda, "isso aqui quebrou", "a legenda viaja com o anexo");
});

Deno.test("anexoTelegram: documento classificado pelo MIME", () => {
  igual(
    anexoTelegram({
      message: { chat: { id: 5 }, document: { file_id: "d1", file_name: "nota.pdf", mime_type: "application/pdf" } },
    })?.tipo,
    "arquivo",
    "pdf",
  );
  igual(
    anexoTelegram({
      message: { chat: { id: 5 }, document: { file_id: "d2", mime_type: "image/png" } },
    })?.tipo,
    "foto",
    "documento que e imagem continua foto",
  );
  igual(
    anexoTelegram({
      message: { chat: { id: 5 }, document: { file_id: "d3", file_name: "contrato.docx" } },
    })?.mime,
    "",
    "sem mime_type, o mime fica vazio e quem classifica e o worker",
  );
});

Deno.test("anexoTelegram: audio e video sao traduzidos", () => {
  igual(
    anexoTelegram({ message: { chat: { id: 5 }, voice: { file_id: "v1", file_unique_id: "uv" } } }),
    { tipo: "audio", mime: "audio/ogg", nome: "voice_uv", fonte: { ref: "v1" }, legenda: "" },
    "voice",
  );
  igual(
    anexoTelegram({
      message: {
        chat: { id: 5 },
        video: { file_id: "vid", file_unique_id: "uv2" },
        video_note: { file_id: "nota", file_unique_id: "uv3" },
      },
    })?.fonte,
    { ref: "nota" },
    "video_note ganha de video (mesma ordem do app do Render)",
  );
});

Deno.test("anexoTelegram: campo sem file_id é ignorado", () => {
  igual(
    anexoTelegram({ message: { chat: { id: 5 }, photo: [{ width: 90 }] } }),
    null,
    "foto sem file_id nao vira anexo quebrado",
  );
});

Deno.test("anexoTelegram: edited_message tambem", () => {
  igual(
    anexoTelegram({ edited_message: { chat: { id: 5 }, photo: [{ file_id: "p" }] } })?.fonte,
    { ref: "p" },
    "anexo de mensagem editada",
  );
});

// --------------------------------------------------------------------------
// anexoEvolution
// --------------------------------------------------------------------------

Deno.test("anexoEvolution: mediaUrl vira fonte url, com a legenda", () => {
  const a = anexoEvolution({
    key: { id: "K1" },
    message: {
      imageMessage: {
        mimetype: "image/jpeg",
        mediaUrl: "https://mmg.whatsapp.net/x",
        caption: "essa maquina travou",
      },
    },
  });
  igual(a?.tipo, "foto", "foto");
  igual(a?.fonte, { url: "https://mmg.whatsapp.net/x" }, "url publica");
  igual(a?.legenda, "essa maquina travou", "a legenda e o texto da mensagem");
});

Deno.test("anexoEvolution: base64 tem precedencia sobre a url", () => {
  igual(
    anexoEvolution({
      message: {
        documentMessage: { mimetype: "application/pdf", fileName: "a.pdf", media: "QUJD", mediaUrl: "https://x" },
      },
    }),
    { tipo: "arquivo", mime: "application/pdf", nome: "a.pdf", fonte: { base64: "QUJD" }, legenda: "" },
    "ALLOW_BASE64",
  );
});

Deno.test("anexoEvolution: sticker e foto, documento e arquivo", () => {
  igual(
    anexoEvolution({ message: { stickerMessage: { mimetype: "image/webp", mediaUrl: "https://s" } } })?.tipo,
    "foto",
    "sticker",
  );
  igual(
    anexoEvolution({ message: { videoMessage: { mimetype: "video/mp4", mediaUrl: "https://v" } } })?.tipo,
    "video",
    "video",
  );
});

Deno.test("anexoEvolution: contato e texto puro nao tem anexo", () => {
  igual(
    anexoEvolution({ message: { conversation: "oi", contactMessage: { vcard: "x" } } }),
    null,
    "contato e texto",
  );
});

Deno.test("anexoEvolution: sem base64 e sem url nao inventa fonte", () => {
  igual(
    anexoEvolution({ message: { imageMessage: { mimetype: "image/jpeg" } } }),
    null,
    "midia sem bytes e sem link nao entra na fila",
  );
});

// --------------------------------------------------------------------------
// descrever e origemFallback
// --------------------------------------------------------------------------

Deno.test("descrever: a linha honesta do historico", () => {
  igual(descrever({ tipo: "foto", nome: "lajota.jpg", mime: "image/jpeg" }), "[foto] lajota.jpg (image/jpeg)", "formato");
  igual(descrever(null), "", "sem anexo nao ha descricao");
});

Deno.test("origemFallback: estavel no reenvio, diferente entre mensagens", async () => {
  const p1 = { message: { chat: { id: 5 }, text: "primeira" } };
  const p2 = { message: { chat: { id: 5 }, text: "segunda" } };
  const a = await origemFallback("tg", p1);
  igual(a, await origemFallback("tg", p1), "o mesmo webhook reenviado tem de ser a mesma origem");
  verdadeiro(a !== (await origemFallback("tg", p2)), "mensagens diferentes nao podem colidir");
  verdadeiro(a.startsWith("tg#"), "prefixo do canal preservado");
  verdadeiro(a.length > 3, "origem nunca degenerada: 'tg:' colidiria todas as mensagens");
});

Deno.test("origemFallback: payload vazio nao quebra", async () => {
  verdadeiro((await origemFallback("wa", undefined)).startsWith("wa#"), "sem payload");
});
