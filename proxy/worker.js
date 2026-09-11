// Cloudflare Worker - releu pentru cele 3 GET-uri publice ale calendarului ASP.
//
// DE CE EXISTA: pe 04.08.2026 WAF-ul de la eservicii.gov.md a inceput sa
// raspunda "402 Payment Required" cererilor venite de pe IP-ul serverului
// Render. Aceleasi cereri, cu aceleasi antete, mergeau in acelasi timp de
// acasa SI de pe alte IP-uri de datacenter => blocarea e pe reputatia IP-ului
// (acelasi IP interoga la fiecare 2 minute), nu pe antete. Niciun antet nu o
// poate ocoli; singura solutie e sa iesim pe alt IP. Worker-ul asta e iesirea.
//
// DEPLOY (o singura data):
//   1. dash.cloudflare.com -> Workers & Pages -> Create -> Worker
//   2. lipeste fisierul asta, Deploy
//   3. Settings -> Variables -> adauga secretul PROXY_KEY (un sir aleator)
//   4. in Render, pe serviciul asp-monitor, adauga:
//        ASP_API_PROXY        = https://<numele-worker-ului>.workers.dev
//        ASP_API_PROXY_SECRET = <acelasi sir ca PROXY_KEY>
//
// Daca IP-ul Worker-ului ajunge si el blocat, se schimba doar ASP_API_PROXY -
// aplicatia nu se atinge.

const UPSTREAM = "https://eservicii.gov.md/asp/dimtcca/api";
const REFERER = "https://eservicii.gov.md/asp/dimtcca/cerere/apo01";
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

// Lista alba: releul NU e un proxy general. Doar rutele publice ale
// calendarului, exact in forma pe care o cere monitorul.
//   GET  : get-service (hash serviciu), locations (lista locatii). Constante,
//          deci cacheabile o ora.
//   POST : 2026-08-17 ASP a mutat calendarul pe POST /qmatic/dates cu corpul
//          {PublicServiceId,PublicLocationId,Idnp}. Ruta GET veche
//          (/qmatic/dates/<svc>/<loc>) a disparut, dar o pastram in lista GET
//          ca sa se reia automat daca ASP o redeschide. Vezi
//          [[asp-qmatic-dates-gated-behind-wizard-2026-08-17]].
//   2026-08-18: releul duce acum si REPROGRAMAREA, nu doar calendarul. Pana
//     acum ea se facea prin Chromium (care iese direct, cu amprenta lui de
//     browser real), dar Chromium plus pagina Blazor cer ~670 MB pe o instanta
//     de 512 MB si Render a omorat procesul exact in timpul unei reprogramari.
//     Fluxul echivalent, fara browser, are nevoie de rutele de mai jos.
const GUID = "[0-9a-fA-F-]{36}";
const ALLOWED_GET = [
  /^\/apo-request\/get-service\/[A-Za-z]+\/(True|False)\/[A-Za-z]+$/,
  /^\/qmatic\/locations\/[0-9a-fA-F]{16,}$/,
  /^\/qmatic\/dates\/[0-9a-fA-F]{16,}\/[0-9a-fA-F]{16,}$/,
  // cererea intreaga, de unde se construieste corpul reprogramarii
  new RegExp(`^/fod/request/APO01/${GUID}$`),
  new RegExp(`^/apo-request/${GUID}/can-modify$`),
];
const ALLOWED_POST = [
  /^\/qmatic\/dates$/,
  /^\/qmatic\/times$/,
  /^\/apo-request\/get-appointment$/,
  /^\/apo-request\/validate-appointment$/,
  /^\/qmatic\/update$/,          // commit-ul reprogramarii
];

export default {
  async fetch(request, env) {
    const method = request.method;
    if (method !== "GET" && method !== "POST") {
      return new Response("method not allowed", { status: 405 });
    }
    // Fara cheie ar fi un proxy deschis pe care l-ar putea folosi oricine.
    if (!env.PROXY_KEY || request.headers.get("X-ASP-Proxy-Key") !== env.PROXY_KEY) {
      return new Response("forbidden", { status: 403 });
    }

    const url = new URL(request.url);
    const allowed = method === "GET" ? ALLOWED_GET : ALLOWED_POST;
    if (!allowed.some((re) => re.test(url.pathname))) {
      return new Response("path not allowed", { status: 404 });
    }

    // Doar /qmatic/dates se schimba de la un scan la altul. Hash-ul
    // serviciului si lista locatiilor sunt practic constante, asa ca le tinem
    // in cache o ora: scanul ramane la fel de des (2 minute, alegerea
    // utilizatorului), dar lovim ASP cu mai putine cereri pe ciclu, fara nicio
    // intarziere in detectie.
    // ⛔ Calendarul NU se pune niciodata in cache: raspuns vechi = loc ratat.
    //   (Un POST oricum nu se cacheaza, deci calendarul e mereu proaspat.)
    // ⛔ Se pun in cache DOAR cele doua rute practic constante. Orice altceva
    //   (calendarul, orele, cererea, starea programarii) trebuie citit proaspat:
    //   un raspuns vechi inseamna ori un loc ratat, ori - la /fod/request -
    //   reprogramarea trimisa cu datele dinainte de ultima modificare.
    const cacheable = method === "GET" &&
      (url.pathname.startsWith("/apo-request/get-service/") ||
       url.pathname.startsWith("/qmatic/locations/"));
    const ttl = cacheable ? 3600 : 0;

    const upstreamHeaders = {
      "User-Agent": UA,
      "Accept": "application/json, text/plain, */*",
      "Referer": REFERER,
      "Sec-Fetch-Site": "same-origin",
      "Sec-Fetch-Mode": "cors",
      "Sec-Fetch-Dest": "empty",
    };
    const init = {
      method,
      headers: upstreamHeaders,
      cf: { cacheTtl: ttl, cacheEverything: ttl > 0 },
    };
    if (method === "POST") {
      // Corpul JSON al calendarului trece mai departe neschimbat.
      init.body = await request.text();
      upstreamHeaders["Content-Type"] =
        request.headers.get("Content-Type") || "application/json";
    }

    let upstream;
    try {
      upstream = await fetch(UPSTREAM + url.pathname + url.search, init);
    } catch (e) {
      return new Response("upstream error: " + e, { status: 502 });
    }

    // Statusul upstream trece mai departe neschimbat: daca ASP da iar 402,
    // vrem sa vedem 402 in alerta, nu o eroare a releului.
    const body = await upstream.text();
    return new Response(body, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("Content-Type") || "text/plain",
        "Cache-Control": "no-store",
      },
    });
  },
};
