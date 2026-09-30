/* Sessão e API compartilhadas pelas três telas (index, admin, painel).
 *
 * A chave é a sessão do Supabase Auth no navegador. O frontend fala com o
 * GoTrue (`/auth/v1/*`) usando a anon key — que é pública por definição — e
 * depois chama a nossa API com o access_token como `Authorization: Bearer`.
 * A API valida esse token no servidor antes de qualquer query.
 *
 * Nenhum segredo do servidor aparece aqui: nem DATABASE_URL, nem a
 * service_role, nem os segredos dos canais. O que o navegador recebe das
 * rotas de canal já vem redigido pelo servidor.
 */
(function () {
  "use strict";

  // Mesma regra do painel antigo: servida pelo FastAPI a API está na mesma
  // origem; no GitHub Pages a página é estática e aponta para o backend.
  var API_RENDER = "https://chatbotproject-1-l9zr.onrender.com";
  var ABRINDO_DO_DISCO = location.protocol === "file:";
  var EM_PAGINA_ESTATICA = /\.github\.io$/i.test(location.hostname) || ABRINDO_DO_DISCO;
  var API_BASE = EM_PAGINA_ESTATICA ? API_RENDER : "";

  /* No GitHub Pages não existe servidor para rotear: a URL da home é
   * ".../chatbotproject/" e a do painel seria ".../chatbotproject/admin",
   * que o Pages procura como arquivo e devolve 404. Por isso, em página
   * estática o caminho leva .html. No Render é o FastAPI que decide, e as
   * rotas limpas (/admin) são o certo.
   *
   * E o caminho tem de ser RELATIVO, não "/auth.js": um site de projeto do
   * Pages é servido em ".../chatbotproject/", então "/auth.js" pediria
   * "https://jgabrielss-dev.github.io/auth.js" e daria 404.
   */

  var CHAVE_USUARIO = "chatbotproject.usuario";
  var _sessao = null;
  var _config = null;
  var _carregandoConfig = null;

  /* ---------------------------------------------------------------- rotas */

  /** Caminho de uma tela, igual nos dois hosts mas escrito do jeito de cada um. */
  function pagina(nome) {
    if (nome === "index") {
      // A home e index.html na raiz. No Pages ela responde em ".../repo/", sem
      // o ".html"; mandar index.html ali funciona, mas a URL fica feia.
      return EM_PAGINA_ESTATICA ? "./" : "/";
    }
    // "./admin.html" e nao "/admin.html": o Pages serve o site de projeto em
    // ".../<repo>/", entao a barra a esquerda apontaria para a raiz do dominio,
    // fora do repo, e daria 404.
    return EM_PAGINA_ESTATICA ? "./" + nome + ".html" : "/" + nome;
  }

  /** Para onde este usuário vai depois de entrar. */
  function destino(eu, redir) {
    if (redir && (redir !== "/admin" || eu.eh_admin)) {
      // O ?redir= pode ter vindo de uma visita ao GitHub Pages, onde o destino
      // e "./admin.html". Devolvido cru no Render, esse ".html" nao existe la e
      // o servidor respondia 401 em JSON — a tela em branca. Reduzimos para
      // "/admin" ou "/painel" e ignoramos qualquer outra coisa.
      var alvo = String(redir).replace(/^\.?\/*/, "").replace(/\.html$/, "").replace(/\/$/, "");
      if (alvo === "") alvo = "index";
      if (alvo !== "admin" && alvo !== "painel" && alvo !== "index" && alvo !== "login") {
        return pagina(eu.eh_admin ? "admin" : "painel");
      }
      if (alvo === "login") return pagina("login");
      // A home no Pages e "./index.html" (o dir listing ja serve, mas o
      // "./index.html" explicito nao quebra em nenhum dos dois hosts).
      if (alvo === "index") return EM_PAGINA_ESTATICA ? "./index.html" : "/";
      // O Pages precisa do ".html"; o Render usa a rota sem extensão.
      if (!EM_PAGINA_ESTATICA) return pagina(alvo);
      return "./" + alvo + ".html";
    }
    return pagina(eu.eh_admin ? "admin" : "painel");
  }

  /** Vai para o login guardando a pagina atual, para voltar depois de entrar. */
  function irParaLogin() {
    var atual = pagina(eu_admin_ou_painel_atual());
    location.replace(pagina("login") + "?redir=" + encodeURIComponent(atual));
  }

  function eu_admin_ou_painel_atual() {
    return /(?:^|\/)(admin|painel)/.test(location.pathname) ? "admin" : "painel";
  }

  /* ---------------------------------------------------------------- config */

  function config() {
    if (!_carregandoConfig) {
      _carregandoConfig = fetch(API_BASE + "/api/config").then(function (r) {
        if (!r.ok) throw new Error("Configuração da API indisponível.");
        return r.json();
      }).then(function (c) { _config = c; return c; });
    }
    return _carregandoConfig;
  }

  /* --------------------------------------------------------------- sessão */

  function guardarToken(accessToken) {
    try { localStorage.setItem(CHAVE_USUARIO, accessToken); } catch (e) { /* modo privado */ }
  }

  function lerToken() {
    try { return localStorage.getItem(CHAVE_USUARIO) || ""; } catch (e) { return ""; }
  }

  function limparSessao() {
    _sessao = null;
    try { localStorage.removeItem(CHAVE_USUARIO); } catch (e) { /* ignorado */ }
  }

  /** Headers de auth para as chamadas à API. Vazio enquanto não há sessão. */
  function cabecalhoAuth() {
    var t = lerToken();
    return t ? { Authorization: "Bearer " + t } : {};
  }

  /**
   * Quem sou eu. Usa o cache em sessionStorage para o F5 não piscar a tela,
   * mas sempre revalida contra /api/eu — o papel pode ter mudado no servidor
   * (ex.: o admin bloqueou a conta) e o cache não pode esconder isso.
   */
  function obterSessao() {
    if (_sessao) return Promise.resolve(_sessao);
    var token = lerToken();
    if (!token) return Promise.resolve(null);

    return fetch(API_BASE + "/api/eu", { headers: cabecalhoAuth() })
      .then(function (r) {
        if (r.status === 401 || r.status === 403) { limparSessao(); return null; }
        if (r.status === 503) {
          throw new Error(
            "O servidor não tem Supabase Auth configurado. Defina SUPABASE_URL e SUPABASE_ANON_KEY."
          );
        }
        if (!r.ok) throw new Error("Não foi possível validar a sessão.");
        return r.json();
      })
      .then(function (eu) { _sessao = eu; return eu; });
  }

  /* ------------------------------------------------------------ GoTrue API */

  /** Cria o cliente do Supabase na mão, para não depender de CDN. */
  function gotrue(c) {
    return {
      signup: function (email, senha, meta) {
        // `data` vira `user_metadata` no Supabase Auth. Mandamos o nome com
        // duas chaves porque integrações diferentes leem uma ou outra, e o
        // que importa aqui é o trigger do banco ver o nome em conta nova.
        return fetch(c.supabase_url + "/auth/v1/signup", {
          method: "POST",
          headers: headersGotrue(c),
          body: JSON.stringify({
            email: email,
            password: senha,
            data: Object.assign({ app: "chatbotproject" }, meta || {})
          })
        });
      },
      signin: function (email, senha) {
        return fetch(c.supabase_url + "/auth/v1/token?grant_type=password", {
          method: "POST",
          headers: headersGotrue(c),
          body: JSON.stringify({ email: email, password: senha })
        });
      },
      refresh: function (refreshToken) {
        return fetch(c.supabase_url + "/auth/v1/token?grant_type=refresh_token", {
          method: "POST",
          headers: headersGotrue(c),
          body: JSON.stringify({ refresh_token: refreshToken })
        });
      },
      recover: function (email) {
        return fetch(c.supabase_url + "/auth/v1/recover", {
          method: "POST",
          headers: headersGotrue(c),
          body: JSON.stringify({ email: email })
        });
      }
    };
  }

  function headersGotrue(c) {
    return { "Content-Type": "application/json", apikey: c.supabase_anon_key };
  }

  /**
   * Lê e-mail/senha, chama o GoTrue e guarda o access_token.
   *
   * Devolve `{ mfa: true, fatorId }` quando a conta tem 2FA e o token saiu no
   * nível 1. A tela do login abre o campo do código; nada da aplicação é
   * carregado antes disso, porque a nossa API recusa token de nível 1 (item 5)
   * — ou seja, o segundo fator é obrigatório, não um extra da tela.
   */
  function entrar(email, senha) {
    return config().then(function (c) {
      return gotrue(c).signin(email, senha).then(function (r) {
        return r.json().then(function (d) {
          if (!r.ok) throw new Error(d.error_description || d.msg || "E-mail ou senha inválidos.");
          guardarToken(d.access_token);
          _sessao = null;
          return segundoFatorPendente().then(function (pendente) {
            if (pendente) return pendente;
            return obterSessao();
          });
        });
      });
    });
  }

  /** O token atual é de nível 1 e a conta tem fator inscrito? */
  function segundoFatorPendente() {
    var token = lerToken();
    if (!token) return Promise.resolve(null);
    return estadoDaConta(token).then(function (u) {
      if (u.aal === "aal2" || !u.factors || !u.factors.length) return null;
      var fatores = u.factors.filter(function (f) { return f.status !== "unverified"; });
      if (!fatores.length) return null;
      return { mfa: true, fatorId: fatores[0].id };
    }).catch(function () {
      // Se a leitura da conta falhar, seguimos com a senha: o `/api/eu` volta
      // e exige o segundo fator se ele for mesmo necessário. Inventar um
      // "mfa" aqui mandaria a pessoa preencher um campo à toa.
      return null;
    });
  }

  function cadastrar(email, senha, nome) {
    return config().then(function (c) {
      var meta = {};
      if (nome) { meta.nome = nome; meta.full_name = nome; }
      return gotrue(c).signup(email, senha, meta).then(function (r) {
        return r.json().then(function (d) {
          // 200 com sessão = confirmação de e-mail desligada, já entrou.
          // 200/422 sem sessão = o Supabase mandou o e-mail de confirmação.
          if (d.access_token) {
            guardarToken(d.access_token);
            _sessao = null;
            return { ok: true, entrou: true, mensagem: "Conta criada. Bem-vindo!" };
          }
          // 422 com "already registered" é o caso real de quem já tem conta e
          // clica em "Criar conta" por engano. Sem tratar, a tela mostra
          // "conta criada" e a pessoa tenta entrar sem nunca ter senha.
          if (r.status === 422 && /already|registered|exists/i.test(
              (d.error_description || d.msg || ""))) {
            throw new Error("Já existe uma conta com esse e-mail. Tente entrar.");
          }
          if (r.ok || r.status === 422) {
            return {
              ok: true,
              entrou: false,
              mensagem: "Conta criada. Confira seu e-mail para confirmar e depois entre."
            };
          }
          throw new Error(d.error_description || d.msg || "Não foi possível criar a conta.");
        });
      });
    });
  }

  /**
   * Sai. Tentamos avisar o GoTrue para invalidar o refresh_token; se a chamada
   * falhar, limpamos o local mesmo assim — o logout local é o que importa para
   * a UX, e o token expira sozinho.
   */
  function sair() {
    return config()
      .then(function (c) {
        if (!c.supabase_url) return;
        return fetch(c.supabase_url + "/auth/v1/logout", {
          method: "POST",
          headers: { ...headersGotrue(c), ...cabecalhoAuth() }
        }).catch(function () {});
      })
      .then(function () {
        limparSessao();
        // A home da aplicacao e index.html na raiz do repo (GitHub Pages). No
        // Render, "/" ainda resolve para a home, entao os dois funcionam.
        location.replace(pagina("index"));
      });
  }

  /**
   * "Esqueci minha senha". O GoTrue responde 200 tanto para e-mail existente
   * quanto para inexistente, e é isso que queremos: responder diferente
   * entregaria a lista de quem tem conta. O front diz "se houver, enviamos".
   */
  function recuperar(email) {
    return config().then(function (c) {
      if (!c.supabase_url) throw new Error("Supabase Auth não configurado no servidor.");
      return gotrue(c).recover(email).then(function (r) {
        if (!r.ok) {
          return r.json().then(function (d) {
            throw new Error(d.error_description || d.msg || "Não foi possível enviar o e-mail.");
          });
        }
      });
    });
  }


  /* ------------------------------------------------------- segundo fator */

  /**
   * Estado da sessão no GoTrue: traz `aal` e os fatores da conta. É o que
   * decide se o login precisa de um código a mais, sem adivinhar por nada.
   */
  function estadoDaConta(token) {
    return config().then(function (c) {
      return fetch(c.supabase_url + "/auth/v1/user", {
        headers: { apikey: c.supabase_anon_key, Authorization: "Bearer " + token }
      }).then(function (r) {
        return r.json().then(function (d) {
          if (!r.ok) throw new Error(d.error_description || d.msg || "Não foi possível ler a conta.");
          return d;
        });
      });
    });
  }

  function fatores(token) {
    return config().then(function (c) {
      return fetch(c.supabase_url + "/auth/v1/factors", {
        headers: { apikey: c.supabase_anon_key, Authorization: "Bearer " + token }
      }).then(function (r) {
        return r.json().then(function (d) {
          if (!r.ok) throw new Error(d.error_description || d.msg || "Não foi possível ler os fatores.");
          return Array.isArray(d.factors) ? d.factors : [];
        });
      });
    });
  }

  /** Inscreve um autenticador. Devolve QR (data URI), segredo e uri otpauth. */
  function inscreverFator(token, nome) {
    return config().then(function (c) {
      return fetch(c.supabase_url + "/auth/v1/factors", {
        method: "POST",
        headers: { apikey: c.supabase_anon_key, Authorization: "Bearer " + token,
                   "Content-Type": "application/json" },
        body: JSON.stringify({ friendly_name: nome || "App autenticador" })
      }).then(function (r) {
        return r.json().then(function (d) {
          // 422/400 com "MFA" é o projeto sem MFA habilitado no painel do
          // Supabase. A mensagem padrão ("Invalid request") não diria nada.
          if (!r.ok) {
            var msg = d.error_description || d.msg || "";
            if (/mfa/i.test(d.error_code || "") || /mfa|totp|multi/i.test(msg)) {
              throw new Error("A verificação em duas etapas ainda não está habilitada "
                + "no projeto. O administrador precisa ativá-la em Authentication → MFA.");
            }
            throw new Error(msg || "Não foi possível cadastrar o aplicativo autenticador.");
          }
          return d;
        });
      });
    });
  }

  function desafiarFator(token, fatorId) {
    return config().then(function (c) {
      return fetch(c.supabase_url + "/auth/v1/factors/" + encodeURIComponent(fatorId) + "/challenge", {
        method: "POST",
        headers: { apikey: c.supabase_anon_key, Authorization: "Bearer " + token,
                   "Content-Type": "application/json" },
        body: "{}"
      }).then(function (r) {
        return r.json().then(function (d) {
          if (!r.ok) throw new Error(d.error_description || d.msg || "Não foi possível pedir o código.");
          return d;
        });
      });
    });
  }

  /**
   * Confere o código. A resposta traz um access_token NOVO, de nível `aal2`:
   * é este token que passa a valer, e é o que a nossa API exige quando a conta
   * tem o 2FA ativo. Por isso o antigo é trocado aqui — guardá-lo deixaria a
   * conta entrando com senha só.
   */
  function confirmarFator(token, fatorId, desafioId, codigo) {
    return config().then(function (c) {
      return fetch(c.supabase_url + "/auth/v1/factors/" + encodeURIComponent(fatorId) + "/verify", {
        method: "POST",
        headers: { apikey: c.supabase_anon_key, Authorization: "Bearer " + token,
                   "Content-Type": "application/json" },
        body: JSON.stringify(
          // Na inscricao do fator nao existe desafio ainda: o GoTrue quer so
          // o codigo. Mandar `challenge_id: null` faz a validacao do corpo
          // recusar, entao a chave e omitida quando nao ha desafio.
          desafioId ? { challenge_id: desafioId, code: String(codigo || "").trim() }
                    : { code: String(codigo || "").trim() })
      }).then(function (r) {
        return r.json().then(function (d) {
          if (!r.ok) {
            var m = d.error_description || d.msg || "";
            throw new Error(/invalid|expired|mismatch|verif/i.test(m)
              ? "Código inválido ou expirado. Confira o relógio do celular e tente de novo."
              : (m || "Não foi possível confirmar o código."));
          }
          if (d.access_token) { guardarToken(d.access_token); _sessao = null; }
          return d;
        });
      });
    });
  }

  function removerFator(token, fatorId) {
    return config().then(function (c) {
      return fetch(c.supabase_url + "/auth/v1/factors/" + encodeURIComponent(fatorId), {
        method: "DELETE",
        headers: { apikey: c.supabase_anon_key, Authorization: "Bearer " + token }
      }).then(function (r) {
        return r.json().then(function (d) {
          if (!r.ok) throw new Error(d.error_description || d.msg || "Não foi possível remover.");
          return d;
        });
      });
    });
  }

  /* ----------------------------------------------------------------- misc */

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function mostrarGate(msg) {
    var g = document.getElementById("gate");
    var e = document.getElementById("gateErro");
    if (g) g.classList.remove("hidden");
    if (e && msg) e.textContent = msg;
  }

  function esconderGate() {
    var g = document.getElementById("gate");
    if (g) g.classList.add("hidden");
  }

  window.Auth = {
    API_BASE: API_BASE,
    API_RENDER: API_RENDER,
    ABRINDO_DO_DISCO: ABRINDO_DO_DISCO,
    EM_PAGINA_ESTATICA: EM_PAGINA_ESTATICA,
    pagina: pagina,
    destino: destino,
    irParaLogin: irParaLogin,
    config: config,
    obterSessao: obterSessao,
    cabecalhoAuth: cabecalhoAuth,
    entrar: entrar,
    cadastrar: cadastrar,
    recuperar: recuperar,
    estadoDaConta: estadoDaConta,
    fatores: fatores,
    inscreverFator: inscreverFator,
    desafiarFator: desafiarFator,
    confirmarFator: confirmarFator,
    removerFator: removerFator,
    segundoFatorPendente: segundoFatorPendente,
    sair: sair,
    esc: esc,
    mostrarGate: mostrarGate,
    esconderGate: esconderGate
  };
})();
